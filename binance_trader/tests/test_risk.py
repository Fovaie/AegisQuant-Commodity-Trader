import copy
import time
import unittest
from unittest.mock import patch
from binance_trader.api import APIError, Binance
from binance_trader.core import plan
from binance_trader.risk import estimate_liquidation, maintenance_margin, validate_brackets, validate_position_risk


def inputs():
    now = time.time()
    limits = dict(symbol='XAUUSDT', updated=now, expires=now+60, source='test account',
                  brackets=[dict(leverage=100, floor=0, cap=10000, maintMarginRatio=.005, cum=0),
                            dict(leverage=50, floor=10000, cap=100000, maintMarginRatio=.01, cum=50)])
    filters = dict(tick=.01, step=.001, minQty=.001, maxQty=1000, minNotional=5,
                   liquidationFee=.01, leverageLimits=limits)
    quote = dict(mark=100, price=100, time=int(now*1000), server=int(now*1000), funding=.0001)
    fees = dict(taker=.0005, maker=.0002, updated=now, expires=now+300, source='test account')
    return filters, quote, fees


def make_plan(side='BUY', leverage=2, risk=1.5, available=300, **changes):
    filters, quote, fees = inputs()
    filters.update(changes.pop('filters', {}))
    quote.update(changes.pop('quote', {}))
    fees.update(changes.pop('fees', {}))
    return plan(dict(side=side, atr=.3, reference=100), 100, filters, risk, leverage, available,
                quote=quote, fees=fees)


