"""Explicit live capability tests with fake transport/accounts; no exchange requests."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.error import HTTPError
from urllib.request import Request
from binance_trader.api import Binance, APIError, MAIN, _NoRedirect
from binance_trader.engine import Engine
from test_trader import Fake, verified_news


def response(value=None):
    return io.BytesIO(json.dumps({} if value is None else value).encode())


class LiveTransportTests(unittest.TestCase):
    def client(self, live=True):
        result=Binance(False,'fixture-key','fixture-secret',live_trading=live)
        result.synced=time.time()
        result._clock_anchor=(int(result.synced*1000),time.monotonic(),result.synced)
        return result

    def test_default_mainnet_still_read_only(self):
        with patch('binance_trader.api.urlopen') as network:
            for method in ('POST','PUT','DELETE'):
                with self.assertRaises(APIError):
                    self.client(False).request(method,'/fapi/v1/order',{'symbol':'XAUUSDT'},True)
            network.assert_not_called()
        with self.assertRaises(ValueError): Binance(True,live_trading=True)

    def test_only_precise_signed_commodity_write_dialect_is_allowed(self):
        orders=[('POST','/fapi/v1/marginType',dict(symbol='XAUUSDT',marginType='ISOLATED')),
                ('POST','/fapi/v1/leverage',dict(symbol='XAUUSDT',leverage=2)),
                ('POST','/fapi/v1/order',dict(symbol='CLUSDT',side='BUY',type='MARKET',quantity='1',newClientOrderId='bttest',newOrderRespType='RESULT')),
                ('POST','/fapi/v1/order',dict(symbol='CLUSDT',side='SELL',type='MARKET',quantity='1',reduceOnly='true',newClientOrderId='bctest',newOrderRespType='RESULT')),
                ('POST','/fapi/v1/algoOrder',dict(symbol='BZUSDT',algoType='CONDITIONAL',side='SELL',type='STOP_MARKET',closePosition='true',triggerPrice='90',workingType='MARK_PRICE',clientAlgoId='bttests')),
                ('DELETE','/fapi/v1/order',dict(symbol='CLUSDT',origClientOrderId='bttest'))]
        for method,path,params in orders:
            with self.subTest(path=path,params=params),patch('binance_trader.api.urlopen',return_value=response()) as network:
                self.client().request(method,path,params,True)
                request=network.call_args.args[0]
                self.assertEqual(request.full_url,MAIN+path)
                self.assertEqual(request.method,method)
                self.assertIn('signature',parse_qs(request.data.decode()))
                self.assertEqual(request.get_header('X-mbx-apikey'),'fixture-key')

    def test_transfers_unknown_fields_unsigned_cross_and_unsupported_orders_rejected(self):
        requests=[('POST','/fapi/v1/transfer',dict(symbol='XAUUSDT'),True),
                  ('POST','/sapi/v1/capital/withdraw/apply',{},True),
                  ('PUT','/fapi/v1/leverage',dict(symbol='XAUUSDT',leverage=2),True),
                  ('POST','/fapi/v1/leverage',dict(symbol='XAUUSDT',leverage=2),False),
                  ('POST','/fapi/v1/leverage',dict(symbol='BTCUSDT',leverage=2),True),
                  ('POST','/fapi/v1/leverage',dict(symbol='XAUUSDT',leverage=2,recvWindow=60000),True),
                  ('POST','/fapi/v1/marginType',dict(symbol='XAUUSDT',marginType='CROSSED'),True),
                  ('POST','/fapi/v1/order',dict(symbol='XAUUSDT',side='BUY',type='LIMIT',quantity=1,price=100),True)]
        with patch('binance_trader.api.urlopen') as network:
            for method,path,params,signed in requests:
                with self.subTest(params=params), self.assertRaises(APIError): self.client().request(method,path,params,signed)
            network.assert_not_called()

    def test_cancel_algo_reads_symbol_and_omits_unsupported_symbol_on_wire(self):
        with patch('binance_trader.api.urlopen',side_effect=[response(dict(symbol='XAUUSDT',clientAlgoId='bttests')),response()]) as network:
            self.client().request('DELETE','/fapi/v1/algoOrder',dict(symbol='XAUUSDT',algoId=123),True)
            self.assertEqual(network.call_count,2)
            data=parse_qs(network.call_args.args[0].data.decode())
            self.assertNotIn('symbol',data)
            self.assertEqual(data['algoId'],['123'])
        for remote in (dict(symbol='CLUSDT',clientAlgoId='bttests'),dict(symbol='XAUUSDT',clientAlgoId='foreign')):
            with patch('binance_trader.api.urlopen',return_value=response(remote)) as network:
                with self.assertRaises(APIError): self.client().request('DELETE','/fapi/v1/algoOrder',dict(symbol='XAUUSDT',algoId=123),True)
                self.assertEqual(network.call_count,1)

    def test_reduce_only_and_protection_semantics_reject_mutations(self):
        valid=dict(symbol='XAUUSDT',side='SELL',type='MARKET',quantity='1',newClientOrderId='bctest',newOrderRespType='RESULT',reduceOnly='true')
        for changes in ({'reduceOnly':'false'},{'quantity':True},{'quantity':'nan'},{'quantity':0},{'newClientOrderId':'user-order'}):
            with self.assertRaises(APIError): Binance.validate_live_write('POST','/fapi/v1/order',dict(valid,**changes),True)
        protection=dict(symbol='XAUUSDT',algoType='CONDITIONAL',side='SELL',type='STOP_MARKET',closePosition='true',triggerPrice='99',workingType='MARK_PRICE',clientAlgoId='bttests')
        for changes in ({'closePosition':'false'},{'workingType':'CONTRACT_PRICE'},{'triggerPrice':0},{'quantity':1}):
            with self.assertRaises(APIError): Binance.validate_live_write('POST','/fapi/v1/algoOrder',dict(protection,**changes),True)

    def test_fixed_host_and_redirect_guards(self):
        client=self.client();client.base='https://attacker.invalid'
        with patch('binance_trader.api.urlopen') as network:
            with self.assertRaises(APIError):client.request('GET','/fapi/v3/account',signed=True)
            network.assert_not_called()
        with self.assertRaises(HTTPError):
            _NoRedirect().redirect_request(Request(MAIN+'/fapi/v3/account'),None,302,'redirect',{},'https://attacker.invalid')


class LiveFake(Fake):
    demo=False
    live_trading=True
    def __init__(self,wallet=300):
        super().__init__();self.wallet=wallet;self.available=wallet
    def account(self):
        return dict(super().account(),assets=[dict(asset='USDT',walletBalance=str(self.wallet),availableBalance=str(self.available),unrealizedProfit='0')])
    def request(self,method,path,params=None,signed=False):
        if method=='GET' and path.endswith('accountConfig'):
            self.calls.append((method,path,params))
            return dict(canTrade=True,multiAssetsMargin=False,dualSidePosition=False)
        if method=='GET' and path.endswith('openAlgoOrders'):
            self.calls.append((method,path,params))
            return list(self.algos.values())
        if method=='DELETE' and path.endswith('algoOrder'):
            self.algos.pop(params['algoId'],None)
        before=list(self.positions)
        result=super().request(method,path,params,signed)
        if method=='POST' and path.endswith('/order'):
            qty=float(params['quantity'])
            if params.get('reduceOnly')=='true' and before:
                position=before[0]
                self.wallet+=(self.price-float(position['entryPrice']))*float(position['positionAmt'])
            self.wallet-=qty*self.price*.0005
            self.available=self.wallet
        return result


class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'live.sqlite'
        self.api=LiveFake()
        self.engine=Engine(self.api,self.path,'live')
        self.engine.news_provider=verified_news
        self.arm()
    def tearDown(self):
        self.engine.db.close();self.temp.cleanup()
    def arm(self):
        self.engine.quote=self.api.market('XAUUSDT');self.engine.updated=time.time()
        self.engine.sig=dict(side='BUY',atr=.001,reference=.092,previous_close=.091,fast=.092,slow=.09,candle=int(time.time()*1000)-3601000)
    def connect(self):return self.engine.attach_live(self.api)
    def enter(self):
        self.connect();self.engine.start();self.engine.maybe_enter()
    def writes(self):return [(method,path,p) for method,path,p in self.api.calls if method!='GET']

    def test_live_requires_opt_in_and_different_ledger(self):
        with self.assertRaises(ValueError):Engine(Fake(),Path(self.temp.name)/'wrong.sqlite','live')
        with self.assertRaises(ValueError):Engine(self.api,self.path,'paper')
        with self.assertRaises(ValueError):self.engine.start()
        with self.assertRaises(ValueError):self.engine.maybe_enter()
        self.assertEqual(self.writes(),[])

    def test_attach_reads_only_never_arms_and_budget_uses_actual_usdt(self):
        self.api.wallet=self.api.available=80
        result=self.connect()
        self.assertEqual(result['balance'],80)
        self.assertEqual(result['available'],80)
        self.assertEqual(result['positions'],0)
        self.assertEqual(self.engine.s['wallet'],80)
        self.assertEqual(self.engine.capital_budget(),80)
        self.assertEqual(self.engine.loss_limit(),12)
        self.assertEqual(self.engine.daily_loss_limit(),1.6)
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.writes(),[])
        with self.assertRaises(ValueError): self.engine.maybe_enter()

    def test_larger_actual_account_does_not_inflate_strategy_budget(self):
        self.api.wallet=self.api.available=1000
        self.connect()
        self.assertEqual(self.engine.equity(),1000)
        self.assertEqual(self.engine.strategy_equity(),100)
        self.assertEqual(self.engine.sizing_balance(),100)
        self.assertEqual(self.engine.snapshot()['initial_equity'],1000)
        self.assertEqual(self.engine.snapshot()['capital_limit'],100)
        self.assertEqual(self.engine.loss_limit(),15)
        self.assertEqual(self.engine.daily_loss_limit(),2)

    def test_live_orders_protection_and_close_use_remote_not_paper_wallet(self):
        self.enter()
        p=self.engine.s['position']
        self.assertIsNotNone(p)
        self.assertEqual(len(p['algos']),2)
        self.assertIsNone(self.engine.s['pending'])
        self.engine.close()
        self.assertIsNone(self.engine.s['position'])
        self.assertIsNone(self.engine.s['pending'])
        self.assertAlmostEqual(self.engine.s['wallet'],self.api.wallet)
        self.assertLess(self.engine.s['wallet'],300)
        self.assertEqual(self.engine.s['remote_base'],300)
        orders=[params for method,path,params in self.writes() if path.endswith('/order') and method=='POST']
        self.assertEqual(len(orders),2)
        self.assertEqual(orders[-1]['reduceOnly'],'true')

    def test_live_unsafe_venue_liquidation_emergency_close_keeps_baseline_frozen(self):
        self.connect();self.engine.start();self.api.liquidation_override=.091
        with self.assertRaises(ValueError):self.engine.maybe_enter()
        orders=[params for method,path,params in self.writes() if path.endswith('/order') and method=='POST']
        self.assertEqual(len(orders),2)
        self.assertEqual(orders[-1]['reduceOnly'],'true')
        self.assertTrue(self.engine.s['has_traded'])
        self.assertTrue(self.engine.s['halted'])
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.engine.s['remote_base'],300)

    def test_available_balance_is_not_replaced_by_total_wallet(self):
        self.api.available=.001
        self.connect()
        self.assertEqual(self.engine.s['wallet'],300)
        self.assertEqual(self.engine.sizing_balance(),.001)
        with self.assertRaisesRegex(ValueError,'余额不足'):self.engine.start()
        self.assertEqual(self.writes(),[])

    def test_apply_leverage_is_explicit_flat_paused_durable_and_unknown_locks(self):
        self.connect()
        self.engine.configure(dict(symbol='XAUUSDT',risk=1.5,leverage=3))
        self.assertEqual(self.writes(),[])
        result=self.engine.apply_leverage('XAUUSDT',3)
        self.assertEqual(result['leverage'],3)
        self.assertIsNone(self.engine.s['pending'])
        self.assertFalse(self.engine.s['enabled'])
        self.engine.configure(dict(symbol='XAUUSDT',risk=1.5,leverage=2))
        original=self.api.request
        def fail(method,path,params=None,signed=False):
            if method=='POST':
                self.assertEqual(self.engine.s['pending']['kind'],'leverage')
                persisted=json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
                self.assertEqual(persisted['pending']['leverage'],2)
                raise APIError('unknown response')
            return original(method,path,params,signed)
        with patch.object(self.api,'request',side_effect=fail):
            with self.assertRaises(APIError):self.engine.apply_leverage()
        self.assertEqual(self.engine.s['pending']['kind'],'leverage')
        self.assertTrue(self.engine.s['halted'])
        with self.assertRaises(ValueError): self.engine.apply_leverage()

    def test_external_flat_balance_change_latches_without_resetting_baseline(self):
        self.enter();self.engine.close();self.api.wallet=self.api.available=400
        self.engine.sync_demo()
        self.assertEqual(self.engine.s['remote_base'],300)
        self.assertEqual(self.engine.capital_budget(),100)
        self.assertTrue(self.engine.s['halted'])
        self.assertFalse(self.engine.s['enabled'])
        self.connect()
        self.assertEqual(self.engine.s['remote_base'],300)
        self.assertTrue(self.engine.s['halted'])

    def test_zero_balance_can_connect_then_fund_before_first_real_entry(self):
        self.api.wallet=self.api.available=0
        self.connect()
        self.assertFalse(self.engine.s['total_latch'])
        self.assertFalse(self.engine.s['has_traded'])
        with self.assertRaisesRegex(ValueError,'余额不足'):self.engine.start()
        self.assertEqual(self.writes(),[])
        self.api.wallet=self.api.available=300
        self.engine.start()
        self.assertEqual(self.engine.s['remote_base'],300)
        self.assertEqual(self.engine.capital_budget(),100)
        self.assertFalse(self.engine.s['has_traded'])
        self.assertEqual(self.writes(),[])
        self.engine.maybe_enter()
        self.assertTrue(self.engine.s['has_traded'])

    def test_key_account_modes_and_foreign_positions_rejected_without_writes(self):
        self.connect()
        other=LiveFake();other.key='other-key'
        with self.assertRaises(ValueError):self.engine.attach_live(other)
        self.api.positions=[dict(symbol='BTCUSDT',positionAmt='1',entryPrice='100')]
        with self.assertRaises(ValueError):self.engine.attach_live(self.api)
        self.api.positions=[]
        original=self.api.request
        def dual(method,path,params=None,signed=False):
            return {'dualSidePosition':True} if path.endswith('positionSide/dual') else original(method,path,params,signed)
        with patch.object(self.api,'request',side_effect=dual),self.assertRaises(ValueError):self.engine.attach_live(self.api)
        self.assertEqual(self.writes(),[])

    def test_trial_cap_handles_empty_partial_full_and_excess_funding(self):
        for wallet in (0, .00147528, 40, 100, 100.00147528, 1000):
            with self.subTest(wallet=wallet):
                self.api.wallet=self.api.available=wallet
                self.connect()
                budget=min(100,wallet)
                self.assertEqual(self.engine.capital_budget(),budget)
                self.assertEqual(self.engine.sizing_balance(),budget)
                self.assertAlmostEqual(self.engine.loss_limit(),budget*.15)
                self.assertAlmostEqual(self.engine.daily_loss_limit(),budget*.02)
                self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.writes(),[])

    def test_hundred_trial_remaining_daily_risk_and_latch(self):
        self.api.wallet=self.api.available=100
        self.connect()
        self.engine.s.update(has_traded=True,leverage=15,wallet=98.5)
        self.assertFalse(self.engine.loss_check())
        self.assertAlmostEqual(self.engine.remaining_risk(),.5)
        self.engine.s.update(wallet=98,enabled=True)
        self.assertTrue(self.engine.loss_check())
        self.assertTrue(self.engine.s['daily_latch'])
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.engine.s['leverage'],15)
        self.assertEqual(self.writes(),[])

    def test_untouched_legacy_ledger_adopts_cap_without_erasing_loss_latches(self):
        self.connect()
        self.engine.s.pop('capital_limit')
        self.engine.s.update(capital_budget=300,loss_limit=45,daily_loss_limit=6,
                             day_start=299.5,leverage=15,daily_latch=True)
        self.engine.save();self.engine.db.close()
        self.engine=Engine(self.api,self.path,'live')
        self.assertEqual(self.engine.capital_budget(),100)
        self.assertEqual(self.engine.s['day_start'],99.5)
        self.assertTrue(self.engine.s['daily_latch'])
        self.assertEqual(self.engine.s['leverage'],15)
        self.assertFalse(self.engine.live_connected)
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.writes(),[])

    def test_preexisting_traded_ledger_keeps_frozen_budget_and_losses(self):
        self.connect()
        self.engine.s.pop('capital_limit')
        self.engine.s.update(has_traded=True,capital_budget=300,loss_limit=45,
                             daily_loss_limit=6,day_start=297,wallet=296)
        self.engine.save();self.engine.db.close()
        self.engine=Engine(self.api,self.path,'live')
        self.assertEqual(self.engine.s['capital_limit'],300)
        self.assertEqual(self.engine.capital_budget(),300)
        self.assertEqual(self.engine.strategy_equity(),296)
        self.assertEqual(self.engine.s['day_start'],297)
        self.assertEqual(self.engine.loss_limit(),45)
        self.assertEqual(self.engine.daily_loss_limit(),6)
        self.assertFalse(self.engine.s['enabled'])

    def test_journal_retains_trade_fills_and_history_beyond_ui_window(self):
        self.enter()
        trade_id=self.engine.s['position']['id']
        self.engine.close()
        for index in range(151):self.engine.event('复盘测试记录 '+str(index))
        self.engine.save()
        raw=[r[0] for r in self.engine.db.execute('SELECT body FROM journal ORDER BY id')]
        records=[json.loads(r) for r in raw]
        fills=[r for r in records if r.get('execution',{}) and r['execution'].get('status')=='FILLED']
        self.assertEqual(len(fills),2)
        self.assertEqual([r['trade']['id'] for r in fills],[trade_id,trade_id])
        self.assertTrue(all(float(r['execution']['avgPrice'])>0 for r in fills))
        self.assertTrue(all('实际成交手续费' in r['cost_note'] for r in fills))
        self.assertEqual(len(self.engine.s['events']),150)
        self.assertGreater(len(records),150)
        self.assertNotIn(self.api.key,'\n'.join(raw))
        self.assertNotIn(self.api.secret,'\n'.join(raw))
        self.engine.db.close();self.engine=Engine(self.api,self.path,'live')
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM journal').fetchone()[0],len(records))
        self.assertFalse(self.engine.s['enabled'])

    def test_restart_disconnected_preserves_position_and_only_reads_public_data(self):
        self.enter(); saved=self.engine.s['position']['id']
        self.engine.db.close()
        self.engine=Engine(self.api,self.path,'live')
        self.api.calls.clear()
        with patch.object(self.api,'account',side_effect=AssertionError('signed account read')), \
             patch.object(self.api,'commission_rates',side_effect=AssertionError('signed fee read')):
            self.engine.tick()
        self.assertFalse(self.engine.live_connected)
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.engine.s['position']['id'],saved)
        self.assertEqual(self.writes(),[])
        self.connect()
        self.assertEqual(self.engine.s['position']['id'],saved)
        self.assertEqual(self.writes(),[])

    def test_remote_exit_while_offline_can_reconnect_get_only_then_sync_cleanup(self):
        self.enter();saved=self.engine.s['position']['id']
        self.engine.db.close();self.engine=Engine(self.api,self.path,'live')
        self.api.positions=[]
        for row in self.api.algos.values():row['algoStatus']='FINISHED'
        self.api.calls.clear()
        self.connect()
        self.assertEqual(self.engine.s['position']['id'],saved)
        self.assertEqual(self.writes(),[])
        self.engine.sync_demo()
        self.assertIsNone(self.engine.s['position'])
        self.assertTrue(all(method=='DELETE' and path.endswith('algoOrder') for method,path,p in self.writes()))

    def test_unknown_live_entry_remains_durable_and_never_retries(self):
        self.connect();self.engine.start();self.api.unknown_entry=True
        with self.assertRaises(APIError):self.engine.maybe_enter()
        self.assertIsNotNone(self.engine.s['pending'])
        self.assertTrue(self.engine.s['halted'])
        count=len(self.writes())
        self.engine.tick();self.engine.tick()
        self.assertEqual(len(self.writes()),count)
        self.engine.db.close();self.engine=Engine(self.api,self.path,'live')
        self.assertIsNotNone(self.engine.s['pending'])
        self.assertFalse(self.engine.live_connected)
        self.assertFalse(self.engine.s['enabled'])

if __name__=='__main__':unittest.main()
