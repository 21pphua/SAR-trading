"""Tests for the SAR breakout scanner (offline — synthetic bars, fake fetcher)."""

import json
import random

from stockscan.sar.engine import Bar, score_setup, passes_filters, market_regime, sma
from stockscan.sar.scan import run_sar_scan, write_shortlist


def _bars(phases, seed=11, start_price=9.6, base_vol=1.4e6):
    """Build a deterministic series. phase = (drift, vol_mult, range) or 'BO'."""
    rnd = random.Random(seed)
    bars, p = [], start_price
    for n, ph in enumerate(phases):
        date = f"2026-{1 + n // 28:02d}-{1 + n % 28:02d}"
        if ph == "BO":
            hi20 = max(b.high for b in bars[-20:])
            o, c = p * 1.004, hi20 * 1.03
            h, l, vm = c * 1.004, min(o, p) * 0.985, 3.4
        else:
            dr, vm, rg = ph
            o = p * (1 + (rnd.random() - 0.5) * rg * 0.3)
            c = p * (1 + dr + (rnd.random() - 0.5) * rg * 0.55)
            h = max(o, c) * (1 + rg * (0.15 + 0.3 * rnd.random()))
            l = min(o, c) * (1 - rg * (0.15 + 0.3 * rnd.random()))
        bars.append(Bar(date, o, h, l, c, base_vol * vm * (0.8 + 0.4 * rnd.random())))
        p = c
    return bars


def textbook(breakout=True):
    ph = [(0, 1.0, 0.06)] * 30 + [(0.019, 2.1, 0.075)] * 24
    ph += [((-0.003 if i < 9 else 0.002), 0.6 - i * 0.014, 0.062 - i * 0.0016) for i in range(18)]
    if breakout:
        ph.append("BO")
    return _bars(ph)


def flat():
    return _bars([(0, 1.0, 0.06)] * 80, seed=3)


def test_sma_matches_naive():
    v = [float(x) for x in range(1, 31)]
    s = sma(v, 10)
    assert s[8] is None and abs(s[9] - 5.5) < 1e-9 and abs(s[-1] - 25.5) < 1e-9


def test_textbook_breakout_scores_take():
    s = score_setup(textbook(), ticker="TXT")
    assert s.is_breakout
    assert s.verdict == "Take", s.score
    assert s.steps[0].status == "met"          # run-up
    assert s.steps[4].status == "met"          # range break
    assert s.steps[5].points >= 15             # breakout volume weighted heavily


def test_weights_sum_to_100():
    s = score_setup(textbook())
    assert sum(st.max_points for st in s.steps) == 100


def test_flat_series_scores_low():
    s = score_setup(flat())
    assert s.verdict == "Skip", s.score
    assert s.steps[0].frac < 0.8


def test_coiling_detected_before_breakout():
    s = score_setup(textbook(breakout=False))
    assert not s.is_breakout
    assert s.prep_points >= 25


def test_targets_and_stop():
    s = score_setup(textbook())
    labels = [t.label for t in s.targets]
    assert "5R partial" in labels and "Measured move" in labels and labels[-1] == "Stop"
    five_r = next(t for t in s.targets if t.label == "5R partial")
    assert abs(five_r.price - (s.entry + 5 * s.risk)) < 1e-9
    assert s.stop < s.entry


def _wide(price=20.0, vol=1e6, n=70):
    # ~10.5% ADR, $20M/day at the defaults
    return [Bar(str(i), price, price * 1.05, price * 0.95, price, vol) for i in range(n)]


def test_filters():
    assert passes_filters(_wide())[0]
    assert passes_filters(_wide(price=0.8, vol=1e8))[1] == "price"
    assert passes_filters(_wide(vol=1e5))[1] == "dollar volume"
    tight = [Bar(b.date, b.open, b.close * 1.01, b.close * 0.99, b.close, b.volume) for b in _wide()]
    assert passes_filters(tight)[1] == "adr"
    assert not passes_filters(_wide(n=40))[0]


