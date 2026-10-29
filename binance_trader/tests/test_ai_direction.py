"""All-commodity AI/Kline decisions and model comparison; no live orders."""
import copy
import json
import time
import unittest
from unittest.mock import patch
from binance_trader.ai_policy import project_ai, active_direction
from binance_trader.ai_service import AIService
from binance_trader.ai_news import ModelClient
from binance_trader.core import SYMBOLS
from binance_trader.news_risk import DRIVERS
from binance_trader.strategy import decide
from test_strategy import kline_news, verified_news, risk
from test_ai_news import CONFIG, Capture, MODEL, ARTICLE, NOW
import test_ai_integration as integration


def model_result(symbol, now, direction='bearish'):
    return dict(symbol=symbol,status='ok',model='gpt-6-luna',analyzed_at=now-10,
        trading_authority=False,direction=direction,events=[],drivers=[
            dict(key=k,status='analyzed',direction=direction,summary='合成样例：有依据的背景方向，仅用于测试。',
                 sources=[dict(source='BBC',url='https://www.bbc.com/news/fixture')]) for k,_ in DRIVERS[symbol]])


class AIDirectionTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.ms=self.now*1000
        self.buy=dict(side='BUY',candle=self.ms-3601000,fast=102,slow=100,reference=102,previous_close=101,atr=1)
        self.sell=dict(self.buy,side='SELL',fast=98,reference=98,previous_close=99)

    def test_retained_analysis_requires_fresh_input_check(self):
        value=model_result('XAUUSDT',self.now)
        value['analyzed_at']=self.now-86400
        value['retained_input_checked_at']=self.now
        projected=project_ai(value,'XAUUSDT',self.now)
        self.assertEqual(active_direction(projected,'XAUUSDT',self.now),'SELL')
        self.assertIsNone(active_direction(projected,'XAUUSDT',self.now+90))
        value['retained_input_checked_at']=self.now-91
        self.assertEqual(project_ai(value,'XAUUSDT',self.now)['status'],'fallback')

    def input(self,symbol='XAUUSDT',direction='bearish',news=None):
        return dict(news or kline_news(symbol,now=self.now),ai=project_ai(model_result(symbol,self.now,direction),symbol,self.now))

    def test_all_five_ai_directions_require_matching_breakout_and_trend(self):
        for symbol in SYMBOLS:
            for direction,sig,opposite in [('bearish',self.sell,self.buy),('bullish',self.buy,self.sell)]:
                with self.subTest(symbol=symbol,direction=direction):
                    n=self.input(symbol,direction)
                    d=decide(sig,n,symbol,self.ms)
                    self.assertEqual(d['action'],sig['side']);self.assertEqual(d['source'],'ai+kline')
                    self.assertEqual(decide(opposite,n,symbol,self.ms)['action'],'WAIT')
                    self.assertEqual(decide(dict(sig,side=None),n,symbol,self.ms)['action'],'WAIT')
                    self.assertEqual(decide(dict(sig,fast=100,slow=100),n,symbol,self.ms)['action'],'WAIT')
                    self.assertEqual(decide(dict(sig,candle=self.ms-3730000),n,symbol,self.ms)['action'],'WAIT')

    def test_rule_conflicts_cooldowns_and_source_failures_cannot_be_overridden(self):
        for symbol in SYMBOLS:
            for n in [verified_news(symbol,'BUY',self.now),
                      dict(kline_news(symbol,now=self.now),risks=[risk(self.now)]),
                      dict(kline_news(symbol,now=self.now),expires=self.now-1),
                      dict(kline_news(symbol,now=self.now),status='unavailable',kline_allowed=False)]:
                self.assertEqual(decide(self.sell,self.input(symbol,news=n),symbol,self.ms)['action'],'WAIT')

    def test_mixed_unknown_failed_and_expired_ai_preserve_healthy_kline_fallback(self):
        raw=model_result('XAUUSDT',self.now)
        variants=[dict(raw,status=s) for s in ['disabled','pending','analyzing','stale','historical','error','no_news']]
        variants += [dict(raw,direction=d) for d in ['mixed','uncertain']]
        variants += [dict(raw,analyzed_at=self.now-900),dict(raw,analyzed_at=self.now+1),dict(raw,symbol='BZUSDT'),
                     dict(raw,drivers=[]),dict(raw,trading_authority=True),dict(raw,events=[{'evidence_valid':False}])]
        for value in variants:
            a=project_ai(value,'XAUUSDT',self.now);self.assertEqual(a['status'],'fallback')
            self.assertEqual(decide(self.buy,dict(kline_news(now=self.now),ai=a),'XAUUSDT',self.ms)['action'],'BUY')

    def test_factor_bias_does_not_override_overall_mixed_and_projection_expires(self):
        raw=model_result('XAUUSDT',self.now);raw['direction']='mixed'
        self.assertIsNone(project_ai(raw,'XAUUSDT',self.now)['direction'])
        a=self.input()['ai']
        self.assertEqual(active_direction(a,'XAUUSDT',self.now),'SELL')
        for change in [{'policy_version':True},{'analyzed_at':True},{'expires':float('nan')},
                       {'expires':self.now+99999},{'result_id':'fake'},{'model':''},{'symbol':'CLUSDT'}]:
            self.assertIsNone(active_direction(dict(a,**change),'XAUUSDT',self.now))
        self.assertIsNone(active_direction(a,'XAUUSDT',a['expires']))

    def test_ai_exit_needs_opposite_new_breakout_not_model_alone(self):
        n=self.input();position={'side':'BUY'}
        self.assertEqual(decide(self.sell,n,'XAUUSDT',self.ms,position)['action'],'EXIT')
        for sig in [dict(self.sell,side=None),dict(self.sell,candle=self.ms-3730000),self.buy]:
            self.assertEqual(decide(sig,n,'XAUUSDT',self.ms,position)['action'],'WAIT')
        self.assertEqual(decide(self.sell,n,'XAUUSDT',self.ms,{'side':'SELL'})['action'],'WAIT')

    def test_producer_and_engine_share_current_projection_without_account_mutation(self):
        case=integration.AIIntegrationTests();case.setUp()
        try:
            case.publish();state=copy.deepcopy(case.engine.s)
            with patch.object(case.service,'snapshot',return_value=model_result('XAUUSDT',self.now)):
                snapshot=case.analysis.snapshot('XAUUSDT');value=case.analysis.decision('XAUUSDT')
                self.assertEqual(value['ai'],snapshot['news']['decision']['ai'])
                self.assertEqual(decide(self.buy,value,'XAUUSDT',self.ms)['action'],'WAIT')
                self.assertEqual(decide(self.sell,value,'XAUUSDT',self.ms)['action'],'SELL')
            self.assertEqual(case.engine.s,state);self.assertEqual(case.api.calls,[])
        finally:case.tearDown()

    def test_engine_rechecks_ai_before_sizing_and_stores_no_order_on_conflict(self):
        case=integration.AIIntegrationTests();case.setUp()
        try:
            engine=case.engine;engine.sig=self.buy;engine.quote=case.api.market('XAUUSDT')
            inputs=[self.input(direction='bullish'),self.input(direction='bearish')]
            engine.news_provider=lambda symbol:inputs.pop(0)
            engine.maybe_enter()
            self.assertEqual(inputs,[])
            self.assertIsNone(engine.s['position']);self.assertIsNone(engine.s['pending'])
            self.assertEqual(engine.decision['action'],'WAIT')
            self.assertIn('下单前信号或新闻已失效',engine.decision['reason'])
            self.assertEqual(case.api.calls,[])
        finally:case.tearDown()


