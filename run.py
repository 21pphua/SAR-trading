"""Glue: data -> signals -> candidate trades -> one account -> report.

Data is pulled in chunks (``fetch`` is stockscan.sar.scan.fetch_ohlcv, or any
function with the same shape, e.g. a test stub) so thousands of tickers fit in
memory: per ticker we keep only the candidate trades and the closing prices
on days those trades could be open.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from stockscan.config import SAR_REGIME_INDEXES
from stockscan.lab.core import Candidate, CostModel, ExitRules, simulate
from stockscan.lab.metrics import Scorecard, scorecard, yearly_returns, yearly_drawdowns, equity_stats
from stockscan.lab.portfolio import AccountResult, AccountRules, run_account
from stockscan.lab.setups import SETUPS
from stockscan.lab.walkforward import WalkForward, walk_forward
from stockscan.sar.engine import Series, sma
from stockscan.sar.scan import regime_size
from stockscan.sar.strength import group_keys, load_sectors


@dataclass
class Variant:
    """One complete rule set to test: which setup, its settings, and how exits work."""
    name: str
    setup: str = "sar_breakout"
    params: dict = field(default_factory=dict)
    exits: ExitRules = field(default_factory=ExitRules)
    slippage_bps: Optional[float] = None    # None = the run's default cost
    use_regime: Optional[bool] = None       # None = the run's default
    in_walkforward: bool = True             # reference runs (cost/regime checks) stay out


def default_variants() -> list[Variant]:
    """Step 1 line-up: the same SAR checklist, entered and exited different ways."""
    return [
        Variant("A. Close entry, 10 SMA exit (the docs)", params={"entry": "close"}),
        Variant("B. Next-day open entry", params={"entry": "next_open"}),
        Variant("C. Coil break buy-stop + confirm-or-cut", params={"entry": "coil_break"}),
        Variant("D. Close entry, 20 SMA trail after 5R (live positions rule)",
                params={"entry": "close"}, exits=ExitRules(trail_after_partial=20)),
        Variant("E. Close entry, 3R partial (1/3) + 10-day time stop",
                params={"entry": "close"}, exits=ExitRules(partial_r=3.0, partial_frac=1 / 3, time_stop_bars=10)),
        # reference runs: same as A with different costs / without the regime switch
        Variant("ref: A with zero costs", params={"entry": "close"}, slippage_bps=0.0, in_walkforward=False),
        Variant("ref: A with 25 bps costs", params={"entry": "close"}, slippage_bps=25.0, in_walkforward=False),
        Variant("ref: A without regime switch", params={"entry": "close"}, use_regime=False, in_walkforward=False),
    ]


@dataclass
class LabResult:
    generated: str
    period: str
    universe_size: int
    tickers_with_data: int
    calendar: list[str]
    costs: CostModel
    rules: AccountRules
    variants: list[Variant]
    accounts: dict[str, AccountResult]
    cards: dict[str, Scorecard]
    raw: dict[str, dict]                 # unconstrained per-trade stats (every signal taken)
    benchmark: dict
    wf: Optional[WalkForward]
    regime_days: dict[str, int]


def _regime_map(spy: Sequence, breadth: dict[str, float]) -> dict[str, float]:
    C = [b.close for b in spy]
    s10, s20 = sma(C, 10), sma(C, 20)
    out = {}
    for k, b in enumerate(spy):
        ok = (s10[k] > s20[k]) if s20[k] is not None else None
        mult, _ = regime_size(breadth.get(b.date), ok)
        out[b.date] = mult
    return out


def run_lab(tickers: Sequence[str], fetch: Callable[..., dict], period: str = "10y", chunk: int = 200,
            variants: Optional[Sequence[Variant]] = None, rules: AccountRules = AccountRules(),
            costs: CostModel = CostModel(), max_gap: Optional[float] = None,
            on_progress=None, wf_criterion: str = "sharpe") -> LabResult:
    variants = list(variants or default_variants())
    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    idx = fetch([SAR_REGIME_INDEXES[0]], period=period)
    spy = idx.get(SAR_REGIME_INDEXES[0], [])
    calendar = [b.date for b in spy]
    groups = group_keys(tickers, load_sectors())

    above: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    cands: dict[str, list[Candidate]] = {v.name: [] for v in variants}
    closes: dict[str, dict[str, float]] = {}
    with_data = 0

    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        data = fetch(batch, period=period)
        for tk in batch:
            bars = data.get(tk) or []
            if not bars:
                continue
            with_data += 1
            # breadth: share of stocks above their 50-day average, per day
            m50 = sma([b.close for b in bars], 50)
            for b, m in zip(bars, m50):
                if m is not None and b.close >= 1:
                    total[b.date] += 1
                    above[b.date] += b.close > m
            S = None
            sig_cache: dict[str, list] = {}
            keep_idx: set[int] = set()
            for v in variants:
                key = v.setup + json.dumps(v.params, sort_keys=True)
                if key not in sig_cache:
                    sig_cache[key] = SETUPS[v.setup].signals(tk, bars, group=groups.get(tk, ""), **v.params)
                sigs = sig_cache[key]
                if not sigs:
                    continue
                if S is None:
                    S = Series(bars)
                cm = costs if v.slippage_bps is None else CostModel(v.slippage_bps, costs.commission_per_share,
                                                                   costs.min_commission)
                for sg in sigs:
                    c = simulate(sg, S, v.exits, cm, max_gap)
                    if c is not None:
                        cands[v.name].append(c)
                        keep_idx.update(range(c.fill_i, c.exit_i + 1))
            if keep_idx:
                closes[tk] = {bars[k].date: bars[k].close for k in keep_idx}
        if on_progress:
            on_progress(min(start + chunk, len(tickers)), len(tickers), batch[-1])

    breadth = {d: 100.0 * above[d] / total[d] for d in total if total[d] >= 50}
    regime = _regime_map(spy, breadth)
    regime_days = {"full size": sum(1 for d in calendar if regime.get(d, 1) == 1),
                   "half size": sum(1 for d in calendar if regime.get(d, 1) == 0.5),
                   "no new trades": sum(1 for d in calendar if regime.get(d, 1) == 0)}

    accounts, cards, raw = {}, {}, {}
    for v in variants:
        r = AccountRules(**{**asdict(rules), **({"use_regime": v.use_regime} if v.use_regime is not None else {})})
        cm = costs if v.slippage_bps is None else CostModel(v.slippage_bps, costs.commission_per_share,
                                                           costs.min_commission)
        acc = run_account(cands[v.name], closes, calendar, r, cm, regime)
        accounts[v.name] = acc
        cards[v.name] = scorecard(acc)
        raw[v.name] = _raw_stats(cands[v.name])

    bench = {}
    if spy:
        eq = [b.close for b in spy]
        yrs, cagr, mdd, sharpe = equity_stats(calendar, eq)
        bench = {"name": SAR_REGIME_INDEXES[0] + " buy & hold", "cagr": cagr, "max_drawdown": mdd,
                 "sharpe": sharpe, "yearly": yearly_returns(calendar, eq)}
    wf = walk_forward({v.name: accounts[v.name] for v in variants if v.in_walkforward},
                      criterion=wf_criterion)
    return LabResult(datetime.now(timezone.utc).isoformat(timespec="seconds"), period, len(tickers), with_data,
                     calendar, costs, rules, variants, accounts, cards, raw, bench, wf, regime_days)


def _raw_stats(cs: Sequence[Candidate]) -> dict:
    closed = [c for c in cs if not c.open]
    if not closed:
        return {"signals": 0}
    rs = sorted(c.r for c in closed)
    trimmed = rs[:-10] if len(rs) > 20 else []
    return {"signals": len(closed), "expectancy_r": sum(rs) / len(rs),
            "win_rate": sum(1 for r in rs if r > 0) / len(rs),
            "expectancy_wo_top10": sum(trimmed) / len(trimmed) if trimmed else None}


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------

def write_trades_csv(res: LabResult, path: str) -> None:
    cols = ["variant", "ticker", "setup", "group", "signal_date", "entry_date", "exit_date", "days", "shares",
            "entry", "stop", "risk_dollars", "pnl", "r", "reason", "open", "meta"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for name, acc in res.accounts.items():
            for t in acc.trades:
                w.writerow([name, t.ticker, t.setup, t.group, t.signal_date, t.entry_date, t.exit_date, t.days,
                            t.shares, t.entry, t.stop, t.risk_dollars, t.pnl, t.r, t.reason, t.open,
                            json.dumps(t.meta)])


def write_summary_json(res: LabResult, path: str) -> None:
    out = {
        "generated": res.generated, "period": res.period, "universe": res.universe_size,
        "with_data": res.tickers_with_data, "costs": asdict(res.costs), "rules": asdict(res.rules),
        "regime_days": res.regime_days, "benchmark": res.benchmark,
        "variants": {name: {"scorecard": res.cards[name].as_dict(), "raw": res.raw[name],
                            "yearly": yearly_returns(res.accounts[name].dates, res.accounts[name].equity),
                            "skipped": dict(res.accounts[name].skipped),
                            "equity": res.accounts[name].equity[::5],
                            "dates": res.accounts[name].dates[::5]}
                     for name in res.accounts},
        "walkforward": None if not res.wf else {
            "criterion": res.wf.criterion, "picks": res.wf.picks, "oos_returns": res.wf.oos_returns,
            "oos_cagr": res.wf.oos_cagr, "oos_total": res.wf.oos_total},
    }
    with open(path, "w") as f:
        json.dump(_clean(out), f, indent=1)


def _clean(x):
    """JSON can't hold NaN/inf: turn them into null."""
    if isinstance(x, float):
        return None if (x != x or x in (float("inf"), float("-inf"))) else round(x, 6)
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x
