"""Evidence cannot fabricate availability, stock quality, or a paper trade."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.screening import providers, store
from app.screening.screen import build, _features

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def memory():
    con = sqlite3.connect(":memory:")
    store.initialise(con)
    return con


def prices(start=100, end=110):
    dates = pd.bdate_range(end="2026-09-30", periods=140)
    c = pd.Series([start+(end-start)*i/139 for i in range(140)], index=dates)
    return pd.DataFrame(dict(open=c, close=c, high=c*1.01, low=c*.99, volume=5_000_000.))


def test_evidence_is_not_available_before_capture_and_expires():
    con = memory()
    store.save(con, "TEST", "news", "NSE", {"events":[]}, NOW.isoformat(), NOW)
    assert store.latest(con, "TEST", "news", NOW-timedelta(seconds=1), 1) is None
    assert store.latest(con, "TEST", "news", NOW+timedelta(days=2), 1) is None
    with pytest.raises(ValueError):
        store.save(con, "TEST", "news", "NSE", {}, (NOW+timedelta(seconds=1)).isoformat(), NOW)
    with pytest.raises(ValueError):
        store.save(con, "TEST", "news", "NSE", {}, "2026-09-30", NOW)


def test_missing_fundamentals_news_and_benchmark_are_not_good_quality():
    con = memory()
    result = build({"TEST":prices()}, {"TEST"}, {}, con, NOW, pd.Timestamp("2026-09-30"))
    row = result["equities"][0]
    assert row["actionable"] is False
    assert row["components"]["quality"] == 0
    assert row["fundamentals"] is None
    assert "official news feed not freshly checked" in row["flags"]
    assert "Nifty benchmark history unavailable" in row["flags"]
    assert row["status"] == "REVIEW REQUIRED"


def test_volume_average_excludes_signal_bar_and_corrupt_price_is_rejected():
    frame = prices()
    frame.loc[frame.index[-1], "volume"] *= 3
    f = _features(frame, frame.index[-1])
    assert f["relative_volume"] == 3
    frame.loc[frame.index[-1], "close"] = float("nan")
    assert _features(frame, frame.index[-1]) is None


def test_current_filing_uses_dissemination_and_only_exchange_urls():
    rows = [dict(symbol="TEST", an_dt="30-Sep-2026 17:29:00",
                 exchdisstime="30-Sep-2026 17:30:00",
                 attchmntFile="https://nsearchives.nseindia.com/corporate/test.pdf",
                 desc="Disclosure", attchmntText="Rating downgrade")]
    news, _ = providers.events(rows, [], NOW)
    assert news["TEST"][0]["classification"] == "risk_review"
    assert news["TEST"][0]["published_at"].endswith("17:30:00+05:30")
    rows[0]["exchdisstime"] = "30-Sep-2026 17:30:01"  # after evaluation time
    assert providers.events(rows, [], NOW)[0] == {}
    rows[0]["exchdisstime"] = "30-Sep-2026 17:30:00"
    rows[0]["attchmntFile"] = "https://nseindia.com.attacker.invalid/fake"
    assert providers.events(rows, [], NOW)[0] == {}


def test_latest_earnings_notice_wins_not_feed_order():
    rows = [dict(bm_symbol="TEST", bm_purpose="Financial Results",
                 bm_timestamp="30-Sep-2026 16:00:00", bm_date="05-Oct-2026"),
            dict(bm_symbol="TEST", bm_purpose="Financial Results",
                 bm_timestamp="29-Sep-2026 16:00:00", bm_date="02-Oct-2026")]
    _, cal = providers.events([], rows, NOW)
    assert cal["TEST"]["date"] == "2026-10-05"


def financial_payload():
    values = {"annualNetIncome":{"2024-03-31":10,"2025-03-31":12,"2026-03-31":15},
              "annualStockholdersEquity":{"2026-03-31":100},
              "annualTotalRevenue":{"2025-03-31":100,"2026-03-31":120},
              "annualTotalDebt":{"2026-03-31":30},
              "annualOperatingCashFlow":{"2026-03-31":18},
              "annualBasicEPS":{"2026-03-31":5}}
    return {"timeseries":{"result":[{name:[dict(asOfDate=d,currencyCode="INR",reportedValue={"raw":v})
                                                for d,v in vals.items()]}
                                     for name,vals in values.items()]}}


def test_same_period_ratios_and_no_invented_year_on_year_growth():
    p = financial_payload()
    f = providers.statements(p, NOW)
    assert f["roe_pct"] == 15
    assert f["debt_equity"] == .3
    assert f["cash_conversion"] == 1.2
    assert f["earnings_growth_pct"] == 25
    p["timeseries"]["result"][0]["annualNetIncome"].pop(1)  # missing previous year
    assert providers.statements(p, NOW)["earnings_growth_pct"] is None
    p["timeseries"]["result"][1]["annualStockholdersEquity"][0]["asOfDate"] = "2025-03-31"
    assert providers.statements(p, NOW)["roe_pct"] is None


def test_currency_and_future_fiscal_periods_are_not_used():
    p = financial_payload()
    for entry in p["timeseries"]["result"][0]["annualNetIncome"]:
        entry["currencyCode"] = "USD"
    with pytest.raises(ValueError): providers.statements(p, NOW)


def test_bank_does_not_get_points_for_industrial_cashflow_or_leverage():
    con = memory()
    f = providers.statements(financial_payload(), NOW)
    store.save(con, "BANK", "fundamentals", "secondary", f, NOW.isoformat(), NOW)
    result = build({"BANK":prices()}, {"BANK"}, {"BANK":"Private Banks"}, con, NOW, pd.Timestamp("2026-09-30"))
    row = result["equities"][0]
    assert row["fundamentals"]["points"] <= 30
    assert "financial-sector asset quality / capital adequacy not verified" in row["flags"]


def test_missing_delivery_and_adverse_news_remain_flags_even_on_strong_stock():
    con = memory()
    store.save(con, "TEST", "news", "NSE", {"events":[{"classification":"risk_review"}]}, NOW.isoformat(), NOW)
    store.save(con, "TEST", "participation", "NSE", {"session":"2026-09-29","delivery_pct":95,"delivery_avg20_pct":40}, NOW.isoformat(), NOW)
    result = build({"TEST":prices(100,150),"NIFTYBEES":prices()}, {"TEST"}, {}, con, NOW, pd.Timestamp("2026-09-30"))
    row=result["equities"][0]
    assert "adverse filing headline; manual review required" in row["flags"]
    assert row["participation"] is None
    assert row["actionable"] is False


def test_future_prices_do_not_change_current_screen():
    con = memory()
    frame = prices()
    base = build({"TEST":frame}, {"TEST"}, {}, con, NOW, frame.index[-1])
    future = pd.DataFrame(dict(open=[10000.],high=[10001.],low=[9999.],close=[10000.],volume=[1e9]),index=[pd.Timestamp("2026-10-01")])
    result = build({"TEST":pd.concat([frame,future])}, {"TEST"}, {}, con, NOW, frame.index[-1])
    assert result["equities"] == base["equities"]


def test_report_marks_old_evidence_stale_and_does_not_open_missing_db(tmp_path):
    path = tmp_path/"evidence.db"
    assert store.report(path, NOW)["status"] == "unavailable"
    assert not path.exists()
    with sqlite3.connect(path) as con:
        store.initialise(con)
        con.execute("INSERT INTO screens VALUES(?,?)",(NOW.isoformat(),json.dumps({"generated_at":NOW.isoformat()})))
    assert store.report(path,NOW+timedelta(hours=3))["stale"] is True


def test_capture_job_cannot_write_into_a_paper_book(tmp_path):
    path=Path(__file__).resolve().parents[1]/"scripts"/"evidence_screen.py"
    spec=importlib.util.spec_from_file_location("evidence_job",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    paper=tmp_path/"renamed.db"
    with sqlite3.connect(paper) as con:
        con.execute("CREATE TABLE v2_positions(symbol TEXT)")
        con.execute("INSERT INTO v2_positions VALUES('UNCHANGED')")
    before=paper.read_bytes()
    with pytest.raises(ValueError,match="non-screening"):
        module.run(SimpleNamespace(output_db=str(paper), market_db=str(tmp_path/"market.db")))
    assert paper.read_bytes()==before


def test_screen_route_and_ideas_share_research_payload(monkeypatch):
    from app import v2_web
    monkeypatch.setattr("app.screening.store.report",lambda path:{"equities":[{"symbol":"TEST","actionable":False}]})
    assert json.loads(v2_web.api_screen("IN", {"id":2}).body)["equities"][0]["actionable"] is False
    assert v2_web._evidence_screen("US")["equities"] == []


def test_foreign_currency_ratios_are_valid_but_inr_pe_is_not_invented():
    con = memory()
    p = financial_payload()
    for block in p['timeseries']['result']:
        for values in block.values():
            for entry in values:
                entry['currencyCode'] = 'USD'
    f = providers.statements(p, NOW)
    assert f['roe_pct'] == 15
    assert f['statement_currency'] == 'USD'
    store.save(con,'TEST','fundamentals','secondary',f,NOW.isoformat(),NOW)
    row=build({'TEST':prices()},{'TEST'}, {}, con,NOW,pd.Timestamp('2026-09-30'))['equities'][0]
    assert row['fundamentals']['price_to_annual_eps'] is None


def test_cached_web_report_expires_option_chain_by_exchange_time(tmp_path):
    path=tmp_path/'evidence.db'
    data=dict(generated_at=NOW.isoformat(),indices=[dict(symbol='NIFTY',options={
        'published_at':NOW.isoformat(),'pcr_oi':1.2}, flags=[])])
    with sqlite3.connect(path) as con:
        store.initialise(con)
        con.execute('INSERT INTO screens VALUES(?,?)',(NOW.isoformat(),json.dumps(data)))
    assert store.report(path,NOW+timedelta(minutes=6))['indices'][0]['options'] is None
