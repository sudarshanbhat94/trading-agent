"""No quota filler, misleading reward or retrospective cohort replacement."""
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.screening import confirmation, plans, selection, tracking, validation
from tests.test_stock_plans import screen, book, NOW


class SelectiveIdeasTest(unittest.TestCase):
    def result(self, data=None, **kwargs):
        return plans.shortlist(data or screen(1), book(), now=NOW, **kwargs)

    def test_cap_cannot_be_raised_into_a_quota_and_short_sessions_are_valid(self):
        self.assertEqual(self.result(screen(12), limit=100)['count'], 3)
        self.assertEqual(self.result()['count'], 1)
        self.assertEqual(self.result(screen(0))['count'], 0)
        self.assertEqual(self.result()['requested'], 0)
        self.assertEqual(self.result()['selection_policy']['min_ideas'], 0)

    def test_high_score_cannot_offset_weak_or_unknown_evidence(self):
        changes = [('fundamentals', 'roe_pct', 14.9), ('fundamentals', 'roe_pct', None),
            ('fundamentals', 'debt_equity', 1.01), ('fundamentals', 'cash_conversion', .49),
            ('fundamentals', 'positive_earnings_years', 2), ('fundamentals', 'earnings_years', 2),
            ('fundamentals', 'annual_income', -1), ('fundamentals', 'earnings_growth_pct', -1),
            ('fundamentals', 'profit_margin_pct', 0), ('fundamentals', 'roe_pct', True),
            ('metrics', 'relative_volume', 1.49), ('metrics', 'relative_volume', float('nan')),
            ('metrics', 'relative_volume', None), ('metrics', 'turnover', 249_999_999),
            ('metrics', 'rs_vs_nifty20_pct', float('inf')), ('metrics', 'return126_pct', float('nan')),
            ('metrics', 'setup', 'no controlled entry setup'),
            ('participation', 'delivery_pct', 50), ('participation', 'delivery_pct', 101),
            ('participation', 'delivery_avg20_pct', None), ('participation', 'session', '2026-09-29')]
        for group, key, value in changes:
            with self.subTest(group=group, key=key, value=value):
                data = screen(1); data['equities'][0]['score'] = 100
                data['equities'][0][group][key] = value
                result = self.result(data)
                self.assertEqual(result['count'], 0)
                self.assertTrue(result['rejected'][0]['reason'])

    def test_stale_screen_or_invalidated_live_plan_is_not_a_new_idea(self):
        for flag in ('stale', 'price_stale'):
            data = screen(1); data[flag] = True
            self.assertEqual(self.result(data)['count'], 0)
        for price in (750, 730):
            result = self.result(quotes={'STOCK0':dict(price=price, ts=NOW.isoformat())})
            self.assertEqual(result['count'], 0)
            self.assertIn('invalidates', result['rejected'][0]['reason'])

    def test_lower_score_in_another_sector_beats_concentration(self):
        data = screen(4); data['equities'][1]['sector'] = data['equities'][0]['sector']
        result = self.result(data)
        self.assertEqual([p['symbol'] for p in result['ideas']], ['STOCK0','STOCK2','STOCK3'])
        self.assertIn('sector', result['rejected'][0]['reason'])

    def test_full_exit_reward_must_cover_modeled_stop_loss_without_raising_targets(self):
        data = screen(1); data['equities'][0]['metrics']['atr_pct'] = 3
        result = self.result(data)
        self.assertEqual(result['count'], 0)
        self.assertIn('Final-target net reward', result['rejected'][0]['reason'])
        p = self.result()['ideas'][0]
        self.assertGreaterEqual(plans._net(p['entry_high'],p['t3'],p['qty']),p['estimated_stop_loss'])
        self.assertEqual((p['stop'],p['t1'],p['t2'],p['t3']), (736,832,864,896))
        self.assertEqual(p['model_version'], selection.MODEL_VERSION)
        self.assertFalse(p['actionable'])

    def test_unchanged_refresh_keeps_original_publication_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'tracker.db'
            original = self.result()['ideas'][0]
            old = dict(original, model_version='conditional-pullback-v2')
            old.pop('selection_policy')
            legacy = tracking.publish(path,2,[old],now=NOW)
            current = tracking.publish(path,2,[original],now=NOW)
            again = tracking.publish(path,2,[dict(original,score=99)],now=NOW+timedelta(days=1))
            self.assertEqual(current,again)
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM publications').fetchone()[0],2)
            pid = next(iter(current.values()))
            frozen = tracking.publication(path,2,pid)
            self.assertEqual(frozen['issued_at'],NOW.isoformat())
            self.assertEqual(frozen['plan']['score'],original['score'])
            self.assertIsNotNone(tracking.publication(path,2,next(iter(legacy.values()))))
            self.assertIsNone(tracking.publication(path,3,pid))

    def test_new_selection_does_not_enter_or_rewrite_registered_v2_cohort(self):
        from tests.test_idea_validation import fixture
        protocol, publication, decisions, quote = fixture()
        original = json.dumps(protocol,sort_keys=True)
        new = (2,publication[1],dict(publication[2],model_version=selection.MODEL_VERSION))
        end = quote(30,99.5)[1]
        before = validation.replay([publication],[quote(30,99.5)],decisions,protocol,end)
        after = validation.replay([publication,new],[quote(30,99.5)],
            decisions+[(2,publication[1],copy.deepcopy(decisions[0][2]))],protocol,end)
        self.assertEqual(before,after)
        self.assertEqual(json.dumps(protocol,sort_keys=True),original)

    def test_v3_observes_same_confirmation_without_becoming_an_approved_order(self):
        from tests.test_idea_hardening import confirmation_fixture
        p,state,bars,quote,context,now = confirmation_fixture()
        p.update(model_version=selection.MODEL_VERSION,selection_policy=dict(selection.POLICY))
        context['selection_ok'] = True
        first = confirmation.assess(p,state,bars,quote,context,now)
        self.assertTrue(next(c for c in first['checks'] if c['code']=='model')['passed'])
        self.assertFalse(first['eligible'])  # No retroactive confirmation fill.
        quote['ts'] = (now+timedelta(seconds=30)).isoformat()
        later = confirmation.assess(p,state,bars,quote,context,now+timedelta(seconds=30),first)
        self.assertTrue(later['eligible'])
        self.assertFalse(later['production_approved'])
        context.update(selection_ok=False,selection_reason='Delivery confirmation deteriorated')
        weak = confirmation.assess(p,state,bars,quote,context,now+timedelta(seconds=30),first)
        self.assertFalse(weak['eligible'])
        self.assertFalse(weak['baseline_eligible'])
        self.assertEqual(next(c for c in weak['checks'] if c['code']=='selection')['reason'],context['selection_reason'])
