"""One account, real constraints: replay every candidate trade day by day.

Why this matters: "average +0.2R per trade" says nothing about whether you
could have taken those trades. Signals cluster (twenty breakouts on one good
day, none for weeks), so with a fixed account you can only take a few. This
simulator decides, each day, which signals the account can actually afford,
then tracks cash and equity like a broker statement.

Rules applied to every new trade, in this order:
  1. Market regime multiplier for the signal day (0 = no new trades,
     0.5 = half size, 1 = full size).
  2. Not already holding that ticker.
  3. Open positions < max_positions.
  4. Positions in the same industry group < max_per_group.
  5. Size: shares = (equity x risk_pct x regime) / (entry - stop).
  6. Position value <= max_position_pct of equity (cuts shares if needed).
  7. Total open risk ("heat") after the trade <= max_heat_pct of equity.
  8. Enough cash (no margin).
When more signals fill on the same day than there is room for, the highest
``rank`` (relative strength by default) goes first.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional, Sequence

from stockscan.lab.core import Candidate, CostModel


@dataclass
class AccountRules:
    start_equity: float = 25_000.0
    risk_pct: float = 0.5            # % of equity risked per trade
    max_positions: int = 8
    max_heat_pct: float = 6.0        # total open risk, % of equity
    max_per_group: int = 2
    max_position_pct: float = 25.0   # largest position, % of equity
    use_regime: bool = True


@dataclass
class TradeRecord:
    ticker: str
    setup: str
    signal_date: str
    entry_date: str
    exit_date: str
    shares: int
    entry: float
    stop: float
    risk_dollars: float
    pnl: float
    r: float
    reason: str
    days: int
    open: bool = False
    group: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class _Pos:
    c: Candidate
    shares: int
    left: float           # shares still held
    stop: float
    risk_dollars: float
    cost_basis: float     # cash paid incl. commission
    cash_back: float = 0.0
    k: int = 0            # next event index


@dataclass
class AccountResult:
    dates: list[str]
    equity: list[float]
    exposure: list[float]          # fraction of equity in positions each day
    trades: list[TradeRecord]
    skipped: Counter
    rules: AccountRules
    costs: CostModel


def run_account(candidates: Sequence[Candidate], closes: dict[str, dict[str, float]],
                calendar: Sequence[str], rules: AccountRules = AccountRules(),
                costs: CostModel = CostModel(), regime: Optional[dict[str, float]] = None
                ) -> AccountResult:
    by_fill: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_fill[c.fill_date].append(c)
    cash = rules.start_equity
    pos: dict[str, _Pos] = {}
    last_px: dict[str, float] = {}
    eq_prev = rules.start_equity
    out_dates, out_eq, out_exp = [], [], []
    trades: list[TradeRecord] = []
    skipped: Counter = Counter()

    def apply_events(tk: str, p: _Pos, d: str) -> bool:
        """Apply this position's events dated d. True if it closed."""
        nonlocal cash
        ev = p.c.events
        while p.k < len(ev) and ev[p.k].date == d:
            e = ev[p.k]
            p.k += 1
            if e.kind == "stop":
                p.stop = e.price
            elif e.kind == "sell":
                final = e.frac >= p.left / p.shares - 1e-6
                qty = p.left if final else min(p.left, round(p.shares * e.frac))
                if qty <= 0:
                    continue
                got = qty * e.price - costs.commission(qty)
                cash += got
                p.cash_back += got
                p.left -= qty
        if p.left <= 0:
            pnl = p.cash_back - p.cost_basis
            trades.append(_record(tk, p, pnl, open_=False))
            return True
        return False

    for d in calendar:
        # 1) manage positions opened on earlier days
        for tk in list(pos):
            if apply_events(tk, pos[tk], d):
                del pos[tk]
        # 2) new entries that fill today, strongest first
        todays = sorted(by_fill.get(d, ()), key=lambda c: c.signal.rank, reverse=True)
        for c in todays:
            sig = c.signal
            m = regime.get(sig.date, 1.0) if (regime is not None and rules.use_regime) else 1.0
            if m <= 0:
                skipped["regime: no new trades"] += 1
                continue
            if sig.ticker in pos:
                skipped["already holding"] += 1
                continue
            if len(pos) >= rules.max_positions:
                skipped["max positions"] += 1
                continue
            if sig.group and sum(1 for p in pos.values() if p.c.signal.group == sig.group) >= rules.max_per_group:
                skipped["group cap"] += 1
                continue
            rps = c.risk
            risk_budget = eq_prev * rules.risk_pct / 100 * m
            shares = math.floor(risk_budget / rps) if rps > 0 else 0
            shares = min(shares, math.floor(eq_prev * rules.max_position_pct / 100 / c.entry))
            heat = sum(p.left * max(0.0, p.c.entry - p.stop) for p in pos.values())
            if heat + shares * rps > eq_prev * rules.max_heat_pct / 100:
                skipped["heat cap"] += 1
                continue
            while shares > 0 and shares * c.entry + costs.commission(shares) > cash:
                shares = math.floor((cash - costs.commission(shares)) / c.entry)
            if shares < 1:
                skipped["too small / no cash"] += 1
                continue
            cost = shares * c.entry + costs.commission(shares)
            cash -= cost
            p = _Pos(c, shares, shares, c.stop, shares * rps, cost)
            pos[sig.ticker] = p
            if apply_events(sig.ticker, p, d):     # e.g. stopped out on the fill day
                del pos[sig.ticker]
        # 3) mark to market at the close
        mv = 0.0
        for tk, p in pos.items():
            px = closes.get(tk, {}).get(d)
            if px is not None:
                last_px[tk] = px
            mv += p.left * last_px.get(tk, p.c.entry)
        eq = cash + mv
        out_dates.append(d)
        out_eq.append(eq)
        out_exp.append(mv / eq if eq > 0 else 0.0)
        eq_prev = eq

    # positions still open at the end: mark at the last price
    for tk, p in pos.items():
        px = last_px.get(tk, p.c.entry)
        val = p.left * costs.sell_px(px)
        pnl = p.cash_back + val - p.cost_basis
        trades.append(_record(tk, p, pnl, open_=True, end_date=calendar[-1] if calendar else ""))
    return AccountResult(out_dates, out_eq, out_exp, trades, skipped, rules, costs)


def _record(tk: str, p: _Pos, pnl: float, open_: bool, end_date: str = "") -> TradeRecord:
    c = p.c
    exit_date = end_date if open_ else c.exit_date
    return TradeRecord(
        ticker=tk, setup=c.signal.setup, signal_date=c.signal.date, entry_date=c.fill_date,
        exit_date=exit_date, shares=p.shares, entry=round(c.entry, 4), stop=round(c.stop, 4),
        risk_dollars=round(p.risk_dollars, 2), pnl=round(pnl, 2),
        r=round(pnl / p.risk_dollars, 3) if p.risk_dollars else 0.0,
        reason="open" if open_ else c.reason, days=max(0, c.exit_i - c.fill_i), open=open_,
        group=c.signal.group, meta=c.signal.meta)
