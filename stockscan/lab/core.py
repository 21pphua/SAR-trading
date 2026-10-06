"""Signals, fills, costs and single-trade simulation on daily bars.

The three honesty rules this file enforces:

1. A signal may only use data up to and including its own bar (the "signal
   bar"). Its stop price must be known at that moment, never a later low.
2. Orders fill on LATER bars using rules a real broker would follow:
     buy-stop at P: if the day opens above P you pay the open (gap), else P if
                    the high reaches P, else no fill.
     buy-limit at P: if the day opens below P you pay the open, else P if the
                    low reaches P, else no fill.
     next open:     you pay the open.
     at close:      you pay the signal bar's close (a market-on-close order;
                    flagged in reports because it assumes you act in the last
                    minutes using almost-final prices and volume).
3. When a daily bar can't tell us the order of events (did the low come
   before or after our fill?), assume the worse outcome. A fill day whose low
   touches the stop counts as stopped out.

Every fill pays slippage + commission, so R multiples are net of costs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from stockscan.sar.engine import Series


@dataclass
class CostModel:
    """Trading costs per fill. slippage_bps=10 means you pay 0.10% worse than
    the quoted price on every buy and every sell."""
    slippage_bps: float = 10.0
    commission_per_share: float = 0.0
    min_commission: float = 0.0

    def buy_px(self, px: float) -> float:
        return px * (1 + self.slippage_bps / 10_000)

    def sell_px(self, px: float) -> float:
        return px * (1 - self.slippage_bps / 10_000)

    def commission(self, shares: float) -> float:
        if shares <= 0:
            return 0.0
        return max(self.min_commission, self.commission_per_share * shares)


@dataclass
class Signal:
    """A trade idea produced at the close of bar ``i``.

    entry_type: "close" | "open" | "stop" | "limit"
    entry_price: the order price for "stop"/"limit" (ignored otherwise)
    stop: initial stop price, known at the signal bar
    valid_bars: how many following days a stop/limit order stays working
    rank: priority when there are more signals than open slots (higher first)
    confirm_level: optional "must close above this on the fill day" check;
                   if the fill day closes below it, sell at that close
                   (confirm-or-cut; uses only information known at the close)
    """
    ticker: str
    i: int
    date: str
    setup: str
    entry_type: str
    stop: float
    entry_price: Optional[float] = None
    valid_bars: int = 1
    rank: float = 0.0
    group: str = ""
    confirm_level: Optional[float] = None
    meta: dict = field(default_factory=dict)


@dataclass
class ExitRules:
    """How an open trade is managed (defaults = the SAR docs).

    partial_r / partial_frac: sell this fraction once price reaches this many R
    breakeven_after_partial:  then raise the stop to the entry price
    trail_sma:                sell everything on a daily close below this SMA
    trail_after_partial:      switch to this SMA after the partial (None = same)
    time_stop_bars / time_stop_min_r: sell at the close after N days if the
                              trade never reached time_stop_min_r
    max_hold_bars:            hard limit on days held (None = no limit)
    """
    partial_r: Optional[float] = 5.0
    partial_frac: float = 0.20
    breakeven_after_partial: bool = True
    trail_sma: Optional[int] = 10
    trail_after_partial: Optional[int] = None
    time_stop_bars: Optional[int] = None
    time_stop_min_r: float = 1.0
    max_hold_bars: Optional[int] = None


@dataclass
class Event:
    """Something that happens to an open trade on a given day."""
    date: str
    kind: str          # "stop" (stop moved) | "sell"
    frac: float = 0.0  # fraction of the ORIGINAL position sold
    price: float = 0.0  # net fill price (after slippage) or new stop level
    reason: str = ""


@dataclass
class Candidate:
    """One signal played out on its own, before the account decides whether
    it can afford it. Prices are per share; R is net of slippage."""
    signal: Signal
    fill_i: int
    fill_date: str
    entry: float           # net entry price (after slippage)
    stop: float
    events: list[Event] = field(default_factory=list)
    exit_date: str = ""
    exit_i: int = 0
    open: bool = False      # still open at the end of the data
    r: float = 0.0          # result in R, slippage included (commission is per-share, added in the account)
    hit_partial: bool = False
    reason: str = ""

    @property
    def risk(self) -> float:
        return self.entry - self.stop


def _sma_line(S: Series, n: Optional[int]) -> Optional[list]:
    if n is None:
        return None
    if n == 10:
        return S.s10
    if n == 20:
        return S.s20
    if n == 50:
        return S.s50
    from stockscan.sar.engine import sma
    return sma(S.C, n)


def try_fill(sig: Signal, S: Series, costs: CostModel, max_gap: Optional[float] = None
             ) -> Optional[tuple[int, float]]:
    """Find the first bar that fills the signal's order. Returns (bar, net price) or None."""
    n = len(S)
    if sig.entry_type == "close":
        return sig.i, costs.buy_px(S.C[sig.i])
    if sig.entry_type == "open":
        j = sig.i + 1
        return (j, costs.buy_px(S.O[j])) if j < n else None
    p = sig.entry_price
    if p is None:
        return None
    for j in range(sig.i + 1, min(n, sig.i + 1 + sig.valid_bars)):
        o, h, l = S.O[j], S.H[j], S.L[j]
        if sig.entry_type == "stop":
            if max_gap is not None and o > p * (1 + max_gap):
                return None      # gapped too far past the trigger: don't chase
            if o >= p:
                return j, costs.buy_px(o)
            if h >= p:
                return j, costs.buy_px(p)
        elif sig.entry_type == "limit":
            if o <= p:
                return j, costs.buy_px(o)
            if l <= p:
                return j, costs.buy_px(p)
        else:
            raise ValueError(f"unknown entry_type {sig.entry_type!r}")
    return None


