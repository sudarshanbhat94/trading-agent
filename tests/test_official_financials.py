from datetime import datetime,timezone
from pathlib import Path
import tempfile
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
            self.assertEqual(result['collector_version'],official.BANK_COLLECTOR_VERSION)
            self.assertEqual(result['contract_version'],'nse-bank-ratios-v1')
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

    def response(self,payload):
        return Mock(content=b'{}',json=Mock(return_value=payload))

    def test_paginated_index_collects_all_rows_with_one_based_requests(self):
        client=Mock()
        client.get.side_effect=[self.response(dict(data=[{'id':1},{'id':2}],totalCount=3)),
                                self.response(dict(data=[{'id':3}],totalCount=3))]
        rows=official._rows(client,'STOCK',NOW,'integrated-filing-results')
        self.assertEqual(rows,[{'id':1},{'id':2},{'id':3}])
        self.assertEqual([c.kwargs['params']['page'] for c in client.get.call_args_list],[1,2])
        self.assertTrue(all(c.kwargs['follow_redirects'] is False for c in client.get.call_args_list))

    def test_ignored_pagination_changed_count_and_page_bound_refuse(self):
        first=dict(data=[{'id':1}],totalCount=2)
        for second in (first,dict(data=[{'id':2}],totalCount=3)):
            client=Mock();client.get.side_effect=[self.response(first),self.response(second)]
            with self.assertRaises(ValueError):official._rows(client,'STOCK',NOW,'integrated-filing-results')
        client=Mock();client.get.return_value=self.response(first)
        with patch.object(official,'MAX_INDEX_PAGES',1),self.assertRaisesRegex(ValueError,'page bound'):
            official._rows(client,'STOCK',NOW,'integrated-filing-results')

    def test_complete_list_index_needs_no_pagination(self):
        client=Mock();client.get.return_value=self.response([{'id':1}])
        self.assertEqual(official._rows(client,'STOCK',NOW,'corporates-financial-results'),[{'id':1}])
        self.assertEqual(client.get.call_count,1)

    def test_integrated_related_party_size_and_official_cet1_tag(self):
        # Integrated bank filings can exceed the old 2 MiB bound.
        raw=xml('<f:CET1Ratio contextRef="D" unitRef="pure">0.1887</f:CET1Ratio>')
        raw=raw.replace(b'</x:xbrl>',b'<!--'+b' '*(3*1024*1024)+b'--></x:xbrl>')
        result=official.document(raw,'STOCK')
        self.assertEqual(result['bank'],[dict(metric='cet1_pct',period_end='2026-03-31',value_pct=18.87)])
        with self.assertRaisesRegex(ValueError,'oversized'):
            official.document(b' '*(official.MAX_XML_BYTES+1),'STOCK')

    def test_bank_collection_skips_consolidated_revisions_before_document_bound(self):
        base='https://nsearchives.nseindia.com/corporate/xbrl/'
        rows=[dict(symbol='STOCK',type='Integrated Filing- Financials',consolidated='Consolidated',
                   xbrl=base+str(i)+'.xml',broadcast_Date='10-Oct-2026 10:00:00') for i in range(3)]
        rows.append(dict(symbol='STOCK',type='Integrated Filing- Financials',consolidated='Standalone',
                         xbrl=base+'standalone.xml',broadcast_Date='09-Oct-2026 10:00:00'))
        raw=xml('<f:PercentageOfGrossNpa contextRef="D" unitRef="pure">0.0111</f:PercentageOfGrossNpa>')
        client=Mock();client.get.return_value=Mock(content=raw)
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'SCREENING_DB':str(Path(tmp)/'screen.db')}),patch.object(official,'_rows',return_value=rows):
            docs=official.filings(client,'STOCK',NOW,bank_only=True)
            self.assertEqual(len(list((Path(tmp)/'financial-source').glob('*.xml'))),1)
        self.assertEqual(len(docs),1)
        self.assertEqual(docs[0]['source'],base+'standalone.xml')
        self.assertEqual(client.get.call_count,1)

    def test_bank_revision_uses_one_newest_snapshot_without_filling_missing_capital(self):
        old=dict(basis='Standalone',source='old',sha256='a'*64,published_at='2026-10-08T10:00:00+00:00',bank=[
            dict(metric='gross_npa_pct',period_end='2026-06-30',value_pct=2),
            dict(metric='capital_adequacy_pct',period_end='2026-06-30',value_pct=17)])
        new=dict(basis='Standalone',source='new',sha256='b'*64,published_at='2026-10-09T10:00:00+00:00',bank=[
            dict(metric='gross_npa_pct',period_end='2026-06-30',value_pct=1.1),
            dict(metric='net_npa_pct',period_end='2026-06-30',value_pct=.3)])
        with patch.object(official,'filings',return_value=[old,new]):
            result=official.fetch_bank_metrics(Mock(),'STOCK',NOW)
        self.assertEqual(result['gross_npa_pct'],1.1)
        self.assertFalse(result['complete'])
        self.assertNotIn('capital_adequacy_pct',result)
        self.assertEqual(result['source_filings'],['new'])
        self.assertEqual(result['source_evidence'],[{k:new[k] for k in ('source','sha256','published_at')}])

    def test_equal_publication_bank_conflicts_are_not_arbitrarily_selected(self):
        doc=dict(basis='Standalone',source='source',published_at=NOW.isoformat(),bank=[
            dict(metric='gross_npa_pct',period_end='2026-06-30',value_pct=1)])
        other=dict(doc,bank=[dict(metric='gross_npa_pct',period_end='2026-06-30',value_pct=2)])
        with patch.object(official,'filings',return_value=[doc,other]),self.assertRaisesRegex(ValueError,'Conflicting'):
            official.fetch_bank_metrics(Mock(),'STOCK',NOW)
