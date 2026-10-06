"""Setup library. Each setup exposes ``signals(ticker, bars, **params) -> list[Signal]``
and must only use bars up to each signal's own bar (tested in tests/test_lab.py)."""

from stockscan.lab.setups import sar_breakout

SETUPS = {
    "sar_breakout": sar_breakout,
}