def test_market_regime():
    up = [Bar(str(i), 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i in range(30)]
    down = [Bar(str(i), 130 - i, 131 - i, 129 - i, 130 - i, 1e6) for i in range(30)]
    assert market_regime(up) is True
    assert market_regime(down) is False
    assert market_regime(up[:5]) is None


def test_run_scan_with_fake_fetcher(tmp_path):
    universe = {"BRK": textbook(), "COIL": textbook(breakout=False), "FLAT": flat(), "NODATA": []}
    up = [Bar(str(i), 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i in range(30)]

    def fake_fetch(tickers, on_progress=None, **_):
        return {t: (up if t in ("SPY", "QQQ") else universe.get(t, [])) for t in tickers}

    res = run_sar_scan(list(universe), fetch=fake_fetch, min_score=65, apply_filters=False,
                       today=None, earnings=lambda tks: {"BRK": "2099-01-01"}, require_tight_stop=False)
    assert [s.ticker for s in res.breakouts] == ["BRK"]
    assert "FLAT" not in [s.ticker for s in res.breakouts + res.coiling]
    assert res.regime_ok is True
    assert res.with_data == 3

    out = tmp_path / "shortlist.json"
    write_shortlist(res, str(out))
    doc = json.loads(out.read_text())
    assert doc["format"] == "sar-shortlist/1"
    first = doc["results"][0]
    assert first["ticker"] == "BRK" and first["kind"] == "breakout"
    assert len(first["bars"][0]) == 6 and len(first["steps"]) == 7
    assert first["earnings_date"] == "2099-01-01" and first["earnings_soon"] is False
    assert "risk_adr" in first and "wide_stop" in first


# --- risk / ADR -------------------------------------------------------------

def test_risk_adr():
    s = score_setup(textbook())
    assert s.risk_adr > 0
    assert s.wide_stop == (s.risk_adr > 1.0)


# --- backtest ---------------------------------------------------------------

from stockscan.sar import engine as _engine
from stockscan.sar.backtest import backtest_ticker, summarize, run_backtest, render_backtest


def _phases_textbook():
    ph = [(0, 1.0, 0.06)] * 30 + [(0.019, 2.1, 0.075)] * 24
    ph += [((-0.003 if i < 9 else 0.002), 0.6 - i * 0.014, 0.062 - i * 0.0016) for i in range(18)]
    return ph + ["BO"]


def _loosen(monkeypatch):
    monkeypatch.setattr(_engine, "SAR_MIN_ADR", 0.0)
    monkeypatch.setattr(_engine, "SAR_MIN_DOLLAR_VOL", 0.0)


def test_backtest_winner_exits_on_10sma(monkeypatch):
    _loosen(monkeypatch)
    bars = _bars(_phases_textbook() + [(0.03, 1.4, 0.04)] * 15 + [(-0.04, 1.0, 0.04)] * 10)
    bo_date = bars[72].date
    trades = backtest_ticker("T", bars, min_score=65, trend_filter=False)
    bo = [t for t in trades if t.entry_date == bo_date]
    assert bo, [t.entry_date for t in trades]
    tr = bo[0]
    assert not tr.open and tr.exit_reason in ("sma", "breakeven")
    assert tr.r > 0


def test_backtest_stopped_out(monkeypatch):
    _loosen(monkeypatch)
    bars = _bars(_phases_textbook() + [(-0.15, 1.0, 0.05)] + [(0, 1.0, 0.05)] * 5)
    bo_date = bars[72].date
    tr = [t for t in backtest_ticker("T", bars, min_score=65, trend_filter=False) if t.entry_date == bo_date][0]
    assert tr.exit_reason == "stop"
    assert tr.r <= -0.99


def test_summarize_and_render(monkeypatch):
    _loosen(monkeypatch)
    win = _bars(_phases_textbook() + [(0.03, 1.4, 0.04)] * 15 + [(-0.04, 1.0, 0.04)] * 10)
    lose = _bars(_phases_textbook() + [(-0.15, 1.0, 0.05)] + [(0, 1.0, 0.05)] * 5)
    spy = [Bar(b.date, 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i, b in enumerate(win)]

    def fake_fetch(tickers, period=None, **_):
        d = {"WIN": win, "LOSE": lose, "SPY": spy}
        return {t: d.get(t, []) for t in tickers}

    res = run_backtest(["WIN", "LOSE"], fake_fetch, min_score=65, trend_filter=False)
    st = summarize(res.trades)
    assert st and st.trades >= 2
    assert 0 < st.win_rate < 1
    assert st.max_consec_losses >= 1
    text = render_backtest(res)
    assert "PLAIN ENGLISH" in text and "made money" in text


# --- long-term trend filter -------------------------------------------------

def _bounce_in_downtrend():
    ph = [(-0.008, 1.0, 0.05)] * 60 + [(0.0, 0.7, 0.04)] * 20 + ["BO"]
    return _bars(ph, seed=7, start_price=47.0)


def test_trend_filter_rejects_downtrend_bounce():
    s = score_setup(_bounce_in_downtrend())
    assert s.trend_ok is False
    assert "50 SMA" in s.trend_note or "52-week" in s.trend_note


def test_trend_filter_passes_textbook():
    s = score_setup(textbook())
    assert s.trend_ok is True, s.trend_note


def test_scan_sets_aside_counter_trend(tmp_path):
    universe = {"BRK": textbook(), "BNC": _bounce_in_downtrend()}
    up = [Bar(str(i), 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i in range(30)]

    def fake_fetch(tickers, on_progress=None, **_):
        return {t: (up if t in ("SPY", "QQQ") else universe.get(t, [])) for t in tickers}

    res = run_sar_scan(list(universe), fetch=fake_fetch, min_score=0, apply_filters=False,
                       today=None, earnings=None, require_tight_stop=False)
    listed = [s.ticker for s in res.breakouts + res.coiling]
    assert "BRK" in listed and "BNC" not in listed
    assert [s.ticker for s in res.counter_trend] == ["BNC"]
    out = tmp_path / "s.json"
    write_shortlist(res, str(out))
    doc = json.loads(out.read_text())
    assert doc["counter_trend"][0]["ticker"] == "BNC"
    assert "trend_ok" in doc["results"][0]
    alld = json.loads((tmp_path / "all_scores.json").read_text())
    assert alld["format"] == "sar-all/1"
    kinds = {r["ticker"]: r["kind"] for r in alld["scores"]}
    assert kinds["BNC"] == "counter" and "BRK" in kinds


# --- positions ----------------------------------------------------------------

from stockscan.sar.scan import Position, evaluate_position, read_positions


def _line(prices, start="2026-05-01"):
    out = []
    for i, (lo, hi, c) in enumerate(prices):
        out.append(Bar(f"2026-05-{1 + i:02d}", c, hi, lo, c, 1e6))
    return out


def test_position_hold_then_stop():
    base = [(9.9, 10.1, 10.0)] * 15
    bars = _line(base + [(9.4, 10.0, 9.6)])
    p = evaluate_position(Position("X", 100, 10.0, 9.5, bars[10].date), bars)
    assert p.status == "STOPPED"


def test_position_5r_then_breakeven_hold():
    rise = [(10 + i * 0.4 - 0.1, 10 + i * 0.4 + 0.1, 10 + i * 0.4) for i in range(15)]
    bars = _line(rise)
    p = evaluate_position(Position("X", 100, 10.0, 9.5, bars[0].date), bars)
    assert p.hit_5r_date and p.stop_now == 10.0
    assert p.status in ("5R HIT", "NEAR EXIT", "HOLD")   # 5R badge shows 2 bars; exit line is now the 20 SMA
    assert p.r_now > 5


def test_position_exit_below_10sma():
    up = [(10 + i * 0.2 - 0.1, 10 + i * 0.2 + 0.1, 10 + i * 0.2) for i in range(14)]
    drop = [(10.9, 12.6, 11.0)]
    bars = _line(up + drop)
    p = evaluate_position(Position("X", 100, 10.0, 9.0, bars[0].date), bars)
    assert p.status == "EXIT"


def test_read_positions(tmp_path):
    f = tmp_path / "positions.txt"
    f.write_text("ticker,shares,entry,stop,date\n# comment\nARHS,97,10.24,9.86,2026-10-02\nbad line\n")
    ps = read_positions(str(f))
    assert len(ps) == 1 and ps[0].ticker == "ARHS" and ps[0].shares == 97


def test_shortlist_new_and_fired(tmp_path):
    universe = {"BRK": textbook()}
    up = [Bar(str(i), 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i in range(30)]

    def fake_fetch(tickers, on_progress=None, **_):
        return {t: (up if t in ("SPY", "QQQ") else universe.get(t, [])) for t in tickers}

    out = tmp_path / "s.json"
    out.write_text(json.dumps({"generated": "2000-01-01T00:00:00", "results": [{"ticker": "BRK", "kind": "coiling"}]}))
    res = run_sar_scan(["BRK"], fetch=fake_fetch, min_score=0, apply_filters=False, today=None, earnings=None, require_tight_stop=False)
    write_shortlist(res, str(out))
    doc = json.loads(out.read_text())
    brk = [r for r in doc["results"] if r["ticker"] == "BRK"][0]
    if brk["kind"] == "breakout":
        assert brk["fired"] is True
    assert brk["new"] is False
    assert doc["previous"]["coiling"] == ["BRK"]


def test_wide_stop_goes_to_watch_list():
    universe = {"BRK": textbook()}
    up = [Bar(str(i), 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i in range(30)]

    def fake_fetch(tickers, on_progress=None, **_):
        return {t: (up if t in ("SPY", "QQQ") else universe.get(t, [])) for t in tickers}

    s = score_setup(textbook())
    res = run_sar_scan(["BRK"], fetch=fake_fetch, min_score=0, apply_filters=False, today=None, earnings=None)
    if s.wide_stop:
        assert [x.ticker for x in res.wide_stop] == ["BRK"] and not res.breakouts
    else:
        assert [x.ticker for x in res.breakouts] == ["BRK"] and not res.wide_stop


# --- strength + intraday backtest ------------------------------------------

from stockscan.sar.strength import rs_raw, percentile_ranks, group_keys, group_ranks


def test_rs_rank_orders_by_performance():
    up = [100 * (1.01 ** i) for i in range(200)]
    flat = [100.0] * 200
    down = [100 * (0.99 ** i) for i in range(200)]
    ranks = percentile_ranks({"UP": rs_raw(up), "FLAT": rs_raw(flat), "DOWN": rs_raw(down)})
    assert ranks["UP"] > ranks["FLAT"] > ranks["DOWN"]
    assert rs_raw([100.0] * 100) is None  # under 6 months


def test_group_ranks_use_industry_then_sector():
    sectors = {f"A{i}": ("Tech", "Chips") for i in range(4)} | {f"B{i}": ("Health", "Biotech") for i in range(4)} \
        | {"C0": ("Tech", "Tiny")}
    g = group_keys(list(sectors), sectors)
    assert g["A0"] == "Chips" and g["C0"] == "Tech"
    rs = {f"A{i}": 0.5 for i in range(4)} | {f"B{i}": -0.2 for i in range(4)}
    gr = group_ranks(rs, g)
    assert gr["Chips"] > gr["Biotech"]


def test_intraday_mode_runs(monkeypatch):
    _loosen(monkeypatch)
    bars = _bars(_phases_textbook() + [(0.03, 1.4, 0.04)] * 15 + [(-0.04, 1.0, 0.04)] * 10)
    trades = backtest_ticker("T", bars, min_score=65, trend_filter=False, mode="intraday")
    for t in trades:
        assert t.mode == "intraday" and t.entry > t.stop
        assert t.false_break == (t.exit_reason == "false break")


# --- SAR_MIN_RISK_ADR: tight-stop floor / exclusion -------------------------

from stockscan.sar.engine import effective_risk_per_share


def test_effective_risk_floors_a_near_zero_stop():
    # entry=301.82, stop=301.69 (raw risk 0.13) with a 1% ADR: the floor
    # (0.15 * 0.01 * 301.82 ~= 0.45) is wider than the raw stop, so sizing
    # should use the floor, not the unrealistic raw distance.
    floored = effective_risk_per_share(301.82, 301.69, 0.01)
    assert floored > (301.82 - 301.69)
    assert abs(floored - 0.15 * 0.01 * 301.82) < 1e-9


def test_effective_risk_leaves_a_normal_stop_alone():
    # A normal, wide-enough stop shouldn't be touched by the floor.
    assert effective_risk_per_share(100, 90, 0.05) == 10


def test_tight_stop_flag_on_setup_score():
    s = score_setup(textbook())
    # textbook() is built with a normal-width stop, not a near-zero one.
    assert s.tight_stop is False
    assert s.sizing_risk >= s.risk


def _near_zero_stop_bars():
    """A breakout whose close sits a hair above its own low (tight_stop case)."""
    ph = _phases_textbook()
    bars = _bars(ph[:-1])  # everything up to (not including) the "BO" phase
    hi20 = max(b.high for b in bars[-20:])
    p = bars[-1].close
    o, c = p * 1.004, hi20 * 1.03
    # low is a hair under the close -- an almost-zero stop distance.
    tiny_bar = Bar("tiny", o, c * 1.001, c * 0.9995, c, bars[-1].volume * 3.0)
    return bars + [tiny_bar]


def test_backtest_excludes_tight_stop_trades(monkeypatch):
    _loosen(monkeypatch)
    bars = _near_zero_stop_bars()
    bo_date = bars[-1].date
    trades = backtest_ticker("T", bars, min_score=0, trend_filter=False)
    # However this setup scores, it must never show up as an entered trade on
    # the day its stop is unrealistically tight to entry.
    assert bo_date not in [t.entry_date for t in trades]


# --- position sizing ---------------------------------------------------------

from stockscan.sar.sizing import recommend_size


def test_recommend_size_basic():
    s = score_setup(textbook())
    rec = recommend_size(s, equity=100_000, open_sectors=[])
    assert rec.shares > 0
    assert rec.risk_dollars == 1000.0  # 1% of 100k at the default risk %
    assert not rec.sector_cap_breached
    assert not rec.max_positions_breached


def test_recommend_size_flags_sector_cap():
    s = score_setup(textbook())
    s.group = "Semiconductors"
    open_sectors = ["Semiconductors", "Semiconductors"]  # already 2 open -> cap is 2
    rec = recommend_size(s, equity=100_000, open_sectors=open_sectors, max_per_sector=2)
    assert rec.sector_cap_breached
    assert any("Semiconductors" in n for n in rec.notes)


def test_recommend_size_flags_max_positions():
    s = score_setup(textbook())
    rec = recommend_size(s, equity=100_000, open_sectors=["A", "B", "C", "D", "E", "F", "G"], max_positions=7)
    assert rec.max_positions_breached


def test_recommend_size_zero_equity_is_safe():
    s = score_setup(textbook())
    rec = recommend_size(s, equity=0, open_sectors=[])
    assert rec.shares == 0 and rec.notes


# --- walk-forward split -------------------------------------------------------

def test_backtest_split_partitions_by_entry_date(monkeypatch):
    _loosen(monkeypatch)
    win = _bars(_phases_textbook() + [(0.03, 1.4, 0.04)] * 15 + [(-0.04, 1.0, 0.04)] * 10)
    lose = _bars(_phases_textbook() + [(-0.15, 1.0, 0.05)] + [(0, 1.0, 0.05)] * 5)
    spy = [Bar(b.date, 100 + i, 101 + i, 99 + i, 100 + i, 1e6) for i, b in enumerate(win)]

    def fake_fetch(tickers, period=None, **_):
        d = {"WIN": win, "LOSE": lose, "SPY": spy}
        return {t: d.get(t, []) for t in tickers}

    res = run_backtest(["WIN", "LOSE"], fake_fetch, min_score=65, trend_filter=False)
    all_dates = sorted(t.entry_date for t in res.trades)
    assert all_dates, "fixture produced no trades -- test setup is broken"
    mid = all_dates[len(all_dates) // 2]
    train, test = res.split(mid)
    assert len(train.trades) + len(test.trades) == len(res.trades)
    assert all(t.entry_date < mid for t in train.trades)
    assert all(t.entry_date >= mid for t in test.trades)
