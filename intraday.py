#!/usr/bin/env python3
"""Intraday trigger check for last night's alert list (runs every 30 min in market hours).

For each stock on the alert list:
  opening-range high (ORH) = the high of the first 30 minutes
  trigger = max(base high, ORH)
  TRIGGERED = price above the trigger, after 10:00 ET
  stop = the low of the day so far (the doc's "low of the breakout day")
Volume pace = volume so far vs a normal full day, scaled by time elapsed.
Volume is heavier near the open, so early pace readings run high.

Writes results/intraday.json for the dashboard and pings phone/Discord once
per ticker per day when one triggers (same secrets as notify.py).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from notify import post  # noqa: E402

ET = ZoneInfo("America/New_York")
SHORTLIST = os.environ.get("SHORTLIST", "results/shortlist.json")
OUT = os.environ.get("INTRADAY_OUT", "results/intraday.json")
OR_MINUTES = 30
OPEN, CLOSE = time(9, 30), time(16, 0)


def _frame(df, tk, many=None):
    """One ticker's OHLCV frame out of a yf.download() result (any column layout)."""
    try:
        f = df
        cols = f.columns
        if getattr(cols, "nlevels", 1) > 1:
            if tk in cols.get_level_values(0):
                f = f[tk]
            elif tk in cols.get_level_values(1):
                f = f.xs(tk, axis=1, level=1)
            else:
                f = f.droplevel(list(range(1, cols.nlevels)), axis=1)
        return f.dropna(how="all")
    except Exception:
        return None


POSITIONS = os.environ.get("POSITIONS", "positions.txt")


def track_positions(now, prev_pos: dict) -> tuple[list[dict], list[str]]:
    """Live check of every trade in positions.txt, using today's price so far.

    Same exit rules as the nightly scan. EXIT here means "below the 10 SMA right now";
    the rule is a daily CLOSE below it, so decide near 4 PM.
    """
    from dataclasses import asdict
    from stockscan.sar.scan import read_positions, evaluate_position, fetch_ohlcv
    ps = read_positions(POSITIONS)
    if not ps:
        return [], []
    data = fetch_ohlcv([p.ticker for p in ps], period="3mo")
    out, alerts = [], []
    for p in ps:
        evaluate_position(p, data.get(p.ticker) or [])
        d = asdict(p)
        d["live"] = True
        if p.status == "EXIT":
            d["action"] = (f"Trading at {p.last:.2f}, below the 10 SMA ({(p.sma10 or 0):.2f}). The rule is a daily "
                           "CLOSE below it: if it's still below near 4 PM, sell the rest.")
        out.append(d)
        before = prev_pos.get(p.ticker, {}).get("status")
        if p.status in ("STOPPED", "5R HIT", "EXIT") and before != p.status:
            alerts.append(f"{p.ticker} {p.status}: {d['action']}")
    return out, alerts


