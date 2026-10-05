"""SAR Trading breakout-setup scoring engine (pure Python, no deps).

"SAR" here is the SAR Trading educator's system, NOT the Parabolic SAR
indicator. The checklist (daily chart):

  01  30%+ run-up over days/weeks before the pullback
  02  10/20 SMA inclining (10 rising, above a rising 20)
  03  Orderly pullback with a tightening range
  04  Volume drying up during the pullback
  05a Close breaks the pullback range
  05b ...on high volume
  05c ...closing near the high of day

Each item earns partial credit (0..1) times its weight; weights sum to 100.
The weighting is this tool's own (breakout volume + range break heaviest) —
the source docs don't specify points. Scoring mirrors the web walkthrough
(SAR Setup Walkthrough.dc.html) so a name scores the same in both.

Indicators are precomputed once per series (``Series``) so the backtester
can score every bar of a multi-year history quickly.

Educational decision framework, not financial advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from stockscan.config import (
    SAR_RUNUP_LOOKBACK,
    SAR_PULLBACK_LOOKBACK,
    SAR_WEIGHTS,
    SAR_TAKE_AT,
    SAR_WATCH_AT,
    SAR_MIN_PRICE,
    SAR_MIN_ADR,
    SAR_MIN_DOLLAR_VOL,
    SAR_COIL_MIN_PREP,
    SAR_COIL_MAX_GAP,
    SAR_MAX_RISK_ADR,
    SAR_MIN_RISK_ADR,
    SAR_MAX_FROM_HIGH,
)


def effective_risk_per_share(entry: float, stop: float, adr_pct: float,
                             min_risk_adr: float = SAR_MIN_RISK_ADR) -> float:
    """Risk-per-share floored at a minimum fraction of a normal day's range.

    A stop a few cents from entry gives a near-zero raw risk -- any
    R-multiple or share count computed by dividing by it is an artifact of
    that denominator, not a real, fillable result (see SAR_MIN_RISK_ADR's
    comment in config.py). Position sizing uses this floored figure; the
    actual stop price a trade exits at is unaffected.
    """
    raw = entry - stop
    floor = min_risk_adr * adr_pct * entry if adr_pct else raw
    return max(raw, floor)


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class StepScore:
    key: str
    title: str
    frac: float      # 0..1 partial credit
    points: int
    max_points: int
    metric: str

    @property
    def status(self) -> str:
        return "met" if self.frac >= 0.8 else "partial" if self.frac >= 0.4 else "missing"


@dataclass
class Target:
    label: str
    price: float
    r_multiple: float
    note: str = ""


@dataclass
class SetupScore:
    ticker: str
    date: str
    score: int
    verdict: str            # "Take" | "Watch" | "Skip"
    steps: list[StepScore]
    entry: float
    stop: float
    base_high: float
    base_low: float
    run_low: float
    run_high: float
    runup_pct: float
    adr_pct: float
    dollar_vol: float
    sma10: Optional[float]
    volx: float = 0.0
    targets: list[Target] = field(default_factory=list)
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    trend_ok: Optional[bool] = None
    trend_note: str = ""
    sma50: Optional[float] = None
    sma200: Optional[float] = None
    pct_from_high: Optional[float] = None
    rs_rank: Optional[int] = None       # 1-99 vs every stock scanned
    sector: str = ""
    group: str = ""                     # industry (or sector if the industry is small)
    group_rank: Optional[int] = None    # 1-99 vs all groups

    @property
    def risk(self) -> float:
        return self.entry - self.stop

    @property
    def risk_adr(self) -> float:
        """Stop distance in units of average daily range (>1 = stop wider than a normal day)."""
        if not self.adr_pct or not self.entry:
            return 0.0
        return (self.risk / self.entry) / self.adr_pct

    @property
    def wide_stop(self) -> bool:
        return self.risk_adr > SAR_MAX_RISK_ADR

    @property
    def tight_stop(self) -> bool:
        """Stop is unrealistically close to entry (near-zero risk) -- see
        SAR_MIN_RISK_ADR. Not a real signal to trade or count in stats."""
        return 0 < self.risk_adr < SAR_MIN_RISK_ADR

    @property
    def sizing_risk(self) -> float:
        """Floored risk-per-share for position sizing (see effective_risk_per_share)."""
        return effective_risk_per_share(self.entry, self.stop, self.adr_pct)

    @property
    def is_breakout(self) -> bool:
        return self.entry > self.base_high

    @property
    def prep_points(self) -> int:
        """Points from steps 01-04 (the setup forming, before any breakout)."""
        return sum(s.points for s in self.steps[:4])

    @property
    def gap_to_base(self) -> float:
        """Fractional distance from close to the base high (negative = below)."""
        return self.entry / self.base_high - 1.0 if self.base_high else 0.0

    @property
    def is_coiling(self) -> bool:
        """Setup is formed but hasn't broken out: strong 01-04, close just under the base."""
        return (not self.is_breakout
                and self.prep_points >= SAR_COIL_MIN_PREP
                and self.gap_to_base >= -SAR_COIL_MAX_GAP)


