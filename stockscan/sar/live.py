"""Intraday live-rescan: poll real-time quotes for today's shortlisted names
and flag which ones are actually breaking out RIGHT NOW.

Why this exists (and what it is not)
-------------------------------------
``stockscan sar`` scores *completed daily bars* -- steps 01-04 (run-up, SMA
incline, tightening pullback, volume dry-up) are read off finished sessions,
so the engine has to be run after the close (or, mid-session, on yesterday's
bar -- see ``drop_partial_bar`` in scan.py). That's deliberate: those steps
describe how a setup FORMED over days/weeks, not something a live quote can
tell you.

But step 05 -- the breakout itself -- happens live, intraday, while the
market's open. This module does NOT re-run the full checklist on live data.
It takes the COILING (and, optionally, already-confirmed BREAKOUT) names a
prior ``sar --out shortlist.json`` run already identified as well-formed
setups, and just polls their live price/volume against the trigger
(``base_high``) that run already computed, so you can see a real breakout
crossing as it happens instead of waiting for the close.

Workflow this is built for:
  1. ~30 min before the open: ``stockscan sar --out shortlist.json`` -- the
     COILING list is tonight's "setup formed, not broken out yet" names.
  2. After the open, as price moves: ``stockscan sar-live`` (optionally
     ``--loop 60`` to keep polling) -- flags which COILING names have now
     actually crossed the trigger on real volume, vs. a thin, unconfirmed poke.
  3. You assess the chart yourself and decide -- this is a live confirmation
     check, not a trade signal, same caveat as the rest of stockscan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from stockscan.sar.scan import MIN_BREAKOUT_VOLX

# Regular US equity session, US/Eastern.
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)
SESSION_MINUTES = (SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]) - (SESSION_OPEN[0] * 60 + SESSION_OPEN[1])


@dataclass
class LiveQuote:
    ticker: str
    price: float
    day_high: float
    day_low: float
    cum_volume: float
    as_of: str
    bars: int


def fetch_live_quotes(tickers: Sequence[str], interval: str = "1m",
                      prepost: bool = False) -> dict[str, LiveQuote]:
    """Today's intraday bars for a (small!) list of tickers via yfinance.

    Meant for the handful of names on a shortlist, not a universe scan --
    1-minute bars for hundreds of tickers would be slow and likely to get
    rate-limited. Cumulative volume = sum of today's minute bars so far.
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError("yfinance is required. Install with: pip install 'stockscan[data]'") from exc

    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    out: dict[str, LiveQuote] = {}
    if not tickers:
        return out
    try:
        df = yf.download(tickers, period="1d", interval=interval, group_by="ticker",
                         threads=True, progress=False, prepost=prepost)
    except Exception:  # pragma: no cover - network resilience
        df = None
    if df is None or df.empty:
        return out
    for tk in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                sub = df[tk] if tk in df.columns.get_level_values(0) else None
            else:
                sub = df if len(tickers) == 1 else None
            if sub is None:
                continue
            sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
            if sub.empty:
                continue
            out[tk] = LiveQuote(
                ticker=tk,
                price=float(sub["Close"].iloc[-1]),
                day_high=float(sub["High"].max()),
                day_low=float(sub["Low"].min()),
                cum_volume=float(sub["Volume"].fillna(0).sum()),
                as_of=sub.index[-1].strftime("%Y-%m-%d %H:%M"),
                bars=len(sub),
            )
        except Exception:  # pragma: no cover
            continue
    return out


def session_fraction_elapsed(now: Optional[datetime] = None) -> float:
    """Fraction of the regular session elapsed (US/Eastern), clamped to
    [0.03, 1.0] -- a pace-adjusted relvol dividing by ~0 right at the open
    would otherwise read as absurdly, meaninglessly high."""
    try:
        from zoneinfo import ZoneInfo
        now = now or datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # pragma: no cover
        return 1.0
    open_m = SESSION_OPEN[0] * 60 + SESSION_OPEN[1]
    close_m = SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]
    cur_m = now.hour * 60 + now.minute
    if now.weekday() >= 5 or cur_m < open_m:
        return 0.03
    if cur_m >= close_m:
        return 1.0
    return max(0.03, (cur_m - open_m) / SESSION_MINUTES)


def market_is_open(now: Optional[datetime] = None) -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = now or datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # pragma: no cover
        return True  # fail open rather than silently never looping
    if now.weekday() >= 5:
        return False
    cur = (now.hour, now.minute)
    return SESSION_OPEN <= cur < SESSION_CLOSE


@dataclass
class WatchEntry:
    ticker: str
    kind: str
    base_high: float
    score: int
    avg_vol20: float


