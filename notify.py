#!/usr/bin/env python3
"""Send tonight's SAR scan summary to your phone and/or Discord.

Reads results/shortlist.json. Uses whichever of these are set (GitHub repo
Settings -> Secrets and variables -> Actions -> New repository secret):

  NTFY_TOPIC       a private topic name for the free ntfy app (ntfy.sh)
  DISCORD_WEBHOOK  a Discord channel webhook URL

Neither set -> does nothing (exits 0).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

PATH = os.environ.get("SHORTLIST", "results/shortlist.json")


def summary(doc: dict) -> tuple[str, str]:
    reg = doc.get("regime") or {}
    vals = [v for v in reg.values() if v is not None]
    ok = all(vals) if vals else None
    market = "Market FAVORABLE" if ok else "Market UNFAVORABLE" if ok is not None else "Market unknown"
    res = doc.get("results", [])
    bo = [r for r in res if r.get("kind") == "breakout"]
    co = [r for r in res if r.get("kind") == "coiling"]
    if doc.get("size_note"):
        market += " · " + doc["size_note"]
    lines = [market, ""]
    pos = [p for p in doc.get("positions", []) if p.get("status") not in ("HOLD", None)]
    if pos:
        lines.append("ACTION ON YOUR POSITIONS")
        lines += [f"{p['ticker']} {p['status']}: {p['action']}" for p in pos]
        lines.append("")
    if bo:
        lines.append(f"BREAKOUTS ({len(bo)})")
        for r in bo[:10]:
            flags = []
            if r.get("earnings_soon"):
                flags.append("earnings soon")
            if r.get("wide_stop"):
                flags.append("wide stop")
            if r.get("fired"):
                flags.insert(0, "FIRED from alerts")
            elif r.get("new"):
                flags.insert(0, "new")
            f = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"{r['ticker']} {r['score']}  buy {r['entry']:.2f} stop {r['stop']:.2f}{f}")
    else:
        lines.append("No confirmed breakouts today.")
    if co:
        lines += ["", f"ALERTS ({len(co)})"]
        lines += [f"{r['ticker']} > {r['base_high']:.2f}" for r in co[:10]]
    title = (f"SAR: {len(pos)} position action{'s' if len(pos) != 1 else ''} · " if pos else "SAR scan: ") + \
            f"{len(bo)} breakout{'s' if len(bo) != 1 else ''} · {market.split()[1].lower()}"
    return title, "\n".join(lines)


def post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main() -> int:
    if not os.path.exists(PATH):
        print(f"{PATH} not found; nothing to send")
        return 0
    with open(PATH, encoding="utf-8") as fh:
        doc = json.load(fh)
    title, body = summary(doc)
    sent = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        post(f"https://ntfy.sh/{topic}", body.encode("utf-8"), {"Title": title.encode("ascii", "ignore").decode()})
        sent = True
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if hook:
        post(hook, json.dumps({"content": f"**{title}**\n```\n{body[:1800]}\n```"}).encode("utf-8"),
             {"Content-Type": "application/json", "User-Agent": "sar-scan"})
        sent = True
    print("sent" if sent else "no NTFY_TOPIC or DISCORD_WEBHOOK set; skipped")
    return 0


if __name__ == "__main__":
    rc = main()
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from snapshot import safe_snapshot  # scripts/ or repo root
        safe_snapshot()
    except Exception as e:
        print(f"snapshot skipped: {e}")
    sys.exit(rc)
