"""A research trade must pass the same economic sizing as paper."""
from __future__ import annotations

import pandas as pd

from app.sleeves.risk import stop_loss_including_costs
from scripts.research_stock_replay import _funded_entry


def _bars(price):
    return pd.DataFrame({"open": [price]},
                        index=[pd.Timestamp("2026-09-23")])


def test_gross_stop_affordable_but_after_cost_stop_is_not():
    day = pd.Timestamp("2026-09-23")
    # Old replay allowed 1 share: Rs 136 price risk is below Rs 150.
    assert 2179 - 2043.4 < 150
    assert stop_loss_including_costs(2179, 2043.4, 1) > 150
    assert _funded_entry([(1., "EXPENSIVE", 2179, (2179-2043.4)/3)],
                         {"EXPENSIVE": _bars(2179)}, day, 10_000, 10_000,
                         10_000) is None


def test_replay_tries_next_ranked_name_if_top_is_unfundable():
    day = pd.Timestamp("2026-09-23")
    rows = [(2., "EXPENSIVE", 2179, (2179-2043.4)/3),
            (1., "AFFORDABLE", 250, 8/3)]
    funded = _funded_entry(rows, {"EXPENSIVE": _bars(2179),
                                  "AFFORDABLE": _bars(250)}, day,
                           10_000, 10_000, 10_000)
    assert funded is not None
    assert funded[0] == "AFFORDABLE"
    assert stop_loss_including_costs(funded[1], funded[2], funded[3]) <= 150