def avg_vol20_from_bars(bars: Sequence[Sequence]) -> float:
    """``bars``: ``[[date, o, h, l, c, v], ...]`` as stored in shortlist.json
    (most recent last). Average volume of the last 20 COMPLETED sessions."""
    if not bars:
        return 0.0
    window = bars[-20:]
    vols = [b[5] for b in window if len(b) > 5 and b[5] is not None]
    return sum(vols) / len(vols) if vols else 0.0


def load_watchlist(path: str, kinds: Sequence[str] = ("coiling",)) -> list[WatchEntry]:
    """Read a shortlist.json written by ``sar --out`` and pull out the
    tickers/triggers worth polling live. Raises FileNotFoundError if the
    shortlist doesn't exist -- the caller should tell the user to run
    ``sar --out <path>`` first."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    kinds = set(kinds)
    out = []
    for r in doc.get("results", []):
        if r.get("kind") not in kinds:
            continue
        out.append(WatchEntry(
            ticker=r["ticker"], kind=r["kind"], base_high=float(r.get("base_high", 0.0)),
            score=int(r.get("score", 0)), avg_vol20=avg_vol20_from_bars(r.get("bars", [])),
        ))
    return out


@dataclass
class LiveCheck:
    ticker: str
    kind: str
    score: int
    trigger: float
    price: float
    pct_to_trigger: float   # price / trigger - 1 (negative = still below the trigger)
    relvol: float            # today's volume so far vs. 20d avg, paced to time-of-day
    crossed: bool
    confirmed: bool          # crossed AND relvol >= the volume bar
    as_of: str
    note: str


def build_live_checks(entries: Sequence[WatchEntry], quotes: dict[str, LiveQuote],
                      min_relvol: float = MIN_BREAKOUT_VOLX, now: Optional[datetime] = None) -> list[LiveCheck]:
    frac = session_fraction_elapsed(now)
    out: list[LiveCheck] = []
    for e in entries:
        q = quotes.get(e.ticker)
        if q is None:
            out.append(LiveCheck(e.ticker, e.kind, e.score, e.base_high, 0.0, 0.0, 0.0,
                                 False, False, "", "no live quote (delisted? wrong ticker? market data gap)"))
            continue
        pct = (q.price / e.base_high - 1.0) if e.base_high else 0.0
        relvol = (q.cum_volume / (e.avg_vol20 * frac)) if e.avg_vol20 and frac else 0.0
        crossed = bool(e.base_high) and q.price > e.base_high
        confirmed = crossed and relvol >= min_relvol
        if confirmed:
            note = f"CONFIRMED -- crossed {e.base_high:.2f} on {relvol:.2f}x paced volume"
        elif crossed:
            note = f"crossed {e.base_high:.2f} but only {relvol:.2f}x paced volume so far -- could fade, don't chase alone"
        elif pct < 0:
            note = f"{abs(pct):.1%} below the {e.base_high:.2f} trigger"
        else:
            note = "sitting right at the trigger"
        out.append(LiveCheck(e.ticker, e.kind, e.score, e.base_high, q.price, pct, relvol,
                             crossed, confirmed, q.as_of, note))

    def sort_key(c: LiveCheck):
        rank = 0 if c.confirmed else 1 if c.crossed else 2
        return (rank, -c.pct_to_trigger)

    out.sort(key=sort_key)
    return out


def render_live_checks(checks: Sequence[LiveCheck], generated: Optional[str] = None) -> str:
    if not checks:
        return ("\nLIVE RESCAN\n" + "=" * 78 +
                "\n  nothing to watch -- run `stockscan sar --out shortlist.json` before/at the open first\n")
    lines = ["", "LIVE RESCAN" + (f"  ({generated})" if generated else ""), "=" * 78,
             f"  {'TICKER':<7}{'KIND':<10}{'TRIGGER':>9}{'LAST':>9}{'TO TRIG':>9}{'RELVOL':>8}  STATUS"]
    for c in checks:
        status = "CONFIRMED" if c.confirmed else "crossed, thin vol" if c.crossed else "watching"
        lines.append(f"  {c.ticker:<7}{c.kind:<10}{c.trigger:>9.2f}{c.price:>9.2f}"
                     f"{c.pct_to_trigger:>+9.1%}{c.relvol:>7.2f}x  {status}")
        if c.note:
            lines.append(f"           {c.note}")
    lines += [
        "",
        "RELVOL = today's cumulative volume vs. the 20-day average, paced to the fraction of the",
        "session elapsed -- a crude early-day estimate (noisy in the first 15-20 min), not the",
        "real end-of-day volume multiple the daily scan will confirm later.",
        "CONFIRMED still needs your own chart check before acting -- this is a live confirmation",
        "check, not a trade signal.",
        "",
    ]
    return "\n".join(lines)