def main() -> int:
    now = datetime.now(ET)
    if now.weekday() >= 5 or not (OPEN <= now.time() <= time(16, 15)):
        print(f"market closed ({now:%a %H:%M} ET); nothing to do")
        return 0
    doc = {}
    if os.path.exists(SHORTLIST):
        with open(SHORTLIST, encoding="utf-8") as fh:
            doc = json.load(fh)
    alerts = [r for r in doc.get("results", []) if r.get("kind") == "coiling"]
    prev, prev_pos = {}, {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("session") == now.date().isoformat():
                prev = {r["ticker"]: r for r in old.get("rows", [])}
                prev_pos = {r["ticker"]: r for r in old.get("positions", [])}
        except Exception:
            prev, prev_pos = {}, {}
    try:
        positions, pos_alerts = track_positions(now, prev_pos)
    except Exception as e:  # never let position tracking break the alert check
        print(f"position tracking failed: {e}")
        positions, pos_alerts = [], []
    rows, fresh = [], []
    if alerts:
        import yfinance as yf
        tks = [r["ticker"] for r in alerts]
        df = yf.download(tks, period="1d", interval="5m", group_by="ticker", progress=False,
                         prepost=False, auto_adjust=False, threads=True)
        many = len(tks) > 1
        mins = max(1, (datetime.combine(now.date(), min(now.time(), CLOSE)) - datetime.combine(now.date(), OPEN)).seconds // 60)
        for r in alerts:
            tk = r["ticker"]
            f = _frame(df, tk, many)
            row = {"ticker": tk, "base_high": r["base_high"], "status": "NO DATA"}
            if f is not None and len(f):
                idx = f.index.tz_convert(ET) if f.index.tz is not None else f.index.tz_localize(ET)
                f = f[[t.date() == now.date() for t in idx]]
                idx = [t for t in idx if t.date() == now.date()]
            if f is not None and len(f):
                first = [k for k, t in enumerate(idx) if (t.hour * 60 + t.minute) < 9 * 60 + 30 + OR_MINUTES]
                orh = float(f["High"].iloc[first].max()) if first else float(f["High"].iloc[0])
                price = float(f["Close"].iloc[-1])
                low = float(f["Low"].min())
                vol = float(f["Volume"].sum())
                vols = [b[5] for b in r.get("bars", [])[-20:]]
                avg = sum(vols) / len(vols) if vols else 0
                pace = vol / (avg * min(1.0, mins / 390)) if avg else None
                trig = max(r["base_high"], orh)
                if mins < OR_MINUTES:
                    status = "OPENING RANGE"
                elif price > trig:
                    status = "TRIGGERED"
                elif price > r["base_high"]:
                    status = "ABOVE BASE"
                else:
                    status = "WAITING"
                first_t = prev.get(tk, {}).get("first_triggered")
                if status == "TRIGGERED" and not first_t:
                    first_t = now.strftime("%H:%M")
                    fresh.append((tk, price, trig, low, pace))
                row.update(status=status, price=round(price, 4), orh=round(orh, 4), trigger=round(trig, 4),
                           low=round(low, 4), pace=round(pace, 2) if pace else None,
                           pct_to_trigger=round(trig / price - 1, 4), first_triggered=first_t)
            rows.append(row)
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"format": "sar-intraday/1", "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "session": now.date().isoformat(), "as_of_et": now.strftime("%H:%M"),
                   "scan_generated": doc.get("generated"), "rows": rows, "positions": positions}, fh, indent=1)
    print(f"{len(positions)} positions tracked; {len(pos_alerts)} need action")
    if pos_alerts:
        body = "\n".join(pos_alerts)
        title = "SAR positions: " + ", ".join(a.split(" ")[0] for a in pos_alerts)
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if topic:
            post(f"https://ntfy.sh/{topic}", body.encode("utf-8"), {"Title": title, "Priority": "high"})
        hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
        if hook:
            post(hook, json.dumps({"content": f"**{title}**\n```\n{body[:1800]}\n```"}).encode("utf-8"),
                 {"Content-Type": "application/json", "User-Agent": "sar-scan"})
    print(f"{len(rows)} alerts checked; {sum(r['status'] == 'TRIGGERED' for r in rows)} triggered; {len(fresh)} new")
    if fresh:
        body = "\n".join(f"{tk} above {trig:.2f} (now {p:.2f}) · stop = day low {lo:.2f}"
                         + (f" · volume pace {pc:.1f}x" if pc else "") for tk, p, trig, lo, pc in fresh)
        body += "\nCheck the chart before buying. Pace under ~1.3x = weaker break."
        title = f"SAR intraday: {', '.join(t[0] for t in fresh)} triggered"
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if topic:
            post(f"https://ntfy.sh/{topic}", body.encode("utf-8"), {"Title": title})
        hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
        if hook:
            post(hook, json.dumps({"content": f"**{title}**\n```\n{body[:1800]}\n```"}).encode("utf-8"),
                 {"Content-Type": "application/json", "User-Agent": "sar-scan"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
