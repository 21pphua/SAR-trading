"""The model lab: an honest, setup-agnostic backtester.

Plain-English map of the pieces:

  core.py        What a trade signal is, how an order gets filled on daily
                 bars WITHOUT peeking at the future, what it costs, and how a
                 single trade plays out (stop, partial profit, trailing exit).
  portfolio.py   Replays every trade signal day by day through ONE account:
                 real starting cash, fixed % risk per trade, a limit on open
                 positions, total open risk ("heat"), per-group caps, and a
                 market-regime switch. This is what turns "average R per trade"
                 into the number that matters: how the account would have grown.
  metrics.py     Scorecard: CAGR, max drawdown, Sharpe, win rate, expectancy,
                 and "expectancy without the 10 best trades" (is the edge broad
                 or riding a few lottery tickets?).
  walkforward.py Year-by-year check plus walk-forward selection: pick settings
                 using only the past, then grade them on the next year that
                 they never saw.
  setups/        One file per setup. Each turns price history into Signals.
  run.py         Glue: download data in chunks, build signals, run the account,
                 write the report.

Educational research tool, not financial advice.
"""
