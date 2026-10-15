from datetime import datetime,timezone
import unittest
from unittest.mock import Mock,patch

from app.screening import official_financials as official,providers

NOW=datetime(2026,10,11,10,tzinfo=timezone.utc)


def xml(extra='',symbol='STOCK',start='2025-04-01',end='2026-03-31'):
    return ('''<x:xbrl xmlns:x="http://www.xbrl.org/2003/instance" xmlns:f="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt">
      <x:context id="D"><x:entity><x:identifier scheme="official">1234</x:identifier></x:entity><x:period><x:startDate>'''+start+'''</x:startDate><x:endDate>'''+end+'''</x:endDate></x:period></x:context>
      <x:unit id="INR"><x:measure>iso4217:INR</x:measure></x:unit><x:unit id="pure"><x:measure>xbrli:pure</x:measure></x:unit>
      <f:Symbol contextRef="D">'''+symbol+'''</f:Symbol><f:NatureOfReportStandaloneConsolidated contextRef="D">Standalone</f:NatureOfReportStandaloneConsolidated>
      <f:ProfitLossForPeriod contextRef="D" unitRef="INR">1000</f:ProfitLossForPeriod>'''+extra+'''</x:xbrl>''').encode()


class OfficialFinancialTest(unittest.TestCase):
    def test_explicit_whole_year_income_and_unit(self):
        r=official.document(xml(),'STOCK')
        self.assertEqual(r['basis'],'Standalone')
        self.assertEqual(r['series']['annualNetIncome'],{'2026-03-31':1000})

    def test_quarter_or_fifteen_months_are_not_annualized(self):
        for start in ('2026-01-01','2025-01-01'):
            r=official.document(xml(start=start),'STOCK')
            self.assertNotIn('annualNetIncome',r['series'])

    def test_bank_fraction_is_converted_to_percentage_once(self):
        r=official.document(xml('<f:PercentageOfGrossNpa contextRef="D" unitRef="pure">0.0133</f:PercentageOfGrossNpa>'),'STOCK')
        self.assertAlmostEqual(r['bank'][0]['value_pct'],1.33)

    def test_identity_dtd_conflicting_values_and_unknown_units_cannot_pass(self):
        conflict='<f:ProfitLossForPeriod contextRef="D" unitRef="INR">2000</f:ProfitLossForPeriod>'
        for raw in (xml(symbol='OTHER'),b'<!DOCTYPE x>'+xml(),xml(conflict)):
            with self.assertRaises(ValueError):official.document(raw,'STOCK')
        r=official.document(xml().replace(b'iso4217:INR',b'iso4217:USD'),'STOCK')
        self.assertNotIn('annualNetIncome',r['series'])

    def test_fallback_runs_same_normalizer_without_mixing_bases(self):
        docs=[]
        for year in (2026,2025,2024):
            docs.append(dict(basis='Standalone',series={'annualNetIncome':{str(year)+'-03-31':1000}},bank=[],nonannual_periods=[],source='https://nsearchives.nseindia.com/corporate/xbrl/a.xml',sha256='a'*64,published_at=NOW.isoformat()))
        docs.insert(0,dict(docs[0],basis='Consolidated',series={'annualNetIncome':{'2026-03-31':9000}}))
        with patch.object(official,'filings',return_value=docs):r=official.fetch_statements(Mock(),'STOCK',NOW)
        self.assertEqual((r['annual_income'],r['earnings_years']),(1000,3))
        self.assertEqual(r['source_name'],'NSE official financial XBRL')

    def test_stale_secondary_response_uses_primary_fallback(self):
        client=Mock();client.get.return_value.json.return_value={}
        with patch.object(official,'fetch_statements',return_value={'source_name':'official'}) as fallback:
            self.assertEqual(providers.fetch_statements(client,'STOCK',NOW),{'source_name':'official'})
        fallback.assert_called_once()

    def test_bank_missing_capital_ratio_stays_incomplete_and_stale_ratios_refuse(self):
        doc=dict(basis='Standalone',source='official',bank=[
            dict(metric='gross_npa_pct',value_pct=1.17,period_end='2026-06-30'),
            dict(metric='net_npa_pct',value_pct=.41,period_end='2026-06-30')])
        with patch.object(official,'filings',return_value=[doc]):
            result=official.fetch_bank_metrics(Mock(),'BANK',NOW)
            self.assertFalse(result['complete'])
            self.assertNotIn('capital_adequacy_pct',result)
        for point in doc['bank']:point['period_end']='2025-03-31'
        with patch.object(official,'filings',return_value=[doc]),self.assertRaisesRegex(ValueError,'stale'):
            official.fetch_bank_metrics(Mock(),'BANK',NOW)

    def test_segment_context_cannot_supply_whole_company_earnings(self):
        raw=xml().replace(b'<x:entity>',b'<x:entity><segment><explicitMember>division</explicitMember></segment>')
        self.assertNotIn('annualNetIncome',official.document(raw,'STOCK')['series'])

    def test_incomplete_filings_index_refuses_instead_of_claiming_coverage(self):
        client=Mock();client.get.return_value.content=b'{}'
        client.get.return_value.json.return_value={'data':[],'totalCount':20}
        with self.assertRaisesRegex(ValueError,'incomplete'):
            official._rows(client,'STOCK',NOW,'integrated-filing-results')
