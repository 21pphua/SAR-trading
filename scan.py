"""Universe scan for SAR breakout setups.

Pipeline:
  1. Batched daily OHLCV download (yfinance, chunked).
  2. Cheap stock filters (price, ADR%, $ volume) drop most of the universe.
  3. Score the latest bar of every survivor with the SAR checklist.
  4. Split into BREAKOUTS (score >= min_score, range broken) and COILING
     (setup formed, close within a few % of the base high — set alerts).
  5. Long-term trend filter: setups not in an uptrend (below / falling 50 SMA,
     below the 200 SMA, or >25% off the 52-week high) are set aside as
     COUNTER-TREND — bounces in a downtrend aren't SAR continuation setups.
  6. Market-regime check on SPY / QQQ.

Run it after the close: the checklist is defined on completed daily bars.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from stockscan.config import (SAR_TAKE_AT, SAR_REGIME_INDEXES, SAR_EARNINGS_WARN_DAYS, SAR_TREND_FILTER,
                              SAR_REQUIRE_TIGHT_STOP, SAR_HOT_GROUP, SAR_HOT_GROUP_BONUS)
from stockscan.sar.strength import rs_raw, percentile_ranks, load_sectors, group_keys, group_ranks
from stockscan.sar.engine import (
    Bar, SetupScore, Series, passes_filters, score_setup, market_regime,
)

Fetcher = Callable[..., dict]


def fetch_ohlcv(tickers: Sequence[str], period: str = "1y", chunk: int = 200,
                on_progress=None, **_) -> dict[str, list[Bar]]:
    """Daily OHLCV for many tickers via yfinance, in batched chunks."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError("yfinance is required. Install with: pip install 'stockscan[data]'") from exc

    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    out: dict[str, list[Bar]] = {}
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        try:
            df = yf.download(batch, period=period, interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
        except Exception:  # pragma: no cover - network resilience
            df = None
        for tk in batch:
            bars: list[Bar] = []
            if df is not None and not df.empty:
                try:
                    if isinstance(df.columns, pd.MultiIndex):
                        sub = df[tk] if tk in df.columns.get_level_values(0) else None
                    else:
                        sub = df
                    if sub is not None:
                        sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
                        for ts, r in sub.iterrows():
                            bars.append(Bar(ts.strftime("%Y-%m-%d"), float(r["Open"]), float(r["High"]),
                                            float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0) or 0)))
                except Exception:  # pragma: no cover
                    bars = []
            out[tk] = bars
        if on_progress:
            done = min(start + chunk, len(tickers))
            on_progress(done, len(tickers), batch[-1])
    return out


@dataclass
class Position:
    """A trade you hold, read from positions.txt: ticker,shares,entry,stop,date."""
    ticker: str
    shares: int
    entry: float
    stop: float
    date: str = ""
    status: str = ""          # HOLD | NEAR EXIT | 5R HIT | EXIT | STOPPED | NO DATA
    action: str = ""
    last: float = 0.0
    last_date: str = ""
    sma10: Optional[float] = None
    sma20: Optional[float] = None
    exit_line: str = "10 SMA"   # switches to the 20 SMA once 5R is hit (backtest update 9)
    stop_now: float = 0.0
    target_5r: Optional[float] = None
    hit_5r_date: str = ""
    r_now: float = 0.0
    pnl: float = 0.0
    bars: list = field(default_factory=list)


def read_positions(path: Optional[str]) -> list[Position]:
    out: list[Position] = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = [p for p in re.split(r"[,\s]+", line) if p]
            if parts[0].lower() == "ticker":
                continue
            try:
                out.append(Position(parts[0].upper(), int(float(parts[1])), float(parts[2]), float(parts[3]),
                                    parts[4] if len(parts) > 4 else ""))
            except (IndexError, ValueError):
                continue
    return out


