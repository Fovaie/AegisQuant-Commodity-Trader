import unittest
from unittest.mock import patch
from binance_trader.api import Binance,APIError
from binance_trader.core import leverage_cap,plan
from test_risk import inputs

class LeverageTests(unittest.TestCase):
    def test_account_brackets_and_cache(self):
        client=Binance(False,'dummy','dummy')
        item={'symbol':'XAUUSDT','notionalCoef':1.5,'brackets':[
            {'initialLeverage':100,'notionalFloor':0,'notionalCap':10000,'maintMarginRatio':.005,'cum':0},
            {'initialLeverage':50,'notionalFloor':10000,'notionalCap':50000,'maintMarginRatio':.01,'cum':50}]}
        with patch.object(client,'request',return_value=[item]) as request:
            a=client.leverage_limits('XAUUSDT');self.assertEqual(a['maximum'],100)
            self.assertEqual(leverage_cap(75,a),10000)
            self.assertEqual(leverage_cap(50,a),50000)
            self.assertEqual(a['brackets'][1]['maintMarginRatio'],.01)
            self.assertEqual(a['brackets'][1]['cum'],50)
            client.leverage_limits('XAUUSDT');self.assertEqual(request.call_count,1)
            client.leverage_limits('XAUUSDT',force=True);self.assertEqual(request.call_count,2)
            self.assertEqual(request.call_args.args[0],'GET')
            self.assertTrue(request.call_args.kwargs['signed'])
    def test_reject_wrong_symbol_missing_invalid_tiers(self):
        client=Binance()
        for response in ({'symbol':'BTCUSDT','brackets':[]},{'symbol':'XAUUSDT','brackets':[]},
              {'symbol':'XAUUSDT','brackets':[{'initialLeverage':2.5,'notionalFloor':0,'notionalCap':5}]}):
            with patch.object(client,'request',return_value=response):
                with self.assertRaises(APIError):client.leverage_limits('XAUUSDT')
    def test_plan_accepts_exchange_limit_and_keeps_risk_caps(self):
        filters,quote,fees=inputs()
        filters['leverageLimits']['brackets']=[{'leverage':75,'cap':30,'floor':0,'maintMarginRatio':.005,'cum':0}]
        p=plan(dict(side='BUY',atr=.3,reference=100),100,filters,leverage=10,quote=quote,fees=fees)
        self.assertLessEqual(p['notional'],30);self.assertLessEqual(p['planned_loss'],1.5)
        for value in (76,2.5,0,float('nan')):
            with self.assertRaises(ValueError):plan(dict(side='BUY',atr=.3,reference=100),100,filters,leverage=value,quote=quote,fees=fees)
        # Exchange eligibility is necessary but not sufficient for a safe stop.
        with self.assertRaises(ValueError):plan(dict(side='BUY',atr=.3,reference=100),100,filters,leverage=50,quote=quote,fees=fees)
    def test_missing_limits_never_falls_back_to_fixed_max(self):
        with self.assertRaises(ValueError):leverage_cap(2,{})
