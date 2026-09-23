"""The research ledger must not manufacture a trade at an already-passed open."""
import sqlite3
from types import SimpleNamespace

import pandas as pd

from app.sleeves import forward_watch


def _result(watch=True):
    decision = SimpleNamespace(sleeve="quality_momentum", diagnostics={
        "watch": [dict(symbol="TEST", score=.8)] if watch else []})
    return SimpleNamespace(decisions=[decision], regime=SimpleNamespace(state="OFF"))


def test_forward_watch_waits_for_next_open_and_charges_real_costs(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=27)
    frame = pd.DataFrame({"open": 100.0, "close": 100.0}, index=dates)
    path = str(tmp_path / "research.db")
    observed_on = str(dates[1].date())
    forward_watch.update(_result(), {"TEST": frame.loc[:dates[0]]}, dates[0],
                         observed_on, path)
    forward_watch.update(_result(), {"TEST": frame.loc[:dates[0]]}, dates[0],
                         observed_on, path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM quality_forward").fetchone()[0] == 1
        assert con.execute("SELECT entry_on FROM quality_forward").fetchone()[0] is None

    # A future-loaded frame must not let a current-day report see later bars.
    forward_watch.update(_result(), {"TEST": frame}, dates[0], observed_on, path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT entry_on,net5_pct FROM quality_forward").fetchone() == (
            None, None)
    forward_watch.update(_result(False), {"TEST": frame}, dates[5],
                         str(dates[6].date()), path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT net5_pct,net20_pct FROM quality_forward").fetchone() == (
            None, None)  # only four complete sessions after the observation
    forward_watch.update(_result(False), {"TEST": frame}, dates[21],
                         str(dates[22].date()), path)
    with sqlite3.connect(path) as con:
        entry_on, qty, net5, net20, status = con.execute(
            "SELECT entry_on,qty,net5_pct,net20_pct,status FROM quality_forward").fetchone()
    assert entry_on == str(dates[2].date())  # after the observation, never before
    assert qty == 29  # Rs 3,000 / slippage-adjusted Rs 100.20
    assert net5 < -2.0 and net20 < -2.0  # flat stock still loses fees and slippage
    assert status == "complete"
    assert forward_watch.summary(path) == [dict(
        regime="OFF", observed=1, matured=1, winners=0, avg_net20_pct=net20)]


def test_changed_historical_close_requires_corporate_action_review(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=27)
    frame = pd.DataFrame({"open": 100.0, "close": 100.0}, index=dates)
    path = str(tmp_path / "research.db")
    observed_on = str(dates[1].date())
    forward_watch.update(_result(), {"TEST": frame.loc[:dates[0]]}, dates[0],
                         observed_on, path)
    adjusted = frame.copy()
    adjusted.loc[dates[0], "close"] = 20.0
    forward_watch.update(_result(), {"TEST": adjusted}, dates[0], observed_on, path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT status,net20_pct FROM quality_forward").fetchone() == (
            "corporate_action_review", None)
