"""Runtime/HTTP contract tests. All exchange clients are in-memory doubles."""
import http.client
import json
import re
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from binance_trader.__main__ import server_for
from binance_trader.api import APIError
from binance_trader.engine import Engine
from binance_trader.runtime import Runtime
from test_trader import Fake


class LiveServerClient(Fake):
    def __init__(self, demo=False, key='', secret='', live_trading=False):
        super().__init__()
        self.demo, self.key, self.secret = demo, key, secret
        self.live_trading = live_trading
        self.wallet = 300

    def account(self):
        return dict(canTrade=True, multiAssetsMargin=False, totalWalletBalance=str(self.wallet),
                    availableBalance=str(self.wallet), totalUnrealizedProfit='0', positions=self.positions,
                    assets=[dict(asset='USDT', walletBalance=str(self.wallet), availableBalance=str(self.wallet))])

    def request(self, method, path, params=None, signed=False):
        if path == '/fapi/v1/accountConfig':
            self.calls.append((method, path, params))
            return dict(canTrade=True, multiAssetsMargin=False)
        return super().request(method, path, params, signed)


class LiveServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        engines = {
            'paper': Engine(LiveServerClient(), self.directory/'paper.sqlite', 'paper'),
            'live': Engine(LiveServerClient(live_trading=True), self.directory/'live.sqlite', 'live'),
        }
        self.runtime = Runtime(self.directory, engines=engines)
        self.paper, self.live = engines['paper'], engines['live']
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.server = server_for(self.runtime.engine, self.port, runtime=self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, html = self.request('/')
        token = re.search(b'name="csrf" content="([^"]+)"', html).group(1).decode()
        self.headers = {'Origin':'http://127.0.0.1:'+str(self.port), 'X-CSRF-Token':token,
                        'Content-Type':'application/json'}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.runtime.close_databases(); self.temp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def context(self):
        return dict(context_mode=self.runtime.mode, context_id=self.runtime.context_id)

    def post(self, path, payload=None, context=None):
        body = dict(self.context() if context is None else context)
        body.update(payload or {})
        status, data = self.request(path, 'POST', json.dumps(body), self.headers)
        return status, json.loads(data)

    def switch(self, mode='live'):
        status, result = self.post('/api/mode', {'mode':mode})
        self.assertEqual(status, 200, result)

    def connect_live(self):
        with patch('binance_trader.__main__.Binance', side_effect=LiveServerClient) as constructor:
            status, result = self.post('/api/connect', {'mode':'live', 'key':'unit-key-'+'k'*40, 'secret':'unit-secret-'+'s'*40})
        self.assertEqual(status, 200, result)
        constructor.assert_called_once_with(False, 'unit-key-'+'k'*40, 'unit-secret-'+'s'*40, live_trading=True)
        return result

    def test_switch_is_separate_ledger_and_no_credentials_or_automatic_start(self):
        old = self.context()
        self.paper.s['wallet'] = 271.25; self.paper.save()
        self.switch()
        self.assertNotEqual(old['context_id'], self.runtime.context_id)
        self.assertEqual(self.live.s['wallet'], 0)
        self.assertFalse(self.live.live_connected)
        self.assertFalse(self.live.s['enabled'])
        self.assertFalse(self.live.api.key)
        self.assertEqual(json.loads((self.directory/'runtime.json').read_text()), {'mode':'live'})
        self.switch('paper')
        self.assertEqual(self.paper.s['wallet'], 271.25)

    def test_stale_or_missing_context_cannot_reach_mutating_handlers(self):
        old = self.context(); self.switch()
        with patch.object(self.live,'start') as start, patch.object(self.live,'close') as close, \
             patch.object(self.live,'configure') as configure, patch.object(self.live,'apply_leverage') as leverage:
            for endpoint in ('/api/start','/api/close','/api/config','/api/apply-leverage','/api/pause','/api/mode','/api/connect'):
                for context in (old, {}):
                    with self.subTest(endpoint=endpoint,context=context):
                        self.assertEqual(self.post(endpoint, context=context)[0],400)
            start.assert_not_called(); close.assert_not_called(); configure.assert_not_called(); leverage.assert_not_called()

    def test_all_environments_must_be_paused_flat_and_without_pending_for_switch(self):
        for engine in (self.paper,self.live):
            for field,value in (('enabled',True),('position',{'id':'existing'}),('pending',{'id':'unknown'})):
                with self.subTest(mode=engine.mode,field=field):
                    engine.s[field] = value
                    before = self.context()
                    self.assertEqual(self.post('/api/mode',{'mode':'live'})[0],400)
                    self.assertEqual(self.context(),before)
                    engine.s[field] = False if field == 'enabled' else None

    def test_live_connect_is_read_only_memory_only_and_invalidates_old_context(self):
        self.switch(); old = self.context(); self.connect_live()
        self.assertTrue(self.live.live_connected)
        self.assertFalse(self.live.s['enabled'])
        self.assertTrue(self.live.api.calls)
        self.assertTrue(all(c[0] == 'GET' for c in self.live.api.calls))
        self.assertEqual(self.post('/api/start',context=old)[0],400)
        _, data = self.request('/api/state')
        state = json.loads(data)
        self.assertTrue(state['live_connected']); self.assertEqual(state['mode'],'live')
        for raw in ('unit-key-'+'k'*40, 'unit-secret-'+'s'*40):
            self.assertNotIn(raw, data.decode())
            for engine in (self.paper,self.live):
                self.assertNotIn(raw, engine.db.execute('SELECT body FROM state').fetchone()[0])
            self.assertNotIn(raw, (self.directory/'runtime.json').read_text())
        self.assertFalse(list(self.directory.glob('*.credentials.json')))

    def test_failed_live_connection_preserves_context_and_existing_client(self):
        self.switch(); self.connect_live()
        before = self.context(); client = self.live.api
        with patch('binance_trader.__main__.Binance', side_effect=LiveServerClient), \
             patch.object(LiveServerClient,'account',side_effect=APIError('测试账户读取失败')):
            result = self.post('/api/connect', {'mode':'live','key':'unit-key-'+'k'*40,'secret':'different-secret-'+'x'*40})
        self.assertEqual(result[0],400)
        self.assertEqual(self.context(),before)
        self.assertIs(self.live.api,client)
        self.assertFalse(self.live.s['enabled'])

    def test_account_query_validates_new_deposit_rebases_before_trading_and_never_arms(self):
        self.switch();self.connect_live()
        self.live.api.wallet=.00147528
        self.live.check_account()
        self.live.api.wallet=100.00147528
        self.live.sync_demo()
        self.assertTrue(self.live.s['halted'])
        self.live.api.calls.clear()
        status,result=self.post('/api/account')
        self.assertEqual(status,200,result)
        self.assertEqual(result['balance'],100.00147528)
        self.assertEqual(result['capital_budget'],100)
        self.assertEqual(result['capital_limit'],100)
        self.assertEqual(result['daily_loss_limit'],2)
        self.assertEqual(result['loss_limit'],15)
        self.assertFalse(result['enabled']);self.assertFalse(result['halted'])
        self.assertTrue(all(call[0]=='GET' for call in self.live.api.calls))
        self.assertEqual(self.live.s['remote_base'],100.00147528)

    def test_account_query_preserves_frozen_budget_and_rejects_invalid_remote_modes(self):
        self.switch();self.connect_live()
        baseline=self.live.s['remote_base']
        self.live.s['has_traded']=True
        self.live.api.wallet=baseline-1
        status,result=self.post('/api/account')
        self.assertEqual(status,200,result)
        self.assertEqual(self.live.s['remote_base'],baseline)
        self.assertEqual(self.live.capital_budget(),100)
        self.assertEqual(self.live.daily_loss_limit(),2)
        self.assertTrue(result['halted'])
        original=self.live.api.request
        def dual(method,path,params=None,signed=False):
            return {'dualSidePosition':True} if path.endswith('positionSide/dual') else original(method,path,params,signed)
        with patch.object(self.live.api,'request',side_effect=dual):
            self.assertEqual(self.post('/api/account')[0],400)
        self.assertFalse(self.live.s['enabled'])

    def test_readonly_connection_never_upgrades_or_crosses_environment(self):
        old = self.context()
        with patch('binance_trader.__main__.Binance', side_effect=LiveServerClient) as constructor:
            self.assertEqual(self.post('/api/connect',{'mode':'live','key':'k'*40,'secret':'s'*40})[0],400)
            constructor.assert_not_called()
            self.assertEqual(self.post('/api/connect',{'mode':'readonly','key':'k'*40,'secret':'s'*40})[0],200)
        self.assertNotEqual(old['context_id'],self.runtime.context_id)
        self.assertFalse(self.runtime.readonly.live_trading)
        self.assertFalse(self.live.live_connected)
        self.switch()
        self.assertEqual(self.post('/api/account')[0],400)
        with patch('binance_trader.__main__.Binance') as constructor:
            self.assertEqual(self.post('/api/connect',{'mode':'readonly','key':'k'*40,'secret':'s'*40})[0],400)
            constructor.assert_not_called()

    def test_live_actions_require_exact_explicit_confirmation(self):
        self.switch()
        good = dict(confirm_live=True,expected_symbol=self.live.s['symbol'],expected_leverage=self.live.s['leverage'],
                    expected_risk=self.live.s['risk'])
        for endpoint, method in (('/api/start','start'),('/api/apply-leverage','apply_leverage')):
            with patch.object(self.live,method,return_value={'ok':True}) as action, patch.object(self.paper,method,create=True) as paper_action:
                for payload in ({},dict(good,confirm_live='true'),dict(good,expected_symbol='BZUSDT'),dict(good,expected_leverage=50)):
                    self.assertEqual(self.post(endpoint,payload)[0],400)
                action.assert_not_called()
                self.assertEqual(self.post(endpoint,good)[0],200)
                action.assert_called_once_with(); paper_action.assert_not_called()

    def test_start_confirmation_is_bound_to_saved_risk_budget(self):
        self.switch()
        base=dict(confirm_live=True,expected_symbol=self.live.s['symbol'],expected_leverage=self.live.s['leverage'])
        old_risk=self.live.s['risk']
        with patch.object(self.live,'start') as start:
            self.assertEqual(self.post('/api/start',base)[0],400)
            # A second window can change risk without changing symbol or leverage.
            self.live.s['risk']=old_risk+0.5
            self.assertEqual(self.post('/api/start',dict(base,expected_risk=old_risk))[0],400)
            start.assert_not_called()
            self.assertEqual(self.post('/api/start',dict(base,expected_risk=self.live.s['risk']))[0],200)
            start.assert_called_once_with()

    def test_close_confirmation_is_bound_to_current_position(self):
        self.switch(); self.live.s['position']={'id':'position-new'}
        with patch.object(self.live,'close') as close, patch.object(self.live,'pause') as pause:
            base=dict(confirm_live=True,expected_symbol=self.live.s['symbol'])
            for payload in ({},dict(base,expected_position_id='position-old')):
                self.assertEqual(self.post('/api/close',payload)[0],400)
            close.assert_not_called(); pause.assert_not_called()
            self.assertEqual(self.post('/api/close',dict(base,expected_position_id='position-new'))[0],200)
            close.assert_called_once_with(); pause.assert_called_once_with()
        self.live.s['position']=None

    def test_two_same_epoch_switches_do_not_both_commit(self):
        old = self.context()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.post('/api/mode',{'mode':'live'},context=old)[0],range(2)))
        self.assertEqual(sorted(results),[200,400])
        self.assertEqual(self.runtime.mode,'live')

    def test_pending_recovery_routes_require_context_review_and_explicit_confirmation(self):
        self.switch();self.connect_live()
        self.live.s.update(pending=dict(kind='leverage',symbol='XAUUSDT',id='blfixture',leverage=2),halted='杠杆修改结果待核对')
        self.live.save()
        original=self.live.api.request
        calls=[]
        def request(method,path,params=None,signed=False):
            calls.append(method)
            if path=='/fapi/v1/symbolConfig':return [dict(symbol='XAUUSDT',leverage=2)]
            return original(method,path,params,signed)
        with patch.object(self.live.api,'request',side_effect=request):
            self.assertEqual(self.post('/api/pending/inspect',context=dict(context_mode='live',context_id='old'))[0],400)
            status,review=self.post('/api/pending/inspect');self.assertEqual(status,200,review)
            payload=dict(expected_pending_id='blfixture',review_id=review['review_id'])
            self.assertEqual(self.post('/api/pending/resolve',payload)[0],400)
            payload['confirm_reconcile']=True
            status,result=self.post('/api/pending/resolve',payload);self.assertEqual(status,200,result)
            self.assertEqual(self.post('/api/pending/resolve',payload)[0],400)
        self.assertIsNone(self.live.s['pending']);self.assertFalse(self.live.s['enabled'])
        self.assertTrue(all(method=='GET' for method in calls))

    def test_csrf_remains_required_even_with_current_context(self):
        status,_=self.request('/api/mode','POST',json.dumps(dict(self.context(),mode='live')),headers={'Content-Type':'application/json'})
        self.assertEqual(status,403)
        self.assertEqual(self.runtime.mode,'paper')

    def test_server_close_waits_for_accepted_slow_operation(self):
        entered=threading.Event(); release=threading.Event(); closing=threading.Event(); closed=threading.Event()
        def slow_operation():
            entered.set()
            if not release.wait(5): raise RuntimeError('test release timeout')
        def close_server():
            closing.set(); self.server.server_close(); closed.set()
        with patch.object(self.paper,'pause',side_effect=slow_operation), ThreadPoolExecutor(max_workers=1) as pool:
            request=pool.submit(self.post,'/api/pause')
            try:
                self.assertTrue(entered.wait(2),'HTTP handler did not begin')
                self.server.shutdown()
                closer=threading.Thread(target=close_server); closer.start()
                self.assertTrue(closing.wait(2))
                self.assertFalse(closed.wait(.1),'server_close returned while accepted operation was still active')
            finally:
                release.set()
            self.assertEqual(request.result(timeout=2)[0],200)
            self.assertTrue(closed.wait(2))
            closer.join(timeout=2)

    def test_stopping_rejects_post_already_accepted_but_waiting_for_runtime(self):
        entered=threading.Event()
        handler=self.server.RequestHandlerClass
        original=handler.do_POST
        def observed(request_handler):
            entered.set(); return original(request_handler)
        with patch.object(handler,'do_POST',observed), patch.object(self.paper,'pause') as action, \
             ThreadPoolExecutor(max_workers=1) as pool:
            with self.runtime.lock:
                request=pool.submit(self.post,'/api/pause')
                self.assertTrue(entered.wait(2),'request was not accepted')
                self.server.stopping.set()
            status,data=request.result(timeout=2)
            self.assertEqual(status,503)
            self.assertIn('未提交',data['error'])
            action.assert_not_called()

    def test_restarted_live_runtime_never_restores_credentials_or_enabled(self):
        self.switch(); self.connect_live()
        self.live.s['enabled']=True; self.live.save()
        # No network calls occur in constructors; the restarted transport is real
        # but keyless, so this test cannot submit anything to the exchange.
        restarted=Runtime(self.directory)
        try:
            self.assertEqual(restarted.mode,'live')
            self.assertFalse(restarted.engine.s['enabled'])
            self.assertFalse(restarted.engine.live_connected)
            self.assertFalse(restarted.engine.api.key)
            self.assertFalse(restarted.engine.api.secret)
            self.assertNotEqual(restarted.context_id,self.runtime.context_id)
        finally:
            restarted.close_databases()
