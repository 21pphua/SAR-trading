# SAR breakout scan

`stockscan sar` scans a universe for **SAR Trading** breakout swing setups (the
educator's system — not the Parabolic SAR indicator), scores each on a 0–100
checklist, and projects price targets. Same scoring as the web
*SAR Setup Walkthrough*, so a name rates identically in both.

> Rules-based rating only. Not a trade signal, not financial advice. The
> source's win-rate / drawdown statistics are self-reported by its author.

## Install

Drop these files into the repo (paths mirror the repo layout):

```
stockscan/sar/__init__.py
stockscan/sar/engine.py     pure scoring engine (no deps)
stockscan/sar/scan.py       yfinance batch fetch, filters, ranking, JSON export
stockscan/sar/render.py     terminal tables
stockscan/cli.py            + `sar` command
stockscan/config.py         + SAR_* thresholds and weights
scripts/build_us_universe.py
tests/test_sar.py
```

```bash
pip install -e '.[data]'
pytest tests/test_sar.py
```

## Daily workflow

```bash
python scripts/build_us_universe.py      # once a week: ~6-7k US common stocks -> us_all
stockscan sar --out shortlist.json       # after the close (defaults to us_all if built)
```

1. **Filters** drop anything failing price > $1, ADR% > 5, 20-day avg $ volume > $3.5M.
   Typically leaves a few hundred names.
2. **Score** the latest daily bar of each survivor.
3. Two lists:
   - **BREAKOUTS** — closed above the pullback base today and scored ≥ `--min-score` (65).
   - **COILING** — steps 01–04 strong (≥ 35/50), close within 3% of the base high. Set an
     alert at the base high; rescore when it breaks.
4. **Market regime** — SPY and QQQ 10 SMA vs 20 SMA. Unfavorable = expect a lower win rate.
5. Open `shortlist.json` in the walkthrough (**Load scan file**) to review each chart,
   step scores and targets before acting.

Run after 4pm ET — mid-session the last bar is incomplete and volume/close-near-high
reads are wrong.

## Trading it intraday: premarket scan + live rescan

The checklist itself only ever reads completed daily bars (that's what "run-up", "tightening
pullback" and "volume dry-up" mean), so it can't be re-scored live. But the COILING list —
setups that are fully formed and just haven't broken the base yet — doesn't need to wait for
the close, and the breakout trigger (`base_high`) it already computed is exactly the level a
live quote needs to be checked against:

```bash
stockscan sar --out shortlist.json     # ~30 min before the open: tonight's COILING list
# ... market opens, price moves ...
stockscan sar-live                     # polls live quotes for the COILING tickers;
                                        # flags which ones have crossed base_high on real
                                        # (pace-adjusted) volume, vs. a thin, unconfirmed poke
stockscan sar-live --loop 60           # keep repolling every 60s until the close or Ctrl+C
```

`sar-live` never re-screens the universe intraday — it only tracks names the premarket scan
already vetted (steps 01-04), so it's cheap (a handful of tickers, not hundreds) and it's only
checking the one thing that genuinely happens live: has the price crossed the base high, and
is the volume behind it real. A CONFIRMED line still isn't a trade signal — check the chart
yourself before acting, same as everywhere else in this tool. `--kinds coiling,breakout` also
lets you keep an eye on whether an already-confirmed breakout is holding up on continued volume.

## Scoring (0–100)

| Step | Weight | Full marks at |
|---|---|---|
| 01 Run-up before base | 15 | ≥ 30% rise in the 40 bars before the base |
| 02 10/20 SMA inclining | 15 | 10 rising, above 20, 20 rising |
| 03 Tightening pullback | 10 | 2nd-half base range ≤ 60% of 1st half |
| 04 Volume dry-up | 10 | base volume ≤ 70% of run-up volume |
| 05a Range break | 20 | close above the 20-bar base high |
| 05b Breakout volume | 20 | ≥ 1.8× 20-day avg volume |
| 05c Close near high | 10 | close in top 5% of the day's range |

Take ≥ 65 · Watch 40–64 · Skip < 40. All thresholds live in `config.py` (`SAR_*`).

## Targets

5R partial (sell 10–30%, stop to breakeven) · measured move (run-up height + base high) ·
up to two prior swing highs above entry · +1 / +3 ADR · trailing exit at the 10 SMA ·
stop at the breakout-day low.

## Useful flags

```bash
stockscan sar --tickers NET,CRWD,HOOD --detail     # full checklist per breakout
stockscan sar --universe my_watchlist.txt --min-score 55
stockscan sar --no-filters --tickers SPY           # bypass liquidity filters
```
