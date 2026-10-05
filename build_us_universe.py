#!/usr/bin/env python3
"""Build stockscan/universes/us_all.txt — every NASDAQ/NYSE/AMEX common stock.

Source: NASDAQ Trader symbol directory (public, updated daily):
  https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
  https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

ETFs and test issues are dropped; preferreds/units/warrants (symbols with
'.', '$', or a 5th-letter W/U/R suffix on NASDAQ) are dropped too. The SAR
scan's own liquidity filters (price, ADR%, $ volume) trim the rest.
"""

from __future__ import annotations

import csv
import json
import os
import re
import urllib.request

URLS = {
    "nasdaq": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "other": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
OUT = os.path.join(os.path.dirname(__file__), "..", "stockscan", "universes", "us_all.txt")
SECTORS_OUT = os.path.join(os.path.dirname(OUT), "sectors.csv")
SCREENER = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"


def write_sectors() -> None:
    """Sector + industry per ticker from NASDAQ's public screener (best effort)."""
    try:
        req = urllib.request.Request(SCREENER, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Accept": "application/json, text/plain, */*"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            rows = json.load(resp)["data"]["rows"]
    except Exception as e:  # keep going without sectors; group strength just shows "unknown"
        print(f"sector download failed ({e}); group strength will be blank")
        return
    n = 0
    with open(SECTORS_OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "sector", "industry"])
        for r in rows:
            s = (r.get("symbol") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{1,5}", s) and r.get("sector"):
                w.writerow([s, r.get("sector", "").strip(), r.get("industry", "").strip()])
                n += 1
    print(f"wrote {n} sector rows -> {os.path.normpath(SECTORS_OUT)}")


def _rows(url: str) -> list[dict]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    lines = [l for l in text.splitlines() if l and not l.startswith("File Creation Time")]
    hdr = lines[0].split("|")
    return [dict(zip(hdr, l.split("|"))) for l in lines[1:]]


def main() -> None:
    syms: set[str] = set()
    for r in _rows(URLS["nasdaq"]):
        s = r.get("Symbol", "")
        if r.get("ETF") == "Y" or r.get("Test Issue") == "Y":
            continue
        if not re.fullmatch(r"[A-Z]{1,5}", s) or (len(s) == 5 and s[-1] in "WUR"):
            continue
        syms.add(s)
    for r in _rows(URLS["other"]):
        s = r.get("ACT Symbol", "")
        if r.get("ETF") == "Y" or r.get("Test Issue") == "Y":
            continue
        if not re.fullmatch(r"[A-Z]{1,5}", s):
            continue
        syms.add(s)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("# All US-listed common stocks (NASDAQ Trader symbol directory)\n")
        fh.write("\n".join(sorted(syms)) + "\n")
    print(f"wrote {len(syms)} tickers -> {os.path.normpath(OUT)}")
    write_sectors()


if __name__ == "__main__":
    main()
