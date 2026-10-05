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

from stockscan.config import (SAR_TAKE_AT, SAR_REGIME_INDEXES, SAR_PULLBACK_LOOKBACK, SAR_RS_LEADER,
                              SAR_HOT_GROUP, SAR_MIN_RISK_ADR)
from stockscan.sar.engine import Bar, Series, score_setup, sma, min_bars
from stockscan.sar.strength import rs_raw_series, rank_in, load_sectors, group_keys, MIN_GROUP

MIN_VOLX = 1.3

# Exit/entry variants tested side by side on the SAME live-rules setups (see run_backtest).
# Each is judged on the first ~2/3 of history (TRAIN) and the last ~1/3 (TEST); only a
# variant that beats the current rules in BOTH halves is worth adopting.
VARIANTS: dict[str, dict] = {
    "Current rules":              {},
    "Partial at 3R (1/3 size)":   {"partial_r": 3.0, "partial": 1 / 3},
    "Breakeven stop at +2R":      {"be_at_r": 2.0},
    "Time stop: 10 days, <+1R":   {"time_bars": 10},
    "Trail 20 SMA after partial": {"trail_after": 20},
    "Trail 20 SMA from day 1":    {"trail": 20},
    "Combo: 3R partial+20 SMA+time": {"partial_r": 3.0, "partial": 1 / 3, "trail_after": 20, "time_bars": 10},
}


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
    variant: str = ""


def _regime_by_date(index_bars: Sequence[Bar]) -> dict[str, bool]:
    C = [b.close for b in index_bars]
    s10, s20 = sma(C, 10), sma(C, 20)
    return {b.date: (s10[i] > s20[i]) for i, b in enumerate(index_bars) if s20[i] is not None}