def evaluate_position(p: Position, bars: Sequence[Bar]) -> Position:
    """Apply the doc's exit rules to a held position."""
    if not bars:
        p.status, p.action = "NO DATA", "No price data. Check the ticker in positions.txt."
        return p
    S = Series(bars)
    n = len(bars)
    start = next((k for k, b in enumerate(bars) if b.date > p.date), n) if p.date else n - 1
    R = p.entry - p.stop
    p.target_5r = p.entry + 5 * R if R > 0 else None
    stop, stopped = p.stop, ""
    for k in range(start, n):
        b = bars[k]
        if b.low <= stop:
            stopped = b.date
            break
        if p.target_5r and not p.hit_5r_date and b.high >= p.target_5r:
            p.hit_5r_date, stop = b.date, p.entry
    last = bars[-1]
    p.last, p.last_date, p.sma10, p.sma20, p.stop_now = last.close, last.date, S.s10[-1], S.s20[-1], stop
    p.r_now = (p.last - p.entry) / R if R > 0 else 0.0
    p.pnl = (p.last - p.entry) * p.shares
    # After the 5R partial, trail the 20 SMA instead of the 10 (better in both backtest halves).
    trail = p.sma20 if p.hit_5r_date else p.sma10
    p.exit_line = "20 SMA" if p.hit_5r_date else "10 SMA"
    if stopped:
        p.status = "STOPPED"
        p.action = (f"{'Breakeven stop' if p.hit_5r_date else 'Stop'} {stop:.2f} was hit on {stopped}. "
                    "Close the position if your broker hasn't already.")
    elif trail and start < n and p.last < trail:
        p.status = "EXIT"
        p.action = f"Closed {p.last:.2f}, below the {p.exit_line} ({trail:.2f}). Sell the rest at the next open."
    elif p.hit_5r_date and p.target_5r and (n - 1 - next((k for k, b in enumerate(bars) if b.date == p.hit_5r_date), n - 1)) <= 2:
        p.status = "5R HIT"
        p.action = (f"Reached 5R ({p.target_5r:.2f}) on {p.hit_5r_date}. Sell 10-30% if you haven't, "
                    f"and raise your stop to {p.entry:.2f}. From now on, exit on a close below the 20 SMA "
                    f"({(p.sma20 or 0):.2f}), not the 10.")
    elif trail and p.last < trail * 1.02:
        p.status = "NEAR EXIT"
        p.action = f"Within 2% of the {p.exit_line} ({trail:.2f}). A daily close below it means sell."
    else:
        p.status = "HOLD"
        p.action = (f"On track. Stop {stop:.2f}. Sell on a daily close below the {p.exit_line} ({(trail or 0):.2f})."
                    + (" (5R partial taken: trailing the 20 SMA.)" if p.hit_5r_date else ""))
    p.bars = [[b.date, round(b.open, 4), round(b.high, 4), round(b.low, 4), round(b.close, 4), int(b.volume)]
              for b in bars[-260:]]
    return p


@dataclass
class SarScanResult:
    generated: str
    scanned: int
    with_data: int
    passed_filters: int
    regime: dict[str, Optional[bool]]
    breakouts: list[SetupScore] = field(default_factory=list)
    coiling: list[SetupScore] = field(default_factory=list)
    counter_trend: list[SetupScore] = field(default_factory=list)
    wide_stop: list[SetupScore] = field(default_factory=list)
    too_tight: list[SetupScore] = field(default_factory=list)   # stop unrealistically close (SAR_MIN_RISK_ADR)
    positions: list[Position] = field(default_factory=list)
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    all_scored: list[SetupScore] = field(default_factory=list)
    all_bars: dict[str, list[Bar]] = field(default_factory=dict)
    filtered_out: dict[str, str] = field(default_factory=dict)
    live_as_of: Optional[str] = None    # "HH:MM" ET when scored on today's unfinished bar

    @property
    def regime_ok(self) -> Optional[bool]:
        vals = [v for v in self.regime.values() if v is not None]
        return all(vals) if vals else None


