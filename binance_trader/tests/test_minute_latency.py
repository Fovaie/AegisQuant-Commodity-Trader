"""Minute strategy and bounded background work, with no exchange writes/model charges."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from binance_trader.core import SYMBOLS, signal
from binance_trader.timeframes import timeframe
from binance_trader.strategy import decide
from binance_trader.engine import Engine
from binance_trader.analysis import Analysis
from binance_trader.ai_service import AIService
from binance_trader.ai_news import AINewsError, ModelClient
from binance_trader.news import NewsService
from binance_trader.chatgpt_chat import build_context
from test_trader import Fake, candles
from test_strategy import kline_news
from test_ai_integration import ModelHarness
from test_ai_news import CONFIG, MODEL, ARTICLE, Capture, NOW


def minute_rows(now, side='BUY', interval='1m'):
    rows=candles()[0]
    duration=timeframe(interval)['duration_ms']
    end=int(now*1000)//duration*duration
    for i,row in enumerate(rows):
        row[0]=end-(len(rows)-i)*duration;row[6]=row[0]+duration-1
        if side=='SELL':
            o,h,l,c=map(float,row[1:5]);row[1:5]=[str(.184-o),str(.184-l),str(.184-h),str(.184-c)]
    return rows


class MinuteTests(unittest.TestCase):
    def setUp(self):
        self.now=[int(time.time())//60*60+1]
        self.clock=patch('time.time',side_effect=lambda:self.now[0]);self.clock.start()
    def tearDown(self):self.clock.stop()

    def test_minute_boundary_live_candle_and_wrong_timeframe(self):
        rows=minute_rows(self.now[0]);sig=signal(rows,self.now[0]*1000,'1m')
        self.assertEqual(sig['side'],'BUY')
        end=rows[-1][6]+1
        open_row=[end,'.09','.2','.01','.18','1',end+59999]
        self.assertEqual(signal(rows+[open_row],end+29000,'1m'),sig)
        n=kline_news('XAUUSDT',now=end/1000+30)
        self.assertEqual(decide(sig,n,'XAUUSDT',end+30000,interval='1m')['action'],'BUY')
        self.assertEqual(decide(sig,n,'XAUUSDT',end+30001,interval='1m')['action'],'WAIT')
        self.assertEqual(decide(sig,n,'XAUUSDT',end+1000,interval='1h')['action'],'WAIT')
        with self.assertRaises(ValueError):signal(rows,end+1000,'1h')
        with self.assertRaises(ValueError):signal(rows[:-2]+rows[-1:],end+1000,'1m')
        with self.assertRaises(ValueError):signal(rows,end+76000,'1m')

    def test_all_five_long_short_paper_and_restart_dedup(self):
        for side in ('BUY','SELL'):
            for chosen in SYMBOLS:
                with self.subTest(side=side,symbol=chosen),tempfile.TemporaryDirectory() as tmp:
                    api=Fake();rows=minute_rows(self.now[0],side)
                    api.candles=lambda symbol,interval='1h':copy.deepcopy(rows)
                    api.order_book=lambda symbol:dict(T=self.now[0]*1000,bids=[['.09199','1000000']],asks=[['.09201','1000000']])
                    e=Engine(api,Path(tmp)/'paper.sqlite')
                    try:
                        e.configure(dict(symbol=chosen,interval='1m',risk=1.5,leverage=2,selection_mode='auto'))
                        def values():
                            return {s:dict(symbol=s,checked=self.now[0],kline_checked=self.now[0],
                                quote=api.market(s),book=api.order_book(s),rows=rows,
                                signal=signal(rows,self.now[0]*1000,'1m'),error='' if s==chosen else 'excluded fixture') for s in SYMBOLS}
                        e.candidate_provider=values;e.news_provider=lambda s:kline_news(s,now=self.now[0])
                        e.start();e.tick()
                        self.assertIsNotNone(e.s['position'],e.error)
                        self.assertEqual((e.s['position']['symbol'],e.s['position']['side'],e.s['position']['interval']),(chosen,side,'1m'))
                        self.assertLessEqual(e.s['position']['planned_loss'],1.5)
                        with self.assertRaises(ValueError):e.configure(dict(symbol=chosen,interval='5m',risk=1.5,leverage=2))
                        e.pause();e.close();e.s['enabled']=True;e.tick()
                        self.assertIsNone(e.s['position'])
                        e.pause();e.configure(dict(symbol=chosen,interval='1h',risk=1.5,leverage=2))
                        e.configure(dict(symbol=chosen,interval='1m',risk=1.5,leverage=2))
                        e.start();e.tick();self.assertIsNone(e.s['position'])
                        e.save();other=Engine(api,e.path)
                        self.assertEqual(other.s['interval'],'1m');self.assertFalse(other.s['enabled'])
                        self.assertEqual(other.s['last_entry_close'],rows[-1][6]+1);other.db.close()
                        self.assertEqual(api.calls,[])
                    finally:e.db.close()

    def test_period_switch_discards_inflight_old_chart_and_chat_uses_minute(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAUUSDT','interval':'1m'})
        a=Analysis(engine)
        rows=minute_rows(self.now[0])
        a.api.candles=lambda symbol,interval:copy.deepcopy(rows)
        a.kline_cycle()
        self.assertTrue(all(a.snapshot(s)['kline']['interval']=='1m' for s in SYMBOLS))
        context=build_context(a,now=self.now[0])
        self.assertEqual(context['rule']['entry_window_seconds'],30)
        self.assertTrue(all(len(r['closed_klines'])==40 and r['closed_1h']==[] and r['interval']=='1m' for r in context['symbols']))
        def old_response(symbol,interval):
            engine.s['interval']='5m';return rows
        a.api.candles=old_response;a.kline_cycle()
        self.assertNotIn('data',a.snapshot()['kline'])
        self.assertTrue(all(v['error'] for v in a.candidates().values()))


class BackgroundLatencyTests(unittest.TestCase):
    def test_queue_starts_when_enabled_and_client_failure_does_not_leak_slot(self):
        now=[NOW];model=ModelHarness();ai=AIService(model.factory,clock=lambda:now[0])
        ai.submit('XAUUSDT',[ARTICLE],now[0]);now[0]+=90
        ai.configure(**CONFIG);now[0]+=3
        self.assertEqual(ai.snapshot('XAUUSDT')['queue_wait_ms'],3000)
        with patch.object(ai,'factory',side_effect=ValueError('fixture')):
            with self.assertRaises(ValueError):ai.run_once('XAUUSDT')
        self.assertEqual(ai.status()['active_count'],0)
        self.assertEqual(ai.status()['request_count'],0)
        self.assertTrue(ai.run_once('XAUUSDT'))
        self.assertEqual(ai.snapshot('XAUUSDT')['queue_wait_ms'],3000)

    def make_ai(self):
        self.now=[NOW];self.model=ModelHarness();self.model.block=True
        ai=AIService(self.model.factory,clock=lambda:self.now[0]);ai.configure(**CONFIG)
        for s in SYMBOLS:ai.submit(s,[ARTICLE],self.now[0])
        return ai

    def test_two_independent_jobs_no_duplicate_third_slot_and_new_input_not_hidden(self):
        ai=self.make_ai();threads=[]
        try:
            t=threading.Thread(target=ai.run_once,args=('XAUUSDT',));threads.append(t);t.start()
            self.assertTrue(self.model.entered.wait(1));self.now[0]+=1
            t=threading.Thread(target=ai.run_once,args=('CLUSDT',));threads.append(t);t.start()
            deadline=time.monotonic()+1
            while len(self.model.calls)<2 and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(len(self.model.calls),2);self.now[0]+=1
            self.assertFalse(ai.run_once('XAUUSDT'));self.assertFalse(ai.run_once('BZUSDT'))
            self.assertEqual(ai.status()['active_count'],2)
            self.assertEqual(ai.snapshot('XAUUSDT')['status'],'analyzing')
            revised=dict(ARTICLE,body=ARTICLE['body']+' Correction.')
            ai.submit('XAUUSDT',[revised],self.now[0])
            self.model.release.set()
            for t in threads:t.join(1);self.assertFalse(t.is_alive())
            self.assertEqual(ai.snapshot('XAUUSDT')['status'],'pending')
            self.assertEqual(ai.snapshot('CLUSDT')['status'],'ok')
            self.assertTrue(ai.run_once('XAUUSDT'))
            self.assertEqual(ai.snapshot('XAUUSDT')['status'],'ok')
            self.now[0]+=1;self.assertFalse(ai.run_once('XAUUSDT'))
        finally:
            self.model.release.set()
            for t in threads:t.join(2)

    def test_timeout_does_not_block_other_symbol_but_429_has_backoff(self):
        ai=self.make_ai();self.model.block=False;self.model.failure=AINewsError('TIMEOUT','fixture')
        ai.run_once('XAUUSDT');self.now[0]+=1;self.model.failure=None
        self.assertTrue(ai.run_once('CLUSDT'));self.assertIsNotNone(ai.status()['last_analysis_latency_ms'])
        self.now[0]+=1;ai.request_test();ai.run_once('XAUUSDT')
        self.assertIsNotNone(ai.status()['last_test_latency_ms'])
        self.now[0]+=1;self.model.failure=AINewsError('HTTP','fixture',429);ai.run_once('BZUSDT')
        self.now[0]+=1;self.model.failure=None;self.assertFalse(ai.run_once('XAGUSDT'))
        self.assertEqual(ai.snapshot('XAUUSDT')['status'],'error')
        self.assertIsNotNone(ai.snapshot('XAUUSDT')['latency_ms'])

    def test_shared_feed_single_download_per_poll_window(self):
        calls=[]
        def fetch(url):
            calls.append(url);time.sleep(.015);return b'<rss><channel><title>Fixture</title></channel></rss>'
        service=NewsService(fetch)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda _:service.load_feed('BBC',NOW),range(5)))
        self.assertEqual(len(calls),1);self.assertTrue(all(not r[2] for r in results))
        service.load_feed('BBC',NOW+9);self.assertEqual(len(calls),1)
        service.load_feed('BBC',NOW+10);self.assertEqual(len(calls),2)

    def test_local_gateway_gets_low_effort_and_compact_full_schema_prompt(self):
        capture=Capture(MODEL)
        client=ModelClient(dict(CONFIG,model='gpt-6.1-sol',base_url='http://127.0.0.1:8080/v1'),capture)
        result=client.analyze('XAUUSDT',[ARTICLE],NOW)
        import json
        sent=json.loads(capture.calls[0]['body'])
        self.assertEqual(sent['reasoning_effort'],'low')
        self.assertIn('不超过80字',sent['messages'][0]['content'])
        self.assertEqual(len(result['drivers']),4)


if __name__=='__main__':unittest.main()
