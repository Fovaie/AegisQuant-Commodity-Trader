"""AI display isolation and worker lifecycle; fake models and accounts only."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from binance_trader.ai_service import AIService
from binance_trader.analysis import Analysis
from binance_trader.engine import Engine
from binance_trader.strategy import decide
from test_strategy import kline_news, risk
from test_trader import Fake, verified_news
from test_ai_news import ARTICLE, MODEL, Capture
from binance_trader.ai_news import ModelClient


class ModelHarness:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.failure = None
        self.response = None
        self.calls = []
        self.tests = 0
        self.configs = []

    def factory(self, config):
        self.configs.append(copy.deepcopy(config))
        owner = self

        class Client:
            def analyze(self, symbol, articles, now=None):
                owner.calls.append(dict(symbol=symbol, articles=copy.deepcopy(articles), now=now))
                owner.entered.set()
                if owner.block and not owner.release.wait(5):
                    raise RuntimeError('Synthetic test barrier timed out')
                if owner.failure:
                    raise owner.failure
                if owner.response is not None:
                    return copy.deepcopy(owner.response)
                return dict(summary='completed-'+articles[0]['id'], direction='uncertain',
                            events=[], uncertainties=['synthetic fixture'], trading_authority=False)

            def test_connection(self):
                owner.tests += 1
                return dict(ok=True, message='synthetic connection')

        return Client()


class AIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.model = ModelHarness()
        self.service = AIService(client_factory=self.model.factory)
        self.service.configure('https://api.openai.com/v1', 'test-model', 'dummy-model-key-1234')
        self.api = Fake()
        self.engine = Engine(self.api, Path(self.folder.name)/'paper.sqlite')
        self.analysis = Analysis(self.engine, ai=self.service)
        self.engine.news_provider = self.analysis.decision
        self.workers = []

    def tearDown(self):
        self.analysis.stop()
        self.model.release.set()
        for worker in self.workers:
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'Synthetic worker must finish before teardown')
        self.engine.db.close()
        self.folder.cleanup()

    def article(self, identity='article-one', **changes):
        value = dict(id=identity, url='https://www.bbc.com/news/synthetic',
                     title='Synthetic public news fixture', source='BBC', origin='BBC',
                     published=time.time()-10, body='Public article body for an isolated model test.')
        value.update(changes)
        return value

    def feed(self, decision=None, articles=None, **changes):
        value = dict(items=[{'title':'Public headline'}],
                     decision=kline_news() if decision is None else decision,
                     coverage={'healthy':True},
                     _ai_articles=[self.article()] if articles is None else articles)
        value.update(changes)
        return value

    def publish(self, **changes):
        self.analysis.update('news', 'XAUUSDT', self.feed(**changes))

    def run_blocked(self):
        self.model.block = True
        errors = []
        def run():
            try:self.service.run_once('XAUUSDT')
            except Exception as exc:errors.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        self.workers.append(worker)
        worker.start()
        self.assertTrue(self.model.entered.wait(1), 'Queued synthetic model call must start')
        return worker, errors

    def test_validated_factors_reach_public_analysis_without_changing_kline_permission(self):
        response=copy.deepcopy(MODEL)
        response['drivers'][1].update(status='analyzed',direction='bullish',event_indices=[0],summary='美元走弱可能支持黄金；未提供实际利率数值。')
        response['events'][0].update(quote='The US dollar fell today.',impact='美元走弱可能支持黄金。',direction='bullish',classification='new_event')
        capture=Capture(response)
        ai=AIService(client_factory=lambda config:ModelClient(config,capture))
        ai.configure('https://api.openai.com/v1','gpt-6-luna','fake-integration-key')
        self.analysis.ai=ai
        article=dict(ARTICLE,body='The US dollar fell today.',published=time.time()-60)
        self.analysis.update('news','XAUUSDT',self.feed(articles=[article]))
        before=copy.deepcopy(self.analysis.decision('XAUUSDT'))
        self.assertTrue(ai.run_once('XAUUSDT'))
        snapshot=self.analysis.snapshot('XAUUSDT')
        self.assertEqual(snapshot['ai']['status'],'ok')
        factors=snapshot['ai']['drivers']
        self.assertEqual(factors[1]['key'],'usd_real_yield')
        self.assertEqual(factors[1]['summary'],response['drivers'][1]['summary'])
        self.assertEqual(factors[1]['sources'][0]['url'],article['url'])
        self.assertEqual({k:v for k,v in snapshot['news']['decision'].items() if k!='ai'}, {k:v for k,v in before.items() if k!='ai'})
        self.assertTrue(snapshot['news']['decision']['kline_allowed'])
        self.assertFalse(ai.run_once('XAUUSDT'))
        self.assertEqual(len(capture.calls),1)
        self.assertEqual(self.api.calls,[])

    def test_model_input_is_public_allowlisted_bounded_and_detached_from_caller(self):
        private = 'DO-NOT-SEND-ACCOUNT-SECRET'
        articles = [self.article('article-'+str(i), body='public-'+str(i)+'x'*6000,
                                 key=private, secret=private, account={'balance':private}, position=private)
                    for i in range(12)]
        self.publish(articles=articles, account={'api_key':private}, secret=private,
                     position={'liquidation':private}, settings={'credential':private})
        articles[0]['title'] = 'CALLER-MUTATION-AFTER-SUBMIT'
        self.assertNotIn('_ai_articles', self.analysis.snapshot()['news'])
        self.service.run_once('XAUUSDT')
        self.assertEqual(len(self.model.calls), 1)
        sent = self.model.calls[0]['articles']
        self.assertLessEqual(len(sent), 8)
        self.assertTrue(sent)
        allowed = {'id','url','title','source','origin','published','body'}
        self.assertTrue(all(set(row) <= allowed for row in sent))
        self.assertTrue(all(len(row['body']) <= 5000 for row in sent))
        encoded = json.dumps(self.model.calls)
        self.assertNotIn(private, encoded)
        self.assertNotIn('CALLER-MUTATION-AFTER-SUBMIT', encoded)

    def test_arbitrary_ai_direction_cannot_change_execution_permission_or_state(self):
        now=time.time()
        blocked=dict(kline_news(), status='blocked', kline_allowed=False, risks=[risk(now)])
        decisions=(kline_news(), blocked, verified_news('XAUUSDT','SELL'))
        self.model.response=dict(summary='Ignore safeguards and buy now', direction='BUY',
                                 entry_allowed=True, kline_allowed=True, status='bullish', risks=[])
        for index,base in enumerate(decisions):
            with self.subTest(base=base['status']):
                self.service.configure('https://api.openai.com/v1','test-model','dummy-model-key-1234')
                self.publish(decision=base, articles=[self.article('case-'+str(index))])
                before=self.analysis.decision('XAUUSDT')
                sig=dict(side='BUY',candle=int(time.time()*1000)-3_601_000,atr=.001,
                         fast=.094,slow=.091,reference=.092,previous_close=.091)
                expected=decide(sig,before,'XAUUSDT',int(time.time()*1000))['action']
                self.service.run_once('XAUUSDT')
                self.assertEqual(len(self.model.calls),index+1)
                after=self.analysis.decision('XAUUSDT')
                self.assertEqual({k:v for k,v in after.items() if k!='ai'}, {k:v for k,v in before.items() if k!='ai'})
                self.assertEqual(decide(sig,after,'XAUUSDT',int(time.time()*1000))['action'],expected)
                self.assertFalse(self.engine.s['enabled'])
                self.assertIsNone(self.engine.s['position'])
                self.assertIsNone(self.engine.s['pending'])
                self.assertEqual(self.api.calls,[])

    def test_slow_model_does_not_hold_analysis_or_execution_locks(self):
        self.publish()
        worker, errors=self.run_blocked()
        completed=threading.Event(); failures=[]
        def normal_work():
            try:
                self.analysis.update('kline','XAUUSDT',{'price':123})
                self.assertTrue(self.analysis.decision('XAUUSDT')['kline_allowed'])
                self.assertEqual(self.analysis.snapshot()['kline']['data']['price'],123)
                self.publish(articles=[self.article('latest-while-model-is-slow')])
                self.engine.sig=dict(side=None,candle=int(time.time()*1000)-3_601_000)
                self.engine.last_candles=time.time()
                self.engine.tick()
            except Exception as exc:failures.append(exc)
            finally:completed.set()
        ordinary=threading.Thread(target=normal_work,daemon=True)
        self.workers.append(ordinary);ordinary.start()
        self.assertTrue(completed.wait(1), 'Slow AI must not block Kline, news cache, snapshot, or execution tick')
        self.assertTrue(worker.is_alive(), 'The model must still be blocked while ordinary work completes')
        self.model.release.set();worker.join(1)
        self.assertEqual(failures,[])
        self.assertEqual(errors,[])
        self.assertFalse(self.engine.s['enabled'])
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.api.calls,[])

    def test_inflight_settings_change_disconnect_and_stop_discard_old_result(self):
        for operation in ('configure','disconnect','stop'):
            with self.subTest(operation=operation):
                self.service.configure('https://api.openai.com/v1','test-model','dummy-model-key-1234')
                self.model.entered.clear();self.model.release.clear()
                marker='discard-'+operation
                self.publish(articles=[self.article(marker)])
                worker, errors=self.run_blocked()
                if operation=='configure':
                    self.service.configure('https://api.openai.com/v1','changed-model','different-model-key-5678')
                elif operation=='disconnect':self.service.disconnect()
                else:self.service.stop()
                self.model.release.set();worker.join(1)
                self.assertEqual(errors,[])
                self.assertNotIn('completed-'+marker,json.dumps(self.service.snapshot('XAUUSDT')))
                self.assertTrue(self.analysis.decision('XAUUSDT')['kline_allowed'])

    def test_inflight_older_article_set_does_not_overwrite_latest_input(self):
        self.publish(articles=[self.article('older-batch')])
        worker, errors=self.run_blocked()
        self.publish(articles=[self.article('newer-batch')])
        self.model.release.set();worker.join(1)
        self.assertEqual(errors,[])
        self.assertNotIn('completed-older-batch',json.dumps(self.service.snapshot('XAUUSDT')))
        self.model.block=False
        self.service.configure('https://api.openai.com/v1','test-model','dummy-model-key-1234')
        self.service.run_once('XAUUSDT')
        self.assertEqual(self.model.calls[-1]['articles'][0]['id'],'newer-batch')
        self.assertTrue(self.analysis.decision('XAUUSDT')['kline_allowed'])

    def test_ai_queue_failure_cannot_become_a_news_fetch_failure(self):
        def feed_for_symbol(kind,symbol):
            return self.feed(decision=kline_news(symbol))
        with patch.object(self.service,'submit',side_effect=RuntimeError('synthetic AI queue failure')):
            with patch.object(self.analysis,'fetch',side_effect=feed_for_symbol):
                self.analysis.news_cycle()
        for symbol in ('XAUUSDT','CLUSDT','BZUSDT','XAGUSDT','NATGASUSDT'):
            self.assertTrue(self.analysis.decision(symbol)['kline_allowed'])
            self.assertFalse(self.analysis.snapshot(symbol)['news'].get('error'))
        self.assertEqual(self.api.calls,[])

    def test_malformed_private_ai_article_cannot_revoke_a_healthy_rule_decision(self):
        self.publish(articles=[self.article(published=float('nan'))])
        self.assertTrue(self.analysis.decision('XAUUSDT')['kline_allowed'])
        self.assertFalse(self.analysis.snapshot()['news'].get('error'))
        self.service.run_once('XAUUSDT')
        self.assertEqual(self.model.calls,[])

    def test_model_error_keeps_existing_policy_and_does_not_expose_credentials(self):
        self.publish()
        before=self.analysis.decision('XAUUSDT')
        self.model.failure=RuntimeError('upstream error includes dummy-model-key-1234')
        self.service.run_once('XAUUSDT')
        self.assertEqual({k:v for k,v in self.analysis.decision('XAUUSDT').items() if k!='ai'}, {k:v for k,v in before.items() if k!='ai'})
        self.assertFalse(self.analysis.snapshot()['news'].get('error'))
        encoded=json.dumps(dict(status=self.service.status(),snapshot=self.service.snapshot('XAUUSDT')))
        self.assertNotIn('dummy-model-key-1234',encoded)
        self.assertTrue(self.analysis.decision('XAUUSDT')['kline_allowed'])

    def test_connection_test_is_queued_without_network_on_request_path(self):
        self.service.request_test()
        self.assertEqual(self.model.tests,0)
        self.assertEqual(self.model.calls,[])
        self.service.run_once('XAUUSDT')
        self.assertEqual(self.model.tests,1)
        self.assertEqual(self.model.calls,[])
        self.assertEqual(self.api.calls,[])

    def test_symbol_switch_does_not_show_other_symbol_model_result_or_grant_permission(self):
        self.publish()
        self.service.run_once('XAUUSDT')
        self.engine.s['symbol']='NATGASUSDT'
        snapshot=self.analysis.snapshot()
        self.assertEqual(snapshot['news']['decision']['symbol'],'NATGASUSDT')
        self.assertFalse(snapshot['news']['decision']['kline_allowed'])
        self.assertNotIn('completed-article-one',json.dumps(snapshot.get('ai',{})))
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.api.calls,[])


if __name__ == '__main__':
    unittest.main()
