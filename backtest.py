"""Backtest the SAR rules over history — does the checklist actually pay?

Two entry styles are simulated side by side:

  CLOSE (the live rules): buy at the close of a qualifying breakout day —
    score >= min_score, close above the 20-bar base, volume >= 1.3x its
    20-day average, stock filters passing. Stop = breakout-day low.

  INTRADAY (opening-range style): the stock was on the alert list (setup
    formed, uptrend) at the prior close, and today's high takes out the
    base high. Buy at max(open, base high) + slippage. Stop = the day's low.
    Daily bars can't see the opening range, so this is an approximation;
    a real opening-range entry is usually a little higher. If the day closes
    back below the base high (a false break), the trade is counted as a full
    stop-out (-1R) — the conservative assumption.

Exits (both): at +5R sell ``partial`` and move the stop to breakeven; sell
the rest on a daily close below the 10 SMA. One position per ticker at a time.

Every trade also records its relative-strength rank and industry-group rank
on the entry date, ranked against all stocks in the backtest that day.

Caveats, stated in the report too: survivorship bias (today's tickers only);
fills at close/stop with no commissions; trades across tickers overlap.
"""

from __future__ import annotations

import csv
from array import array
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from statistics import median
from typing import Callable, Optional, Sequence

from stockscan.config import SAR_TAKE_AT, SAR_REGIME_INDEXES, SAR_PULLBACK_LOOKBACK, SAR_RS_LEADER, SAR_HOT_GROUP
from stockscan.sar.engine import Bar, Series, score_setup, sma, min_bars
from stockscan.sar.strength import rs_raw_series, rank_in, load_sectors, group_keys, MIN_GROUP

MIN_VOLX = 1.3


@dataclass
class Trade:
    ticker: str
    entry_date: str
    entry: float
    stop: float
    score: int
    risk_adr: float
    regime_ok: Optional[bool]
    trend_ok: Optional[bool] = None
    mode: str = "close"
    volx: float = 0.0
    rs_raw: Optional[float] = None
    rs_rank: Optional[int] = None
    group: str = ""
    group_rank: Optional[int] = None
    false_break: bool = False
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    r: float = 0.0
    hit_5r: bool = False
    bars_held: int = 0
    open: bool = False


def _regime_by_date(index_bars: Sequence[Bar]) -> dict[str, bool]:
    C = [b.close for b in index_bars]
    s10, s20 = sma(C, 10), sma(C, 20)
    return {b.date: (s10[i] > s20[i]) for i, b in enumerate(index_bars) if s20[i] is not None}


def _manage(S: Series, t: Trade, i: int, partial: float) -> int:
    """Run the exit rules from bar i+1; fills t's exit fields; returns the exit bar."""
    n = len(S)
    R, stop, target, banked, left = t.entry - t.stop, t.stop, t.entry + 5 * (t.entry - t.stop), 0.0, 1.0
    j = i + 1
    px = S.C[n - 1]
    while j < n:
        o, h, l, c = S.O[j], S.H[j], S.L[j], S.C[j]
        if l <= stop:
            px = min(stop, o)
            t.exit_reason = "breakeven" if t.hit_5r else "stop"
            break
        if not t.hit_5r and h >= target:
            t.hit_5r, banked, left, stop = True, partial * 5.0, 1 - partial, t.entry
        if S.s10[j] is not None and c < S.s10[j]:
            px = c
            t.exit_reason = "10sma"
            break
        j += 1
    else:
        j, t.open, t.exit_reason = n - 1, True, "open"
    t.exit_date, t.exit_price, t.bars_held = S.bars[j].date, px, j - i
    t.r = banked + left * (px - t.entry) / R
    return j


