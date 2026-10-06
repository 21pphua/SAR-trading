"""Plain-text lab report."""

from __future__ import annotations

import math

from stockscan.lab.metrics import yearly_returns, yearly_drawdowns
from stockscan.lab.run import LabResult


def _p(x, d=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{d}f}%"


def _r(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.2f}R"


def render(res: LabResult) -> str:
    rl, cm = res.rules, res.costs
    L = []
    L.append("MODEL LAB — honest backtest (Step 1)")
    L.append(f"Generated {res.generated} | period {res.period} | {res.calendar[0] if res.calendar else '?'}"
             f" to {res.calendar[-1] if res.calendar else '?'} | {res.tickers_with_data}/{res.universe_size} tickers with data")
    L.append(f"Account: ${rl.start_equity:,.0f} start, {rl.risk_pct}% risk/trade, max {rl.max_positions} positions,"
             f" max heat {rl.max_heat_pct}%, max {rl.max_per_group}/group, max position {rl.max_position_pct}%")
    L.append(f"Costs: {cm.slippage_bps:g} bps slippage per side, ${cm.commission_per_share}/share commission")
    L.append(f"Regime switch (breadth + SPY 10/20): {res.regime_days}")
    L.append("")
    L.append("HOW TO READ THIS: each variant is the same account trading one rule set. CAGR = yearly growth,")
    L.append("MaxDD = worst drop, MAR = CAGR/MaxDD. 'w/o top10' = expectancy after removing the 10 best trades;")
    L.append("if it is negative the edge depends on a few outliers. 'All signals' = every signal taken with no")
    L.append("account limits (the old way of measuring).")
    L.append("")
    hdr = f"{'Variant':58} {'CAGR':>7} {'MaxDD':>7} {'MAR':>5} {'Sharpe':>6} {'Trades':>6} {'Win':>5} {'Exp':>7} {'w/o top10':>9} {'Expo':>5}"
    L.append(hdr)
    L.append("-" * len(hdr))
    for name, c in res.cards.items():
        L.append(f"{name[:58]:58} {_p(c.cagr):>7} {c.max_drawdown * 100:6.1f}% {c.mar:5.2f} {c.sharpe:6.2f}"
                 f" {c.trades:6d} {c.win_rate * 100:4.0f}% {_r(c.expectancy_r):>7} {_r(c.expectancy_wo_top10):>9}"
                 f" {c.exposure * 100:4.0f}%")
    if res.benchmark:
        b = res.benchmark
        L.append(f"{b['name']:58} {_p(b['cagr']):>7} {b['max_drawdown'] * 100:6.1f}% "
                 f"{(b['cagr'] / b['max_drawdown'] if b['max_drawdown'] else 0):5.2f} {b['sharpe']:6.2f}")
    L.append("")
    L.append("ALL SIGNALS, NO ACCOUNT LIMITS (per-trade view, net of slippage)")
    for name, r in res.raw.items():
        if r.get("signals"):
            L.append(f"  {name[:58]:58} {r['signals']:6d} signals  win {r['win_rate'] * 100:4.0f}%  "
                     f"exp {_r(r['expectancy_r'])}  w/o top10 {_r(r.get('expectancy_wo_top10'))}")
        else:
            L.append(f"  {name[:58]:58} no signals")
    L.append("")
    L.append("YEAR BY YEAR (account return; does it hold up in different markets?)")
    years = sorted({y for a in res.accounts.values() for y in yearly_returns(a.dates, a.equity)})
    L.append(f"  {'Variant':40} " + " ".join(f"{y:>7}" for y in years))
    for name, a in res.accounts.items():
        yr = yearly_returns(a.dates, a.equity)
        L.append(f"  {name[:40]:40} " + " ".join(f"{_p(yr.get(y), 0):>7}" for y in years))
    if res.benchmark:
        yr = res.benchmark["yearly"]
        L.append(f"  {res.benchmark['name'][:40]:40} " + " ".join(f"{_p(yr.get(y), 0):>7}" for y in years))
    L.append("")
    wf = res.wf
    if wf and wf.picks:
        L.append(f"WALK-FORWARD (pick the best variant by past {wf.criterion}, then grade it on the next year)")
        for y in sorted(wf.picks):
            L.append(f"  {y}: picked '{wf.picks[y][:50]}' -> earned {_p(wf.oos_returns[y])}")
        L.append(f"  Out-of-sample total {_p(wf.oos_total)} | CAGR {_p(wf.oos_cagr)}")
        L.append("")
    L.append("WHY TRADES WERE SKIPPED (account limits)")
    for name, a in res.accounts.items():
        if a.skipped:
            L.append(f"  {name[:50]:50} " + ", ".join(f"{k}: {v}" for k, v in a.skipped.most_common()))
    L.append("")
    L.append("CAVEATS")
    L.append("  * Survivorship bias: only stocks listed TODAY are tested; delisted losers are missing, so")
    L.append("    real results would likely be worse. Free data can't fix this.")
    L.append("  * 'Close entry' assumes you buy in the last minutes using almost-final price and volume.")
    L.append("  * Daily bars hide the order of the day's high and low; the simulator assumes the worse case.")
    L.append("  * Prices are split/dividend adjusted, so old price and $-volume filters are approximate.")
    L.append("Educational research tool, not financial advice.")
    return "\n".join(L)
