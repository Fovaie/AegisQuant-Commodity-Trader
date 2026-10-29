"""Public-feed fixtures through parser, analysis, strategy and fake execution.

No network, user credentials, real ledgers or exchange writes are used.
"""
import tempfile
import time
import unittest
import copy
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path

from binance_trader.analysis import Analysis
from binance_trader.engine import Engine
from binance_trader.news import FEEDS, NewsService
from binance_trader.strategy import news_state
from test_trader import Fake


FILLER = (' The publication provides the original release and its operational details. '
          'The report identifies the reporting period and the units used in the data. '
          'This text is a synthetic fixture for validating the local processing pipeline.')


def feed_fixture(symbol, now, side='BUY'):
    published=now-60
    iso=datetime.fromtimestamp(published,timezone.utc).isoformat()
    date=datetime.fromtimestamp(published,timezone.utc).strftime('%Y%m%d')
    titles={'XAUUSDT':'Federal Reserve issues FOMC statement','XAGUSDT':'Federal Reserve issues FOMC statement',
            'CLUSDT':'US crude oil inventories update','BZUSDT':'US crude oil inventories update',
            'NATGASUSDT':'US natural gas storage update'}
    articles={}
    if symbol in ('XAUUSDT','XAGUSDT'):
        verb='lower' if side=='BUY' else 'raise'
        articles['Fed']=(f'https://www.federalreserve.gov/newsevents/pressreleases/monetary{date}a.htm',
                          f'The Committee decided to {verb} the target range for the federal funds rate today.')
    else:
        asset,unit=('US natural gas storage','billion cubic feet') if symbol=='NATGASUSDT' else ('US crude oil inventories','million barrels')
        actual='5' if side=='BUY' else '1'
        base=f'{asset} fell by {actual} {unit} today, compared with an expected fall of 2 {unit}'
        articles['BBC']=('https://www.bbc.com/news/fixture',base+' according to the release.')
        articles['Guardian']=('https://www.theguardian.com/business/fixture',base+' in the published report.')
    def fetch(url):
        if url in FEEDS.values():
            publisher=next((p for p in articles if FEEDS.get(p)==url),None)
            if publisher is None:return b'<rss><channel/></rss>'
            article_url,_=articles[publisher]
            return (f'<rss><channel><item><title>{titles[symbol]}</title><link>{article_url}</link>'
                    f'<pubDate>{formatdate(published,usegmt=True)}</pubDate></item></channel></rss>').encode()
        for article_url,body in articles.values():
            if url==article_url:
                return (f'<meta property="article:published_time" content="{iso}"><article><p>'
                        f'{body}{FILLER}</p></article>').encode()
        raise AssertionError('Unexpected external URL: '+url)
    return fetch