def backtest_ticker(ticker: str, bars: Sequence[Bar], min_score: int = SAR_TAKE_AT,
                    partial: float = 0.20, max_risk_adr: Optional[float] = None,
                    regime: Optional[dict[str, bool]] = None, trend_filter: bool = True,
                    mode: str = "close", slippage: float = 0.002,
                    rs: Optional[list[Optional[float]]] = None) -> list[Trade]:
    S = Series(bars)
    n = len(S)
    PL = SAR_PULLBACK_LOOKBACK
    trades: list[Trade] = []
    i = min_bars() - (1 if mode == "close" else 0)
    while i < n - 1:
        if mode == "close":
            if not (S.breaks_range(i) and S.volx(i) >= MIN_VOLX and S.passes_filters(i)[0]):
                i += 1
                continue
            s = score_setup(bars, i, ticker=ticker, series=S, with_targets=False)
            if (s.score < min_score or s.risk <= 0 or (max_risk_adr and s.risk_adr > max_risk_adr)
                    or (trend_filter and s.trend_ok is False)):
                i += 1
                continue
            t = Trade(ticker, s.date, s.entry, s.stop, s.score, round(s.risk_adr, 2),
                      (regime or {}).get(s.date), s.trend_ok, "close", round(S.volx(i), 2))
        else:
            trig = max(S.H[i - PL: i])
            if not (S.H[i] > trig and S.passes_filters(i - 1)[0]):
                i += 1
                continue
            p = score_setup(bars, i - 1, ticker=ticker, series=S, with_targets=False)
            if not p.is_coiling or (trend_filter and p.trend_ok is False):
                i += 1
                continue
            entry = max(S.O[i], trig) * (1 + slippage)
            stop = S.L[i]
            adr_d = p.adr_pct * p.entry if p.adr_pct else 0.0
            risk_adr = (entry - stop) / adr_d if adr_d else 0.0
            if entry - stop <= 0 or entry > S.H[i] * (1 + slippage) or (max_risk_adr and risk_adr > max_risk_adr):
                i += 1
                continue
            t = Trade(ticker, S.bars[i].date, entry, stop, p.score, round(risk_adr, 2),
                      (regime or {}).get(S.bars[i].date), p.trend_ok, "intraday", round(S.volx(i), 2))
        if rs is not None:
            t.rs_raw = rs[i]
        if t.mode == "intraday" and S.C[i] < max(S.H[i - PL: i]):
            t.false_break = True
            t.exit_date, t.exit_price, t.exit_reason, t.r = t.entry_date, t.stop, "false break", -1.0
            trades.append(t)
            i += 1
            continue
        i = _manage(S, t, i, partial) + 1
        trades.append(t)
    return trades


@dataclass
class Stats:
    trades: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    profit_factor: float
    max_consec_losses: int
    max_drawdown_r: float
    max_drawdown_pct_1pct_risk: float
    avg_bars_held: float
    hit_5r_rate: float


def summarize(trades: Sequence[Trade]) -> Optional[Stats]:
    closed = sorted((t for t in trades if not t.open), key=lambda t: t.exit_date)
    if not closed:
        return None
    wins = [t.r for t in closed if t.r > 0]
    losses = [t.r for t in closed if t.r <= 0]
    streak = worst = 0
    eq = peak = 0.0
    dd_r = 0.0
    acct = peak_acct = 1.0
    dd_pct = 0.0
    for t in closed:
        streak = streak + 1 if t.r <= 0 else 0
        worst = max(worst, streak)
        eq += t.r
        peak = max(peak, eq)
        dd_r = max(dd_r, peak - eq)
        acct *= 1 + 0.01 * t.r
        peak_acct = max(peak_acct, acct)
        dd_pct = max(dd_pct, 1 - acct / peak_acct)
    gross_loss = -sum(losses)
    return Stats(
        trades=len(closed),
        win_rate=len(wins) / len(closed),
        avg_win_r=sum(wins) / len(wins) if wins else 0.0,
        avg_loss_r=sum(losses) / len(losses) if losses else 0.0,
        expectancy_r=sum(t.r for t in closed) / len(closed),
        profit_factor=(sum(wins) / gross_loss) if gross_loss else float("inf"),
        max_consec_losses=worst,
        max_drawdown_r=dd_r,
        max_drawdown_pct_1pct_risk=dd_pct,
        avg_bars_held=sum(t.bars_held for t in closed) / len(closed),
        hit_5r_rate=sum(1 for t in closed if t.hit_5r) / len(closed),
    )


def _rs_bands(ts: Sequence[Trade]) -> list[tuple[str, Optional[Stats]]]:
    return [(f"RS {SAR_RS_LEADER}+ (leaders)", summarize([t for t in ts if (t.rs_rank or 0) >= SAR_RS_LEADER])),
            (f"RS 50-{SAR_RS_LEADER - 1}", summarize([t for t in ts if 50 <= (t.rs_rank or 0) < SAR_RS_LEADER])),
            ("RS under 50", summarize([t for t in ts if t.rs_rank is not None and t.rs_rank < 50]))]


def _group_bands(ts: Sequence[Trade]) -> list[tuple[str, Optional[Stats]]]:
    return [(f"Hot group ({SAR_HOT_GROUP}+)", summarize([t for t in ts if (t.group_rank or 0) >= SAR_HOT_GROUP])),
            ("Other groups", summarize([t for t in ts if t.group_rank is not None and t.group_rank < SAR_HOT_GROUP])),
            ("Group unknown", summarize([t for t in ts if t.group_rank is None]))]


