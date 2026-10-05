"""Command-line interface for stockscan.

Commands
--------
  demo     Run the worked example (V) offline — proves the engine, no deps.
  screen   Stage 1 only: rank a universe by the quantitative composite.
  assess   Stage 2 on one ticker: LLM-draft -> you confirm -> pressure-test.
  scan     Full funnel: screen -> draft survivors -> confirm -> ranked report.
  sar      SAR Trading breakout scan: filters -> checklist score -> targets.
  sar-live Intraday: poll live quotes for today's shortlist, flag real breakouts as they happen.
  sar-backtest  Replay the SAR rules over history; win rate, R stats, drawdown.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import Optional, Sequence

from stockscan import __version__
from stockscan.config import DEFAULT_TOP_N, DEFAULT_UNIVERSE, M8_MAX, SAR_TAKE_AT, SAR_RISK_PCT_PER_TRADE
from stockscan.assess.pipeline import AssessmentInput, AssessmentResult, assess
from stockscan.universe import resolve_universe, list_builtin_universes
from stockscan import report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    return resolve_universe(args.universe or DEFAULT_UNIVERSE)


def _progress(i: int, n: int, ticker: str) -> None:
    sys.stderr.write(f"\r  fetching {i}/{n}: {ticker:<8}")
    sys.stderr.flush()
    if i == n:
        sys.stderr.write("\n")


def _confirm_draft(inp: AssessmentInput, auto_yes: bool) -> Optional[AssessmentInput]:
    """Show the LLM draft and let the analyst confirm, edit, or skip.

    Returns the (possibly edited) input, or None to skip the name.
    This is the human gate that keeps the evidence-anchor rule honest.
    """
    print(_format_draft(inp))
    if auto_yes:
        print("  [auto-confirmed]\n")
        return inp

    while True:
        choice = input("  [a]ccept / [e]dit / [s]kip > ").strip().lower()
        if choice in ("a", "accept", ""):
            return inp
        if choice in ("s", "skip"):
            return None
        if choice in ("e", "edit"):
            inp = _edit_input(inp)
            print(_format_draft(inp))
            continue
        print("  (a, e, or s)")


def _format_draft(inp: AssessmentInput) -> str:
    ev_lines = []
    for ax in ("theme", "moat", "proof", "entry"):
        fact = inp.evidence.get(ax, "")
        mark = "✓" if fact else "·"
        ev_lines.append(f"      {mark} {ax:<8} {inp.subs[ax]}/{M8_MAX[ax]}  {fact}")
    return "\n".join(
        [
            "",
            f"  DRAFT — {inp.ticker}",
            "  " + "-" * 50,
            "    CORE subscores (evidence required if decisive):",
            *ev_lines,
            f"    other: ceiling {inp.subs['ceiling']} cycle {inp.subs['cycle']} "
            f"falsify {inp.subs['falsify']} confirm {inp.subs['confirm']} "
            f"| bear haircut {inp.bear_haircut}",
            f"    EV inputs: price {inp.price} | bear {inp.bear} base {inp.base} "
            f"bull {inp.bull} | p {inp.p_bear}/{inp.p_base}/{inp.p_bull}",
            f"    spec base_rate: {inp.base_rate_pct}",
            "",
        ]
    )


def _edit_input(inp: AssessmentInput) -> AssessmentInput:
    """Minimal key=value editor for the draft fields."""
    print(
        "    Enter edits as 'field=value', one per line. Blank line to finish.\n"
        "    Subscores: theme/moat/proof/entry/ceiling/cycle/falsify/confirm\n"
        "    Evidence:  ev_theme/ev_moat/ev_proof/ev_entry\n"
        "    EV:        price/bear/base/bull/p_bear/p_base/p_bull/bear_haircut\n"
        "    Spec:      base_rate_pct (blank value clears it)"
    )
    subs = dict(inp.subs)
    evidence = dict(inp.evidence)
    changes: dict = {}
    while True:
        raw = input("    edit> ").strip()
        if not raw:
            break
        if "=" not in raw:
            print("      use field=value")
            continue
        field, _, value = raw.partition("=")
        field, value = field.strip(), value.strip()
        try:
            if field in subs:
                subs[field] = int(value)
            elif field.startswith("ev_"):
                ax = field[3:]
                if value:
                    evidence[ax] = value
                else:
                    evidence.pop(ax, None)
            elif field in ("price", "bear", "base", "bull", "p_bear", "p_base", "p_bull"):
                changes[field] = float(value)
            elif field == "bear_haircut":
                changes[field] = int(value)
            elif field == "base_rate_pct":
                changes[field] = float(value) if value else None
            else:
                print(f"      unknown field: {field}")
        except ValueError:
            print(f"      bad value for {field}: {value!r}")
    return dataclasses.replace(inp, subs=subs, evidence=evidence, **changes)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_demo(args) -> int:
    """The spec's worked example (V), entirely offline."""
    inp = AssessmentInput(
        ticker="V",
        subs={"theme": 13, "moat": 18, "proof": 13, "entry": 11,
              "ceiling": 7, "cycle": 8, "falsify": 4, "confirm": 8},
        evidence={
            "theme": "Secular shift cash->card; ~$240T global payments flows.",
            "moat": "Dual-network duopoly; ~50% incremental margins, returns on capital >30%.",
            "proof": "Double-digit payments-volume growth sustained across cycles.",
            "entry": "Trading at a discount to its own 5y multiple after the pullback.",
        },
        bear_haircut=0,
        price=324, bear=285, base=345, bull=400,
        p_bear=0.25, p_base=0.50, p_bull=0.25, horizon_mo=18,
        add_winner_ev=0.0, cash_yield_pct=4.5, weakest_replace_ev=3.0,
        diversifies=True,
        base_rate_pct=None,
    )
    res = assess(inp)
    print(report.render_assessment(res))

    # Demonstrate the evidence cap: strip the moat citation.
    stripped = dataclasses.replace(
        inp, evidence={k: v for k, v in inp.evidence.items() if k != "moat"}
    )
    res2 = assess(stripped)
    print("  (strip the moat citation -> the decisive subscore auto-caps:)")
    print(report.render_assessment(res2))
    return 0