# The doc's breakout rule needs real volume: below this multiple of the 20-day
# average, a range break is listed as COILING (not confirmed), not BREAKOUT.
MIN_BREAKOUT_VOLX = 1.3


def _session_open_today() -> Optional[str]:
    """Today's date (US/Eastern) if the regular session hasn't closed yet, else None."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # pragma: no cover
        return None
    if now.weekday() < 5 and (now.hour, now.minute) < (16, 15):
        return now.strftime("%Y-%m-%d")
    return None


def drop_partial_bar(bars: list[Bar], today: Optional[str]) -> list[Bar]:
    """Mid-session the last bar is incomplete (volume, close) — score the prior close instead."""
    return bars[:-1] if today and bars and bars[-1].date == today else bars


def _minutes_into_session() -> int:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return max(0, min(390, (now.hour * 60 + now.minute) - (9 * 60 + 30)))


def project_partial_bar(bars: list[Bar], today: Optional[str], minutes: int) -> list[Bar]:
    """LIVE mode: keep today's unfinished bar, scaling its volume up to a full-day estimate.

    Volume so far x (390 / minutes elapsed), with at least 30 minutes counted. Volume is
    heavier near the open, so early-morning estimates run high — treat as a preview.
    """
    if not (today and bars and bars[-1].date == today):
        return bars
    from dataclasses import replace
    frac = max(30, minutes) / 390
    return bars[:-1] + [replace(bars[-1], volume=bars[-1].volume / frac)]


def breakout_volx(bars: Sequence[Bar]) -> float:
    w = bars[-20:]
    avg = sum(b.volume for b in w) / len(w) if w else 0
    return bars[-1].volume / avg if avg else 0.0


def lookup_earnings(tickers: Sequence[str], max_workers: int = 8) -> dict[str, Optional[str]]:
    """Next earnings date (YYYY-MM-DD) per ticker via yfinance; None when unknown."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date

    def one(tk: str) -> Optional[str]:
        try:
            cal = yf.Ticker(tk).calendar
            ds = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if ds is None and cal is not None and hasattr(cal, "loc"):
                ds = list(cal.loc["Earnings Date"])
            if not isinstance(ds, (list, tuple)):
                ds = [ds] if ds else []
            future = sorted(str(d)[:10] for d in ds if d and str(d)[:10] >= date.today().isoformat())
            return future[0] if future else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return dict(zip(tickers, ex.map(one, tickers)))


def _attach_earnings(setups: Sequence[SetupScore], dates: dict[str, Optional[str]]) -> None:
    from datetime import date
    for s in setups:
        d = dates.get(s.ticker)
        if d:
            s.earnings_date = d
            try:
                s.days_to_earnings = (date.fromisoformat(d) - date.today()).days
            except ValueError:
                pass


