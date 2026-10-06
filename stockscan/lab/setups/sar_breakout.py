"""Setup 1: the SAR breakout (your existing checklist), with honest entries.

The checklist itself is unchanged (stockscan/sar/engine.py score_setup).
What changes is HOW you get in, because "buy intraday, stop at the day's
low" can't be simulated on daily bars without peeking at the day's final low.

Entry modes:
  close       Breakout confirmed at the close; buy at that close; stop = that
              day's low. Matches the docs' end-of-day version. Assumes you act
              in the final minutes (market-on-close).
  next_open   Breakout confirmed at the close; buy the next morning's open;
              stop = breakout-day low. Fully honest, but pays any gap.
  coil_break  Stock is "coiling" (setup formed, close within 3% of the base
              high). Next day, a buy-stop sits just above the base high; stop =
              the coil day's low (known the night before). If the breakout day
              closes back under the base high, or volume pace looks weak, sell
              at the close (confirm-or-cut). Closest honest copy of the live
              intraday method.
"""

from __future__ import annotations

from typing import Optional, Sequence

from stockscan.config import SAR_TAKE_AT, SAR_PULLBACK_LOOKBACK, SAR_MAX_RISK_ADR, SAR_MIN_RISK_ADR
from stockscan.lab.core import Signal
from stockscan.sar.engine import Bar, Series, score_setup, min_bars
from stockscan.sar.strength import rs_raw_series

NAME = "sar_breakout"
MIN_VOLX = 1.3
DEFAULTS = dict(entry="close", min_score=SAR_TAKE_AT, trend_filter=True,
                min_risk_adr=SAR_MIN_RISK_ADR, max_risk_adr=SAR_MAX_RISK_ADR,
                tick=0.01, valid_bars=1, apply_filters=True)


def signals(ticker: str, bars: Sequence[Bar], group: str = "", **params) -> list[Signal]:
    p = {**DEFAULTS, **params}
    n = len(bars)
    if n <= min_bars():
        return []
    S = Series(bars)
    rs = rs_raw_series(S.C)
    PL = SAR_PULLBACK_LOOKBACK
    out: list[Signal] = []
    for i in range(min_bars() - 1, n):
        if p["apply_filters"] and not S.passes_filters(i)[0]:
            continue
        mode = p["entry"]
        if mode in ("close", "next_open"):
            if not (S.breaks_range(i) and S.volx(i) >= MIN_VOLX):
                continue
            s = score_setup(bars, i, ticker=ticker, series=S, with_targets=False)
            if s.score < p["min_score"] or (p["trend_filter"] and s.trend_ok is False):
                continue
            stop = S.L[i]
            ref = S.C[i]
            risk_adr = _risk_adr(ref, stop, s.adr_pct)
            if not _risk_ok(risk_adr, p):
                continue
            out.append(Signal(ticker, i, bars[i].date, NAME, "close" if mode == "close" else "open",
                              stop=stop, rank=rs[i] if rs[i] is not None else -9.0, group=group,
                              meta={"score": s.score, "risk_adr": round(risk_adr, 2), "volx": round(S.volx(i), 2)}))
        elif mode == "coil_break":
            bh = max(S.H[i - PL: i])
            if not (bh * 0.97 <= S.C[i] <= bh):      # cheap pre-check before full scoring
                continue
            s = score_setup(bars, i, ticker=ticker, series=S, with_targets=False)
            if not s.is_coiling or (p["trend_filter"] and s.trend_ok is False):
                continue
            trigger = max(S.H[i - PL + 1: i + 1]) + p["tick"]
            stop = S.L[i]
            risk_adr = _risk_adr(trigger, stop, s.adr_pct)
            if not _risk_ok(risk_adr, p):
                continue
            out.append(Signal(ticker, i, bars[i].date, NAME, "stop", stop=stop, entry_price=trigger,
                              valid_bars=p["valid_bars"], rank=rs[i] if rs[i] is not None else -9.0,
                              group=group, confirm_level=trigger - p["tick"],
                              meta={"score": s.score, "risk_adr": round(risk_adr, 2), "prep": s.prep_points}))
        else:
            raise ValueError(f"unknown entry mode {mode!r}")
    return out


def _risk_adr(entry: float, stop: float, adr: float) -> float:
    if not adr or entry <= 0:
        return 0.0
    return ((entry - stop) / entry) / adr


def _risk_ok(risk_adr: float, p: dict) -> bool:
    if risk_adr <= 0:
        return False
    if p["min_risk_adr"] and risk_adr < p["min_risk_adr"]:
        return False
    if p["max_risk_adr"] and risk_adr > p["max_risk_adr"]:
        return False
    return True
