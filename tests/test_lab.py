"""Tests for the model lab (offline: hand-built bars and a fake fetcher)."""

import math

import pytest

from stockscan.lab.core import CostModel, ExitRules, Signal, simulate, try_fill, lookahead_safe
from stockscan.lab.metrics import equity_stats, yearly_returns, trade_stats
from stockscan.lab.portfolio import AccountRules, run_account, AccountResult
from stockscan.lab.walkforward import walk_forward
from stockscan.lab.setups import sar_breakout
from stockscan.sar.engine import Bar, Series

from tests.test_sar import textbook, flat

NOCOST = CostModel(0.0)


def mk(rows, start=0):
    """rows of (open, high, low, close) -> Bars on consecutive fake dates."""
    out = []
    for k, (o, h, l, c) in enumerate(rows):
        n = start + k
        out.append(Bar(f"2020-{1 + n // 28:02d}-{1 + n % 28:02d}", o, h, l, c, 1e6))
    return out


def flat_rows(n, px=10.0):
    return [(px, px * 1.01, px * 0.99, px)] * n


# --------------------------------------------------------------------------- fills

def test_buy_stop_fills_at_trigger_or_gap_open():
    bars = mk(flat_rows(5) + [(10.0, 10.6, 9.95, 10.5)] + [(11.0, 11.2, 10.9, 11.1)])
    S = Series(bars)
    sig = Signal("X", 4, bars[4].date, "t", "stop", stop=9.0, entry_price=10.5)
    assert try_fill(sig, S, NOCOST) == (5, 10.5)               # high reached the trigger
    sig2 = Signal("X", 5, bars[5].date, "t", "stop", stop=9.0, entry_price=10.8)
    assert try_fill(sig2, S, NOCOST) == (6, 11.0)              # gapped over: pay the open
    assert try_fill(sig2, S, NOCOST, max_gap=0.01) is None     # ...unless we refuse to chase


def test_buy_stop_not_reached_is_no_trade():
    bars = mk(flat_rows(8))
    sig = Signal("X", 3, bars[3].date, "t", "stop", stop=9.0, entry_price=12.0, valid_bars=3)
    assert try_fill(sig, Series(bars), NOCOST) is None


def test_limit_fills():
    bars = mk(flat_rows(4) + [(10.0, 10.1, 9.5, 9.9), (9.4, 9.6, 9.3, 9.5)])
    S = Series(bars)
    assert try_fill(Signal("X", 3, "", "t", "limit", stop=9.0, entry_price=9.7), S, NOCOST) == (4, 9.7)
    assert try_fill(Signal("X", 4, "", "t", "limit", stop=9.0, entry_price=9.45), S, NOCOST) == (5, 9.4)


def test_costs_applied_both_sides():
    cm = CostModel(slippage_bps=100)    # 1% for easy math
    assert cm.buy_px(100) == pytest.approx(101)
    assert cm.sell_px(100) == pytest.approx(99)


# --------------------------------------------------------------------------- trade path

def test_fill_day_touching_stop_counts_as_loss():
    # stop entry at 10.5 fills, but the same day's low (9.4) is under the 9.5 stop: assume the worst
    bars = mk(flat_rows(5) + [(10.0, 10.8, 9.4, 10.7)] + flat_rows(5, 10.7))
    S = Series(bars)
    sig = Signal("X", 4, bars[4].date, "t", "stop", stop=9.5, entry_price=10.5)
    c = simulate(sig, S, ExitRules(trail_sma=None), NOCOST)
    assert c is not None and c.r == pytest.approx(-1.0)


def test_partial_breakeven_and_sma_exit_math():
    # enter at close 10 with stop 9 (R = 1). Run to 15 (5R): sell 20%. Then fall: stop at breakeven.
    rows = flat_rows(25)
    rows += [(10, 10.2, 9.8, 10.0)]                       # signal bar, i = 25, close entry at 10
    rows += [(10.5, 11, 10.4, 10.9), (11, 12, 10.9, 11.9), (12, 13, 11.9, 12.9),
             (13, 15.2, 12.9, 15.0)]                      # hits 15 = 5R on bar 29
    rows += [(15, 15.1, 9.9, 10.1)]                       # crashes through breakeven (10)
    bars = mk(rows)
    S = Series(bars)
    sig = Signal("X", 25, bars[25].date, "t", "close", stop=9.0)
    c = simulate(sig, S, ExitRules(trail_sma=None), NOCOST)
    # 20% at 15 (+5R each) + 80% at breakeven (0R) = +1.0R
    assert c.hit_partial
    assert c.r == pytest.approx(1.0)
    assert c.reason == "breakeven"


