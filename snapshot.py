#!/usr/bin/env python3
"""Write results/now.md — ONE up-to-date summary for Claude (and you) to read.

Runs at the end of every intraday check and every nightly scan. It grabs FRESH
prices right now for everything that matters (open positions, breakouts,
alerts), so nothing in the summary is older than the run itself:

  - during market hours: last 5-minute price, today's high/low, volume pace
  - outside market hours: the latest daily close

and puts them next to the scan's levels (entry, stop, trigger, 5R, 10/20 SMA)
with a plain verdict for each line. Every number is stamped with its time in PT.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
OUT = os.environ.get("NOW_OUT", "results/now.md")
SHORTLIST = "results/shortlist.json"
INTRADAY = "results/intraday.json"
POSITIONS = "positions.txt"


def _pt(dt: datetime) -> str:
    return dt.astimezone(PT).strftime("%a %b %-d, %-I:%M %p PT")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _positions() -> list[dict]:
    out = []
    if not os.path.exists(POSITIONS):
        return out
    for line in open(POSITIONS, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        p = [x.strip() for x in line.replace("\t", ",").split(",") if x.strip()]
        try:
            out.append({"ticker": p[0].upper(), "shares": int(float(p[1])), "entry": float(p[2]),
                        "stop": float(p[3]), "date": p[4] if len(p) > 4 else ""})
        except Exception:
            continue
    return out


def _quotes(tickers: list[str], market_open: bool) -> dict:
    """{ticker: {price, high, low, vol, prev_close, asof}} fetched now."""
    if not tickers:
        return {}
    import yfinance as yf
    out = {}
    try:
        if market_open:
            df = yf.download(tickers, period="1d", interval="5m", group_by="ticker", progress=False,
                             prepost=False, auto_adjust=False, threads=True)
        else:
            df = yf.download(tickers, period="5d", interval="1d", group_by="ticker", progress=False,
                             auto_adjust=False, threads=True)
    except Exception as e:
        print(f"snapshot: quote download failed ({e})")
        return {}
    many = len(tickers) > 1
    for tk in tickers:
        try:
            f = (df[tk] if many else df).dropna(how="all")
            if not len(f):
                continue
            ts = f.index[-1].to_pydatetime()
            ts = ts if ts.tzinfo else ts.replace(tzinfo=ET)
            if market_open:
                out[tk] = {"price": float(f["Close"].iloc[-1]), "high": float(f["High"].max()),
                           "low": float(f["Low"].min()), "vol": float(f["Volume"].sum()), "asof": ts}
            else:
                out[tk] = {"price": float(f["Close"].iloc[-1]), "high": float(f["High"].iloc[-1]),
                           "low": float(f["Low"].iloc[-1]), "vol": float(f["Volume"].iloc[-1]),
                           "prev_close": float(f["Close"].iloc[-2]) if len(f) > 1 else None, "asof": ts}
        except Exception:
            continue
    return out


def _f(v, d=2):
    return "—" if v is None else f"{v:,.{d}f}"


def _pct(a, b):
    return "—" if not a or not b else f"{(a / b - 1) * 100:+.1f}%"


def write_snapshot() -> None:
    now = datetime.now(ET)
    mkt = now.weekday() < 5 and time(9, 30) <= now.time() <= time(16, 0)
    sl = _load(SHORTLIST) or {}
    intra = _load(INTRADAY) or {}
    if intra.get("session") != now.date().isoformat():
        intra = {}
    res = sl.get("results", [])
    bars = {r["ticker"]: r.get("bars") or [] for r in res}
    bo = [r for r in res if r.get("kind") == "breakout"]
    al = [r for r in res if r.get("kind") == "coiling"]
    pos = _positions()
    spos = {p["ticker"]: p for p in sl.get("positions", []) or []}
    ipos = {p["ticker"]: p for p in intra.get("positions", []) or []}
    irow = {r["ticker"]: r for r in intra.get("rows", []) or []}
    tks = list(dict.fromkeys([p["ticker"] for p in pos] + [r["ticker"] for r in bo + al]))
    q = _quotes(tks, mkt)

    def pace(tk, vol):
        vs = [b[5] for b in bars.get(tk, [])[-20:]]
        if not vs or not vol:
            return None
        avg = sum(vs) / len(vs)
        mins = max(1, (now.hour * 60 + now.minute) - (9 * 60 + 30))
        frac = min(1.0, mins / 390) if mkt else 1.0
        return vol / (avg * frac) if avg else None

    def prev_close(tk):
        b = bars.get(tk)
        if mkt:
            return b[-1][4] if b else None
        return (q.get(tk) or {}).get("prev_close")

    L = ["# SAR — current snapshot", "",
         f"- **Snapshot taken:** {_pt(now)} ({'MARKET OPEN, prices are live' if mkt else 'market closed, prices are the latest close'})",
         f"- **Nightly scan used for levels:** {_pt(datetime.fromisoformat(sl['generated'])) if sl.get('generated') else 'none'}",
         (f"- **Last intraday check:** {intra.get('as_of_pt', '—')} PT" if intra else "- **Last intraday check:** none today"),
         ""]
    reg = sl.get("regime") or {}
    L += ["## Market", "",
          "- Trend: " + " · ".join(f"{k} 10{'>' if v else '<'}20" for k, v in reg.items() if v is not None),
          f"- Breadth: {_f(sl.get('breadth'), 0)}% of stocks above their 50 SMA",
          f"- **Sizing today: {sl.get('size_note', 'unknown')}**", ""]

    L += ["## Open positions", ""]
    if not pos:
        L += ["None (positions.txt is empty).", ""]
    else:
        L += ["| Ticker | Shares | Entry | Now | Day low | R now | P&L | Stop | Exit line | Status |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for p in pos:
            tk, qq = p["ticker"], q.get(p["ticker"]) or {}
            sp, ip = spos.get(tk, {}), ipos.get(tk, {})
            last = qq.get("price") or ip.get("last") or sp.get("last")
            stop = sp.get("stop_now") or p["stop"]
            R = p["entry"] - p["stop"]
            rnow = (last - p["entry"]) / R if last and R > 0 else None
            pnl = "—" if not last else f"{(last - p['entry']) * p['shares']:+,.0f}"
            rtxt = "—" if rnow is None else f"{rnow:+.2f}R"
            line = sp.get("exit_line") or "10 SMA"
            lv = sp.get("sma20") if line == "20 SMA" else sp.get("sma10")
            # On the entry day the stop IS the day's low, so only a lower low is a hit.
            entered_today = p.get("date") == datetime.now(ET).strftime("%Y-%m-%d")
            low = qq.get("low")
            hit = low is not None and (low < stop if entered_today else low <= stop)
            if last and hit:
                st = f"STOP HIT today (low {_f(qq['low'])} ≤ {_f(stop)})"
            elif last and lv and last < lv:
                st = f"BELOW {line} — sell if it closes here"
            elif last and lv and last < lv * 1.02:
                st = f"Near exit (within 2% of {line})"
            else:
                st = sp.get("status") or "HOLD"
            L.append(f"| {tk} | {p['shares']} | {_f(p['entry'])} | **{_f(last)}** | {_f(qq.get('low'))} | "
                     f"{rtxt} | {pnl} | {_f(stop)} | {line} {_f(lv)} | {st} |")
        L.append("")

    L += ["## Breakouts (from last night's scan, priced now)", ""]
    if not bo:
        L += ["None.", ""]
    else:
        L += ["| Ticker | Score | RS | Entry | Stop | Now | vs entry | Today | Verdict now |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(bo, key=lambda r: -r.get("score", 0)):
            tk, qq = r["ticker"], q.get(r["ticker"]) or {}
            last = qq.get("price")
            adr = r.get("adr_pct") or 0.05
            if not last:
                v = "no price"
            elif qq.get("low") is not None and qq["low"] <= r["stop"]:
                v = "FAILED — traded below the stop"
            elif last > r["entry"] * (1 + adr):
                v = f"SKIP — {(last / r['entry'] - 1) * 100:.1f}% above entry, stop too far now"
            elif last < r["entry"]:
                v = "Below entry — wait or skip"
            else:
                v = "Still buyable near entry"
            L.append(f"| {tk} | {r.get('score')} | {r.get('rs_rank', '—')} | {_f(r['entry'])} | {_f(r['stop'])} | "
                     f"**{_f(last)}** | {_pct(last, r['entry'])} | {_pct(last, prev_close(tk))} | {v} |")
        L.append("")

    L += ["## Alerts (trigger = higher of base high and today's opening-range high)", ""]
    if not al:
        L += ["None.", ""]
    else:
        L += ["| Ticker | Now | Trigger | Away | Day high | Vol pace | Status |", "|---|---|---|---|---|---|---|"]
        rows = []
        for r in al:
            tk, qq, ir = r["ticker"], q.get(r["ticker"]) or {}, irow.get(r["ticker"], {})
            last = qq.get("price") or ir.get("price")
            trig = ir.get("trigger") or r.get("base_high")
            pc = pace(tk, qq.get("vol"))
            if ir.get("confirm"):
                stt = f"{ir['confirm']} (close check {ir.get('confirm_at', '')})"
            elif ir.get("first_triggered"):
                stt = f"TRIGGERED at {ir['first_triggered']}"
            elif last and trig and last > trig:
                stt = "Above trigger now (wait for the next live check to confirm)"
            else:
                stt = ir.get("status") or "Waiting"
            away = (trig / last - 1) if last and trig else None
            atxt = "—" if away is None else f"{away * 100:.1f}%"
            ptxt = "—" if pc is None else f"{pc:.2f}x"
            rows.append((abs(away) if away is not None else 9,
                         f"| {tk} | **{_f(last)}** | {_f(trig)} | {atxt} | {_f(qq.get('high'))} | {ptxt} | {stt} |"))
        L += [x for _, x in sorted(rows)]
        L.append("")

    L += ["---", "Rules-based summary, not a trade signal. Check each chart before acting.", ""]
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"snapshot: wrote {OUT} ({len(tks)} tickers priced, market {'open' if mkt else 'closed'})")


def safe_snapshot() -> None:
    try:
        write_snapshot()
    except Exception as e:  # never break the main job
        print(f"snapshot failed: {e}")


if __name__ == "__main__":
    write_snapshot()