class ComparisonTests(unittest.TestCase):
    def service(self):
        self.capture=Capture();self.configs=[];self.now=[NOW]
        def factory(config):self.configs.append(dict(config));return ModelClient(config,self.capture)
        ai=AIService(factory,clock=lambda:self.now[0]);ai.configure(**dict(CONFIG,model='gpt-6-luna'))
        ai.submit('XAUUSDT',[ARTICLE],NOW);return ai

    def test_two_models_use_identical_public_input_once_and_never_publish_decisions(self):
        ai=self.service();ai.request_comparison('XAUUSDT')
        self.assertTrue(ai.run_comparison_once());self.assertTrue(ai.run_comparison_once());self.assertFalse(ai.run_comparison_once())
        self.assertEqual(ai.results,{})
        status=ai.status();self.assertEqual(status['comparison']['status'],'complete')
        self.assertEqual(status['request_count'],2)
        payloads=[json.loads(call['body']) for call in self.capture.calls]
        self.assertEqual([p['model'] for p in payloads],['gpt-6-luna','gpt-6.1-sol'])
        self.assertTrue(all(p['reasoning_effort']=='low' for p in payloads))
        inputs=[json.loads(p['messages'][1]['content']) for p in payloads]
        self.assertEqual(inputs[0],inputs[1])
        self.assertNotIn(CONFIG['api_key'],json.dumps(status));self.assertNotIn('body',json.dumps(status))
        ai.request_comparison('XAUUSDT');self.assertFalse(ai.run_comparison_once());self.assertEqual(len(self.capture.calls),2)

    def test_comparison_failure_is_separate_and_switch_invalidates_old_model_results(self):
        ai=self.service();ai.run_once('XAUUSDT');original=copy.deepcopy(ai.results)
        ai.request_comparison('XAUUSDT');self.capture.status=404
        ai.run_comparison_once();self.assertEqual(ai.results,original);self.assertEqual(ai.last_error,'')
        self.assertEqual(ai.comparison_status()['rows'][0]['status'],'error')
        ai.select_model('gpt-6.1-sol');self.assertEqual(ai.status()['model'],'gpt-6.1-sol')
        self.assertEqual(ai.results,{});self.assertEqual(ai.snapshot('XAUUSDT')['status'],'pending')
        self.assertEqual(self.configs[-1]['api_key'],CONFIG['api_key'])

    def test_comparison_requires_current_input_and_disallows_arbitrary_upgrade(self):
        ai=self.service()
        for model in ['unknown','gpt-6-astra',None]:
            with self.assertRaises(ValueError):ai.select_model(model)
        self.now[0]+=121
        with self.assertRaises(ValueError):ai.request_comparison('XAUUSDT')
        ai.disconnect();self.assertFalse(ai.run_comparison_once());self.assertIsNone(ai.comparison_status())