class IsolatedRiskTests(unittest.TestCase):
    def test_long_and_short_liquidation_match_equity_maintenance_equation(self):
        limits = inputs()[0]['leverageLimits']
        for side, sign in (('BUY', 1), ('SELL', -1)):
            result = estimate_liquidation(side, 10, 100, 100, limits)
            mark = result['estimated_liquidation']
            equity = 100+sign*10*(mark-100)
            self.assertAlmostEqual(equity, maintenance_margin(10*mark, limits))
            self.assertEqual(mark < 100, sign == 1)

    def test_tier_crossing_recomputes_mmr_and_cum_at_liquidation(self):
        limits = inputs()[0]['leverageLimits']
        long = estimate_liquidation('BUY', 101, 100, 1010, limits)
        short = estimate_liquidation('SELL', 99, 100, 990, limits)
        self.assertEqual(long['liquidation_tier'], 0)
        self.assertEqual(short['liquidation_tier'], 1)
        for side, q, wallet, result in ((1,101,1010,long),(-1,99,990,short)):
            p = result['estimated_liquidation']
            self.assertAlmostEqual(wallet+side*q*(p-100),maintenance_margin(q*p,limits))

    def test_safe_plan_has_buffers_and_respects_all_budgets(self):
        for side in ('BUY', 'SELL'):
            p = make_plan(side, 10)
            self.assertLessEqual(p['planned_loss'], p['risk_budget'])
            self.assertLessEqual(p['cash_required'], 60)
            self.assertLessEqual(p['notional'], 150)
            self.assertGreaterEqual(p['liquidation_gap'], p['required_liquidation_gap'])
            self.assertGreater(p['margin_reserve'], 0)
            self.assertGreater(p['risk_audit']['funding_reserve'], 0)
            self.assertEqual(p['risk_audit']['fee_source'], 'test account')
            self.assertLess(p['risk_audit']['conservative_isolated_wallet'],p['margin'])

    def test_high_leverage_rejected_before_stop_would_approach_liquidation(self):
        for side in ('BUY', 'SELL'):
            for leverage in (50, 100):
                with self.assertRaisesRegex(ValueError, '强平安全间距'):
                    make_plan(side, leverage)

    def test_increasing_fee_or_funding_reduces_quantity(self):
        base = make_plan()
        costly = make_plan(fees={'taker':.002})
        funded = make_plan(quote={'funding':.002})
        self.assertLess(costly['quantity'], base['quantity'])
        self.assertLess(funded['quantity'], base['quantity'])
        with self.assertRaises(ValueError): make_plan(leverage=25, quote={'funding':.02})

    def test_funding_credit_never_used_to_inflate_pretrade_risk(self):
        self.assertEqual(make_plan(quote={'funding':.001})['quantity'],
                         make_plan(quote={'funding':-.001})['quantity'])

    def test_missing_stale_metadata_fails_closed(self):
        f, q, fees = inputs()
        sig = dict(side='BUY', atr=.3, reference=100)
        for missing in ('maintMarginRatio', 'cum'):
            bad = copy.deepcopy(f)
            del bad['leverageLimits']['brackets'][0][missing]
            with self.assertRaises(ValueError): plan(sig,100,bad,quote=q,fees=fees)
        for absent in ('liquidationFee',):
            bad = copy.deepcopy(f); del bad[absent]
            with self.assertRaises(ValueError): plan(sig,100,bad,quote=q,fees=fees)
        with self.assertRaises(ValueError): plan(sig,100,f,quote=q)
        with self.assertRaises(ValueError): plan(sig,100,f,fees=fees)
        f['leverageLimits']['expires'] = time.time()-1
        with self.assertRaises(ValueError): plan(sig,100,f,quote=q,fees=fees)
        with self.assertRaises(ValueError): make_plan(fees={'updated':time.time()-400})
        with self.assertRaises(ValueError): make_plan(quote={'time':int(time.time()*1000)-30000})
        with self.assertRaises(ValueError): make_plan(quote={'server':int(time.time()*1000)-30000})

    def test_discontinuous_or_inconsistent_brackets_rejected(self):
        limits = inputs()[0]['leverageLimits']
        for key, value in (('floor',9000), ('cum',0), ('maintMarginRatio',.001), ('leverage',101)):
            broken = copy.deepcopy(limits); broken['brackets'][1][key]=value
            with self.assertRaises(ValueError): validate_brackets(broken)

    def test_mark_divergence_and_crossed_protection_rejected(self):
        for mark in (99.6, 100.4, 0):
            with self.assertRaises(ValueError): make_plan(quote={'mark':mark})

    def test_full_collateral_long_no_positive_liquidation(self):
        self.assertEqual(estimate_liquidation('BUY',1,100,100,inputs()[0]['leverageLimits'])['estimated_liquidation'],0)

    def test_uncovered_short_tier_rejected(self):
        with self.assertRaises(ValueError):
            estimate_liquidation('SELL',990,100,99000,inputs()[0]['leverageLimits'])

    def test_stop_rounding_is_toward_entry(self):
        for side in ('BUY','SELL'):
            p = make_plan(side,filters={'tick':.3})
            self.assertLessEqual(abs(p['entry']-p['stop']),1.0)

    def test_venue_postfill_requires_real_isolated_matching_position(self):
        for side,sign in (('BUY',1),('SELL',-1)):
            order=make_plan(side); order['symbol']='XAUUSDT'
            row=dict(symbol='XAUUSDT',positionSide='BOTH',marginAsset='USDT',
                     positionAmt=sign*order['quantity'],entryPrice=100,markPrice=100,
                     isolatedWallet=order['margin'],isolatedMargin=order['margin'],
                     liquidationPrice=order['estimated_liquidation'])
            result=validate_position_risk(order,row)
            self.assertGreater(result['actual_liquidation_gap'],order['required_liquidation_gap'])
            for patch_row in ({'positionSide':'LONG'},{'isolatedWallet':0},{'isolatedMargin':0},
                              {'marginAsset':'USDC'},{'positionAmt':-sign*order['quantity']},
                              {'positionAmt':sign*order['quantity']*2},{'liquidationPrice':order['stop']},
                              {'markPrice':order['stop']},{'liquidationPrice':0}):
                with self.assertRaises(ValueError): validate_position_risk(order,dict(row,**patch_row))
            with self.assertRaisesRegex(ValueError,'超过预算'):
                validate_position_risk(order,dict(row,entryPrice=100+sign*.2))


