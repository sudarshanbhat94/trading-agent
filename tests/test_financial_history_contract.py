"""Research fundamentals cannot manufacture stability from missing years."""
import copy
import json
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.screening import providers, plans
from app.screening.financials import valid_income_history
from app.screening.screen import _quality
from tests.test_evidence_screen import financial_payload, NOW
from tests.test_stock_plans import screen, book


class FinancialHistoryContractTest(unittest.TestCase):
    def payload(self):return financial_payload()

    def statement(self,payload=None):return providers.statements(payload or self.payload(),NOW)

    def test_history_reports_actual_dated_contiguous_comparable_years(self):
        f=self.statement()
        self.assertTrue(valid_income_history(f))
        self.assertEqual([r['period_end'] for r in f['earnings_periods']],['2024-03-31','2025-03-31','2026-03-31'])
        self.assertEqual((f['earnings_years'],f['positive_earnings_years']),(3,3))
        self.assertEqual(f['roe_pct'],15)
        self.assertEqual(f['financial_contract_version'],'annual-statements-v2')

    def test_disconnected_profitable_years_cannot_be_called_consistent_history(self):
        p=self.payload();p['timeseries']['result'][0]['annualNetIncome'][0]['asOfDate']='2020-03-31'
        f=self.statement(p)
        self.assertEqual((f['earnings_years'],f['positive_earnings_years']),(2,2))
        self.assertEqual(f['excluded_earnings_periods'],['2020-03-31'])
        self.assertIsNone(f['earnings_growth_std_pct'])
        _,flags=_quality(f,'Industrials',800)
        self.assertIn('incomplete profitability / earnings history',flags)

    def test_gap_at_previous_year_retains_only_latest_known_year(self):
        p=self.payload();p['timeseries']['result'][0]['annualNetIncome'].pop(1)
        f=self.statement(p)
        self.assertEqual(f['earnings_years'],1)
        self.assertIsNone(f['earnings_growth_pct'])
        self.assertTrue(valid_income_history(f))  # Honest single-year coverage, not three years.

    def test_split_blocks_merge_without_losing_earlier_annual_records(self):
        p=self.payload();points=p['timeseries']['result'][0]['annualNetIncome']
        p['timeseries']['result'][0]['annualNetIncome']=points[:2]
        p['timeseries']['result'].append(dict(annualNetIncome=points[2:]))
        self.assertEqual(self.statement(p),self.statement())
        p['timeseries']['result'].reverse()
        self.assertEqual(self.statement(p),self.statement())

    def test_exact_duplicate_rows_are_harmless_without_inflating_year_counts(self):
        p=self.payload();p['timeseries']['result'].append(copy.deepcopy(p['timeseries']['result'][0]))
        p['timeseries']['result'][0]['annualNetIncome'].append(copy.deepcopy(p['timeseries']['result'][0]['annualNetIncome'][-1]))
        self.assertEqual(self.statement(p),self.statement())

    def test_conflicting_same_period_amounts_or_currencies_are_not_selected_by_order(self):
        for currency in (False,True):
            for reversed_rows in (False,True):
                with self.subTest(currency=currency,reversed_rows=reversed_rows):
                    p=self.payload();other=copy.deepcopy(p['timeseries']['result'][0])
                    point=other['annualNetIncome'][-1]
                    if currency:point['currencyCode']='USD'
                    else:point['reportedValue']['raw']=1500
                    p['timeseries']['result'].append(other)
                    if reversed_rows:p['timeseries']['result'].reverse()
                    with self.assertRaisesRegex(ValueError,'Conflicting'):
                        self.statement(p)

    def test_conflicting_duplicate_inside_a_block_is_not_last_value_wins(self):
        p=self.payload();points=p['timeseries']['result'][0]['annualNetIncome']
        changed=copy.deepcopy(points[-1]);changed['reportedValue']['raw']=1500;points.append(changed)
        with self.assertRaisesRegex(ValueError,'Conflicting'):self.statement(p)

    def test_invalid_period_or_duration_cannot_fabricate_an_annual_point(self):
        for field,value in [('asOfDate','2026-02-30'),('asOfDate','2026-3-31'),
                             ('asOfDate','2026-03-31T00:00:00Z'),('periodType','3M')]:
            with self.subTest(field=field,value=value):
                p=self.payload();p['timeseries']['result'][0]['annualNetIncome'][-1][field]=value
                with self.assertRaises(ValueError):self.statement(p)

    def test_booleans_nonfinite_amounts_and_malformed_provider_shapes_fail_closed(self):
        for value in (True,float('nan'),float('inf'),None):
            with self.subTest(value=value):
                p=self.payload();p['timeseries']['result'][0]['annualNetIncome'][-1]['reportedValue']['raw']=value
                with self.assertRaises(ValueError):self.statement(p)
        for p in ({'timeseries':{'result':{}}},{'timeseries':{'result':[None]}},
                  {'timeseries':{'result':[{'annualNetIncome':{}}]}},
                  {'timeseries':{'result':self.payload()['timeseries']['result'],'error':{'code':'unavailable'}}}):
            with self.subTest(payload=p),self.assertRaises(ValueError):self.statement(p)

    def test_future_fiscal_point_cannot_change_current_ratios_or_history(self):
        p=self.payload();point=copy.deepcopy(p['timeseries']['result'][0]['annualNetIncome'][-1])
        point['asOfDate']='2027-03-31';point['reportedValue']['raw']=1500
        p['timeseries']['result'][0]['annualNetIncome'].append(point)
        self.assertEqual(self.statement(p),self.statement())
        with self.assertRaises(ValueError):providers.statements(self.payload(),NOW.replace(tzinfo=None))

    def test_cached_history_still_cannot_contain_future_or_expired_periods(self):
        data=screen(1);f=data['equities'][0]['fundamentals']
        for day in ('2026-03-30','2027-10-03'):
            with self.subTest(day=day):
                data['price_asof']=day
                self.assertFalse(valid_income_history(f,day))
                self.assertEqual(plans.shortlist(data,book(),now=NOW)['count'],0)
        data['price_asof']=None
        self.assertEqual(plans.shortlist(data,book(),now=NOW)['count'],0)

    def test_extreme_inputs_do_not_return_nonfinite_quality_ratios(self):
        p=self.payload();p['timeseries']['result'][1]['annualStockholdersEquity'][0]['reportedValue']['raw']=1e-308
        with self.assertRaisesRegex(ValueError,'finite'):self.statement(p)

    def test_foreign_currency_years_cannot_be_reused_as_comparable_inr_history(self):
        p=self.payload();p['timeseries']['result'][0]['annualNetIncome'][0]['currencyCode']='USD'
        self.assertEqual(self.statement(p)['earnings_years'],2)

    def test_counts_without_dates_cannot_admit_a_high_score_idea(self):
        data=screen(1);f=data['equities'][0]['fundamentals'];del f['earnings_periods']
        self.assertFalse(valid_income_history(f))
        result=plans.shortlist(data,book(),now=NOW)
        self.assertEqual(result['count'],0)
        self.assertIn('counts alone',result['rejected'][0]['reason'])
        _,flags=_quality(f,'Industrials',800)
        self.assertIn('consecutive annual earnings history unavailable',flags)

    def test_tampered_history_currency_income_dates_or_counts_cannot_admit_a_plan(self):
        for change in ('duplicate','gap','currency','income','count','period_end'):
            with self.subTest(change=change):
                data=screen(1);f=data['equities'][0]['fundamentals'];rows=f['earnings_periods']
                if change=='duplicate':rows[0]['period_end']=rows[1]['period_end']
                elif change=='gap':rows[0]['period_end']='2020-03-31'
                elif change=='currency':rows[0]['statement_currency']='USD'
                elif change=='income':rows[-1]['annual_income']=1500
                elif change=='count':f['positive_earnings_years']=99
                else:f['period_end']='2025-03-31'
                self.assertFalse(valid_income_history(f))
                self.assertEqual(plans.shortlist(data,book(),now=NOW)['count'],0)

    def test_refresh_job_recollects_legacy_caches_without_looping_on_honest_short_history(self):
        from scripts.evidence_screen import statement_due
        f=self.statement();self.assertFalse(statement_due(f,'2026-09-30'))
        old=copy.deepcopy(f);old.pop('financial_contract_version')
        self.assertTrue(statement_due(old,'2026-09-30'))
        old=copy.deepcopy(f);old.pop('earnings_periods')
        self.assertTrue(statement_due(old,'2026-09-30'))
        self.assertTrue(statement_due(None,'2026-09-30'))
        p=self.payload();p['timeseries']['result'][0]['annualNetIncome'].pop(1)
        incomplete=self.statement(p)
        self.assertEqual(incomplete['earnings_years'],1)
        self.assertFalse(statement_due(incomplete,'2026-09-30'))
        self.assertTrue(statement_due(f,'2027-10-03'))

    def test_cached_api_flags_undated_history_without_rewriting_original_screen(self):
        from app.screening import store
        data=screen(1);data['equities'][0]['fundamentals'].pop('earnings_periods')
        row=data['equities'][0]
        row.update(status='RESEARCH',news=dict(known_at=NOW.isoformat(),checked_at=NOW.isoformat(),events=[]),
                   earnings=dict(known_at=NOW.isoformat(),date=None))
        row['fundamentals']['known_at']=NOW.isoformat()
        original=json.dumps(data)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'screening.db'
            with sqlite3.connect(path) as con:
                store.initialise(con);con.execute('INSERT INTO screens VALUES(?,?)',(NOW.isoformat(),original))
            result=store.report(path,NOW)
            self.assertEqual(result['equities'][0]['flags'],['consecutive annual earnings history unavailable'])
            self.assertEqual(result['equities'][0]['status'],'REVIEW REQUIRED')
            self.assertEqual(plans.shortlist(result,book(),now=NOW)['count'],0)
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute('SELECT payload FROM screens').fetchone()[0],original)

    def test_decision_day_expiry_cannot_hide_behind_previous_session_prices(self):
        data=screen(1);f=data['equities'][0]['fundamentals']
        from app.screening.financials import fiscal_date
        from datetime import datetime, timezone
        last=fiscal_date(f['period_end'])
        previous=last+timedelta(days=550);decision=datetime.combine(previous+timedelta(days=1),datetime.min.time(),timezone.utc)
        data['price_asof']=previous.isoformat();data['equities'][0]['participation']['session']=data['price_asof']
        self.assertTrue(valid_income_history(f,data['price_asof']))
        result=plans.shortlist(data,book(),now=decision)
        self.assertEqual(result['count'],0)
        self.assertIn('decision time',result['rejected'][0]['reason'])
