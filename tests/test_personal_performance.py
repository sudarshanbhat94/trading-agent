import sqlite3
import unittest
from app import books,v2_live,personal_performance as perf
from tests.test_release_safety import quotes


class PersonalPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');v2_live.ensure_schema(self.con);self.addCleanup(self.con.close)

    def buy(self,uid=1):
        return books.buy(self.con,uid,'IN','mean_reversion','TEST',100,20,99,110,
                         sleeve='mean_reversion',regime='NEUTRAL',quotes=quotes(TEST=100))

    def test_actual_closed_trade_is_owned_after_cost_and_risk_at_entry(self):
        self.assertEqual(self.buy(),20);self.buy(2)
        initial=books.positions(self.con,1)[0]['risk_amt']
        self.con.execute('UPDATE user_positions SET stop=105 WHERE user_id=1');self.con.commit()
        pnl,_=books.sell(self.con,1,'IN','TEST',110)
        before=self.con.total_changes;r=perf.report(self.con,1,quotes=quotes(TEST=110))
        self.assertEqual(r['daily']['net_pnl'],round(pnl,2))
        self.assertEqual(r['by_sleeve']['mean_reversion']['trades'],1)
        self.assertEqual(r['by_regime']['NEUTRAL']['average_r'],round(pnl/initial,4))
        self.assertEqual(r['by_regime']['OFF']['trades'],0)
        self.assertEqual(perf.report(self.con,2,quotes=quotes(TEST=110))['daily']['trades'],0)
        self.assertEqual(self.con.total_changes,before)

    def test_reset_excludes_old_results_and_missing_r_is_not_zero(self):
        self.buy();books.sell(self.con,1,'IN','TEST',90)
        self.con.execute('UPDATE user_trades SET risk_amt=NULL');self.con.commit()
        self.assertIsNone(perf.report(self.con,1)['daily']['average_r'])
        books.reset_book(self.con,1,'IN');r=perf.report(self.con,1)
        self.assertEqual(r['current_epoch']['trades'],0);self.assertEqual(r['book']['equity'],10000)

    def test_missing_or_stale_open_marks_cannot_masquerade_as_current_equity(self):
        self.buy();r=perf.report(self.con,1)
        self.assertFalse(r['valuation_complete']);self.assertIsNone(r['book']['equity'])
        self.assertIsNotNone(r['book']['cash']);self.assertEqual(r['missing_marks'],['TEST'])
        self.assertTrue(perf.report(self.con,1,quotes=quotes(TEST=100))['valuation_complete'])

    def test_report_rejects_invalid_dates(self):
        with self.assertRaises(ValueError):perf.report(self.con,1,day='invalid')