STEP_TITLES = [
    ("runup", "30%+ run-up"),
    ("sma", "10/20 SMA inclining"),
    ("tighten", "Tightening pullback"),
    ("dryup", "Volume dries up"),
    ("rangebreak", "Breaks the range"),
    ("breakvol", "On high volume"),
    ("nearhod", "Closes near the high"),
]


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _round_half_up(x: float) -> int:
    return int(x + 0.5)


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def min_bars() -> int:
    return SAR_RUNUP_LOOKBACK + SAR_PULLBACK_LOOKBACK + 1


class Series:
    """Price arrays + indicators computed once for a bar list."""

    def __init__(self, bars: Sequence[Bar]):
        self.bars = bars
        self.O = [b.open for b in bars]
        self.C = [b.close for b in bars]
        self.H = [b.high for b in bars]
        self.L = [b.low for b in bars]
        self.V = [b.volume for b in bars]
        self.s10 = sma(self.C, 10)
        self.s20 = sma(self.C, 20)
        self.av20 = sma(self.V, 20)
        rng = [(b.high / b.low - 1) if b.low > 0 else 0.0 for b in bars]
        dv = [b.close * b.volume for b in bars]
        self.adr20 = sma(rng, 20)
        self.dvol20 = sma(dv, 20)
        self.s50 = sma(self.C, 50)
        self.s200 = sma(self.C, 200)
        # rolling 252-bar high (52 weeks) via monotonic deque
        from collections import deque
        self.hi252: list[float] = []
        dq: deque = deque()
        for i, h in enumerate(self.H):
            while dq and self.H[dq[-1]] <= h:
                dq.pop()
            dq.append(i)
            if dq[0] <= i - 252:
                dq.popleft()
            self.hi252.append(self.H[dq[0]])

    def __len__(self) -> int:
        return len(self.bars)

    def adr(self, i: int) -> float:
        v = self.adr20[i]
        if v is not None:
            return v
        w = self.bars[: i + 1]
        return sum(b.high / b.low - 1 for b in w if b.low > 0) / len(w) if w else 0.0

    def dollar_vol(self, i: int) -> float:
        v = self.dvol20[i]
        if v is not None:
            return v
        w = self.bars[: i + 1]
        return sum(b.close * b.volume for b in w) / len(w) if w else 0.0

    def volx(self, i: int) -> float:
        a = self.av20[i]
        return self.V[i] / a if a else 0.0

    def passes_filters(self, i: int) -> tuple[bool, str]:
        if i + 1 < min_bars():
            return False, f"only {i + 1} bars"
        if self.C[i] <= SAR_MIN_PRICE:
            return False, "price"
        if self.adr(i) <= SAR_MIN_ADR:
            return False, "adr"
        if self.dollar_vol(i) <= SAR_MIN_DOLLAR_VOL:
            return False, "dollar volume"
        return True, ""

    def trend(self, i: int) -> tuple[Optional[bool], str]:
        """Long-term uptrend check. None = not enough history to judge."""
        s50, s200, c = self.s50[i], self.s200[i], self.C[i]
        if s50 is None or i < 70 or self.s50[i - 20] is None:
            return None, "not enough history for the 50 SMA"
        fails = []
        if c <= s50:
            fails.append("below the 50 SMA")
        if s50 <= self.s50[i - 20]:
            fails.append("50 SMA falling")
        if s200 is not None and c <= s200:
            fails.append("below the 200 SMA")
        off = c / self.hi252[i] - 1 if self.hi252[i] else 0.0
        if off < -SAR_MAX_FROM_HIGH:
            fails.append(f"{off:.0%} from 52-week high")
        return (not fails), ("; ".join(fails) if fails else "uptrend")

    def breaks_range(self, i: int) -> bool:
        PL = SAR_PULLBACK_LOOKBACK
        return i >= PL and self.C[i] > max(self.H[i - PL: i])


