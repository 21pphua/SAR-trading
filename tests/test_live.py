"""Tests for the intraday live-rescan (stockscan sar-live).

No network: fetch_live_quotes (the only piece that calls yfinance) is never
exercised directly here -- everything else is pure logic over fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime

from stockscan.sar.live import (
    LiveQuote,
    WatchEntry,
    avg_vol20_from_bars,
    build_live_checks,
    load_watchlist,
    market_is_open,
    render_live_checks,
    session_fraction_elapsed,
)


def _bars(vols):
    """Fake [date, o, h, l, c, v] rows; only volume matters here."""
    return [[f"2026-01-{i + 1:02d}", 10, 11, 9, 10, v] for i, v in enumerate(vols)]


def test_avg_vol20_from_bars_uses_last_20():
    bars = _bars([100] * 30 + [200] * 20)  # last 20 are all 200
    assert avg_vol20_from_bars(bars) == 200


def test_avg_vol20_from_bars_short_history():
    bars = _bars([50, 150])
    assert avg_vol20_from_bars(bars) == 100


def test_avg_vol20_from_bars_empty():
    assert avg_vol20_from_bars([]) == 0.0


def test_session_fraction_elapsed_clamps_near_open():
    # 9:31 Monday -- a minute into the session, would otherwise divide by ~0
    f = session_fraction_elapsed(datetime(2026, 10, 5, 9, 31))
    assert f == 0.03


def test_session_fraction_elapsed_midday():
    # 12:30 -- 3 hours (180 min) of 390 -> ~0.46
    f = session_fraction_elapsed(datetime(2026, 10, 5, 12, 30))
    assert 0.4 < f < 0.5


def test_session_fraction_elapsed_before_open():
    f = session_fraction_elapsed(datetime(2026, 10, 5, 7, 0))
    assert f == 0.03


def test_session_fraction_elapsed_after_close():
    f = session_fraction_elapsed(datetime(2026, 10, 5, 17, 0))
    assert f == 1.0


def test_market_is_open_during_session():
    assert market_is_open(datetime(2026, 10, 5, 10, 0)) is True  # Monday


def test_market_is_open_weekend():
    assert market_is_open(datetime(2026, 10, 10, 10, 0)) is False  # Saturday


def test_market_is_open_after_hours():
    assert market_is_open(datetime(2026, 10, 5, 20, 0)) is False


def test_load_watchlist_filters_by_kind(tmp_path):
    path = tmp_path / "shortlist.json"
    path.write_text(json.dumps({
        "results": [
            {"ticker": "AAA", "kind": "coiling", "base_high": 10.0, "score": 60,
             "bars": _bars([1000] * 20)},
            {"ticker": "BBB", "kind": "breakout", "base_high": 20.0, "score": 80,
             "bars": _bars([2000] * 20)},
        ]
    }), encoding="utf-8")
    coiling = load_watchlist(str(path), kinds=("coiling",))
    assert [e.ticker for e in coiling] == ["AAA"]
    both = load_watchlist(str(path), kinds=("coiling", "breakout"))
    assert {e.ticker for e in both} == {"AAA", "BBB"}


def test_load_watchlist_missing_file_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        load_watchlist(str(tmp_path / "nope.json"))


def _entry(ticker="AAA", base_high=10.0, avg_vol20=1000.0):
    return WatchEntry(ticker=ticker, kind="coiling", base_high=base_high, score=55, avg_vol20=avg_vol20)


def test_build_live_checks_confirmed_breakout():
    entries = [_entry(avg_vol20=1000.0)]
    # Midday (frac ~0.46): price above trigger, volume already ~1.5x the paced average.
    quotes = {"AAA": LiveQuote("AAA", price=10.50, day_high=10.6, day_low=9.9,
                               cum_volume=700, as_of="2026-10-05 12:30", bars=180)}
    checks = build_live_checks(entries, quotes, min_relvol=1.3, now=datetime(2026, 10, 5, 12, 30))
    c = checks[0]
    assert c.crossed is True
    assert c.confirmed is True
    assert c.pct_to_trigger > 0


def test_build_live_checks_crossed_but_thin_volume():
    entries = [_entry(avg_vol20=100_000.0)]
    quotes = {"AAA": LiveQuote("AAA", price=10.10, day_high=10.1, day_low=9.9,
                               cum_volume=500, as_of="2026-10-05 09:35", bars=5)}
    checks = build_live_checks(entries, quotes, min_relvol=1.3, now=datetime(2026, 10, 5, 9, 35))
    c = checks[0]
    assert c.crossed is True
    assert c.confirmed is False
    assert "thin" in c.note or "could fade" in c.note


def test_build_live_checks_still_below_trigger():
    entries = [_entry()]
    quotes = {"AAA": LiveQuote("AAA", price=9.50, day_high=9.6, day_low=9.3,
                               cum_volume=400, as_of="2026-10-05 10:00", bars=30)}
    checks = build_live_checks(entries, quotes, now=datetime(2026, 10, 5, 10, 0))
    c = checks[0]
    assert c.crossed is False
    assert c.confirmed is False
    assert c.pct_to_trigger < 0


def test_build_live_checks_missing_quote():
    entries = [_entry(ticker="ZZZ")]
    checks = build_live_checks(entries, quotes={})
    assert checks[0].ticker == "ZZZ"
    assert checks[0].crossed is False
    assert "no live quote" in checks[0].note


def test_build_live_checks_sorts_confirmed_first():
    entries = [_entry(ticker="LOSER", base_high=10.0, avg_vol20=1000.0),
              _entry(ticker="WINNER", base_high=10.0, avg_vol20=1000.0)]
    quotes = {
        "LOSER": LiveQuote("LOSER", price=9.0, day_high=9.1, day_low=8.9,
                          cum_volume=100, as_of="x", bars=10),
        "WINNER": LiveQuote("WINNER", price=10.5, day_high=10.6, day_low=9.9,
                           cum_volume=700, as_of="x", bars=180),
    }
    checks = build_live_checks(entries, quotes, min_relvol=1.3, now=datetime(2026, 10, 5, 12, 30))
    assert checks[0].ticker == "WINNER"
    assert checks[0].confirmed is True


def test_render_live_checks_empty():
    out = render_live_checks([])
    assert "nothing to watch" in out
    assert "sar --out" in out


def test_render_live_checks_smoke():
    entries = [_entry()]
    quotes = {"AAA": LiveQuote("AAA", price=10.5, day_high=10.6, day_low=9.9,
                               cum_volume=700, as_of="2026-10-05 12:30", bars=180)}
    checks = build_live_checks(entries, quotes, min_relvol=1.3, now=datetime(2026, 10, 5, 12, 30))
    out = render_live_checks(checks, generated="12:30:00")
    assert "AAA" in out
    assert "CONFIRMED" in out
    assert "not a trade signal" in out
