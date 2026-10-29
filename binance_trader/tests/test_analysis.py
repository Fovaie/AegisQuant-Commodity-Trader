import time
import unittest
from types import SimpleNamespace
from email.utils import formatdate
from unittest.mock import patch
from binance_trader.analysis import Analysis, kline_analysis, parse_news, driver
from test_trader import candles
from test_trader import verified_news
from binance_trader.core import SYMBOLS

class AnalysisTests(unittest.TestCase):
    def test_live_candle_does_not_change_confirmed_signal(self):
        rows,end=candles()
        rows.append([end,'.092','.1','.08','.091','1',end+3599999])
        first=kline_analysis(rows,end+1000)
        rows[-1][4]='.15';rows[-1][2]='.15'
        second=kline_analysis(rows,end+1000)
        self.assertEqual(first['side'],second['side'])
        self.assertNotEqual(first['fast'],second['fast'])
    def test_news_word_boundaries(self):
        self.assertIn("美元",driver("Gold slips towards low as Fed rate bets lift dollar"))
    def test_old_kline_rejected(self):
        rows,end=candles()
        with self.assertRaises(ValueError):kline_analysis(rows,end+60000)
    def test_news_dates_dedup_and_safe_urls(self):
        now=time.time();date=formatdate(now-60,usegmt=True);old=formatdate(now-3*86400,usegmt=True)
        def item(title,url,date):return f'<item><title>{title}</title><link>{url}</link><pubDate>{date}</pubDate><source>Test</source></item>'
        good=item('Gold Fed rates','https://news.google.com/rss/articles/test',date)
        xml='<rss><channel>'+good+good+item('Old','https://news.google.com/x',old)+item('Unsafe','javascript:alert(1)',date)+'</channel></rss>'
        result=parse_news(xml.encode(),now)
        self.assertEqual(len(result),1);self.assertIn('美元',result[0]['analysis'])
    def test_symbol_switch_hides_previous_analysis_and_error_preserves_timestamp(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAUUSDT'})
        a=Analysis(engine);a.update('kline','XAUUSDT',{'price':100})
        updated=a.snapshot()['kline']['updated'];a.update('kline','XAUUSDT',error='offline')
        self.assertEqual(a.snapshot()['kline']['updated'],updated)
        engine.s['symbol']='BZUSDT'
        self.assertNotIn('data',a.snapshot()['kline'])
    def test_news_engine_decision_is_fresh_symbol_bound_and_fail_closed(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAUUSDT'})
        a=Analysis(engine);now=time.time()
        d=dict(verified_news('XAUUSDT'), asof=now-1, expires=now+60)
        a.update('news','XAUUSDT',dict(items=[],decision=d,coverage={'healthy':True}))
        self.assertEqual(a.decision('XAUUSDT')['direction'],'BUY')
        self.assertEqual(a.decision('CLUSDT')['status'],'unavailable')
        with patch('binance_trader.analysis.time.time',return_value=now+61):
            self.assertEqual(a.decision('XAUUSDT')['status'],'unavailable')
        before=a.snapshot()['news']['updated']
        a.update('news','XAUUSDT',error='offline')
        self.assertEqual(a.decision('XAUUSDT')['status'],'unavailable')
        self.assertEqual(a.snapshot()['news']['updated'],before)
        engine.s['symbol']='CLUSDT'
        self.assertEqual(a.snapshot()['news']['decision']['symbol'],'CLUSDT')
        self.assertNotIn('data',a.snapshot()['news'])

    def test_all_symbols_scanned_without_changing_trading_settings(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'BZUSDT'})
        a=Analysis(engine)
        order=[]
        def fetch(kind,symbol):
            order.append(symbol)
            return dict(items=[{'title':symbol}],decision=verified_news(symbol),coverage={'healthy':True})
        with patch.object(a,'fetch',side_effect=fetch):
            a.news_cycle()
        self.assertEqual(order[0],'BZUSDT')
        self.assertEqual(set(order),set(SYMBOLS))
        self.assertEqual(len(order),5)
        self.assertEqual(engine.s['symbol'],'BZUSDT')
        for symbol in SYMBOLS:
            self.assertEqual(a.snapshot(symbol)['news']['data'][0]['title'],symbol)
            self.assertEqual(a.decision(symbol)['symbol'],symbol)
        # Switching display reuses the correct background snapshot immediately.
        engine.s['symbol']='NATGASUSDT'
        self.assertEqual(a.snapshot()['news']['data'][0]['title'],'NATGASUSDT')
        with self.assertRaises(ValueError):a.snapshot('BTCUSDT')

    def test_expiry_and_outage_keep_risk_history_per_symbol(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'BZUSDT'})
        a=Analysis(engine);now=time.time()
        risks=[dict(category='shipping',subject='hormuz',status='active',blocking=True,reason='pending resolution')]
        d=dict(verified_news('BZUSDT'),status='blocked',direction=None,entry_allowed=False,risks=risks)
        a.update('news','BZUSDT',dict(items=[],decision=d,coverage={'healthy':True}))
        a.update('news','XAUUSDT',dict(items=[],decision=verified_news('XAUUSDT'),coverage={'healthy':True}))
        with patch('binance_trader.analysis.time.time',return_value=now+120):
            expired=a.decision('BZUSDT')
            self.assertEqual(expired['status'],'unavailable')
            self.assertEqual(expired['risks'],risks)
            self.assertFalse(expired['entry_allowed'])
        a.update('news','BZUSDT',error='offline')
        self.assertEqual(a.decision('BZUSDT')['risks'],risks)
        self.assertFalse(a.decision('BZUSDT')['entry_allowed'])
        self.assertEqual(a.decision('XAUUSDT')['status'],'bullish')

    def test_worker_shutdown_stops_remaining_symbols(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAGUSDT'})
        a=Analysis(engine)
        def fetch(kind,symbol):
            a.stop()
            return dict(items=[],decision=verified_news(symbol),coverage={'healthy':True})
        with patch.object(a,'fetch',side_effect=fetch) as mocked:
            a.news_cycle()
        self.assertEqual(mocked.call_count,1)

    def test_risk_ledger_failure_reason_reaches_workstation_and_engine(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAUUSDT'})
        a=Analysis(engine)
        message='持续新闻风险账本损坏；必须核对恢复'
        a.update('news','XAUUSDT',dict(items=[],decision=dict(verified_news('XAUUSDT'),entry_allowed=False,
            status='unavailable',direction=None),coverage={'healthy':False,'risk_state_error':message}))
        self.assertEqual(a.snapshot()['news']['decision']['reason'],message)
        self.assertEqual(a.decision('XAUUSDT')['reason'],message)
        self.assertFalse(a.decision('XAUUSDT')['entry_allowed'])

    def test_kline_fallback_permission_is_symbol_bound_and_revoked_on_outage_or_expiry(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'NATGASUSDT'})
        a=Analysis(engine);now=time.time()
        d=dict(verified_news('NATGASUSDT'),asof=now-1,expires=now+60,
               status='insufficient',direction=None,event_id=None,evidence=[],
               entry_allowed=False,kline_allowed=True)
        payload=dict(items=[],decision=d,coverage={'healthy':True})
        a.update('news','NATGASUSDT',payload)
        self.assertTrue(a.snapshot()['news']['decision']['kline_allowed'])
        self.assertFalse(a.decision('XAUUSDT')['kline_allowed'])
        with patch('binance_trader.analysis.time.time',return_value=now+61):
            self.assertFalse(a.decision('NATGASUSDT')['kline_allowed'])
        a.update('news','NATGASUSDT',error='offline')
        self.assertFalse(a.decision('NATGASUSDT')['kline_allowed'])
        self.assertTrue(d['kline_allowed'],'Revoking a cached decision must not mutate its original input')

    def test_cooldown_expiry_needs_a_new_healthy_snapshot_without_erasing_background(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAUUSDT'})
        a=Analysis(engine);now=time.time()
        risk=dict(id='fixture',category='geopolitical_conflict',subject='iran_israel_conflict',
                  status='active',blocking=True,reason='Conflict remains ongoing',
                  source='BBC',url='https://www.bbc.com/news/fixture',published=now-3590,
                  first_seen=now-86400,last_seen=now,event_at=now-3590,
                  gate_version=1,entry_gate='cooldown',gate_reason='Recent escalation',
                  acute_event_id='fixture-acute',acute_rule='new_attack',
                  acute_event_at=now-3590,time_basis='event_time',cooldown_until=now+10)
        d=dict(verified_news('XAUUSDT'),asof=now,expires=now+75,status='blocked',
               direction=None,evidence=[],event_id=None,entry_allowed=False,kline_allowed=False,
               risks=[risk])
        a.update('news','XAUUSDT',dict(items=[],decision=d,coverage={'healthy':True}))
        with patch('binance_trader.analysis.time.time',return_value=now+11):
            # Wall time crossing the deadline does not turn an old veto into permission.
            self.assertFalse(a.decision('XAUUSDT')['kline_allowed'])
            fresh=dict(d,asof=now+11,expires=now+86,status='insufficient',kline_allowed=True,
                       risks=[dict(risk,blocking=False,entry_gate='background',gate_reason='Background persists')],
                       drivers=[dict(key='geopolitics',label='Geopolitics',status='background',reason='Ongoing context')])
            a.update('news','XAUUSDT',dict(items=[],decision=fresh,coverage={'healthy':True}))
            result=a.decision('XAUUSDT')
            self.assertTrue(result['kline_allowed'])
            self.assertEqual(result['risks'][0]['status'],'active')
            self.assertEqual(result['risks'][0]['cooldown_until'],now+10)
            a.update('news','XAUUSDT',error='offline')
            result=a.decision('XAUUSDT')
            self.assertFalse(result['kline_allowed'])
            self.assertEqual(result['risks'][0]['entry_gate'],'background')
            self.assertEqual(result['risks'][0]['status'],'active')