@dataclass
class BacktestResult:
    generated: str
    period: str
    tickers: int
    trades: list[Trade] = field(default_factory=list)

    def mode(self, m: str) -> list[Trade]:
        return [t for t in self.trades if t.mode == m]

    def overall(self) -> Optional[Stats]:
        return summarize(self.mode("close"))

    def by_score(self) -> list[tuple[str, Optional[Stats]]]:
        ts = self.mode("close")
        bands = [("65-74", 65, 75), ("75-84", 75, 85), ("85+", 85, 101)]
        return [(lbl, summarize([t for t in ts if lo <= t.score < hi])) for lbl, lo, hi in bands]

    def by_regime(self) -> list[tuple[str, Optional[Stats]]]:
        ts = self.mode("close")
        return [("favorable", summarize([t for t in ts if t.regime_ok is True])),
                ("unfavorable", summarize([t for t in ts if t.regime_ok is False]))]

    def by_trend(self) -> list[tuple[str, Optional[Stats]]]:
        ts = self.mode("close")
        return [("uptrend", summarize([t for t in ts if t.trend_ok is True])),
                ("counter-trend", summarize([t for t in ts if t.trend_ok is False]))]

    def by_stop_width(self) -> list[tuple[str, Optional[Stats]]]:
        ts = self.mode("close")
        return [("stop <= 1 ADR", summarize([t for t in ts if t.risk_adr <= 1.0])),
                ("stop > 1 ADR", summarize([t for t in ts if t.risk_adr > 1.0]))]

    def live(self, m: str = "close") -> list[Trade]:
        """The rules the live scan uses: uptrend, score >= min, stop within 1 ADR."""
        return [t for t in self.mode(m) if t.risk_adr <= 1.0 and t.trend_ok is not False]


def _attach_ranks(trades: list[Trade], by_date: dict[str, array], g_by_date: dict[str, dict[str, array]],
                  groups: dict[str, str]) -> None:
    sorted_rs = {}
    g_sorted: dict[str, tuple[list[float], dict[str, float]]] = {}
    for t in trades:
        t.group = groups.get(t.ticker, "")
        if t.rs_raw is None:
            continue
        d = t.entry_date
        if d not in sorted_rs:
            sorted_rs[d] = sorted(by_date.get(d, array("f")))
        t.rs_rank = rank_in(sorted_rs[d], t.rs_raw)
        if t.group:
            if d not in g_sorted:
                med = {g: median(v) for g, v in g_by_date.get(d, {}).items() if len(v) >= MIN_GROUP}
                g_sorted[d] = (sorted(med.values()), med)
            vals, med = g_sorted[d]
            if t.group in med:
                t.group_rank = rank_in(vals, med[t.group])


def run_backtest(tickers: Sequence[str], fetch: Callable[..., dict], period: str = "3y",
                 chunk: int = 200, min_score: int = SAR_TAKE_AT, partial: float = 0.20,
                 max_risk_adr: Optional[float] = None, on_progress=None,
                 trend_filter: bool = True, intraday: bool = True, slippage: float = 0.002) -> BacktestResult:
    """Fetch history chunk-by-chunk and simulate every ticker, both entry styles."""
    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    idx = fetch([SAR_REGIME_INDEXES[0]], period=period)
    regime = _regime_by_date(idx.get(SAR_REGIME_INDEXES[0], []))
    groups = group_keys(tickers, load_sectors())
    by_date: dict[str, array] = defaultdict(lambda: array("f"))
    g_by_date: dict[str, dict[str, array]] = defaultdict(lambda: defaultdict(lambda: array("f")))
    res = BacktestResult(datetime.now(timezone.utc).isoformat(timespec="seconds"), period, len(tickers))
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        data = fetch(batch, period=period)
        for tk in batch:
            bars = data.get(tk) or []
            rs = rs_raw_series([b.close for b in bars])
            g = groups.get(tk, "")
            for bar, v in zip(bars, rs):
                if v is not None and bar.close >= 1:
                    by_date[bar.date].append(v)
                    if g:
                        g_by_date[bar.date][g].append(v)
            if len(bars) > min_bars():
                res.trades.extend(backtest_ticker(tk, bars, min_score, partial, max_risk_adr, regime,
                                                  trend_filter, "close", slippage, rs))
                if intraday:
                    res.trades.extend(backtest_ticker(tk, bars, min_score, partial, max_risk_adr, regime,
                                                      trend_filter, "intraday", slippage, rs))
        if on_progress:
            on_progress(min(start + chunk, len(tickers)), len(tickers), batch[-1])
    _attach_ranks(res.trades, by_date, g_by_date, groups)
    return res


