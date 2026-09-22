import unittest
import numpy as np
import pandas as pd
from scripts.research_index_baselines import desired,replay


class ResearchBaselineTest(unittest.TestCase):
    def test_positive_momentum_emits_native_boolean_and_executes(self):
        ix=pd.bdate_range('2018-01-01',periods=550).strftime('%Y-%m-%d')
        c=np.linspace(100,180,550)
        f=pd.DataFrame(dict(open=c,high=c,low=c,close=c,volume=10000),index=ix)
        self.assertIs(desired('monthly_momentum252_trend200',f),True)
        r=replay(f,'monthly_momentum252_trend200',ix[300],ix[-1])
        self.assertGreater(r['trades'],0)
        self.assertGreater(r['net_pnl'],0)

    def test_future_close_cannot_create_an_entry_at_todays_open(self):
        ix=pd.bdate_range('2018-01-01',periods=400).strftime('%Y-%m-%d')
        c=np.linspace(180,100,400)
        f=pd.DataFrame(dict(open=c,high=c,low=c,close=c,volume=10000),index=ix)
        f.loc[ix[-1],'close']=1000
        r=replay(f,'monthly_trend200',ix[-1],ix[-1])
        self.assertEqual(r['trades'],0)
