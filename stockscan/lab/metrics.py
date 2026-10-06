"""The scorecard. Plain-English meaning of each number:

  CAGR            average yearly growth of the account, compounded
  Max drawdown    worst peak-to-bottom drop of the account (how much pain)
  MAR             CAGR / max drawdown: return per unit of pain (>0.5 is decent)
  Sharpe          return per unit of day-to-day bumpiness (>1 is good)
  Exposure        average share of the account sitting in positions
  Win rate        % of trades that made money
  Avg win / loss  average result of winners / losers, in R
  Expectancy      average result per trade in R (the edge)
  Profit factor   total won / total lost (>1.3 is healthy after costs)
  Exp. w/o top 10 expectancy after deleting the 10 best trades. If this goes
                  negative, the "edge" is a handful of lucky outliers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from statistics import median
from typing import Optional, Sequence

from stockscan.lab.portfolio import AccountResult, TradeRecord


@dataclass
class Scorecard:
    start: str
    end: str
    years: float
    start_equity: float
    end_equity: float
    cagr: float
    max_drawdown: float
    mar: float
    sharpe: float
    exposure: float
    trades: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    median_r: float
    profit_factor: float
    expectancy_wo_top10: float
    max_losing_streak: int
    avg_days_held: float

    def as_dict(self) -> dict:
        return asdict(self)


def equity_stats(dates: Sequence[str], eq: Sequence[float]) -> tuple[float, float, float, float]:
    """(years, CAGR, max drawdown, Sharpe) from a daily equity curve."""
    if len(eq) < 2 or eq[0] <= 0:
        return 0.0, 0.0, 0.0, 0.0
    years = max(len(eq) / 252.0, 1 / 252)
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if eq[-1] > 0 else -1.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak if peak else 0.0)
    rets = [eq[k] / eq[k - 1] - 1 for k in range(1, len(eq)) if eq[k - 1] > 0]
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = mu / sd * math.sqrt(252) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    return years, cagr, mdd, sharpe


def trade_stats(trades: Sequence[TradeRecord]) -> dict:
    closed = sorted((t for t in trades if not t.open), key=lambda t: t.exit_date)
    if not closed:
        return dict(trades=0, win_rate=0.0, avg_win_r=0.0, avg_loss_r=0.0, expectancy_r=0.0,
                    median_r=0.0, profit_factor=0.0, expectancy_wo_top10=0.0,
                    max_losing_streak=0, avg_days_held=0.0)
    rs = [t.r for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gl = -sum(losses)
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    trimmed = sorted(rs)[:-10] if len(rs) > 20 else []
    return dict(
        trades=len(closed),
        win_rate=len(wins) / len(closed),
        avg_win_r=sum(wins) / len(wins) if wins else 0.0,
        avg_loss_r=sum(losses) / len(losses) if losses else 0.0,
        expectancy_r=sum(rs) / len(rs),
        median_r=median(rs),
        profit_factor=sum(wins) / gl if gl else float("inf"),
        expectancy_wo_top10=sum(trimmed) / len(trimmed) if trimmed else float("nan"),
        max_losing_streak=worst,
        avg_days_held=sum(t.days for t in closed) / len(closed),
    )


def scorecard(res: AccountResult) -> Scorecard:
    years, cagr, mdd, sharpe = equity_stats(res.dates, res.equity)
    ts = trade_stats(res.trades)
    return Scorecard(
        start=res.dates[0] if res.dates else "", end=res.dates[-1] if res.dates else "",
        years=years, start_equity=res.rules.start_equity,
        end_equity=res.equity[-1] if res.equity else res.rules.start_equity,
        cagr=cagr, max_drawdown=mdd, mar=cagr / mdd if mdd > 0 else 0.0, sharpe=sharpe,
        exposure=sum(res.exposure) / len(res.exposure) if res.exposure else 0.0,
        **ts)


def yearly_returns(dates: Sequence[str], eq: Sequence[float]) -> dict[str, float]:
    """Calendar-year return of the account (first year from the start date)."""
    out: dict[str, float] = {}
    start_val: Optional[float] = None
    year = None
    prev = None
    for d, v in zip(dates, eq):
        y = d[:4]
        if y != year:
            if year is not None and start_val:
                out[year] = prev / start_val - 1
            year, start_val = y, (prev if prev is not None else v)
        prev = v
    if year is not None and start_val:
        out[year] = prev / start_val - 1
    return out


def yearly_drawdowns(dates: Sequence[str], eq: Sequence[float]) -> dict[str, float]:
    out: dict[str, float] = {}
    peak = None
    for d, v in zip(dates, eq):
        peak = v if peak is None else max(peak, v)
        y = d[:4]
        out[y] = max(out.get(y, 0.0), 1 - v / peak if peak else 0.0)
    return out