def adr_pct(bars: Sequence[Bar], i: Optional[int] = None, n: int = 20) -> float:
    i = len(bars) - 1 if i is None else i
    w = bars[max(0, i - n + 1): i + 1]
    return sum(b.high / b.low - 1 for b in w if b.low > 0) / len(w) if w else 0.0


def dollar_volume(bars: Sequence[Bar], i: Optional[int] = None, n: int = 20) -> float:
    i = len(bars) - 1 if i is None else i
    w = bars[max(0, i - n + 1): i + 1]
    return sum(b.close * b.volume for b in w) / len(w) if w else 0.0


def passes_filters(bars: Sequence[Bar]) -> tuple[bool, str]:
    """The doc's stock filters: price > $1, ADR% > 5, avg $ volume > $3.5M."""
    if len(bars) < min_bars():
        return False, f"only {len(bars)} bars"
    if bars[-1].close <= SAR_MIN_PRICE:
        return False, "price"
    if adr_pct(bars) <= SAR_MIN_ADR:
        return False, "adr"
    if dollar_volume(bars) <= SAR_MIN_DOLLAR_VOL:
        return False, "dollar volume"
    return True, ""


def _swing_highs(H: Sequence[float], focus: int, above: float, k: int = 3) -> list[float]:
    last = len(H) - 1
    out = []
    for j in range(k, last - k + 1):
        if focus - 2 <= j <= focus:
            continue
        if all(H[j] >= H[j - d] and H[j] >= H[j + d] for d in range(1, k + 1)) and H[j] > above:
            out.append(H[j])
    return sorted(out)