def _manage(S: Series, t: Trade, i: int, partial: float, exits: Optional[dict] = None) -> int:
    """Run the exit rules from bar i+1; fills t's exit fields; returns the exit bar.

    exits (all optional, default = the doc's rules):
      partial_r    R multiple for the partial sale (5)    partial    fraction sold there
      be_at_r      move the stop to breakeven once the high reaches this R
      time_bars    exit at the close after this many bars if the high never reached +1R
      trail        SMA (10/20) whose daily close below exits      trail_after  SMA used after the partial
    """
    x = exits or {}
    pr, partial = x.get("partial_r", 5.0), x.get("partial", partial)
    trail = S.s20 if x.get("trail") == 20 else S.s10
    after = S.s20 if x.get("trail_after") == 20 else trail
    n = len(S)
    R, stop, banked, left = t.entry - t.stop, t.stop, 0.0, 1.0
    target = t.entry + pr * R
    j = i + 1
    px = S.C[n - 1]
    hit_1r = False
    while j < n:
        o, h, l, c = S.O[j], S.H[j], S.L[j], S.C[j]
        if l <= stop:
            px = min(stop, o)
            t.exit_reason = "breakeven" if stop >= t.entry else "stop"
            break
        hit_1r = hit_1r or h >= t.entry + R
        if x.get("be_at_r") and h >= t.entry + x["be_at_r"] * R and stop < t.entry:
            stop = t.entry
        if not t.hit_5r and h >= target:
            t.hit_5r, banked, left, stop = True, partial * pr, 1 - partial, max(stop, t.entry)
        line = after if t.hit_5r else trail
        if line[j] is not None and c < line[j]:
            px = c
            t.exit_reason = "sma"
            break
        if x.get("time_bars") and not hit_1r and j - i >= x["time_bars"]:
            px = c
            t.exit_reason = "time"
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
                    rs: Optional[list[Optional[float]]] = None, exits: Optional[dict] = None,
                    confirm_cut: bool = False, variant: str = "") -> list[Trade]:
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
            if (s.score < min_score or s.risk <= 0 or s.tight_stop
                    or (max_risk_adr and s.risk_adr > max_risk_adr)
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
            if (entry - stop <= 0 or entry > S.H[i] * (1 + slippage)
                    or (max_risk_adr and risk_adr > max_risk_adr)
                    or 0 < risk_adr < SAR_MIN_RISK_ADR):
                i += 1
                continue
            t = Trade(ticker, S.bars[i].date, entry, stop, p.score, round(risk_adr, 2),
                      (regime or {}).get(S.bars[i].date), p.trend_ok, "intraday", round(S.volx(i), 2))
        if rs is not None:
            t.rs_raw = rs[i]
        t.variant = variant
        if t.mode == "intraday" and confirm_cut and (S.C[i] < max(S.H[i - PL: i]) or S.volx(i) < MIN_VOLX):
            # confirm-or-cut: the break didn't confirm by the close -> sell at the close
            t.false_break = True
            t.exit_date, t.exit_price, t.exit_reason = t.entry_date, S.C[i], "cut at close"
            t.r = (S.C[i] - t.entry) / (t.entry - t.stop)
            trades.append(t)
            i += 1
            continue
        if t.mode == "intraday" and S.C[i] < max(S.H[i - PL: i]):
            t.false_break = True
            t.exit_date, t.exit_price, t.exit_reason, t.r = t.entry_date, t.stop, "false break", -1.0
            trades.append(t)
            i += 1
            continue
        i = _manage(S, t, i, partial, exits) + 1
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
    experiments: dict[str, list[Trade]] = field(default_factory=dict)

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

    def live(self, m: str = "close", require_rs_leader: bool = False, require_hot_group: bool = False,
            require_regime: bool = False, rs_leader: int = SAR_RS_LEADER, hot_group: int = SAR_HOT_GROUP
            ) -> list[Trade]:
        """The rules the live scan uses: uptrend, score >= min, stop within 1 ADR
        (and not below SAR_MIN_RISK_ADR -- see backtest_ticker). The three
        ``require_*`` flags let you test the still-unvalidated RS/group/regime
        gates (config.py's SAR_REQUIRE_* defaults) before turning them on live.
        """
        ts = [t for t in self.mode(m) if t.risk_adr <= 1.0 and t.trend_ok is not False]
        if require_rs_leader:
            ts = [t for t in ts if (t.rs_rank or 0) >= rs_leader]
        if require_hot_group:
            ts = [t for t in ts if (t.group_rank or 0) >= hot_group]
        if require_regime:
            ts = [t for t in ts if t.regime_ok is True]
        return ts

    def split(self, split_date: str) -> tuple["BacktestResult", "BacktestResult"]:
        """Partition trades by entry_date into (train, test) for walk-forward
        validation: tune/read filters against ``train`` only, then check the
        SAME frozen filters against ``test`` -- a filter that only works on
        the data it was picked from is overfit, not an edge."""
        train = BacktestResult(self.generated, self.period, self.tickers,
                               [t for t in self.trades if t.entry_date < split_date])
        test = BacktestResult(self.generated, self.period, self.tickers,
                              [t for t in self.trades if t.entry_date >= split_date])
        return train, test


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
                 trend_filter: bool = True, intraday: bool = True, slippage: float = 0.002,
                 experiments: bool = True) -> BacktestResult:
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
                if experiments:
                    for name, ex in VARIANTS.items():
                        if name == "Current rules":
                            continue
                        res.experiments.setdefault(name, []).extend(
                            backtest_ticker(tk, bars, min_score, partial, max_risk_adr, regime,
                                            trend_filter, "close", slippage, rs, exits=ex, variant=name))
                    if intraday:
                        res.experiments.setdefault("Intraday + cut at close if unconfirmed", []).extend(
                            backtest_ticker(tk, bars, min_score, partial, max_risk_adr, regime, trend_filter,
                                            "intraday", slippage, rs, confirm_cut=True,
                                            variant="Intraday + cut at close if unconfirmed"))
        if on_progress:
            on_progress(min(start + chunk, len(tickers)), len(tickers), batch[-1])
    _attach_ranks(res.trades, by_date, g_by_date, groups)
    for ts in res.experiments.values():
        _attach_ranks(ts, by_date, g_by_date, groups)
    return res