def cmd_universes(args) -> int:
    names = list_builtin_universes()
    if not names:
        print("No built-in universes packaged.")
        return 0
    print("Built-in universes:")
    for name in names:
        try:
            count = len(resolve_universe(name))
        except Exception:
            count = "?"
        marker = "  (default)" if name == DEFAULT_UNIVERSE else ""
        print(f"  {name:<10} {count} tickers{marker}")
    print("\nUse: stockscan scan --universe <name|file>")
    return 0


def cmd_screen(args) -> int:
    from stockscan.data.providers import get_provider
    from stockscan.screen.stage1 import screen_universe

    tickers = _load_universe(args)
    provider = get_provider(args.provider)
    print(f"Screening {len(tickers)} names via {args.provider} ...", file=sys.stderr)
    rows = screen_universe(tickers, provider, top_n=args.top, on_progress=_progress)
    print(report.render_screen(rows))
    return 0


def _research_and_assess(snapshot, args) -> Optional[AssessmentResult]:
    from stockscan.research.llm import draft_assessment

    draft = draft_assessment(snapshot, extra_context=args.context or "")
    confirmed = _confirm_draft(draft, auto_yes=args.yes)
    if confirmed is None:
        return None
    return assess(confirmed)


def cmd_assess(args) -> int:
    from stockscan.data.providers import get_provider

    provider = get_provider(args.provider)
    ticker = args.ticker.upper()
    print(f"Fetching {ticker} ...", file=sys.stderr)
    snapshot = provider.snapshot(ticker)
    res = _research_and_assess(snapshot, args)
    if res is None:
        print("Skipped.")
        return 0
    print(report.render_assessment(res))
    return 0