def score_setup(bars: Sequence[Bar], i: Optional[int] = None, ticker: str = "",
                take_at: int = SAR_TAKE_AT, watch_at: int = SAR_WATCH_AT,
                series: Optional[Series] = None, with_targets: bool = True) -> SetupScore:
    """Score bar ``i`` (default: latest) as a potential SAR breakout day."""
    S = series or Series(bars)
    n = len(S)
    i = n - 1 if i is None else i
    RL, PL = SAR_RUNUP_LOOKBACK, SAR_PULLBACK_LOOKBACK
    if i < RL + PL:
        raise ValueError(f"need at least {RL + PL + 1} bars before the scored bar (have {i + 1})")
    C, H, L, V, s10, s20, av20 = S.C, S.H, S.L, S.V, S.s10, S.s20, S.av20
    dates = S.bars

    # 01 run-up: largest low->high rise in the window before the base.
    ws, we = max(0, i - RL - PL), i - PL
    min_c, min_i, best, low_i, high_i = float("inf"), ws, 0.0, ws, ws
    for k in range(ws, we + 1):
        if C[k] < min_c:
            min_c, min_i = C[k], k
        g = C[k] / min_c - 1
        if g > best:
            best, low_i, high_i = g, min_i, k
    run_low, run_high = L[low_i], max(H[low_i: we + 1])
    f1 = _clamp(best / 0.30)

    # 02 SMA incline.
    sl10, sl20 = s10[i] - s10[i - 5], s20[i] - s20[i - 5]
    f2 = (0.4 if sl10 > 0 else 0) + (0.4 if s10[i] > s20[i] else 0) + (0.2 if sl20 > 0 else 0)

    # 03 tightening base: 2nd-half avg range vs 1st-half.
    half = PL // 2
    rg = [(H[k] - L[k]) / C[k] for k in range(i - PL, i)]
    r1, r2 = sum(rg[:half]) / half, sum(rg[half:]) / (PL - half)
    tight = r2 / r1 if r1 else 1.0
    f3 = _clamp((1 - tight) / 0.4)
    base_high, base_low = max(H[i - PL: i]), min(L[i - PL: i])

    # 04 volume dry-up: base avg vs run-up window avg.
    p_avg = sum(V[i - PL: i]) / PL
    ev = V[max(0, i - PL - RL): i - PL]
    e_avg = sum(ev) / len(ev) if ev else p_avg
    dry = p_avg / e_avg if e_avg else 1.0
    f4 = _clamp((1 - dry) / 0.3)

    # 05a range break.
    gap = (C[i] - base_high) / base_high
    f5 = 1.0 if gap > 0 else _clamp(1 + gap / 0.05) * 0.5

    # 05b breakout volume.
    volx = V[i] / av20[i] if av20[i] else 0.0
    f6 = _clamp((volx - 1) / 0.8)

    # 05c close near high of day.
    pos = (C[i] - L[i]) / (H[i] - L[i]) if H[i] > L[i] else 1.0
    f7 = _clamp((pos - 0.5) / 0.45)

    fracs = [f1, f2, f3, f4, f5, f6, f7]
    metrics = [
        f"{best:+.1%} {dates[low_i].date}->{dates[high_i].date}",
        f"10 {s10[i]:.2f} / 20 {s20[i]:.2f}",
        f"range {r1:.1%}->{r2:.1%}",
        f"{dry:.0%} of run-up vol",
        f"close {C[i]:.2f} vs base {base_high:.2f}",
        f"{volx:.2f}x avg vol",
        f"close at {pos:.0%} of range",
    ]
    steps = [
        StepScore(key, title, fr, _round_half_up(fr * w), w, m)
        for (key, title), fr, w, m in zip(STEP_TITLES, fracs, SAR_WEIGHTS, metrics)
    ]
    score = sum(s.points for s in steps)
    verdict = "Take" if score >= take_at else "Watch" if score >= watch_at else "Skip"

    entry, stop = C[i], L[i]
    R = entry - stop
    adr = S.adr(i)
    sma10_last = s10[-1]

    targets: list[Target] = []
    if with_targets:
        rm = (lambda p: (p - entry) / R) if R > 0 else (lambda p: 0.0)
        if R > 0:
            targets.append(Target("5R partial", entry + 5 * R, 5.0, "sell 10-30%, stop to breakeven"))
        mm = base_high + (run_high - run_low)
        targets.append(Target("Measured move", mm, rm(mm), "run-up height added to base high"))
        for j, p in enumerate(_swing_highs(H, i, entry * 1.005)[:2], 1):
            targets.append(Target(f"Resistance {j}", p, rm(p), "prior swing high"))
        if adr:
            targets.append(Target("+1 ADR", entry * (1 + adr), rm(entry * (1 + adr))))
            targets.append(Target("+3 ADR", entry * (1 + 3 * adr), rm(entry * (1 + 3 * adr))))
        targets.sort(key=lambda t: t.price)
        if sma10_last:
            targets.append(Target("Trailing exit", sma10_last, rm(sma10_last), "exit on daily close below 10 SMA"))
        targets.append(Target("Stop", stop, -1.0, "breakout-day low"))

    tr_ok, tr_note = S.trend(i)
    off = C[i] / S.hi252[i] - 1 if S.hi252[i] else None
    return SetupScore(
        trend_ok=tr_ok, trend_note=tr_note, sma50=S.s50[i], sma200=S.s200[i], pct_from_high=off,
        ticker=ticker, date=dates[i].date, score=score, verdict=verdict, steps=steps,
        entry=entry, stop=stop, base_high=base_high, base_low=base_low,
        run_low=run_low, run_high=run_high, runup_pct=best, adr_pct=adr,
        dollar_vol=S.dollar_vol(i), sma10=sma10_last, volx=volx, targets=targets,
    )


def market_regime(bars: Sequence[Bar]) -> Optional[bool]:
    """True when the index's 10 SMA is above its 20 SMA (doc: setup works best)."""
    if len(bars) < 20:
        return None
    C = [b.close for b in bars]
    return sma(C, 10)[-1] > sma(C, 20)[-1]
