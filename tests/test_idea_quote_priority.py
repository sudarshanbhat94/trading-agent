"""Quote coverage for research plans does not require a funded position."""
import sqlite3
from unittest.mock import patch

from scripts import v2_quote_feed as feed
from app.screening import tracking
from tests.test_idea_tracking import publish,quote


def test_issued_unheld_stocks_join_fast_feed_without_duplicates_or_unknowns(tmp_path):
    db=tmp_path/'tracking.db';publish(db)
    with patch.dict('os.environ',{'IDEA_TRACKING_DB':str(db)}),patch.object(feed,'MARKETS',{'IN':'upstox'}),patch.object(feed,'WATCH_HOT',{'IN':['BASE']}):
        active=feed._tracked()
        rows=feed._hot_rows({'IN':{s:{'symbol':s} for s in ('BASE','TEST','HELD')}},{'IN':{'HELD','TEST'}},active | {'UNKNOWN'})
    assert [r['symbol'] for r in rows['IN']]==['BASE','HELD','TEST']
    quote(db,100);quote(db,94,30)
    with patch.dict('os.environ',{'IDEA_TRACKING_DB':str(db)}):assert feed._tracked()==set()


def test_no_tracker_or_bad_tracker_does_not_create_or_disable_held_feed(tmp_path):
    db=tmp_path/'missing.db'
    with patch.dict('os.environ',{'IDEA_TRACKING_DB':str(db)}):assert feed._tracked()==set()
    assert not db.exists()
    con=sqlite3.connect(db);con.execute('CREATE TABLE unrelated(x)');con.commit();con.close()
    before=db.read_bytes()
    with patch.dict('os.environ',{'IDEA_TRACKING_DB':str(db)}):assert feed._tracked()==set()
    assert db.read_bytes()==before
    with patch.object(feed,'MARKETS',{'IN':'upstox'}),patch.object(feed,'WATCH_HOT',{}):
        assert feed._hot_rows({'IN':{'HELD':{'symbol':'HELD'}}},{'IN':{'HELD'}},set())['IN']==[{'symbol':'HELD'}]


def test_india_research_symbols_never_expand_other_market_feed():
    maps={m:{s:{'symbol':s} for s in ('TEST','BASE')} for m in ('IN','US')}
    with patch.object(feed,'MARKETS',{'IN':'upstox','US':'alpaca'}),patch.object(feed,'WATCH_HOT',{'US':['BASE']}):
        rows=feed._hot_rows(maps,{}, {'TEST'})
    assert [r['symbol'] for r in rows['IN']]==['TEST']
    assert [r['symbol'] for r in rows['US']]==['BASE']