def cmd_scan(args) -> int:
    from stockscan.data.providers import get_provider
    from stockscan.screen.stage1 import screen_universe

    tickers = _load_universe(args)
    provider = get_provider(args.provider)

    print(f"STAGE 1: screening {len(tickers)} names ...", file=sys.stderr)
    rows = screen_universe(tickers, provider, top_n=args.top, on_progress=_progress)
    print(report.render_screen(rows))

    print(f"STAGE 2: researching + assessing top {len(rows)} ...", file=sys.stderr)
    results: list[AssessmentResult] = []
    for row in rows:
        res = _research_and_assess(row.snapshot, args)
        if res is not None:
            print(report.render_assessment(res))
            results.append(res)

    if results:
        print(report.render_ranked_assessments(results))
    else:
        print("No names assessed.")
    return 0


def cmd_sar(args) -> int:
    from stockscan.sar.scan import run_sar_scan, write_shortlist, read_positions
    from stockscan.sar.render import render_sar_scan

    if args.universe is None and not args.tickers:
        args.universe = "us_all" if "us_all" in list_builtin_universes() else DEFAULT_UNIVERSE
    tickers = _load_universe(args)
    print(f"SAR scan: {len(tickers)} names (daily bars via yfinance) ...", file=sys.stderr)
    res = run_sar_scan(tickers, min_score=args.min_score, top=args.top,
                       apply_filters=not args.no_filters, on_progress=_progress,
                       trend_filter=not args.no_trend_filter,
                       positions=read_positions(args.positions),
                       require_tight_stop=not args.allow_wide_stops,
                       require_rs_leader=args.require_rs_leader,
                       require_hot_group=args.require_hot_group,
                       require_regime=args.require_regime)
    print(render_sar_scan(res))
    if args.detail:
        from stockscan.sar.render import render_setup
        for s in res.breakouts:
            print(render_setup(s))
    if args.equity and res.breakouts:
        from stockscan.sar.sizing import recommend_size, render_sizing
        from stockscan.sar.strength import load_sectors, group_keys

        open_tickers = [p.ticker for p in res.positions]
        sectors = load_sectors()
        groups = group_keys(open_tickers + [s.ticker for s in res.breakouts], sectors)
        open_sectors = [groups.get(t, "") for t in open_tickers]
        recs = []
        for s in res.breakouts:
            recs.append(recommend_size(s, args.equity, open_sectors, risk_pct=args.risk_pct))
            open_sectors.append(s.group or sectors.get(s.ticker, ("", ""))[0])  # each new one counts for the next
        print(render_sizing(recs))
    if args.out:
        write_shortlist(res, args.out)
        print(f"Shortlist written to {args.out} — load it in the SAR Setup Walkthrough.", file=sys.stderr)
    return 0


def cmd_sar_live(args) -> int:
    import json
    import time
    from datetime import datetime
    from stockscan.sar.live import (load_watchlist, fetch_live_quotes, build_live_checks,
                                    render_live_checks, market_is_open)
    from stockscan.sar.scan import MIN_BREAKOUT_VOLX

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    try:
        entries = load_watchlist(args.shortlist, kinds=kinds)
    except FileNotFoundError:
        print(f"No shortlist at {args.shortlist} -- run `stockscan sar --out {args.shortlist}` "
              "before/at the open first, then rerun this to track it live.", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print(f"{args.shortlist} isn't valid JSON (partial write?) -- rerun `sar --out {args.shortlist}`.",
              file=sys.stderr)
        return 1

    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        entries = [e for e in entries if e.ticker in want]
    if not entries:
        print(f"Nothing to watch in {args.shortlist} for kinds={','.join(kinds)}.", file=sys.stderr)
        return 0

    min_relvol = args.min_relvol if args.min_relvol is not None else MIN_BREAKOUT_VOLX
    print(f"Watching {len(entries)} name(s) from {args.shortlist} ({','.join(kinds)}) ...", file=sys.stderr)
    while True:
        quotes = fetch_live_quotes([e.ticker for e in entries])
        checks = build_live_checks(entries, quotes, min_relvol=min_relvol)
        print(render_live_checks(checks, generated=datetime.now().strftime("%H:%M:%S")))
        if not args.loop:
            break
        if not market_is_open():
            print("Market's closed -- stopping.", file=sys.stderr)
            break
        try:
            time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)
            break
    return 0


