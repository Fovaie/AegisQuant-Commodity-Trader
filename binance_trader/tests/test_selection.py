"""Five-way selection with isolated accounts and synthetic candles; no venue writes."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from binance_trader.api import APIError
from binance_trader.analysis import Analysis
from binance_trader.core import SYMBOLS, signal, plan
from binance_trader.engine import Engine
from binance_trader.selection import scan, depth_cost, fresh_candidate
from test_trader import Fake, candles
from test_strategy import kline_news, risk
import test_live_engine as live
import test_live_server as server


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.now=int(time.time())//3600*3600+1
        self.clock=patch('time.time',return_value=self.now);self.clock.start()
        self.api=Fake()
        self.api.order_book=lambda symbol:dict(T=self.now*1000,bids=[[str(self.api.price-.00001),'1000000']],
                                              asks=[[str(self.api.price+.00001),'1000000']])
        self.rows=candles()[0]
        self.sig=signal(self.rows,self.now*1000)
        self.values={s:dict(symbol=s,checked=self.now,kline_checked=self.now,quote=self.api.market(s),
                           book=self.api.order_book(s),signal=copy.deepcopy(self.sig),rows=copy.deepcopy(self.rows),error='') for s in SYMBOLS}
        self.news=lambda s:kline_news(s,now=self.now)
    def tearDown(self):self.clock.stop()
    def inputs(self,symbol,sig,quote):
        return plan(sig,quote['price'],self.api.filters(symbol),1.5,2,300,quote=quote,fees=self.api.commission_rates(symbol))
    def scan(self,**kwargs):return scan(self.values,self.news,self.inputs,self.now,**kwargs)

    def test_all_five_qualify_both_directions_and_deterministic_tie(self):
        for side in ('BUY','SELL'):
            if side=='SELL':
                for value in self.values.values():
                    value['signal'].update(side='SELL',fast=.092,slow=.094,previous_close=.094,lower=.093)
            result=self.scan()
            self.assertEqual(result['selected'],SYMBOLS[0])
            self.assertEqual([r['rank'] for r in result['rows']],[1,2,3,4,5])
            self.assertTrue(all(r['side']==side and r['status']=='eligible' for r in result['rows']))
        self.assertEqual(self.api.calls,[])

    def test_higher_account_cost_loses_and_plan_rejection_is_isolated(self):
        original=self.api.commission_rates
        self.api.commission_rates=lambda s,force=False:dict(original(s),taker=.002 if s==SYMBOLS[0] else .0005)
        self.assertNotEqual(self.scan()['selected'],SYMBOLS[0])
        original_filters=self.api.filters
        def filters(s):
            if s==SYMBOLS[1]:raise ValueError('本品种杠杆超过上限')
            return original_filters(s)
        self.api.filters=filters
        result=self.scan();self.assertEqual(result['rows'][1]['status'],'excluded')
        self.assertIsNotNone(result['selected'])

    def test_no_signal_no_breakout_bad_news_or_old_inputs_leave_flat(self):
        variants=[lambda v:v['signal'].update(side=None),lambda v:v.update(checked=self.now-21),
                  lambda v:v.update(kline_checked=self.now-11),lambda v:v.update(error='offline'),
                  lambda v:v.update(symbol='BTCUSDT'),lambda v:v['quote'].update(time=(self.now-16)*1000),
                  lambda v:v['signal'].update(candle=(self.now-3730)*1000)]
        good=copy.deepcopy(self.values)
        for change in variants:
            self.values=copy.deepcopy(good)
            for value in self.values.values():change(value)
            self.assertIsNone(self.scan()['selected'])
        self.values=good
        for provider in (lambda s:None,lambda s:dict(kline_news(s,now=self.now),risks=[risk(self.now)])):
            self.news=provider;self.assertIsNone(self.scan()['selected'])

    def test_one_failed_feed_does_not_hide_other_four(self):
        self.values[SYMBOLS[0]]={'symbol':SYMBOLS[0],'error':'offline'}
        result=self.scan();self.assertEqual(result['selected'],SYMBOLS[1])
        self.assertEqual(sum(r['status']=='eligible' for r in result['rows']),4)

    def test_depth_fails_closed_for_wide_thin_old_crossed_or_out_of_order(self):
        good=self.api.order_book(SYMBOLS[0])
        bad=[dict(good,T=(self.now-16)*1000),dict(good,asks=[['.093','1000000']]),
             dict(good,asks=[['.09201','.001']]),dict(good,bids=[['.093','1']]),
             dict(good,asks=[['.09201','1'],['.09200','10000']]),dict(good,bids=[]),
             dict(good,asks=[['nan','100']]),dict(good,asks=[['.09206','1000000']])]
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ValueError):
                depth_cost(value,'BUY',100,.092,self.now*1000)

    def test_consumed_candles_and_position_lock_prevent_second_selection(self):
        candle=self.sig['candle']
        self.assertIsNone(self.scan(global_candle=candle)['selected'])
        self.assertIsNone(self.scan(consumed={s:candle for s in SYMBOLS})['selected'])
        inputs=Mock(side_effect=AssertionError('Must not request metadata during position protection'))
        result=scan(self.values,self.news,inputs,self.now,locked=True)
        self.assertIsNone(result['selected']);inputs.assert_not_called()

    def engine(self,directory,api=None,mode='paper'):
        engine=Engine(api or self.api,Path(directory)/('test-'+mode+'.sqlite'),mode)
        engine.s['selection_mode']='auto'
        engine.candidate_provider=lambda:self.values
        engine.news_provider=self.news
        return engine

    def test_paper_end_to_end_selects_one_and_no_immediate_switch_or_restart_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory)
            try:
                self.values[SYMBOLS[0]]['error']='offline'
                e.tick();self.assertIsNone(e.s['position']);self.assertEqual(e.s['symbol'],SYMBOLS[0])
                e.start();e.tick()
                self.assertEqual(e.s['position']['symbol'],SYMBOLS[1]);self.assertEqual(e.s['symbol'],SYMBOLS[1])
                self.values[SYMBOLS[0]]['error']=''
                original=e.s['position']['id'];e.tick();self.assertEqual(e.s['position']['id'],original)
                with self.assertRaises(ValueError):e.maybe_enter()
                e.pause();e.close();e.s['enabled']=True;e.tick()
                self.assertIsNone(e.s['position'],'same candle must not rotate to another commodity')
                e.save();other=Engine(self.api,e.path,'paper')
                self.assertFalse(other.s['enabled']);self.assertEqual(other.s['last_auto_candle'],self.sig['candle']);other.db.close()
                self.assertEqual(self.api.calls,[])
            finally:e.db.close()

    def test_all_rejected_can_start_monitoring_but_never_opens(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory)
            try:
                for v in self.values.values():v['signal']['side']=None
                e.start();e.tick();self.assertTrue(e.s['enabled']);self.assertIsNone(e.s['position'])
                self.assertIn('保持空仓',e.decision['reason'])
                self.assertEqual(self.api.calls,[])
            finally:e.db.close()

    def test_every_symbol_can_be_selected_and_paper_traded_long_or_short(self):
        for side in ('BUY','SELL'):
            rows=copy.deepcopy(self.rows)
            if side=='SELL':
                for row in rows:
                    o,h,l,c=map(float,row[1:5]);row[1:5]=[str(.184-o),str(.184-l),str(.184-h),str(.184-c)]
            sig=signal(rows,self.now*1000);self.assertEqual(sig['side'],side)
            for selected in SYMBOLS:
                with self.subTest(symbol=selected,side=side),tempfile.TemporaryDirectory() as directory:
                    for symbol,value in self.values.items():
                        value.update(rows=rows,signal=copy.deepcopy(sig),error='' if symbol==selected else '测试排除')
                    e=self.engine(directory)
                    try:
                        with patch.object(self.api,'candles',return_value=rows):e.start();e.tick()
                        self.assertIsNotNone(e.s['position'],e.error)
                        self.assertEqual(e.s['position']['symbol'],selected)
                        self.assertEqual(e.s['position']['side'],side)
                        self.assertLessEqual(e.s['position']['planned_loss'],e.s['risk'])
                        self.assertEqual(self.api.calls,[])
                    finally:e.db.close()

    def test_unavailable_display_symbol_does_not_pause_auto_selection_of_other_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory)
            try:
                self.values['XAUUSDT']['error']='offline'
                original=self.api.market
                def market(symbol):
                    if symbol=='XAUUSDT':raise APIError('display offline')
                    return original(symbol)
                e.start()
                with patch.object(self.api,'market',side_effect=market):e.tick()
                self.assertTrue(e.s['enabled'],e.error)
                self.assertEqual(e.s['position']['symbol'],'CLUSDT')
            finally:e.db.close()

    def test_changed_signal_and_depth_before_submission_cancel_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory)
            try:
                e.start();e.refresh_selection();self.assertTrue(e.select_candidate())
                with patch.object(self.api,'order_book',return_value={'T':0,'bids':[],'asks':[]}):e.maybe_enter()
                self.assertIsNone(e.s['position']);self.assertEqual(self.api.calls,[])
                self.assertEqual(e.s['last_auto_candle'],self.sig['candle'])
                self.assertIsNone(e.s['pending'])
            finally:e.db.close()

    def test_auto_mode_configuration_cannot_rearm_or_change_occupied_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory)
            try:
                e.configure(dict(symbol='BZUSDT',risk=1.5,leverage=2,selection_mode='auto'))
                self.assertFalse(e.s['enabled'])
                for bad in ('all',True,None):
                    with self.assertRaises(ValueError):e.configure(dict(symbol='BZUSDT',risk=1.5,leverage=2,selection_mode=bad))
                e.s['pending']={'kind':'entry','symbol':'BZUSDT'}
                with self.assertRaises(ValueError):e.configure(dict(symbol='BZUSDT',risk=1.5,leverage=2,selection_mode='manual'))
                self.assertFalse(e.select_candidate())
            finally:e.db.close()

    def test_candidate_expiring_after_ranking_does_not_change_symbol_or_pause_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory)
            try:
                e.start();e.refresh_selection()
                before=e.s['symbol']
                for value in self.values.values():value['checked']=self.now-21
                self.assertFalse(e.select_candidate())
                self.assertTrue(e.s['enabled']);self.assertEqual(e.s['symbol'],before)
                self.assertIsNone(e.s['position']);self.assertIsNone(e.selection['selected'])
            finally:e.db.close()

    def test_live_double_selects_and_installs_protection_without_real_requests(self):
        api=live.LiveFake();api.order_book=self.api.order_book
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory,api,'live')
            try:
                e.attach_live(api);self.assertFalse(e.s['enabled'])
                e.start();e.tick()
                self.assertIsNotNone(e.s['position']);self.assertIsNone(e.s['pending'])
                self.assertEqual(len(e.s['position']['algos']),2)
                entries=[c for c in api.calls if c[0]=='POST' and c[1].endswith('/order') and c[2].get('reduceOnly')!='true']
                self.assertEqual(len(entries),1)
                e.tick();self.assertEqual(e.s['position']['symbol'],entries[0][2]['symbol'])
            finally:e.db.close()

    def test_late_depth_change_after_leverage_prevents_auto_market_order(self):
        api=live.LiveFake();api.order_book=self.api.order_book
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory,api,'live')
            try:
                e.attach_live(api);e.start()
                original=api.request
                def mutate(method,path,params=None,signed=False):
                    result=original(method,path,params,signed)
                    if method=='POST' and path.endswith('/leverage'):
                        api.order_book=lambda symbol:dict(T=self.now*1000,bids=[['.09','1000000']],asks=[['.094','1000000']])
                    return result
                api.request=mutate;e.tick()
                self.assertFalse(any(c[0]=='POST' and c[1].endswith('/order') for c in api.calls))
                self.assertFalse(e.s['enabled']);self.assertIsNone(e.s['position'])
                self.assertEqual(e.s['pending']['phase'],'preparing')
            finally:e.db.close()

    def test_late_signal_change_after_leverage_prevents_market_order(self):
        api=live.LiveFake();api.order_book=self.api.order_book
        with tempfile.TemporaryDirectory() as directory:
            e=self.engine(directory,api,'live')
            try:
                e.attach_live(api);e.start()
                request=api.request
                def mutate(method,path,params=None,signed=False):
                    result=request(method,path,params,signed)
                    if method=='POST' and path.endswith('/leverage'):e.news_provider=lambda s:None
                    return result
                api.request=mutate;e.tick()
                self.assertFalse(any(c[0]=='POST' and c[1].endswith('/order') for c in api.calls))
                self.assertFalse(e.s['enabled']);self.assertIsNotNone(e.s['pending'])
            finally:e.db.close()


class SchedulingTests(unittest.TestCase):
    def test_auto_ai_round_robin_and_manual_single_symbol(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'BZUSDT','selection_mode':'auto'})
        a=Analysis(engine);a.ai.run_once=Mock(return_value=True)
        for _ in SYMBOLS:a.ai_cycle()
        self.assertEqual([c.args[0] for c in a.ai.run_once.call_args_list],list(SYMBOLS))
        engine.s['selection_mode']='manual';a.ai_cycle();self.assertEqual(a.ai.run_once.call_args.args[0],'BZUSDT')
        engine.s['selection_mode']='auto'
        a.ai.run_once=Mock(return_value=False);self.assertFalse(a.ai_cycle());self.assertEqual(a.ai.run_once.call_count,5)

    def test_all_market_and_kline_caches_isolate_failures(self):
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s={'symbol':'XAUUSDT'})
        a=Analysis(engine);fake=Fake()
        rows,end=candles();rows.append([end,'.092','.093','.091','.092','1',end+3599999])
        with patch.object(a.api,'candles',return_value=rows), \
             patch.object(a.api,'market',side_effect=lambda s:fake.market(s) if s!='BZUSDT' else (_ for _ in ()).throw(APIError('offline'))), \
             patch.object(a.api,'order_book',return_value={'bids':[],'asks':[]}):
            a.kline_cycle();a.market_cycle()
        values=a.candidates();self.assertEqual(set(values),set(SYMBOLS))
        self.assertTrue(values['BZUSDT']['error'])
        self.assertFalse(values['CLUSDT']['error'])
        for symbol in SYMBOLS:self.assertEqual(a.snapshot(symbol)['kline']['symbol'],symbol)
        self.assertEqual(engine.s['symbol'],'XAUUSDT')


class SelectionServerTests(unittest.TestCase):
    setUp=server.LiveServerTests.setUp
    tearDown=server.LiveServerTests.tearDown
    request=server.LiveServerTests.request
    context=server.LiveServerTests.context
    post=server.LiveServerTests.post
    switch=server.LiveServerTests.switch
    def test_auto_scope_must_be_in_start_confirmation(self):
        self.switch();self.live.s['selection_mode']='auto'
        body=dict(confirm_live=True,expected_symbol=self.live.s['symbol'],expected_leverage=self.live.s['leverage'],expected_risk=self.live.s['risk'])
        with patch.object(self.live,'start') as start:
            self.assertEqual(self.post('/api/start',body)[0],400);start.assert_not_called()
            self.assertEqual(self.post('/api/start',dict(body,expected_selection_mode='auto'))[0],200);start.assert_called_once()
