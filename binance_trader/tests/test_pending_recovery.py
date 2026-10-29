"""Recovery uses read-only fake venue evidence; no network or real ledger."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from binance_trader.api import APIError
from binance_trader.engine import Engine
from test_live_engine import LiveFake

class PendingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=LiveFake()
        self.engine=Engine(self.api,Path(self.temp.name)/'live.sqlite','live')
        self.engine.attach_live(self.api)
        self.original=self.api.request
        self.row=None
        self.config=[dict(symbol='XAUUSDT',leverage=2)]
        self.calls=[]
        def request(method,path,params=None,signed=False):
            self.calls.append((method,path,params))
            if method=='GET' and path=='/fapi/v1/symbolConfig':return copy.deepcopy(self.config)
            if method=='GET' and path=='/fapi/v1/order':
                if isinstance(self.row,Exception):raise self.row
                return copy.deepcopy(self.row)
            return self.original(method,path,params,signed)
        self.api.request=request
    def tearDown(self):
        self.engine.db.close();self.temp.cleanup()
    def pending(self,kind='leverage'):
        prefix={'leverage':'bl','entry':'bt','close':'bc'}[kind]
        self.engine.s.update(enabled=False,halted='杠杆修改结果待核对；禁止重试或开仓，请在交易所检查' if kind=='leverage' else '上次交易未完成核对',pending=dict(kind=kind,symbol='XAUUSDT',id=prefix+'fixture',**({'leverage':2} if kind=='leverage' else {})))
        if kind!='leverage':
            self.row=dict(symbol='XAUUSDT',clientOrderId=prefix+'fixture',type='MARKET',positionSide='BOTH',side='BUY' if kind=='entry' else 'SELL',reduceOnly=kind=='close',status='FILLED',origQty='1',executedQty='1',avgPrice='.092')
        self.engine.save();self.calls.clear()
    def resolve(self):
        review=self.engine.inspect_pending()
        self.assertTrue(review['can_resolve'],review)
        return self.engine.resolve_pending(review['pending_id'],review['review_id'])
    def assert_readonly(self):self.assertTrue(all(method=='GET' for method,_,_ in self.calls),self.calls)
    def test_accepted_leverage_lost_response_is_resolved_without_repeating_write(self):
        self.pending();before=copy.deepcopy(self.engine.s)
        review=self.engine.inspect_pending()
        self.assertEqual(self.engine.s,before)
        self.assertTrue(review['can_resolve']);self.resolve()
        self.assertIsNone(self.engine.s['pending']);self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.engine.s['halted'],'');self.assert_readonly()
        saved=json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        self.assertIsNone(saved['pending'])
    def test_different_leverage_or_unknown_order_cannot_unlock(self):
        self.pending();self.config[0]['leverage']=3
        self.assertFalse(self.engine.inspect_pending()['can_resolve'])
        self.pending('entry');self.row=APIError('not found',-2013)
        with self.assertRaises(APIError):self.engine.inspect_pending()
        self.assertIsNotNone(self.engine.s['pending']);self.assert_readonly()
    def test_terminal_entry_and_close_flat_preserve_budgets_and_latches(self):
        for kind in ('entry','close'):
            with self.subTest(kind=kind):
                self.pending(kind)
                self.engine.s.update(has_traded=True,total_latch=True,daily_latch=True)
                fields=('remote_base','capital_budget','capital_limit','loss_limit','daily_loss_limit','day_start','total_latch','daily_latch','last_candle','last_entry_close','has_traded')
                before={k:copy.deepcopy(self.engine.s[k]) for k in fields}
                self.resolve()
                self.assertEqual(before,{k:self.engine.s[k] for k in fields});self.assert_readonly()
    def test_nonterminal_orders_never_resolve_even_when_account_is_flat(self):
        for status in ('NEW','PARTIALLY_FILLED','PENDING_CANCEL','unknown'):
            self.pending('entry');self.row['status']=status
            self.assertFalse(self.engine.inspect_pending()['can_resolve'])
            self.assertIsNotNone(self.engine.s['pending'])
    def test_order_identity_quantity_and_semantics_must_match(self):
        for changes in (dict(clientOrderId='foreign'),dict(symbol='CLUSDT'),dict(reduceOnly=True),dict(positionSide='LONG'),dict(type='LIMIT'),dict(executedQty='NaN'),dict(executedQty='0'),dict(origQty='0')):
            with self.subTest(changes=changes):
                self.pending('entry');self.row.update(changes)
                with self.assertRaises((ValueError,APIError)):self.engine.inspect_pending()
                self.assertIsNotNone(self.engine.s['pending'])
    def test_expired_or_changed_review_requires_new_confirmation(self):
        self.pending();review=self.engine.inspect_pending()
        self.engine.pending_review['checked_at']-=61
        with self.assertRaisesRegex(ValueError,'失效'):self.engine.resolve_pending(review['pending_id'],review['review_id'])
        review=self.engine.inspect_pending();self.config[0]['leverage']=3
        with self.assertRaisesRegex(ValueError,'改变'):self.engine.resolve_pending(review['pending_id'],review['review_id'])
        self.assertIsNotNone(self.engine.s['pending']);self.assert_readonly()
    def test_failed_save_rolls_back_memory_and_keeps_durable_pending(self):
        self.pending();review=self.engine.inspect_pending();before=copy.deepcopy(self.engine.s)
        count=self.engine.db.execute('SELECT COUNT(*) FROM journal').fetchone()[0]
        with patch.object(self.engine,'save',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):self.engine.resolve_pending(review['pending_id'],review['review_id'])
        self.assertEqual(self.engine.s,before)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM journal').fetchone()[0],count)
        self.assertIsNotNone(json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])['pending'])
    def test_unrelated_halt_and_wallet_loss_survive_reconciliation(self):
        self.pending();self.engine.s['halted']='人工核对外部划转'
        self.api.wallet=self.api.available=280
        self.resolve()
        self.assertEqual(self.engine.s['halted'],'人工核对外部划转')
        self.assertEqual(self.engine.s['remote_base'],300)
        self.assertEqual(self.engine.s['capital_budget'],100)
        self.assertTrue(self.engine.s['total_latch']);self.assertTrue(self.engine.s['daily_latch'])
    def test_remaining_orders_or_cross_endpoint_disagreement_cannot_resolve(self):
        self.pending()
        with patch.object(self.api,'position_risk',return_value=[dict(symbol='XAUUSDT',positionAmt='1')]):
            self.assertFalse(self.engine.inspect_pending()['can_resolve'])
        current=self.api.request
        def orders(method,path,params=None,signed=False):
            if path=='/fapi/v1/openOrders':return [dict(clientOrderId='blfixture')]
            return current(method,path,params,signed)
        with patch.object(self.api,'request',side_effect=orders):
            self.assertFalse(self.engine.inspect_pending()['can_resolve'])
    def test_durable_preparing_phase_proves_no_entry_order_was_submitted(self):
        self.pending('entry');self.engine.s['pending']['phase']='preparing'
        self.row=APIError('not found',-2013)
        self.resolve();self.assertIsNone(self.engine.s['pending']);self.assert_readonly()
        self.assertFalse(any(path=='/fapi/v1/order' for _,path,_ in self.calls))
        self.pending('entry');self.engine.s['pending']['phase']='submitting'
        self.row=APIError('not found',-2013)
        with self.assertRaises(APIError):self.engine.inspect_pending()
        self.assertIsNotNone(self.engine.s['pending'])

    def test_entry_intent_phase_is_durable_before_any_entry_order_write(self):
        import time
        from test_trader import verified_news
        self.engine.news_provider=verified_news
        self.engine.quote=self.api.market('XAUUSDT');self.engine.updated=time.time()
        self.engine.sig=dict(side='BUY',atr=.001,reference=.092,previous_close=.091,fast=.092,slow=.09,candle=int(time.time()*1000)-3601000)
        current=self.api.request
        phases=[]
        def request(method,path,params=None,signed=False):
            if method=='POST' and path in ('/fapi/v1/marginType','/fapi/v1/order'):
                state=json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
                phases.append((path,state['pending']['phase']))
            return current(method,path,params,signed)
        with patch.object(self.api,'request',side_effect=request):
            self.engine.start();self.engine.maybe_enter()
        self.assertEqual(phases,[('/fapi/v1/marginType','preparing'),('/fapi/v1/order','submitting')])

    def test_protected_entry_can_resume_monitoring_without_new_exchange_writes(self):
        import time
        from test_trader import verified_news
        self.engine.news_provider=verified_news
        self.engine.quote=self.api.market('XAUUSDT');self.engine.updated=time.time()
        self.engine.sig=dict(side='BUY',atr=.001,reference=.092,previous_close=.091,fast=.092,slow=.09,candle=int(time.time()*1000)-3601000)
        self.engine.start();self.engine.maybe_enter()
        p=copy.deepcopy(self.engine.s['position'])
        self.pending('entry');self.engine.s['pending']['id']=p['id']
        self.row.update(clientOrderId=p['id'],origQty=str(p['quantity']),executedQty=str(p['quantity']),avgPrice=str(p['entry']))
        self.calls.clear()
        self.resolve()
        self.assertIsNotNone(self.engine.s['position']);self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.engine.s['position']['algos'],p['algos']);self.assert_readonly()

    def test_successful_confirmation_cannot_be_replayed(self):
        self.pending();review=self.engine.inspect_pending()
        self.engine.resolve_pending(review['pending_id'],review['review_id'])
        with self.assertRaises(ValueError):self.engine.resolve_pending(review['pending_id'],review['review_id'])

if __name__=='__main__':unittest.main()
