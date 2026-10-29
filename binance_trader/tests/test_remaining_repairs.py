import copy,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from binance_trader.core import signal,validate_candles,SYMBOLS
from binance_trader.analysis import Analysis,kline_analysis
from binance_trader.api import Binance
from binance_trader.ai_service import AIService
from binance_trader.ai_news import AINewsError
from binance_trader.news_risk import RiskLedger
from binance_trader.news import safe_url
from test_trader import Fake,candles
import test_news_risk as news_fixture
from test_ai_news import CONFIG,ARTICLE

class RemainingRepairsTests(unittest.TestCase):
    def test_every_closed_ohlc_row_is_validated_before_signal(self):
        rows,end=candles()
        for index in (0,30,85,99):
            for field,value in ((2,'0.001'),(3,'1'),(1,'NaN'),(5,'-1')):
                bad=copy.deepcopy(rows);bad[index][field]=value
                with self.subTest(index=index,field=field),self.assertRaises(ValueError):signal(bad,end)
        self.assertEqual(signal(rows,end)['side'],'BUY')
    def test_api_and_chart_reject_bad_unclosed_ohlc(self):
        rows,end=candles();rows.append([end,'.092','.091','.08','.1','1',end+3599999])
        with self.assertRaises(ValueError):kline_analysis(rows,end+1000)
        with patch.object(Binance,'request',return_value=rows):
            with self.assertRaises(ValueError):Binance().candles('XAUUSDT')
    def test_slow_symbol_does_not_hold_other_symbols_next_refresh(self):
        for kind in ('market','kline'):
            with self.subTest(kind=kind):
                a=Analysis(SimpleNamespace(api=SimpleNamespace(demo=False),s=dict(symbol='XAUUSDT',selection_mode='manual')))
                a.api=Fake();release=threading.Event();ready=threading.Event();counts={s:0 for s in SYMBOLS};lock=threading.Lock()
                original=a.market_once if kind=='market' else a.kline_once
                def fetch(symbol):
                    if symbol=='NATGASUSDT':release.wait(3)
                    original(symbol)
                    with lock:
                        counts[symbol]+=1
                        if all(counts[s]>=2 for s in SYMBOLS if s!='NATGASUSDT'):ready.set()
                if kind=='market':a.market_once=fetch
                else:a.kline_once=fetch
                threads=[threading.Thread(target=a.symbol_worker,args=(kind,s,.01),daemon=True) for s in SYMBOLS]
                try:
                    for t in threads:t.start()
                    self.assertTrue(ready.wait(2),counts);self.assertEqual(counts['NATGASUSDT'],0)
                finally:
                    a.done.set();release.set()
                    for t in threads:t.join(2);self.assertFalse(t.is_alive())
    def test_background_research_does_not_advance_risk_or_replace_source(self):
        fixture=news_fixture.NewsRiskTests();fixture.setUp()
        with tempfile.TemporaryDirectory() as directory:
            ledger=RiskLedger(Path(directory)/'risk.json',safe_url);fixture.seed(ledger)
            original=copy.deepcopy(next(iter(ledger.symbols['CLUSDT'].values())))
            obs=fixture.facts('Research shows the war between Iran and Israel harmed mental health.',published=fixture.now+300,now=fixture.now+310)
            self.assertTrue(obs);self.assertTrue(all(o['background'] for o in obs))
            ledger.update('CLUSDT',obs,fixture.now+310)
            row=next(iter(ledger.symbols['CLUSDT'].values()))
            self.assertEqual(row['last_hazard_at'],original['last_hazard_at']);self.assertEqual(row['url'],original['url'])
            self.assertTrue(row['_mentions']);self.assertFalse(RiskLedger(ledger.path,safe_url).error)
    def test_ai_failure_health_survives_other_work_and_retry_is_symbol_scoped(self):
        class Client:
            def analyze(self,*args,**kwargs):raise AINewsError('EVIDENCE','AI 因素结论缺少对应证据')
            def test_connection(self):return dict(ok=True)
        now=[time.time()];a=AIService(client_factory=lambda c:Client(),clock=lambda:now[0]);a.configure(**CONFIG)
        a.submit('XAUUSDT',[ARTICLE],now[0]);self.assertTrue(a.run_once('XAUUSDT'))
        self.assertEqual(a.status()['analysis_health']['error'],1)
        a.attempted['CLUSDT']={'keep'};a.results['CLUSDT']={'keep':'result'}
        a.request_retry('XAUUSDT');self.assertEqual(a.attempted['CLUSDT'],{'keep'});self.assertEqual(a.results['CLUSDT'],{'keep':'result'})
        self.assertFalse(a.attempted.get('XAUUSDT'));self.assertNotIn('XAUUSDT',a.failures)
        with self.assertRaises(ValueError):a.request_retry('CLUSDT')