def _live_filter(ts: Sequence[Trade]) -> list[Trade]:
    return [t for t in ts if t.risk_adr <= 1.0 and t.trend_ok is not False]


def render_experiments(res: BacktestResult) -> str:
    """Every variant on the live-rules setups, split into TRAIN (first 2/3) and TEST (last 1/3)."""
    if not res.experiments:
        return ""
    base = res.live("close")
    dates = sorted(t.entry_date for t in base)
    if len(dates) < 30:
        return ""
    cut = dates[len(dates) * 2 // 3]
    rows = [("Current rules", base)] + [(k, _live_filter(v)) for k, v in res.experiments.items()]
    def half(ts, first):
        return summarize([t for t in ts if (t.entry_date < cut) == first])
    b_tr, b_te = half(base, True), half(base, False)
    out = ["", "EXPERIMENTS — same live-rules setups, different exits/entries", "=" * 90,
           f"  TRAIN = trades before {cut} · TEST = {cut} onward (judge by TEST)",
           f"  {'':<38}{'TRADES':>7}{'WIN%':>6}{'AVG WIN':>8}{'EXP R':>7}{'PF':>6}{'TRAIN R':>9}{'TEST R':>8}  VERDICT"]
    for name, ts in rows:
        s, tr, te = summarize(ts), half(ts, True), half(ts, False)
        if not s:
            continue
        pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
        if name == "Current rules":
            v = "baseline"
        elif tr and te and b_tr and b_te and tr.expectancy_r > b_tr.expectancy_r and te.expectancy_r > b_te.expectancy_r:
            v = "BETTER in both halves"
        elif te and b_te and te.expectancy_r > b_te.expectancy_r:
            v = "better in TEST only"
        else:
            v = "no improvement"
        out.append(f"  {name:<38}{s.trades:>7}{s.win_rate:>6.0%}{s.avg_win_r:>+8.2f}{s.expectancy_r:>+7.2f}{pf:>6}"
                   f"{(tr.expectancy_r if tr else 0):>+9.2f}{(te.expectancy_r if te else 0):>+8.2f}  {v}")
    out += ["", "  Adopt a change only if it says BETTER in both halves AND the gain is more than ~0.03R.", ""]
    return "\n".join(out)


def write_trades_csv(res: BacktestResult, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        rows = [asdict(t) for t in res.trades]
        if not rows:
            fh.write("no trades\n")
            return
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def render_backtest_full(res: BacktestResult) -> str:
    return render_backtest(res) + render_experiments(res)


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
        row("  + favorable regime only", summarize([t for t in live if t.regime_ok is True])),
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
        f"CAVEATS: today's ticker list only (survivorship bias); close fills exact, intraday fills",
        "approximated from daily bars with 0.2% slippage; no commissions; trades across tickers",
        f"overlap; trades with a stop under {SAR_MIN_RISK_ADR} ADR are excluded (see SAR_MIN_RISK_ADR --",
        "those produced unrealistic, unfillable R-multiples on a prior run). Treat as a rough guide,",
        "not proof -- especially the RS/group/regime-stacked rows above, which have NOT been",
        "walk-forward tested (run with --split-date to check a filter out-of-sample before trusting it).",
        "",
    ]
    return "\n".join(lines)


def render_backtest_split(res: BacktestResult, split_date: str) -> str:
    """Walk-forward view: render the pre-split ('train') and post-split ('test')
    periods separately, with the SAME filters in both. A row that only looks
    good in train and falls apart in test was curve-fit to train, not a real
    edge -- that comparison is the point of this report, read it before
    trusting any filter combination above.
    """
    train, test = res.split(split_date)
    header = (
        f"\nWALK-FORWARD SPLIT at {split_date} -- same rules, two non-overlapping periods.\n"
        f"Judge a filter by whether TEST still looks like TRAIN, not by TRAIN alone.\n"
    )
    return (header
            + "\n" + ("=" * 20) + " TRAIN (before split) " + ("=" * 20) + render_backtest(train)
            + "\n" + ("=" * 20) + " TEST (on/after split) " + ("=" * 20) + render_backtest(test))