def run_sar_scan(tickers: Sequence[str], fetch: Fetcher = fetch_ohlcv, min_score: int = SAR_TAKE_AT,
                 top: int = 25, apply_filters: bool = True, on_progress=None,
                 today: Optional[str] = "auto", earnings=lookup_earnings,
                 trend_filter: bool = SAR_TREND_FILTER,
                 positions: Optional[list[Position]] = None,
                 require_tight_stop: bool = SAR_REQUIRE_TIGHT_STOP, live: bool = False) -> SarScanResult:
    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    today = _session_open_today() if today == "auto" else today
    live_as_of = None
    if live and today:
        mins = _minutes_into_session()
        from zoneinfo import ZoneInfo
        live_as_of = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M")
        prep = lambda v: project_partial_bar(v, today, mins)  # noqa: E731
    else:
        prep = lambda v: drop_partial_bar(v, today)  # noqa: E731
    data = {k: prep(v) for k, v in fetch(tickers, on_progress=on_progress).items()}
    idx = {k: prep(v) for k, v in fetch(list(SAR_REGIME_INDEXES)).items()}
    regime = {k: market_regime(idx.get(k, [])) for k in SAR_REGIME_INDEXES}
    positions = list(positions or [])
    missing = sorted({p.ticker for p in positions} - set(data))
    if missing:
        data.update({k: prep(v) for k, v in fetch(missing).items()})
    for p in positions:
        evaluate_position(p, data.get(p.ticker) or [])

    with_data = passed = 0
    breakouts: list[SetupScore] = []
    coiling: list[SetupScore] = []
    counter: list[SetupScore] = []
    wide: list[SetupScore] = []
    tight: list[SetupScore] = []
    scored: list[SetupScore] = []
    filtered_out: dict[str, str] = {}
    for tk in tickers:
        bars = data.get(tk) or []
        if not bars:
            filtered_out[tk] = "no price data"
            continue
        with_data += 1
        if apply_filters:
            ok, why = passes_filters(bars)
            if not ok:
                filtered_out[tk] = why
                continue
        try:
            s = score_setup(bars, ticker=tk)
        except ValueError:
            filtered_out[tk] = "not enough history"
            continue
        passed += 1
        scored.append(s)
        qualifies = (s.is_breakout and s.score >= min_score) or s.is_coiling
        if trend_filter and qualifies and s.trend_ok is False:
            counter.append(s)
            continue
        if s.is_breakout and s.score >= min_score and breakout_volx(bars) >= MIN_BREAKOUT_VOLX:
            if s.tight_stop:
                tight.append(s)
            else:
                (wide if require_tight_stop and s.wide_stop else breakouts).append(s)
        elif s.is_coiling or (s.is_breakout and s.score >= min_score):  # low-volume break = unconfirmed
            coiling.append(s)

    attach_strength(scored, data)
    by_rs = lambda s: (s.rs_rank or 0, s.score)
    breakouts.sort(key=by_rs, reverse=True)
    coiling.sort(key=by_rs, reverse=True)
    counter.sort(key=lambda s: s.score, reverse=True)
    wide.sort(key=by_rs, reverse=True)
    tight.sort(key=lambda s: s.score, reverse=True)
    breakouts, coiling, counter, wide, tight = breakouts[:top], coiling[:top], counter[:top], wide[:top], tight[:top]
    keep = {s.ticker for s in breakouts + coiling + wide}
    if earnings and keep:
        _attach_earnings(breakouts + coiling + wide, earnings(sorted(keep)))
    res_ = SarScanResult(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scanned=len(tickers), with_data=with_data, passed_filters=passed, regime=regime,
        breakouts=breakouts, coiling=coiling, counter_trend=counter, wide_stop=wide, too_tight=tight,
        positions=positions,
        bars={t: data[t] for t in keep},
        all_scored=scored, all_bars={x.ticker: data[x.ticker] for x in scored}, filtered_out=filtered_out,
    )
    res_.live_as_of = live_as_of
    return res_


def attach_strength(scored: list[SetupScore], data: dict[str, list[Bar]]) -> None:
    """RS rank vs every stock with data, and industry-group rank, onto each setup."""
    rs = {}
    for tk, bars in data.items():
        if tk in SAR_REGIME_INDEXES or not bars or bars[-1].close < 1:
            continue
        v = rs_raw([b.close for b in bars])
        if v is not None:
            rs[tk] = v
    ranks = percentile_ranks(rs)
    sectors = load_sectors()
    groups = group_keys(list(data), sectors)
    granks = group_ranks(rs, groups)
    for s in scored:
        s.rs_rank = ranks.get(s.ticker)
        s.sector = sectors.get(s.ticker, ("", ""))[0]
        s.group = groups.get(s.ticker, "")
        s.group_rank = granks.get(s.group)
        if SAR_HOT_GROUP_BONUS and (s.group_rank or 0) >= SAR_HOT_GROUP:
            s.score = min(100, s.score + SAR_HOT_GROUP_BONUS)