class RiskAPITests(unittest.TestCase):
    def test_commission_signed_cached_forced_and_strict(self):
        client=Binance()
        response=dict(symbol='XAUUSDT',makerCommissionRate='.0002',takerCommissionRate='.0005')
        with patch.object(client,'request',return_value=response) as req:
            rates=client.commission_rates('XAUUSDT')
            self.assertEqual(rates['taker'],.0005)
            client.commission_rates('XAUUSDT'); self.assertEqual(req.call_count,1)
            client.commission_rates('XAUUSDT',True); self.assertEqual(req.call_count,2)
            self.assertEqual(req.call_args.args[:2],('GET','/fapi/v1/commissionRate'))
            self.assertTrue(req.call_args.kwargs['signed'])
        for data in ({},dict(response,symbol='CLUSDT'),dict(response,takerCommissionRate='NaN')):
            with patch.object(client,'request',return_value=data), self.assertRaises(APIError):
                client.commission_rates('XAUUSDT',True)

    def test_position_risk_rejects_wrong_symbol_and_nonlist(self):
        client=Binance()
        for response in ({},[{'symbol':'CLUSDT'}]):
            with patch.object(client,'request',return_value=response), self.assertRaises(APIError):
                client.position_risk('XAUUSDT')
        with patch.object(client,'request',return_value=[]) as req:
            self.assertEqual(client.position_risk('XAUUSDT'),[])
            self.assertTrue(req.call_args.kwargs['signed'])

    def test_funding_history_checks_time_symbol_price_and_duplicates(self):
        client=Binance()
        row=dict(symbol='XAUUSDT',fundingTime=10,fundingRate='.0001',markPrice='100')
        with patch.object(client,'request',return_value=[row]):
            self.assertEqual(client.funding_history('XAUUSDT',1,20),[dict(fundingTime=10,fundingRate=.0001,markPrice=100.)])
        for response in ([dict(row,fundingTime=21)],[dict(row,markPrice=0)],
                         [dict(row,symbol='CLUSDT')],[row,row],{}):
            with patch.object(client,'request',return_value=response), self.assertRaises(APIError):
                client.funding_history('XAUUSDT',1,20)

    def test_funding_history_pagination_does_not_silently_truncate(self):
        client=Binance()
        batch=[dict(symbol='XAUUSDT',fundingTime=i,fundingRate='.0001',markPrice='100') for i in range(1,1001)]
        with patch.object(client,'request',side_effect=[batch,[]]) as req:
            result=client.funding_history('XAUUSDT',1,2000)
            self.assertEqual(len(result),1000)
            self.assertEqual(req.call_args.args[2]['startTime'],1001)

    def test_actual_funding_income_signed_deduplicated_and_validated(self):
        client=Binance()
        row=dict(symbol='XAUUSDT',incomeType='FUNDING_FEE',asset='USDT',time=10,income='-.01',tranId=123)
        with patch.object(client,'request',return_value=[row,row]) as req:
            self.assertEqual(client.funding_income('XAUUSDT',1,20),[dict(time=10,income=-.01,tranId='123')])
            self.assertEqual(req.call_args.args[:2],('GET','/fapi/v1/income'))
            self.assertTrue(req.call_args.kwargs['signed'])
            self.assertEqual(req.call_args.args[2]['incomeType'],'FUNDING_FEE')
        for response in ({},[dict(row,symbol='CLUSDT')],[dict(row,asset='USDC')],
                         [dict(row,incomeType='TRANSFER')],[dict(row,time=21)],
                         [dict(row,tranId='')],[dict(row,income='NaN')],
                         [row,dict(row,income='-.02')]):
            with patch.object(client,'request',return_value=response), self.assertRaises(APIError):
                client.funding_income('XAUUSDT',1,20)

    def test_actual_funding_income_pages_use_fixed_window_and_transaction_ids(self):
        client=Binance()
        batch=[dict(symbol='XAUUSDT',incomeType='FUNDING_FEE',asset='USDT',
                    time=i,income='-.01',tranId=i) for i in range(1,1001)]
        with patch.object(client,'request',side_effect=[batch,[batch[-1]]]) as req:
            result=client.funding_income('XAUUSDT',1,2000)
            self.assertEqual(len(result),1000)
            self.assertEqual(req.call_args.args[2]['page'],2)
            self.assertEqual(req.call_args.args[2]['startTime'],1)
            self.assertEqual(req.call_args.args[2]['endTime'],2000)
        with patch.object(client,'request',return_value=batch), self.assertRaises(APIError):
            client.funding_income('XAUUSDT',1,2000)

if __name__ == '__main__': unittest.main()