def test_sma_trailing_exit_at_close():
    rows = flat_rows(25) + [(10, 10.2, 9.8, 10.0)]
    rows += [(10.2, 11, 10.1, 10.8)] * 3 + [(10.8, 10.9, 9.6, 9.7)]   # close under the 10 SMA
    bars = mk(rows)
    S = Series(bars)
    c = simulate(Signal("X", 25, bars[25].date, "t", "close", stop=9.0), S, ExitRules(), NOCOST)
    assert c.reason.startswith("close <") and c.r == pytest.approx(-0.3)


def test_costs_make_results_worse():
    rows = flat_rows(25) + [(10, 10.2, 9.8, 10.0)] + [(10.2, 11, 10.1, 10.8)] * 3 + [(10.8, 10.9, 9.6, 9.7)]
    bars = mk(rows)
    S = Series(bars)
    sig = Signal("X", 25, bars[25].date, "t", "close", stop=9.0)
    free = simulate(sig, S, ExitRules(), NOCOST).r
    paid = simulate(sig, S, ExitRules(), CostModel(25)).r
    assert paid < free


def test_confirm_or_cut_sells_at_close():
    bars = mk(flat_rows(5) + [(10.0, 10.6, 9.95, 10.1)] + flat_rows(5, 10.1))
    sig = Signal("X", 4, bars[4].date, "t", "stop", stop=9.5, entry_price=10.5, confirm_level=10.5)
    c = simulate(sig, Series(bars), ExitRules(), NOCOST)
    assert c.reason.startswith("cut") and c.r == pytest.approx((10.1 - 10.5) / 1.0)


# --------------------------------------------------------------------------- no lookahead

@pytest.mark.parametrize("entry", ["close", "next_open", "coil_break"])
def test_sar_signals_never_use_future_bars(entry):
    for bars in (textbook(True), textbook(False)):
        fn = lambda b: sar_breakout.signals("T", b, entry=entry, trend_filter=False, apply_filters=False,
                                           min_risk_adr=0, max_risk_adr=None)
        for i in range(61, len(bars)):
            assert lookahead_safe(fn, bars, i), (entry, i)


def test_sar_close_signal_on_textbook_breakout():
    bars = textbook(True)
    sigs = sar_breakout.signals("T", bars, entry="close", trend_filter=False, min_risk_adr=0, max_risk_adr=None,
                                apply_filters=False)
    assert sigs and sigs[-1].i == len(bars) - 1
    assert sigs[-1].stop == bars[-1].low


def test_sar_coil_signal_before_breakout():
    bars = textbook(False)
    sigs = sar_breakout.signals("T", bars, entry="coil_break", trend_filter=False, min_risk_adr=0, max_risk_adr=None,
                                apply_filters=False)
    assert sigs and all(s.entry_type == "stop" and s.entry_price > s.stop for s in sigs)


# --------------------------------------------------------------------------- account

def _cand(tk, fill_i, stop=9.0, entry=10.0, exit_px=12.0, hold=5, group="", rank=0.0, bars=None):
    """A ready-made candidate: buy at entry on fill_i, sell everything at exit_px hold bars later."""
    from stockscan.lab.core import Candidate, Event
    sig = Signal(tk, fill_i, bars[fill_i].date, "t", "close", stop=stop, rank=rank, group=group)
    c = Candidate(sig, fill_i, bars[fill_i].date, entry, stop)
    j = fill_i + hold
    c.events = [Event(bars[j].date, "sell", 1.0, exit_px, "test")]
    c.exit_i, c.exit_date, c.reason = j, bars[j].date, "test"
    c.r = (exit_px - entry) / (entry - stop)
    return c


def test_account_pnl_matches_cash():
    bars = mk(flat_rows(30))
    cal = [b.date for b in bars]
    c = _cand("A", 2, bars=bars)
    closes = {"A": {b.date: 11.0 for b in bars}}
    res = run_account([c], closes, cal, AccountRules(start_equity=10_000, risk_pct=1.0, max_position_pct=100),
                      NOCOST)
    t = res.trades[0]
    assert t.shares == 100                         # $100 risk / $1 per share
    assert t.pnl == pytest.approx(200.0)           # 100 x (12 - 10)
    assert res.equity[-1] == pytest.approx(10_200.0)
    assert t.r == pytest.approx(2.0)


def test_account_respects_max_positions_and_rank():
    bars = mk(flat_rows(30))
    cal = [b.date for b in bars]
    cs = [_cand(f"T{k}", 2, rank=k, bars=bars) for k in range(5)]
    res = run_account(cs, {}, cal, AccountRules(max_positions=2, max_heat_pct=100, max_position_pct=100), NOCOST)
    assert sorted(t.ticker for t in res.trades) == ["T3", "T4"]     # strongest two
    assert res.skipped["max positions"] == 3