def write_trades_csv(res: BacktestResult, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        rows = [asdict(t) for t in res.trades]
        if not rows:
            fh.write("no trades\n")
            return
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def render_backtest(res: BacktestResult) -> str:
    def row(label: str, s: Optional[Stats]) -> str:
        if not s:
            return f"  {label:<24}{'—':>7}"
        pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
        return (f"  {label:<24}{s.trades:>7}{s.win_rate:>8.0%}{s.avg_win_r:>+9.2f}{s.avg_loss_r:>+9.2f}"
                f"{s.expectancy_r:>+8.2f}{pf:>7}{s.max_consec_losses:>8}{s.max_drawdown_pct_1pct_risk:>8.0%}")

    hdr = f"  {'':<24}{'TRADES':>7}{'WIN%':>8}{'AVG WIN':>9}{'AVG LOSS':>9}{'EXP R':>8}{'PF':>7}{'STREAK':>8}{'MAX DD':>8}"
    ov = res.overall()
    lines = ["", "SAR BACKTEST", "=" * 90,
             f"{res.tickers} tickers · period {res.period} · generated {res.generated}", ""]
    if not ov:
        lines += ["  No closed trades.", ""]
        return "\n".join(lines)
    live, live_i = res.live("close"), res.live("intraday")
    lead = lambda ts: [t for t in ts if (t.rs_rank or 0) >= SAR_RS_LEADER]
    hot = lambda ts: [t for t in ts if (t.group_rank or 0) >= SAR_HOT_GROUP]
    lv, li = summarize(live), summarize(live_i)
    fb = [t for t in live_i if t.false_break]
    lines += [
        "PLAIN ENGLISH (entries at the close, all trades)",
        "-" * 90,
        f"  Out of {ov.trades} trades, {ov.win_rate:.0%} made money.",
        f"  Winners averaged {ov.avg_win_r:+.1f}R, losers {ov.avg_loss_r:+.1f}R "
        f"(R = what you risked; at 1% risk, 1R = 1% of the account).",
        f"  On average each trade made {ov.expectancy_r:+.2f}R. "
        + ("Positive = the rules made money over this period." if ov.expectancy_r > 0
           else "Negative = the rules lost money over this period."),
        f"  Worst losing streak: {ov.max_consec_losses} in a row. "
        f"Worst account drop at 1% risk: {ov.max_drawdown_pct_1pct_risk:.0%}.",
        f"  {ov.hit_5r_rate:.0%} of trades reached the 5R partial. Average hold: {ov.avg_bars_held:.0f} trading days.",
        "",
        "THE QUESTIONS THAT DECIDE THE NEXT RULE CHANGES",
        "-" * 90, hdr,
        row("Live rules (close)", lv),
        row(f"  + RS {SAR_RS_LEADER}+", summarize(lead(live))),
        row("  + hot group", summarize(hot(live))),
        row("  + RS leader & hot group", summarize(hot(lead(live)))),
        row("Live rules, intraday entry", li),
        row(f"  + RS {SAR_RS_LEADER}+", summarize(lead(live_i))),
        row("  + hot group", summarize(hot(live_i))),
        f"  Intraday false breaks (closed back inside the base, counted as -1R): "
        f"{len(fb)} of {len(live_i)} ({(len(fb) / len(live_i) if live_i else 0):.0%}).",
        "",
        "DETAIL (close entries)", "-" * 90, hdr, row("All trades", ov), "",
        "  By score", *[row(l, s) for l, s in res.by_score()], "",
        "  By relative strength at entry", *[row(l, s) for l, s in _rs_bands(res.mode("close"))], "",
        "  By industry group strength at entry", *[row(l, s) for l, s in _group_bands(res.mode("close"))], "",
        "  By long-term trend (50/200 SMA + near 52-week high)", *[row(l, s) for l, s in res.by_trend()], "",
        "  By market (SPY 10 vs 20 SMA at entry)", *[row(l, s) for l, s in res.by_regime()], "",
        "  By stop width", *[row(l, s) for l, s in res.by_stop_width()], "",
        "DETAIL (intraday entries, live rules)", "-" * 90, hdr,
        *[row(l, s) for l, s in _rs_bands(live_i)],
        row("  closed on 1.3x+ volume", summarize([t for t in live_i if t.volx >= MIN_VOLX])),
        row("  closed on light volume", summarize([t for t in live_i if t.volx < MIN_VOLX])), "",
        "CAVEATS: today's ticker list only (survivorship bias); close fills exact, intraday fills",
        "approximated from daily bars with 0.2% slippage; no commissions; trades across tickers",
        "overlap. Treat as a rough guide, not proof.", "",
    ]
    return "\n".join(lines)