def simulate(sig: Signal, S: Series, rules: ExitRules, costs: CostModel,
             max_gap: Optional[float] = None) -> Optional[Candidate]:
    """Play one signal out bar by bar. None if the order never fills or the
    fill would be at/below the stop (nothing to risk)."""
    f = try_fill(sig, S, costs, max_gap)
    if f is None:
        return None
    j0, entry = f
    stop = sig.stop
    if entry <= stop:
        return None
    n = len(S)
    c = Candidate(sig, j0, S.bars[j0].date, entry, stop)
    R = entry - stop
    left = 1.0
    proceeds = 0.0       # sum(frac * net sell price)
    best_high = entry

    def sell(j: int, frac: float, raw_px: float, reason: str) -> None:
        nonlocal left, proceeds
        px = costs.sell_px(raw_px)
        c.events.append(Event(S.bars[j].date, "sell", frac, px, reason))
        proceeds += frac * px
        left -= frac

    # --- the fill day itself -------------------------------------------------
    if sig.entry_type != "close":
        if S.L[j0] <= stop:
            # can't know if the low came before our fill; assume the worse case
            sell(j0, left, min(stop, S.O[j0]) if S.O[j0] < stop else stop, "stop (fill day)")
        elif sig.confirm_level is not None and S.C[j0] < sig.confirm_level:
            sell(j0, left, S.C[j0], "cut at close (no confirmation)")
    if left <= 1e-9:
        return _finish(c, j0, proceeds, R)

    trail = _sma_line(S, rules.trail_sma)
    trail2 = _sma_line(S, rules.trail_after_partial) if rules.trail_after_partial else trail
    target = entry + rules.partial_r * R if rules.partial_r else None

    for j in range(j0 + 1, n):
        o, h, l, cl = S.O[j], S.H[j], S.L[j], S.C[j]
        # 1) stop first (worst case)
        if l <= stop:
            sell(j, left, min(stop, o), "breakeven" if stop >= entry else "stop")
            return _finish(c, j, proceeds, R)
        # 2) partial profit at the target (limit order)
        if target is not None and not c.hit_partial and h >= target:
            c.hit_partial = True
            sell(j, rules.partial_frac, max(target, o), f"partial {rules.partial_r:g}R")
            if rules.breakeven_after_partial and stop < entry:
                stop = entry
                c.events.append(Event(S.bars[j].date, "stop", 0.0, stop, "breakeven"))
        best_high = max(best_high, h)
        # 3) end-of-day exits
        line = trail2 if c.hit_partial else trail
        if line is not None and line[j] is not None and cl < line[j]:
            sell(j, left, cl, f"close < {rules.trail_after_partial if c.hit_partial and rules.trail_after_partial else rules.trail_sma} SMA")
            return _finish(c, j, proceeds, R)
        held = j - j0
        if (rules.time_stop_bars and held >= rules.time_stop_bars
                and best_high < entry + rules.time_stop_min_r * R):
            sell(j, left, cl, f"time stop {rules.time_stop_bars}d")
            return _finish(c, j, proceeds, R)
        if rules.max_hold_bars and held >= rules.max_hold_bars:
            sell(j, left, cl, "max hold")
            return _finish(c, j, proceeds, R)
    # still open at the end of the data: mark at the last close
    c.open = True
    j = n - 1
    proceeds += left * costs.sell_px(S.C[j])
    left = 0.0
    c.reason = "open"
    c.exit_date, c.exit_i = S.bars[j].date, j
    c.r = (proceeds - entry) / R
    return c


def _finish(c: Candidate, j: int, proceeds: float, R: float) -> Candidate:
    c.exit_i = j
    c.exit_date = c.events[-1].date if c.events else ""
    c.reason = c.events[-1].reason if c.events else ""
    c.r = (proceeds - c.entry) / R
    return c


def lookahead_safe(signals_fn, bars: Sequence, i: int) -> bool:
    """Test helper: signals for bar i must be identical whether or not the
    function can see bars after i."""
    full = [s for s in signals_fn(bars) if s.i == i]
    cut = [s for s in signals_fn(bars[: i + 1]) if s.i == i]
    key = lambda s: (s.entry_type, round(s.stop, 6), round(s.entry_price or 0, 6), s.setup)
    return sorted(map(key, full)) == sorted(map(key, cut))
