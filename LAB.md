# Model Lab

The lab is the test bench for the swing model. Every setup we add gets judged here before it goes live. It trades each rule set through one simulated account, the way a real account would have traded it.

## Why it exists

The old `sar-backtest` measured "average R per trade" across every signal. That number missed three things:

1. **Peeking.** The intraday test used the day's final low as the stop. You can't know that low when you buy, so losing trades went missing.
2. **Costs.** Fills were perfect. About 0.2% per side was enough to erase the edge.
3. **The account.** It assumed you could take every signal. Some days had 20+ at once, and a real account can hold only a few.

## How it works

1. **Signals** (`stockscan/lab/setups/`): each setup reads price history and says "trade idea here", using only data up to that day. A test checks every setup for peeking.
2. **Fills** (`core.py`): orders fill on later bars the way a broker would fill them:
   - Gaps fill at the open.
   - A buy-stop that is never reached means no trade.
   - When a daily bar can't show which came first, the worse outcome is assumed.
   - Every buy and sell pays slippage.
3. **Trade management** (`core.py`):
   - stop
   - partial profit at the target R
   - stop moved to breakeven
   - trailing moving-average exit
   - optional time stop
4. **Account** (`portfolio.py`):
   - starting cash
   - fixed % risk per trade
   - limits on positions, total open risk ("heat"), per-industry exposure and largest position
   - a market-regime switch: full size, half size or no new trades
   
   When too many signals fire on the same day, the strongest relative-strength names go first.
5. **Scorecard** (`metrics.py`):
   - CAGR, max drawdown, MAR, Sharpe
   - win rate, expectancy
   - **expectancy without the 10 best trades**, which tests whether the edge is broad or rests on a few lucky outliers
6. **Walk-forward** (`walkforward.py`): for each year, pick the best variant using only earlier years, then record what it earned in the year it never saw. Those stitched years are the honest estimate.

## Step 1 line-up (same SAR checklist, different ways in and out)

| Variant | What it tests |
|---|---|
| A | Buy the breakout close, exit on a close below the 10 SMA (the SAR docs) |
| B | Buy the next morning's open instead |
| C | Buy-stop above the base the day after a "coiling" signal, then sell at the close if the breakout fails |
| D | A, but trail the 20 SMA after the 5R partial (the rule live positions use) |
| E | A, but take 1/3 at 3R and add a 10-day time stop |
| refs | A with zero costs, A with 25 bps costs, and A without the regime switch |

## Run it

- **GitHub:** Actions → SAR nightly scan → Run workflow → task `lab`. Results land in `results/lab.txt`, `lab_trades.csv` and `lab.json`.
- **Locally:** `stockscan lab --period 10y --out results/lab.txt`.

Account defaults: $25,000, 0.5% risk per trade, 8 positions, 6% heat, 2 per group, 10 bps slippage. Run `stockscan lab -h` for all the options.

## Known limits

- **Survivorship bias.** Only stocks listed today are tested. Delisted losers are missing, so real results would likely be worse.
- **Close entries.** These assume you act in the final minutes of the day.
- **Adjusted prices.** Old price and dollar-volume filters are approximate.

Educational research tool, not financial advice.