def test_account_heat_and_group_caps():
    bars = mk(flat_rows(30))
    cal = [b.date for b in bars]
    cs = [_cand(f"T{k}", 2, rank=k, group="semis", bars=bars) for k in range(4)]
    res = run_account(cs, {}, cal, AccountRules(max_positions=10, max_per_group=2, max_heat_pct=100,
                                                max_position_pct=100), NOCOST)
    assert len(res.trades) == 2 and res.skipped["group cap"] == 2
    cs = [_cand(f"T{k}", 2, rank=k, bars=bars) for k in range(4)]
    res = run_account(cs, {}, cal, AccountRules(risk_pct=1.0, max_positions=10, max_heat_pct=2.5,
                                                max_position_pct=100), NOCOST)
    assert len(res.trades) == 2 and res.skipped["heat cap"] == 2


def test_regime_zero_blocks_and_half_halves():
    bars = mk(flat_rows(30))
    cal = [b.date for b in bars]
    rules = AccountRules(start_equity=10_000, risk_pct=1.0, max_position_pct=100)
    res = run_account([_cand("A", 2, bars=bars)], {}, cal, rules, NOCOST, regime={bars[2].date: 0.0})
    assert not res.trades and res.skipped["regime: no new trades"] == 1
    res = run_account([_cand("A", 2, bars=bars)], {}, cal, rules, NOCOST, regime={bars[2].date: 0.5})
    assert res.trades[0].shares == 50


def test_no_cash_no_trade():
    bars = mk(flat_rows(30))
    cal = [b.date for b in bars]
    c = _cand("A", 2, stop=9.99, bars=bars)   # tiny stop -> huge share count -> capped by cash
    res = run_account([c], {}, cal, AccountRules(start_equity=1_000, risk_pct=1.0, max_position_pct=100), NOCOST)
    assert res.trades[0].shares * 10.0 <= 1_000


# --------------------------------------------------------------------------- metrics + walk-forward

def test_equity_stats_basic():
    eq = [100 * (1.001 ** k) for k in range(253)]
    yrs, cagr, mdd, sharpe = equity_stats([str(k) for k in range(253)], eq)
    assert mdd == 0 and cagr == pytest.approx(1.001 ** 252 - 1, rel=0.01)


def test_yearly_returns():
    d = ["2020-06-01", "2020-12-31", "2021-06-01", "2021-12-31"]
    yr = yearly_returns(d, [100, 110, 121, 99])
    assert yr["2020"] == pytest.approx(0.10) and yr["2021"] == pytest.approx(99 / 110 - 1)


def _fake(dates, eq):
    from collections import Counter
    return AccountResult(dates, eq, [0.0] * len(eq), [], Counter(), AccountRules(), CostModel())


def test_walk_forward_only_uses_the_past():
    dates = [f"{y}-{m:02d}-{d:02d}" for y in (2020, 2021, 2022) for m in range(1, 13) for d in (1, 8, 15, 22)]
    # "early" wins 2020 then collapses; "late" is flat then wins
    early = [100 * (1.01 ** k) if k < 48 else 100 * (1.01 ** 48) * (0.99 ** (k - 47)) for k in range(len(dates))]
    late = [100.0 + (k % 2) * 0.01 if k < 48 else 100 * (1.01 ** (k - 47)) for k in range(len(dates))]
    wf = walk_forward({"early": _fake(dates, early), "late": _fake(dates, late)})
    assert wf.picks["2021"] == "early"            # judged on 2020 only, so it can't know
    assert wf.oos_returns["2021"] < 0             # ...and pays for it out of sample


def test_trade_stats_without_top10():
    from stockscan.lab.portfolio import TradeRecord
    ts = [TradeRecord("X", "t", "", "", f"2020-01-{k:02d}", 1, 1, 1, 1, 0, -1.0, "", 1) for k in range(1, 26)]
    ts += [TradeRecord("Y", "t", "", "", "2020-02-01", 1, 1, 1, 1, 0, 30.0, "", 1)]
    s = trade_stats(ts)
    assert s["expectancy_r"] > 0 and s["expectancy_wo_top10"] < 0


# --------------------------------------------------------------------------- end to end

def test_run_lab_end_to_end_with_fake_data(tmp_path):
    from stockscan.lab.run import run_lab, write_trades_csv, write_summary_json
    from stockscan.lab.report import render

    spy = flat()
    data = {"SPY": spy, "AAA": textbook(True), "BBB": textbook(False), "CCC": flat()}

    def fetch(tks, period="1y", **_):
        return {t: data.get(t, []) for t in tks}

    res = run_lab(["AAA", "BBB", "CCC"], fetch, period="1y")
    text = render(res)
    assert "MODEL LAB" in text and "WALK-FORWARD" in text or "YEAR BY YEAR" in text
    write_trades_csv(res, str(tmp_path / "t.csv"))
    write_summary_json(res, str(tmp_path / "s.json"))
    import json
    json.loads((tmp_path / "s.json").read_text())
