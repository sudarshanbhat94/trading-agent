"""The research ledger must not manufacture a trade at an already-passed open."""
import sqlite3
from types import SimpleNamespace

import pandas as pd

from app.sleeves import forward_watch


def _result(watch=True, regime="ON", gate_open=True):
    decision = SimpleNamespace(sleeve="quality_momentum", diagnostics={
        "screen_gate_open": gate_open,
        "watch": [dict(symbol="TEST", score=.8, planned_stop=99.0,
                       fresh_book_risk_cap=150.0,
                       fresh_book_notional_cap=3000.0,
                       min_ticket=1500.0)] if watch else []})
    return SimpleNamespace(decisions=[decision], regime=SimpleNamespace(state=regime))


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
    assert qty == 29  # entry slippage counts toward the Rs 3,000 sleeve cap
    assert net5 < -2.0 and net20 < -2.0  # flat stock still loses fees and slippage
    assert status == "complete"
    assert forward_watch.summary(path) == [dict(
        regime="ON", observed=1, entry_blocked=0, unfillable=0, legacy_unverified=0,
        matured=1, winners=0, avg_net20_pct=net20)]


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


def test_expensive_stop_is_not_scored_as_a_winning_trade(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=27)
    frame = pd.DataFrame({"open": 2179.0, "close": 2500.0}, index=dates)
    frame.loc[dates[0], "close"] = 2179.0
    result = _result()
    result.decisions[0].diagnostics["watch"][0].update(
        planned_stop=2043.4, fresh_book_notional_cap=3000.0)
    path = str(tmp_path / "research.db")
    forward_watch.update(result, {"TEST": frame.loc[:dates[0]]}, dates[0],
                         str(dates[1].date()), path)
    forward_watch.update(_result(False), {"TEST": frame}, dates[21],
                         str(dates[22].date()), path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT status,qty,net20_pct FROM quality_forward").fetchone() == (
            "unfillable", None, None)
    assert forward_watch.summary(path)[0]["unfillable"] == 1
    assert forward_watch.summary(path)[0]["matured"] == 0


def test_off_regime_watch_cannot_be_reported_as_a_trade(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=27)
    frame = pd.DataFrame({"open": 100.0, "close": 150.0}, index=dates)
    frame.loc[dates[0], "close"] = 100.0
    path = str(tmp_path / "research.db")
    forward_watch.update(_result(regime="OFF"), {"TEST": frame.loc[:dates[0]]},
                         dates[0], str(dates[1].date()), path)
    forward_watch.update(_result(False), {"TEST": frame}, dates[21],
                         str(dates[22].date()), path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT status,net20_pct FROM quality_forward").fetchone() == (
            "entry_blocked", None)
    assert forward_watch.summary(path)[0]["entry_blocked"] == 1
    assert forward_watch.summary(path)[0]["matured"] == 0


def test_on_regime_without_strong_confirmation_is_also_blocked(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=27)
    frame = pd.DataFrame({"open": 100.0, "close": 150.0}, index=dates)
    frame.loc[dates[0], "close"] = 100.0
    path = str(tmp_path / "research.db")
    forward_watch.update(_result(gate_open=False), {"TEST": frame.loc[:dates[0]]},
                         dates[0], str(dates[1].date()), path)
    forward_watch.update(_result(False), {"TEST": frame}, dates[21],
                         str(dates[22].date()), path)
    assert forward_watch.summary(path)[0]["entry_blocked"] == 1
    assert forward_watch.summary(path)[0]["matured"] == 0


def test_old_fixed_ticket_rows_are_retained_but_excluded(tmp_path):
    path = str(tmp_path / "research.db")
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE quality_forward(observed_on TEXT, signal_asof TEXT, "
                    "symbol TEXT, regime TEXT, score REAL, reference_close REAL, "
                    "entry_on TEXT, entry_price REAL, qty INTEGER, net5_pct REAL, "
                    "net20_pct REAL, status TEXT, PRIMARY KEY(observed_on,symbol))")
        con.execute("INSERT INTO quality_forward VALUES(" + ",".join("?" * 12) + ")",
                    ("2026-01-01", "2025-12-31", "OLD", "OFF", .8, 100.,
                     "2026-01-02", 100., 29, 4., 8., "complete"))
    forward_watch.update(_result(False), {}, pd.Timestamp("2026-09-29"),
                         "2026-09-30", path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT status,net20_pct FROM quality_forward").fetchone() == (
            "legacy_unverified", 8.)
    assert forward_watch.summary(path)[0]["matured"] == 0
