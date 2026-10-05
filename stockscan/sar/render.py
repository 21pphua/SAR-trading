"""Plain-text rendering for SAR scan results."""

from __future__ import annotations

from stockscan.config import SAR_EARNINGS_WARN_DAYS
from stockscan.sar.engine import SetupScore
from stockscan.sar.scan import SarScanResult


def _flags(s: SetupScore) -> str:
    f = []
    if s.days_to_earnings is not None and 0 <= s.days_to_earnings <= SAR_EARNINGS_WARN_DAYS:
        f.append(f"EARNINGS {s.days_to_earnings}d")
    if s.wide_stop:
        f.append(f"WIDE STOP {s.risk_adr:.1f}xADR")
    if s.entry < 5:
        f.append("LOW-PRICED")
    return "  " + " · ".join(f) if f else ""


def _regime_line(r: SarScanResult) -> str:
    parts = []
    for k, v in r.regime.items():
        parts.append(f"{k} {'10>20 ✓' if v else '10<20 ✗' if v is not None else 'n/a'}")
    ok = r.regime_ok
    tail = ("FAVORABLE" if ok else "UNFAVORABLE — expect a lower win rate" if ok is not None else "unknown")
    return f"MARKET:  {' · '.join(parts)}  ->  {tail}"


def _target(s: SetupScore, label: str) -> str:
    for t in s.targets:
        if t.label == label:
            return f"{t.price:.2f}"
    return "—"


def _positions_lines(r: SarScanResult) -> list[str]:
    if not r.positions:
        return []
    order = {"STOPPED": 0, "EXIT": 1, "5R HIT": 2, "NEAR EXIT": 3, "HOLD": 4, "NO DATA": 5}
    out = [f"YOUR POSITIONS ({len(r.positions)})", "-" * 78]
    for p in sorted(r.positions, key=lambda p: order.get(p.status, 9)):
        out.append(f"  {p.ticker:<7}{p.status:<10}{p.shares:>6} sh  last {p.last:>8.2f}  {p.r_now:+5.1f}R  "
                   f"{'+' if p.pnl >= 0 else '-'}${abs(p.pnl):,.0f}")
        out.append(f"           {p.action}")
    return out + [""]


def render_sar_scan(r: SarScanResult) -> str:
    lines = [
        "",
        "SAR BREAKOUT SCAN",
        "=" * 78,
        f"Scanned {r.scanned} · with data {r.with_data} · passed filters {r.passed_filters}  ({r.generated})",
        _regime_line(r),
        "",
        *_positions_lines(r),
        f"BREAKOUTS ({len(r.breakouts)})",
        "-" * 78,
    ]
    hdr = f"  {'TICKER':<7}{'SCORE':>6}  {'VERDICT':<6}{'ENTRY':>9}{'STOP':>9}{'R%':>6}{'5R':>9}{'MM':>9}{'VOLx':>6}{'RS':>4}  FLAGS"
    if r.breakouts:
        lines.append(hdr)
        for s in r.breakouts:
            lines.append(
                f"  {s.ticker:<7}{s.score:>6}  {s.verdict:<6}{s.entry:>9.2f}{s.stop:>9.2f}"
                f"{s.risk / s.entry:>6.1%}{_target(s, '5R partial'):>9}{_target(s, 'Measured move'):>9}{s.volx:>6.2f}{(s.rs_rank or 0):>4}{_flags(s)}"
            )
    else:
        lines.append("  none today")
    lines += ["", f"COILING — setup formed, not yet broken out ({len(r.coiling)})", "-" * 78]
    if r.coiling:
        lines.append(f"  {'TICKER':<7}{'PREP':>6}  {'ALERT >':>9}{'GAP':>7}{'RUN-UP':>8}{'ADR':>6}{'10 SMA':>9}  FLAGS")
        for s in r.coiling:
            tag = "  BROKE ON LOW VOLUME ·" if s.is_breakout else ""
            lines.append(
                f"  {s.ticker:<7}{s.prep_points:>4}/50  {s.base_high:>9.2f}{s.gap_to_base:>7.1%}"
                f"{s.runup_pct:>8.0%}{s.adr_pct:>6.1%}{(s.sma10 or 0):>9.2f}{tag}{_flags(s)}"
            )
    else:
        lines.append("  none")
    if r.wide_stop:
        lines += ["", f"WIDE STOP — watch only ({len(r.wide_stop)}): broke out, but the stop is farther than a normal day", "-" * 78]
        for s in r.wide_stop[:12]:
            lines.append(f"  {s.ticker:<7}{s.score:>6}  entry {s.entry:.2f}  stop {s.stop:.2f}  ({s.risk_adr:.1f}x ADR)")
    if r.too_tight:
        lines += ["", f"TOO TIGHT — watch only ({len(r.too_tight)}): broke out, but the stop is unrealistically close to entry",
                  "(not a real edge -- see SAR_MIN_RISK_ADR; verify the chart before treating this as a signal)", "-" * 78]
        for s in r.too_tight[:12]:
            lines.append(f"  {s.ticker:<7}{s.score:>6}  entry {s.entry:.2f}  stop {s.stop:.2f}  ({s.risk_adr:.2f}x ADR)")
    if r.counter_trend:
        lines += ["", f"SKIPPED — COUNTER-TREND ({len(r.counter_trend)}): scored well, but not in a long-term uptrend", "-" * 78]
        for s in r.counter_trend[:12]:
            lines.append(f"  {s.ticker:<7}{s.score:>6}  {s.trend_note}")
    lines += ["", "FLAGS: EARNINGS = report within the next few days (gap risk) · WIDE STOP = stop farther than",
              "one normal day's range (size down or skip) · LOW-PRICED = under $5.",
              "Rules-based rating only — not a trade signal. Verify each chart before acting.", ""]
    return "\n".join(lines)


def render_setup(s: SetupScore) -> str:
    lines = [f"", f"{s.ticker} — SAR SETUP  {s.date}", "=" * 60,
             f"SCORE:  {s.score}/100  {s.verdict.upper()}",
             f"ENTRY:  {s.entry:.2f}   STOP {s.stop:.2f}   1R {s.risk:.2f} ({s.risk / s.entry:.1%})", ""]
    for st in s.steps:
        lines.append(f"  {st.title:<24}{st.points:>3}/{st.max_points:<3} {st.status:<8} {st.metric}")
    lines += ["", "  TARGETS"]
    for t in s.targets:
        lines.append(f"    {t.label:<15}{t.price:>9.2f}  {t.r_multiple:+.1f}R  {t.note}")
    lines.append("")
    return "\n".join(lines)
