"""Relative strength (RS) and industry-group strength.

RS = weighted price performance: 50% the last 3 months + 25% each for the
last 6 and 9 months (needs >= 6 months of bars). Ranked 1-99 against every
stock scanned that day: RS 90 = stronger than 90% of the market.

Group strength = median RS of the stocks in an industry (the sector, if the
industry has fewer than MIN_GROUP members), ranked 1-99 against all groups.
Sector / industry labels come from stockscan/universes/sectors.csv, built by
scripts/build_us_universe.py from NASDAQ's public stock screener.
"""

from __future__ import annotations

import csv
import os
from bisect import bisect_left
from collections import Counter, defaultdict
from statistics import median
from typing import Optional, Sequence

PERIODS = ((63, 0.5), (126, 0.25), (189, 0.25))
MIN_GROUP = 4
SECTORS_PATH = os.path.join(os.path.dirname(__file__), "..", "universes", "sectors.csv")


def rs_raw(C: Sequence[float], i: Optional[int] = None) -> Optional[float]:
    i = len(C) - 1 if i is None else i
    tot = w = 0.0
    for n, wt in PERIODS:
        if i - n < 0:
            break
        base = C[i - n]
        if base <= 0:
            return None
        tot += wt * (C[i] / base - 1.0)
        w += wt
    return tot / w if w >= 0.75 - 1e-9 else None


def rs_raw_series(C: Sequence[float]) -> list[Optional[float]]:
    return [rs_raw(C, i) for i in range(len(C))]


def rank_in(sorted_vals: Sequence[float], v: float) -> int:
    n = len(sorted_vals)
    if not n:
        return 50
    return max(1, min(99, round(100 * bisect_left(sorted_vals, v) / n)))


def percentile_ranks(values: dict[str, float]) -> dict[str, int]:
    vals = sorted(values.values())
    return {k: rank_in(vals, v) for k, v in values.items()}


def load_sectors(path: str = SECTORS_PATH) -> dict[str, tuple[str, str]]:
    if not os.path.exists(path):
        return {}
    out: dict[str, tuple[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            t = (r.get("symbol") or "").strip().upper()
            if t:
                out[t] = ((r.get("sector") or "").strip(), (r.get("industry") or "").strip())
    return out


def group_keys(tickers: Sequence[str], sectors: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Industry if it has enough members, else sector; '' if unknown."""
    cnt = Counter(sectors[t][1] for t in tickers if t in sectors and sectors[t][1])
    out = {}
    for t in tickers:
        sec, ind = sectors.get(t, ("", ""))
        out[t] = ind if ind and cnt[ind] >= MIN_GROUP else sec
    return out


def group_medians(rs: dict[str, float], groups: dict[str, str]) -> dict[str, float]:
    members: dict[str, list[float]] = defaultdict(list)
    for t, v in rs.items():
        g = groups.get(t)
        if g:
            members[g].append(v)
    return {g: median(v) for g, v in members.items() if len(v) >= MIN_GROUP}


def group_ranks(rs: dict[str, float], groups: dict[str, str]) -> dict[str, int]:
    return percentile_ranks(group_medians(rs, groups))