def cmd_sar_backtest(args) -> int:
    from stockscan.sar.scan import fetch_ohlcv
    from stockscan.sar.backtest import run_backtest, render_backtest, render_backtest_split, write_trades_csv

    if args.universe is None and not args.tickers:
        args.universe = "us_all" if "us_all" in list_builtin_universes() else DEFAULT_UNIVERSE
    tickers = _load_universe(args)
    print(f"SAR backtest: {len(tickers)} names over {args.period} ...", file=sys.stderr)
    res = run_backtest(tickers, fetch_ohlcv, period=args.period, min_score=args.min_score,
                       partial=args.partial, max_risk_adr=args.max_risk_adr, on_progress=_progress,
                       trend_filter=not args.all_trends, intraday=not args.no_intraday,
                       slippage=args.slippage)
    text = render_backtest_split(res, args.split_date) if args.split_date else render_backtest(res)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    if args.trades:
        write_trades_csv(res, args.trades)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stockscan",
        description="Elite stock scan/research model — quantitative funnel + A+ assessment engine.",
    )
    p.add_argument("--version", action="version", version=f"stockscan {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_universe_args(sp):
        sp.add_argument("--tickers", help="Comma-separated tickers (overrides --universe).")
        sp.add_argument(
            "--universe",
            help=f"Built-in name ({', '.join(list_builtin_universes()) or 'none'}) "
            f"or a path to a file of tickers (default: {DEFAULT_UNIVERSE}).",
        )
        sp.add_argument("--provider", default="yfinance", help="Data provider (default: yfinance).")

    def add_research_args(sp):
        sp.add_argument("-y", "--yes", action="store_true", help="Auto-confirm LLM drafts.")
        sp.add_argument("--context", help="Extra analyst context passed to the LLM.")

    sp = sub.add_parser("demo", help="Run the worked example offline (no deps).")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("universes", help="List built-in universes.")
    sp.set_defaults(func=cmd_universes)

    sp = sub.add_parser("screen", help="Stage 1 only: rank a universe.")
    add_universe_args(sp)
    sp.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Survivors to keep.")
    sp.set_defaults(func=cmd_screen)

    sp = sub.add_parser("assess", help="Stage 2 on one ticker (LLM draft -> confirm).")
    sp.add_argument("ticker", help="Ticker to assess, e.g. AAPL.")
    sp.add_argument("--provider", default="yfinance", help="Data provider (default: yfinance).")
    add_research_args(sp)
    sp.set_defaults(func=cmd_assess)

    sp = sub.add_parser("scan", help="Full funnel: screen -> draft -> confirm -> ranked report.")
    add_universe_args(sp)
    sp.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Survivors to assess.")
    add_research_args(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("sar", help="SAR Trading breakout scan (checklist score + targets).")
    add_universe_args(sp)
    sp.add_argument("--min-score", type=int, default=SAR_TAKE_AT,
                    help=f"Minimum 0-100 score for a breakout to list (default {SAR_TAKE_AT}).")
    sp.add_argument("--top", type=int, default=25, help="Max names per list (default 25).")
    sp.add_argument("--out", help="Write a shortlist JSON (with candles) for the web walkthrough.")
    sp.add_argument("--detail", action="store_true", help="Print the full checklist for each breakout.")
    sp.add_argument("--no-filters", action="store_true", help="Skip the price / ADR / $ volume filters.")
    sp.add_argument("--positions", default="positions.txt",
                    help="Your open trades, one per line: ticker,shares,entry,stop,date (default positions.txt).")
    sp.add_argument("--allow-wide-stops", action="store_true",
                    help="List wide-stop breakouts with the rest instead of a separate watch list.")
    sp.add_argument("--no-trend-filter", action="store_true",
                    help="Keep counter-trend setups (bounces in a downtrend) in the lists.")
    sp.add_argument("--require-rs-leader", action="store_true",
                    help="Only list breakouts with relative-strength rank >= SAR_RS_LEADER "
                         "(unvalidated -- check the '+ RS 80+' row in sar-backtest's report first).")
    sp.add_argument("--require-hot-group", action="store_true",
                    help="Only list breakouts in a top industry group "
                         "(unvalidated -- check the '+ hot group' row in sar-backtest's report first).")
    sp.add_argument("--require-regime", action="store_true",
                    help="Don't list NEW breakouts when the SPY/QQQ market regime is unfavorable "
                         "(unvalidated -- check the '+ favorable regime only' row in sar-backtest's report first).")
    sp.add_argument("--equity", type=float,
                    help="Account equity (e.g. 50000): if set, print a suggested share count per "
                         "breakout sized at --risk-pct of equity, with a sector-concentration warning.")
    sp.add_argument("--risk-pct", type=float, default=SAR_RISK_PCT_PER_TRADE,
                    help=f"%% of equity to risk per trade when sizing (default {SAR_RISK_PCT_PER_TRADE}).")
    sp.set_defaults(func=cmd_sar)

    sp = sub.add_parser("sar-live", help="Intraday rescan: poll live quotes for today's shortlisted "
                                         "names and flag real breakouts (crossed + real volume) as they happen.")
    sp.add_argument("--shortlist", default="shortlist.json",
                    help="Shortlist JSON from `sar --out` -- run that before/at the open first (default shortlist.json).")
    sp.add_argument("--tickers", help="Only watch these tickers (comma-separated) out of the shortlist.")
    sp.add_argument("--kinds", default="coiling",
                    help="Which shortlist kinds to watch: coiling,breakout,wide,too_tight (default: coiling).")
    sp.add_argument("--min-relvol", type=float, default=None,
                    help="Pace-adjusted volume multiple needed to call a cross CONFIRMED "
                         "(default: same bar as the daily scan's breakout-volume rule).")
    sp.add_argument("--loop", type=int, metavar="SECONDS",
                    help="Repoll every SECONDS until the market closes or you Ctrl+C (default: run once and exit).")
    sp.set_defaults(func=cmd_sar_live)

    sp = sub.add_parser("sar-backtest", help="Replay the SAR rules over history.")
    add_universe_args(sp)
    sp.add_argument("--period", default="3y", help="History length for yfinance (default 3y).")
    sp.add_argument("--min-score", type=int, default=SAR_TAKE_AT, help="Score needed to enter.")
    sp.add_argument("--partial", type=float, default=0.20, help="Fraction sold at 5R (default 0.20).")
    sp.add_argument("--max-risk-adr", type=float, default=None,
                    help="Skip trades whose stop is wider than this many ADRs (default: take all).")
    sp.add_argument("--out", help="Write the text report here.")
    sp.add_argument("--trades", help="Write every simulated trade to this CSV.")
    sp.add_argument("--all-trends", action="store_true",
                    help="Also take counter-trend trades (default: uptrend only, like the live scan).")
    sp.add_argument("--no-intraday", action="store_true", help="Skip the intraday-entry simulation.")
    sp.add_argument("--slippage", type=float, default=0.002,
                    help="Slippage on intraday buy-stop entries (default 0.002 = 0.2%%).")
    sp.add_argument("--split-date", metavar="YYYY-MM-DD",
                    help="Walk-forward check: render TRAIN (before) and TEST (on/after) periods "
                         "separately with the same rules, so a filter curve-fit to one period "
                         "shows up as soon as it stops working on the other.")
    sp.set_defaults(func=cmd_sar_backtest)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except ImportError as exc:
        print(f"\nMissing dependency: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
