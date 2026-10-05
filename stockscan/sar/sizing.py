"""Position sizing: fixed-% risk per trade + sector/group concentration cap.

This module only computes a SUGGESTION. stockscan never places an order --
you review every signal and decide (see README/SAR.md); this just turns
"here's a breakout" into "here's how many shares that would be, and here's
whether it would overload one sector," so sizing isn't a separate mental-math
step done under time pressure after the market closes.

Two rules, both standard, un-exotic risk management:

  1. Risk a fixed % of account equity per trade (SAR_RISK_PCT_PER_TRADE),
     sized off the FLOORED risk-per-share (SetupScore.sizing_risk / see
     engine.effective_risk_per_share) so a near-zero stop can't imply an
     absurdly large, unrealistic share count.
  2. Cap how many open positions can share one industry group/sector
     (SAR_MAX_PER_SECTOR), so one theme unwinding (2021-style reversal)
     can't hit most of the account's open risk at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from stockscan.config import SAR_RISK_PCT_PER_TRADE, SAR_MAX_PER_SECTOR, SAR_MAX_POSITIONS
from stockscan.sar.engine import SetupScore


@dataclass
class SizeRecommendation:
    ticker: str
    shares: int
    risk_dollars: float
    position_dollars: float
    pct_of_equity: float
    sizing_risk_per_share: float
    sector: str
    sector_count_if_added: int
    sector_cap_breached: bool
    max_positions_breached: bool
    notes: list[str] = field(default_factory=list)


def recommend_size(s: SetupScore, equity: float, open_sectors: Sequence[str],
                   risk_pct: float = SAR_RISK_PCT_PER_TRADE,
                   max_per_sector: int = SAR_MAX_PER_SECTOR,
                   max_positions: int = SAR_MAX_POSITIONS) -> SizeRecommendation:
    """Suggest a share count for one setup given account equity and the
    sectors/groups of positions already open (pass the OPEN positions' group
    labels, not the whole universe's)."""
    notes: list[str] = []
    risk_per_share = s.sizing_risk
    sector = s.group or s.sector

    if equity <= 0 or risk_per_share <= 0:
        return SizeRecommendation(s.ticker, 0, 0.0, 0.0, 0.0, risk_per_share, sector, 0, False, False,
                                  ["invalid equity or risk-per-share -- can't size"])

    risk_dollars = equity * (risk_pct / 100.0)
    shares = int(risk_dollars // risk_per_share)
    position_dollars = shares * s.entry
    pct_of_equity = (position_dollars / equity * 100.0) if equity else 0.0

    sector_count = (sum(1 for g in open_sectors if g == sector) + 1) if sector else 0
    sector_cap_breached = bool(sector) and sector_count > max_per_sector
    max_positions_breached = (len(open_sectors) + 1) > max_positions

    if risk_per_share > (s.entry - s.stop) * 1.01:
        notes.append(f"real stop distance ({s.entry - s.stop:.2f}) was floored up to {risk_per_share:.2f} "
                     f"for sizing -- the raw stop was unrealistically tight (see SAR_MIN_RISK_ADR)")
    if sector_cap_breached:
        notes.append(f"would be the {sector_count}{_ordinal_suffix(sector_count)} open position in "
                     f"'{sector}' (cap {max_per_sector}/sector) -- consider skipping or sizing down")
    if max_positions_breached:
        notes.append(f"would be open position #{len(open_sectors) + 1} (cap {max_positions} at once)")
    if pct_of_equity > 25:
        notes.append(f"{pct_of_equity:.0f}% of equity in one name is concentrated -- double-check this is intended")
    if shares <= 0:
        notes.append("rounds to 0 shares at this risk %/equity -- position too small to size meaningfully")

    return SizeRecommendation(s.ticker, shares, risk_dollars, position_dollars, pct_of_equity,
                              risk_per_share, sector, sector_count, sector_cap_breached,
                              max_positions_breached, notes)


def _ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def render_sizing(recs: Sequence[SizeRecommendation]) -> str:
    if not recs:
        return ""
    lines = ["", "SUGGESTED SIZE (not an order -- review every line before acting)", "-" * 78,
             f"  {'TICKER':<7}{'SHARES':>8}{'$ RISK':>10}{'$ POSITION':>12}{'% EQUITY':>10}  SECTOR"]
    for r in recs:
        lines.append(f"  {r.ticker:<7}{r.shares:>8}{r.risk_dollars:>10,.0f}{r.position_dollars:>12,.0f}"
                     f"{r.pct_of_equity:>9.1f}%  {r.sector}")
        for n in r.notes:
            lines.append(f"           ! {n}")
    return "\n".join(lines) + "\n"
