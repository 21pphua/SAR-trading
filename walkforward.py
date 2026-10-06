"""Walk-forward: choose settings using only the past, grade them on the future.

How it works, in plain terms:
  * Run the full account once for every candidate setting ("variant").
  * For each year Y (after a warm-up), look ONLY at the years before Y and
    pick the variant that did best there.
  * Record what that pick actually earned in year Y, a year it never saw.
  * String those out-of-sample years together. That stitched record is the
    honest estimate of what tuning would have bought you in real life.

If the walk-forward record is much worse than the best in-hindsight variant,
the "best" settings were fitted to noise.

(Simplification: each variant's account runs continuously, so positions that
cross a year boundary belong to the year they're marked in.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from stockscan.lab.metrics import equity_stats, yearly_returns
from stockscan.lab.portfolio import AccountResult


@dataclass
class WalkForward:
    years: list[str]
    picks: dict[str, str] = field(default_factory=dict)        # year -> variant chosen
    oos_returns: dict[str, float] = field(default_factory=dict)  # year -> return earned
    variant_returns: dict[str, dict[str, float]] = field(default_factory=dict)
    criterion: str = "sharpe"

    @property
    def oos_total(self) -> float:
        g = 1.0
        for r in self.oos_returns.values():
            g *= 1 + r
        return g - 1

    @property
    def oos_cagr(self) -> float:
        n = len(self.oos_returns)
        return (1 + self.oos_total) ** (1 / n) - 1 if n else 0.0


def _score(res: AccountResult, before: str, criterion: str) -> float:
    idx = [k for k, d in enumerate(res.dates) if d < before]
    if len(idx) < 60:
        return float("-inf")
    eq = [res.equity[k] for k in idx]
    dates = [res.dates[k] for k in idx]
    _, cagr, mdd, sharpe = equity_stats(dates, eq)
    if criterion == "mar":
        return cagr / mdd if mdd > 0 else cagr
    if criterion == "cagr":
        return cagr
    return sharpe


def walk_forward(results: dict[str, AccountResult], warmup_years: int = 1,
                 criterion: str = "sharpe") -> Optional[WalkForward]:
    if not results:
        return None
    yr = {name: yearly_returns(r.dates, r.equity) for name, r in results.items()}
    years = sorted({y for v in yr.values() for y in v})
    wf = WalkForward(years=years, variant_returns=yr, criterion=criterion)
    for y in years[warmup_years:]:
        before = f"{y}-01-01"
        best = max(results, key=lambda name: _score(results[name], before, criterion))
        wf.picks[y] = best
        wf.oos_returns[y] = yr[best].get(y, 0.0)
    return wf