class NewsIntegrationTests(unittest.TestCase):
    def test_unconfirmed_full_report_allows_only_closed_kline_trades_for_five_products(self):
        for symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT'):
            for side in ('BUY','SELL'):
                with self.subTest(symbol=symbol,side=side), tempfile.TemporaryDirectory() as folder:
                    now=time.time()
                    subject=('Federal Reserve interest rates' if symbol in ('XAUUSDT','XAGUSDT') else
                             'US natural gas storage' if symbol=='NATGASUSDT' else 'crude oil inventories')
                    title=subject+' outlook remains uncertain'
                    body=f'{subject} could fall next week. Reporting by Reuters and Associated Press.'
                    iso=datetime.fromtimestamp(now-60,timezone.utc).isoformat()
                    def fetch(url):
                        if url==FEEDS['Guardian']:
                            return (f'<rss><channel><item><title>{title}</title>'
                                    '<link>https://www.theguardian.com/business/fixture</link>'
                                    f'<pubDate>{formatdate(now-60,usegmt=True)}</pubDate>'
                                    '</item></channel></rss>').encode()
                        if url in FEEDS.values():return b'<rss><channel/></rss>'
                        self.assertEqual(url,'https://www.theguardian.com/business/fixture')
                        return (f'<meta property="article:published_time" content="{iso}">'
                                f'<article><p>{body}{FILLER}</p></article>').encode()
                    news=NewsService(fetch,state_path=Path(folder)/'news-risk.json').fetch(symbol,now)
                    self.assertTrue(any(row['uncertain'] for row in news['items']))
                    self.assertTrue(any(d['status']=='review_required' for d in news['decision']['drivers']))
                    api=Fake();engine=Engine(api,Path(folder)/'paper.sqlite')
                    try:
                        engine.s['symbol']=symbol
                        analysis=Analysis(engine);analysis.update('news',symbol,news)
                        engine.news_provider=analysis.decision
                        engine.quote=api.market(symbol);engine.updated=time.time()
                        engine.sig=dict(side=None,atr=.001,reference=.092,candle=int(time.time()*1000)-3_601_000)
                        engine.start();engine.maybe_enter()
                        self.assertIsNone(engine.s['position'],'Reference news must not create a breakout')
                        engine.sig['side']=side
                        engine.maybe_enter()
                        self.assertIsNotNone(engine.s['position'],engine.decision)
                        self.assertEqual(engine.s['position']['side'],side)
                        self.assertEqual(engine.s['position']['decision']['source'],'kline')
                        self.assertFalse(engine.s['position']['decision']['news']['entry_allowed'])
                        self.assertEqual(api.calls,[],'All execution must remain in the paper fixture')
                    finally:engine.db.close()

    def test_all_five_commodities_can_pass_real_pipeline_with_verified_facts(self):
        for symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT'):
            for side in ('BUY','SELL'):
                with self.subTest(symbol=symbol,side=side), tempfile.TemporaryDirectory() as folder:
                    now=time.time()
                    service=NewsService(feed_fixture(symbol,now,side),state_path=Path(folder)/'news-risk.json')
                    news=service.fetch(symbol,now)
                    checked=news_state(news['decision'],symbol,now)
                    self.assertTrue(checked['entry_allowed'], checked['reason'])
                    self.assertEqual(checked['direction'],side)
                    api=Fake();engine=Engine(api,Path(folder)/'paper.sqlite')
                    try:
                        engine.s['symbol']=symbol
                        analysis=Analysis(engine)
                        analysis.update('news',symbol,news)
                        engine.news_provider=analysis.decision
                        engine.quote=api.market(symbol)
                        engine.updated=time.time()
                        engine.sig=dict(side=side,atr=.001,reference=.092,
                            candle=int(time.time()*1000)-3_601_000)
                        engine.start()
                        engine.maybe_enter()
                        self.assertIsNotNone(engine.s['position'],engine.decision)
                        self.assertEqual(engine.s['position']['side'],side)
                        self.assertEqual(engine.s['position']['decision']['source'],'news+kline')
                        self.assertEqual(api.calls,[],'Paper fixture must not call exchange writes')
                    finally:engine.db.close()

    def test_all_five_healthy_empty_feeds_allow_long_and_short_closed_breakouts(self):
        for symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT'):
            for side in ('BUY','SELL'):
                with self.subTest(symbol=symbol,side=side), tempfile.TemporaryDirectory() as folder:
                    service=NewsService(lambda url:b'<rss><channel/></rss>',state_path=Path(folder)/'news-risk.json')
                    news=service.fetch(symbol)
                    self.assertTrue(news['coverage']['healthy'])
                    self.assertEqual(news['decision']['status'],'insufficient')
                    checked=news_state(news['decision'],symbol)
                    self.assertIs(checked['entry_allowed'],False)
                    self.assertIs(checked['kline_allowed'],True)
                    api=Fake();engine=Engine(api,Path(folder)/'paper.sqlite')
                    try:
                        engine.s['symbol']=symbol
                        analysis=Analysis(engine);analysis.update('news',symbol,news)
                        engine.news_provider=analysis.decision
                        engine.quote=api.market(symbol);engine.updated=time.time()
                        engine.sig=dict(side=side,atr=.001,reference=.092,candle=int(time.time()*1000)-3_601_000)
                        engine.start()
                        engine.maybe_enter()
                        self.assertIsNotNone(engine.s['position'],engine.decision)
                        self.assertEqual(engine.s['position']['side'],side)
                        self.assertEqual(engine.s['position']['decision']['source'],'kline')
                        self.assertEqual(api.calls,[],'Paper fallback must not call exchange writes')
                    finally:engine.db.close()

    def test_empty_feed_permission_does_not_replace_a_closed_breakout(self):
        with tempfile.TemporaryDirectory() as folder:
            service=NewsService(lambda url:b'<rss><channel/></rss>')
            api=Fake();engine=Engine(api,Path(folder)/'paper.sqlite')
            try:
                analysis=Analysis(engine);analysis.update('news','XAUUSDT',service.fetch('XAUUSDT'))
                engine.news_provider=analysis.decision
                engine.quote=api.market('XAUUSDT');engine.updated=time.time()
                engine.start()
                for side,candle in ((None,int(time.time()*1000)-3_601_000),
                                    ('BUY',int(time.time()*1000)-3_800_000),
                                    ('SELL',int(time.time()*1000)-1000)):
                    engine.sig=dict(side=side,atr=.001,reference=.092,fast=.094,slow=.091,
                                    previous_close=.091,candle=candle)
                    engine.maybe_enter()
                    self.assertIsNone(engine.s['position'])
                self.assertEqual(api.calls,[])
            finally:engine.db.close()

    def test_fallback_is_revoked_by_analysis_outage_staleness_or_acute_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            service=NewsService(lambda url:b'<rss><channel/></rss>')
            base=service.fetch('XAUUSDT');now=time.time()
            risk=dict(id='synthetic-risk',category='geopolitical_conflict',subject='synthetic',
                      status='review_required',blocking=True,reason='需核验',source='BBC',
                      url='https://www.bbc.com/news/fixture',first_seen=now-60,last_seen=now,
                      published=now-60,event_at=None,gate_version=1,entry_gate='cooldown',
                      gate_reason='新急性事件冷静期',acute_event_id='synthetic-acute',
                      acute_rule='synthetic-escalation',acute_event_at=now-60,
                      time_basis='first_evidence',cooldown_until=now+3540)
            for condition in ('expired','outage','risk'):
                with self.subTest(condition=condition):
                    news=copy.deepcopy(base)
                    if condition=='expired':news['decision']['expires']=now-1
                    elif condition=='outage':news['coverage']['healthy']=False
                    else:news['decision']['risks']=[risk]
                    api=Fake();engine=Engine(api,Path(folder)/(condition+'.sqlite'))
                    try:
                        analysis=Analysis(engine);analysis.update('news','XAUUSDT',news)
                        engine.news_provider=analysis.decision
                        engine.quote=api.market('XAUUSDT');engine.updated=time.time()
                        engine.sig=dict(side='BUY',atr=.001,reference=.092,candle=int(time.time()*1000)-3_601_000)
                        with self.assertRaises(ValueError):engine.start()
                        engine.maybe_enter()
                        self.assertIsNone(engine.s['position'])
                        self.assertEqual(api.calls,[])
                    finally:engine.db.close()