def _setup_json(s: SetupScore, kind: str, bars: list[Bar], keep_bars: int = 260) -> dict:
    d = asdict(s)
    d["kind"] = kind
    d["risk_adr"] = round(s.risk_adr, 2)
    d["wide_stop"] = s.wide_stop
    d["earnings_soon"] = s.days_to_earnings is not None and 0 <= s.days_to_earnings <= SAR_EARNINGS_WARN_DAYS
    d["steps"] = [{**asdict(st), "status": st.status} for st in s.steps]
    d["bars"] = [[b.date, round(b.open, 4), round(b.high, 4), round(b.low, 4), round(b.close, 4), int(b.volume)]
                 for b in bars[-keep_bars:]]
    return d


def _previous(path: str, generated: str) -> Optional[dict]:
    """Yesterday's lists, for NEW / FIRED badges. Same-day reruns keep comparing to the prior day."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:
        return None
    if str(old.get("generated", ""))[:10] == generated[:10] and old.get("previous"):
        return old["previous"]
    res = old.get("results", [])
    return {"generated": old.get("generated"),
            "breakouts": [r["ticker"] for r in res if r.get("kind") in ("breakout", "wide")],
            "coiling": [r["ticker"] for r in res if r.get("kind") == "coiling"]}


def write_shortlist(result: SarScanResult, path: str, prev_path: Optional[str] = None,
                    with_all: bool = True) -> None:
    """JSON the web walkthrough can load ("Load scan file")."""
    prev = _previous(prev_path or path, result.generated)
    doc = {
        "format": "sar-shortlist/1",
        "generated": result.generated,
        "live": result.live_as_of,
        "scanned": result.scanned,
        "passed_filters": result.passed_filters,
        "regime": result.regime,
        "counter_trend": [{"ticker": s.ticker, "score": s.score, "why": s.trend_note} for s in result.counter_trend],
        "too_tight": [{"ticker": s.ticker, "score": s.score, "entry": s.entry, "stop": s.stop,
                       "risk_adr": round(s.risk_adr, 2)} for s in result.too_tight],
        "results": [_setup_json(s, "breakout", result.bars[s.ticker]) for s in result.breakouts]
                   + [_setup_json(s, "coiling", result.bars[s.ticker]) for s in result.coiling]
                   + [_setup_json(s, "wide", result.bars[s.ticker]) for s in result.wide_stop],
        "positions": [asdict(p) for p in result.positions],
        "previous": prev,
    }
    if prev:
        seen = set(prev.get("breakouts", [])) | set(prev.get("coiling", []))
        for r in doc["results"]:
            r["new"] = r["ticker"] not in seen
            r["fired"] = r["kind"] in ("breakout", "wide") and r["ticker"] in set(prev.get("coiling", []))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    if with_all:
        write_all_scores(result, os.path.join(os.path.dirname(path) or ".", "all_scores.json"))


def write_all_scores(result: SarScanResult, path: str, keep_bars: int = 130) -> None:
    """Every stock that passed the filters, scored exactly like the shortlist.

    Lets the web page look up any ticker and get the same score the scanner gives.
    """
    kinds = {s.ticker: "breakout" for s in result.breakouts}
    kinds.update({s.ticker: "coiling" for s in result.coiling})
    kinds.update({s.ticker: "counter" for s in result.counter_trend})
    kinds.update({s.ticker: "wide" for s in result.wide_stop})
    kinds.update({s.ticker: "tight" for s in result.too_tight})
    doc = {
        "format": "sar-all/1",
        "generated": result.generated,
        "scores": [_setup_json(s, kinds.get(s.ticker, "none"), result.all_bars.get(s.ticker, []), keep_bars)
                   for s in result.all_scored],
        "filtered_out": result.filtered_out,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
