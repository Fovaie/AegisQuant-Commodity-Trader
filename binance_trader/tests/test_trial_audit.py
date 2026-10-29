"""Cross-layer money/risk invariants with fake accounts, never real orders."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from binance_trader.core import plan
from binance_trader.engine import Engine
from binance_trader.selection import scan
from test_live_engine import LiveFake
from test_trader import verified_news


class TrialAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=LiveFake(100)
        self.engine=Engine(self.api,Path(self.temp.name)/'live.sqlite','live')
        self.engine.news_provider=verified_news
        self.engine.quote=self.api.market('XAUUSDT');self.engine.updated=time.time()
        self.engine.sig=dict(side='BUY',atr=.001,reference=.092,previous_close=.091,
                             fast=.092,slow=.09,candle=int(time.time()*1000)-3601000)
        self.engine.attach_live(self.api)
    def tearDown(self):
        self.engine.db.close();self.temp.cleanup()
    def orders(self):
        return [call for call in self.api.calls if call[0]=='POST' and call[1].endswith('/order')]

    def test_preview_and_order_both_use_remaining_half_dollar_daily_budget(self):
        self.engine.start()
        original=plan(self.engine.sig,self.api.price,self.api.filters('XAUUSDT'),1.5,2,100,
                      quote=self.api.market('XAUUSDT'),fees=self.api.commission_rates('XAUUSDT'))
        original.update(symbol='XAUUSDT',id='btaudit',opened=int(time.time()*1000),algos=[],funding_paid=0)
        self.api.wallet=self.api.available=98.5
        self.engine.s.update(has_traded=True,wallet=98.5,available_balance=98.5,last_flat_wallet=98.5)
        self.engine.refresh_risk_preview()
        preview=self.engine.risk_preview
        self.assertTrue(preview['ok'])
        self.assertEqual(preview['risk_budget'],.5)
        for side in ('long','short'):
            self.assertLessEqual(preview[side]['planned_loss'],.5)
            self.assertEqual(preview[side]['risk_budget'],.5)
        self.engine.enter_demo(original)
        self.assertEqual(self.engine.s['position']['risk_budget'],.5)
        self.assertLessEqual(self.engine.s['position']['actual_planned_loss'],.5)
        self.assertEqual(len(self.orders()),1)

    def test_every_account_lock_clears_green_preview_and_explains_all_five_rows(self):
        base=copy.deepcopy(self.engine.s)
        for field,value in (('halted','入金后请核验预算'),('daily_latch',True),('total_latch',True),
                            ('pending',{'kind':'entry'}),('position',{'id':'held'})):
            with self.subTest(field=field):
                self.engine.s=copy.deepcopy(base);self.engine.s[field]=value
                self.engine.risk_preview={'ok':True,'long':{'stale':True}}
                self.engine.last_preview=time.time()
                with patch.object(self.engine,'order_inputs',side_effect=AssertionError('locked metadata')):
                    self.engine.refresh_risk_preview();self.engine.refresh_selection()
                self.assertFalse(self.engine.risk_preview['ok'])
                self.assertIsNone(self.engine.risk_preview['long'])
                reason=self.engine.entry_lock_reason()
                self.assertEqual(self.engine.selection['reason'],reason)
                self.assertTrue(all(row['reason']==reason for row in self.engine.selection['rows']))
        self.engine.s=base
        self.assertEqual(self.orders(),[])

    def test_late_news_change_during_leverage_setup_cancels_manual_market_order(self):
        self.engine.start();original=self.api.request
        def request(method,path,params=None,signed=False):
            result=original(method,path,params,signed)
            if method=='POST' and path.endswith('/leverage'):
                self.engine.news_provider=lambda symbol:None
            return result
        with patch.object(self.api,'request',side_effect=request),self.assertRaisesRegex(ValueError,'提交前'):
            self.engine.maybe_enter()
        self.assertEqual(self.orders(),[])
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.engine.s['pending']['kind'],'entry')

    def test_two_minute_window_expiring_during_manual_setup_cancels_market_order(self):
        clock=[time.time()]
        with patch('time.time',side_effect=lambda:clock[0]):
            self.engine.start();original=self.api.request
            def request(method,path,params=None,signed=False):
                result=original(method,path,params,signed)
                if method=='POST' and path.endswith('/leverage'):clock[0]+=121
                return result
            with patch.object(self.api,'request',side_effect=request),self.assertRaisesRegex(ValueError,'提交前'):
                self.engine.maybe_enter()
        self.assertEqual(self.orders(),[])
        self.assertFalse(self.engine.s['enabled'])

    def test_balance_change_between_planning_and_submission_blocks_before_any_write(self):
        self.engine.start()
        original=self.api.account
        def account():
            self.api.wallet=self.api.available=99
            return original()
        with patch.object(self.api,'account',side_effect=account),self.assertRaisesRegex(ValueError,'账户已锁定'):
            self.engine.maybe_enter()
        self.assertEqual([c for c in self.api.calls if c[0]!='GET'],[])
        self.assertTrue(self.engine.s['halted'])
        self.assertFalse(self.engine.s['enabled'])

    def test_reconnect_remote_closed_position_does_not_double_count_old_floating_loss(self):
        self.engine.start();self.engine.maybe_enter()
        self.api.positions=[]
        self.api.wallet=self.api.available=98.8
        self.engine.quote=dict(self.engine.quote,mark=.08)
        self.api.calls.clear()
        self.engine.attach_live(self.api)
        self.assertTrue(self.engine.s['position']['remote_flat_confirmed'])
        self.assertAlmostEqual(self.engine.strategy_equity(),98.8)
        self.assertFalse(self.engine.s['daily_latch'])
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))
        self.assertFalse(self.engine.s['enabled'])

    def test_daily_reset_keeps_paused_and_never_resets_total_loss_latch(self):
        self.engine.s.update(has_traded=True,wallet=98,daily_latch=True,enabled=False)
        with patch('binance_trader.engine.day',return_value='2099-01-01'):
            self.assertFalse(self.engine.loss_check())
        self.assertEqual(self.engine.s['day_start'],98)
        self.assertFalse(self.engine.s['daily_latch'])
        self.assertFalse(self.engine.s['enabled'])
        self.engine.s.update(wallet=85,total_latch=True)
        with patch('binance_trader.engine.day',return_value='2099-01-02'):
            self.assertTrue(self.engine.loss_check())
        self.assertTrue(self.engine.s['total_latch'])
        self.assertFalse(self.engine.s['enabled'])

    def test_locked_scan_does_not_consume_news_or_any_account_metadata(self):
        provider=Mock(side_effect=AssertionError('must not read'))
        result=scan({},provider,provider,time.time(),locked=True,lock_reason='每日亏损已触发')
        self.assertEqual(len(result['rows']),5)
        self.assertIsNone(result['selected']);provider.assert_not_called()
        self.assertTrue(all(row['reason']=='每日亏损已触发' for row in result['rows']))

    def test_paused_existing_position_cannot_restart_entries_in_either_selection_mode(self):
        self.engine.start();self.engine.maybe_enter();self.engine.pause()
        trade_id=self.engine.s['position']['id']
        self.api.calls.clear()
        for mode in ('manual','auto'):
            self.engine.s['selection_mode']=mode
            with self.assertRaisesRegex(ValueError,'已有持仓'):self.engine.start()
            self.assertFalse(self.engine.s['enabled'])
            self.assertEqual(self.engine.s['position']['id'],trade_id)
        self.assertEqual(self.api.calls,[])
