"""Failure injection for execution safety; all accounts and fills are in-memory fakes."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from binance_trader.api import APIError
from binance_trader.engine import Engine
from test_trader import Fake, verified_news


class EngineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = Fake()
        self.path = Path(self.temp.name) / 'paper.sqlite'
        self.engine = Engine(self.api, self.path)
        self.engine.news_provider = verified_news
        self.arm()

    def tearDown(self):
        self.engine.db.close()
        self.temp.cleanup()

    def arm(self, side='BUY'):
        self.engine.quote = self.api.market('XAUUSDT')
        self.engine.updated = time.time()
        self.engine.sig = dict(side=side, atr=.001, reference=.092,
                               candle=int(time.time()*1000)-3_600_000-1000,
                               fast=.092 if side == 'BUY' else .09,
                               slow=.09 if side == 'BUY' else .092,
                               previous_close=.091 if side == 'BUY' else .093)

    def demo(self):
        self.engine.db.close()
        self.api.demo = True
        self.path = Path(self.temp.name) / 'demo.sqlite'
        self.engine = Engine(self.api, self.path, 'demo')
        self.engine.news_provider = verified_news
        self.arm()

    def orders(self):
        return [p for method, path, p in self.api.calls if method == 'POST' and path.endswith('/order')]

    def overdue_funding(self):
        self.engine.maybe_enter()
        position = self.engine.s['position']
        now = int(time.time()*1000)
        position['opened'] = now-7_200_000
        position['next_funding_time'] = now-120_000
        position['funding_cursor'] = now
        self.engine.quote = self.api.market('XAUUSDT')
        return position, position['next_funding_time']

    def test_exchange_eligible_high_leverage_still_refuses_unsafe_order(self):
        limits = self.api.leverage_limits('XAUUSDT')
        limits['maximum'] = 100
        limits['brackets'] = [dict(leverage=100, floor=0, cap=100000, maintMarginRatio=.005, cum=0)]
        self.engine.s['leverage'] = 100
        with patch.object(self.engine, 'leverage_limits', return_value=limits):
            self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.orders(), [])
        self.assertIn('强平', self.engine.decision['reason'])

    def test_overdue_unpublished_funding_does_not_advance_cursor(self):
        p, expected = self.overdue_funding()
        with self.assertRaisesRegex(ValueError, '资金费'):
            self.engine.settle_paper_funding(p, self.engine.quote['server'], force=True)
        self.assertLess(p['funding_cursor'], expected)
        self.assertEqual(p['funding_paid'], 0)
        charge = p['quantity']*.092*.0002
        wallet = self.engine.s['wallet']
        self.api.funding = [dict(fundingTime=expected, fundingRate=.0002, markPrice=.092)]
        self.engine.settle_paper_funding(p, self.engine.quote['server'], force=True)
        self.assertAlmostEqual(p['funding_paid'], charge)
        self.assertAlmostEqual(self.engine.s['wallet'], wallet-charge)
        self.engine.settle_paper_funding(p, self.engine.quote['server'], force=True)
        self.assertAlmostEqual(self.engine.s['wallet'], wallet-charge)

    def test_manual_close_persists_late_funding_and_reconciles_after_restart(self):
        p, expected = self.overdue_funding()
        quantity, reserve = p['quantity'], p['risk_audit']['funding_reserve']
        self.engine.close()
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(len(self.engine.s['funding_unresolved']), 1)
        self.assertFalse(self.engine.s['enabled'])
        self.assertIn('资金费待核对', self.engine.s['halted'])
        after_close = self.engine.s['wallet']
        self.engine.db.close()
        self.engine = Engine(self.api, self.path)
        self.assertEqual(len(self.engine.s['funding_unresolved']), 1)
        self.api.funding = [dict(fundingTime=expected, fundingRate=.0002, markPrice=.092)]
        self.engine.reconcile_paper_funding()
        self.assertEqual(self.engine.s['funding_unresolved'], [])
        self.assertAlmostEqual(self.engine.s['wallet'], after_close+reserve-quantity*.092*.0002)
        self.assertEqual(self.engine.s['halted'], '')
        self.assertFalse(self.engine.s['enabled'])
        settled = self.engine.s['wallet']
        self.engine.funding_reconcile_due = 0
        self.engine.reconcile_paper_funding()
        self.assertEqual(self.engine.s['wallet'], settled)

    def test_local_protective_close_uses_supplied_fresh_quote_without_network(self):
        self.engine.maybe_enter()
        quote = self.api.market('XAUUSDT')
        with patch.object(self.api, 'market', side_effect=APIError('network down')):
            self.engine.close('本地保护退出', quote=quote)
        self.assertIsNone(self.engine.s['position'])

    def test_unsafe_actual_liquidation_triggers_one_emergency_reduce_only_close(self):
        self.demo()
        self.api.liquidation_override = .091
        with self.assertRaises(ValueError): self.engine.maybe_enter()
        orders = self.orders()
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[-1]['reduceOnly'], 'true')
        self.assertEqual(orders[0]['quantity'], orders[-1]['quantity'])
        self.assertFalse(self.engine.s['enabled'])
        self.assertTrue(self.engine.s['halted'])
        self.assertIsNone(self.engine.s['position'])

    def test_paper_gap_through_liquidation_records_full_isolated_margin_loss(self):
        self.engine.maybe_enter()
        margin = self.engine.s['position']['initial_margin']
        self.api.price = .03
        self.engine.tick()
        self.assertIsNone(self.engine.s['position'])
        self.assertIn('强平', self.engine.s['halted'])
        self.assertFalse(self.engine.s['enabled'])
        self.assertAlmostEqual(self.engine.s['wallet'], 300-margin)

    def test_modified_protection_detected_and_closed_during_sync(self):
        self.demo(); self.engine.maybe_enter()
        first = self.engine.s['position']['algos'][0]
        self.api.algos[first]['workingType'] = 'CONTRACT_PRICE'
        self.engine.sync_demo()
        self.assertEqual(len(self.orders()), 2)
        self.assertEqual(self.orders()[-1]['reduceOnly'], 'true')
        self.assertIsNone(self.engine.s['position'])
        self.assertTrue(self.engine.s['halted'])

    def test_known_partial_fill_cancels_remainder_and_reduces_confirmed_quantity(self):
        self.demo()
        original = self.api.request
        known = {}
        def request(method, path, params=None, signed=False):
            if path.endswith('/order') and method == 'POST' and params.get('reduceOnly') != 'true':
                response = original(method, path, params, signed)
                quantity = float(response['executedQty'])/2
                known['quantity'] = quantity
                self.api.positions[0]['positionAmt'] = str(quantity)
                return dict(response, status='PARTIALLY_FILLED', executedQty=str(quantity))
            if path.endswith('/order') and method in ('GET', 'DELETE'):
                self.api.calls.append((method, path, params))
                return dict(status='CANCELED', avgPrice=str(self.api.price), executedQty=str(known['quantity']))
            return original(method, path, params, signed)
        with patch.object(self.api, 'request', side_effect=request):
            with self.assertRaises(APIError): self.engine.maybe_enter()
        orders = self.orders()
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[-1]['reduceOnly'], 'true')
        self.assertEqual(float(orders[-1]['quantity']), known['quantity'])
        self.assertTrue(any(method == 'DELETE' and path.endswith('/order') for method,path,_ in self.api.calls))
        self.assertEqual(self.api.positions, [])
        self.assertTrue(self.engine.s['halted'])

    def test_close_fill_quantity_mismatch_keeps_pending_and_never_retries(self):
        self.demo(); self.engine.maybe_enter()
        original = self.api.request
        def request(method, path, params=None, signed=False):
            result = original(method, path, params, signed)
            if method == 'POST' and path.endswith('/order') and params.get('reduceOnly') == 'true':
                return dict(result, executedQty=str(float(result['executedQty'])/2))
            return result
        with patch.object(self.api, 'request', side_effect=request):
            with self.assertRaises(APIError): self.engine.close()
        self.assertIsNotNone(self.engine.s['pending'])
        self.assertIsNotNone(self.engine.s['position'])
        self.assertTrue(self.engine.s['halted'])
        self.assertFalse(any(method == 'DELETE' and path.endswith('algoOrder') for method,path,_ in self.api.calls))
        count = len(self.orders())
        self.engine.sync_demo()
        self.assertEqual(len(self.orders()), count)

    def test_partial_fill_unknown_cancellation_preserves_residual_entry_intent(self):
        self.demo()
        original = self.api.request
        def request(method, path, params=None, signed=False):
            if path.endswith('/order') and method == 'POST' and params.get('reduceOnly') != 'true':
                response = original(method, path, params, signed)
                quantity = float(response['executedQty'])/2
                self.api.positions[0]['positionAmt'] = str(quantity)
                return dict(response, status='PARTIALLY_FILLED', executedQty=str(quantity))
            if path.endswith('/order') and method == 'DELETE':
                raise APIError('cancellation timeout')
            return original(method, path, params, signed)
        with patch.object(self.api, 'request', side_effect=request):
            with self.assertRaises(APIError): self.engine.maybe_enter()
        self.assertEqual(len(self.orders()), 2)
        self.assertEqual(self.orders()[-1]['reduceOnly'], 'true')
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.engine.s['pending']['kind'], 'entry')
        self.assertTrue(self.engine.s['halted'])
        self.engine.tick()
        self.assertEqual(len(self.orders()), 2)

    def test_empty_account_disagreeing_with_risk_keeps_position_and_protection(self):
        self.demo(); self.engine.maybe_enter()
        original = self.api.account
        before = len(self.orders())
        with patch.object(self.api, 'account', side_effect=lambda: dict(original(), positions=[])):
            self.engine.sync_demo()
        self.assertIsNotNone(self.engine.s['position'])
        self.assertEqual(len(self.orders()), before)
        self.assertFalse(any(method == 'DELETE' and path.endswith('algoOrder') for method,path,_ in self.api.calls))
        self.assertFalse(self.engine.s['enabled'])

    def test_close_ack_without_remote_flat_keeps_pending_and_protection(self):
        self.demo(); self.engine.maybe_enter()
        original = self.api.request
        saved = list(self.api.positions)
        def request(method, path, params=None, signed=False):
            result = original(method, path, params, signed)
            if method == 'POST' and path.endswith('/order') and params.get('reduceOnly') == 'true':
                self.api.positions = saved
            return result
        with patch.object(self.api, 'request', side_effect=request):
            with self.assertRaises(APIError): self.engine.close()
        self.assertIsNotNone(self.engine.s['pending'])
        self.assertIsNotNone(self.engine.s['position'])
        self.assertFalse(any(method == 'DELETE' and path.endswith('algoOrder') for method,path,_ in self.api.calls))

if __name__ == '__main__': unittest.main()
