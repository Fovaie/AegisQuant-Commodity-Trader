import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from binance_trader.api import Binance, APIError
from binance_trader.core import SYMBOLS, floor_step, plan, signal
from binance_trader.engine import Engine

FILTERS = dict(tick=.00001, step=1, minQty=1, maxQty=1e9, minNotional=5, liquidationFee=.01,
               leverageLimits={"brackets":[{"leverage":3,"cap":100000,"floor":0,"maintMarginRatio":.004,"cum":0}]})


def verified_news(symbol, side="BUY"):
    """Synthetic, independently verified evidence; never uses live news/accounts."""
    now = time.time()
    origins = ['Fed'] if symbol in ('XAUUSDT', 'XAGUSDT') else ['BBC', 'Guardian']
    domains = {'Fed': 'www.federalreserve.gov', 'BBC': 'www.bbc.com', 'Guardian': 'www.theguardian.com'}
    rule = ('fed_rate_cut' if side == 'BUY' else 'fed_rate_hike') if symbol in ('XAUUSDT', 'XAGUSDT') else (
        ('oil_supply_loss' if side == 'BUY' else 'oil_supply_restore') if symbol in ('CLUSDT', 'BZUSDT') else (
        'us_gas_supply_loss' if side == 'BUY' else 'us_gas_supply_restore'))
    evidence = [dict(direction=side, rule=rule, event_key='fixture-event',
                     event_at=now-30, reason='synthetic test fixture', fingerprint='fixture-'+source,
                     excerpt='Synthetic fixture only', source=source, origin=source,
                     title='Synthetic fixture only', url='https://'+domains[source]+'/news/fixture',
                     published=now-30, primary=source == 'Fed', verified=True) for source in origins]
    return dict(symbol=symbol, asof=now, expires=now+75, policy_version=2, entry_allowed=True,
                status='bullish' if side == 'BUY' else 'bearish', direction=side,
                reason='隔离测试：已核验合成证据', evidence=evidence, event_id='fixture-id', risks=[],
                drivers=[dict(key='fixture', label='合成测试', status='verified', reason='fixture')])


def candles():
    end = int(time.time()*1000)//3_600_000*3_600_000
    rows = []
    for i in range(100):
        price = .08+i*.0001
        start = end-(100-i)*3_600_000
        rows.append([start,str(price),str(price+.00015),str(price-.00015),str(price),"1",start+3_600_000-1])
    rows[-1][2] = rows[-1][4] = ".092"
    return rows, end


class Fake:
    demo = False
    key = "test-key"
    secret = "test-secret"
    def __init__(self):
        self.price = .092
        self.calls = []
        self.reject_algo = False
        self.unknown_entry = False
        self.positions = []
        self.wallet = 1000
        self.algos = {}
        self.order_history = {}
        self.leverage = 2
        self.funding = []
        self.liquidation_override = None
    def leverage_limits(self, symbol, force=False):
        return dict(FILTERS["leverageLimits"],symbol=symbol,maximum=3,updated=time.time(),expires=time.time()+60,source="Test")
    def market(self, symbol):
        return dict(price=self.price, mark=self.price, server=int(time.time()*1000),time=int(time.time()*1000),funding=0,nextFundingTime=int(time.time()*1000)+3600000,change=0,high=.1,low=.08)
    def commission_rates(self,symbol,force=False):
        return dict(symbol=symbol,taker=.0005,maker=.0002,source="Test account",updated=time.time(),expires=time.time()+300)
    def funding_history(self,symbol,start_time,end_time):
        return [r for r in self.funding if start_time <= r['fundingTime'] <= end_time]
    def funding_income(self,symbol,start_time,end_time):return []
    def position_risk(self,symbol):
        result=[]
        for p in self.positions:
            if p['symbol'] != symbol:continue
            qty=float(p['positionAmt']);entry=float(p['entryPrice'])
            liq=entry*(.51 if qty>0 else 1.49) if self.liquidation_override is None else self.liquidation_override
            result.append(dict(p,positionSide="BOTH",marginAsset="USDT",isolatedWallet=str(abs(qty)*entry/self.leverage),
                               isolatedMargin=str(abs(qty)*entry/self.leverage),markPrice=str(self.price),liquidationPrice=str(liq)))
        return result
    def candles(self, symbol): return candles()[0]
    def order_book(self, symbol):
        return dict(T=int(time.time()*1000),bids=[[str(self.price*.9999),'1000000000']],
                    asks=[[str(self.price*1.0001),'1000000000']])
    def filters(self, symbol): return dict(FILTERS,leverageLimits=self.leverage_limits(symbol))
    def account(self):
        return dict(canTrade=True,multiAssetsMargin=False,totalWalletBalance=str(self.wallet),availableBalance="1000",positions=self.positions)
    def request(self, method, path, params=None, signed=False):
        self.calls.append((method,path,params))
        if path.endswith("positionSide/dual"): return {"dualSidePosition":False}
        if path.endswith("openOrders") or path.endswith("openAlgoOrders"): return []
        if path.endswith("marginType"): return {}
        if path.endswith("leverage"):
            self.leverage=params['leverage']
            return dict(symbol=params['symbol'],leverage=self.leverage,maxNotionalValue='100000')
        if path.endswith("/order") and method == "POST":
            if params.get("reduceOnly") != "true" and self.unknown_entry: raise APIError("timeout")
            if params.get("reduceOnly") == "true":self.positions=[]
            else:self.positions=[dict(symbol=params['symbol'],positionAmt=str(float(params['quantity'])*(1 if params['side']=='BUY' else -1)),entryPrice=str(self.price))]
            result = {"status":"FILLED","avgPrice":str(self.price),"executedQty":params["quantity"]}
            self.order_history[params['newClientOrderId']] = dict(result,symbol=params['symbol'],clientOrderId=params['newClientOrderId'])
            return result
        if path.endswith('/order') and method == 'GET':
            if params['origClientOrderId'] not in self.order_history:raise APIError('order not found',-2013)
            return self.order_history[params['origClientOrderId']]
        if path.endswith("algoOrder"):
            if method == "POST" and self.reject_algo: raise APIError("rejected",-1)
            if method == 'POST':
                identity=len(self.calls)
                self.algos[identity]=dict(params,algoId=identity,algoStatus="NEW",positionSide="BOTH",orderType=params['type'],closePosition=True)
                return self.algos[identity]
            if method == 'GET':return self.algos[params['algoId']]
            return {}
        return {}


class CoreTests(unittest.TestCase):
    def test_unclosed_candle_cannot_signal(self):
        rows, end = candles()
        expected = signal(rows,end)
        self.assertEqual(expected["side"],"BUY")
        rows.append([end,".092","1",".001",".001","1",end+3599999])
        self.assertEqual(signal(rows,end),expected)
    def test_stale_and_missing_candles(self):
        rows,end=candles()
        with self.assertRaises(ValueError):signal(rows,end+4_000_000)
        del rows[-5]
        with self.assertRaises(ValueError):signal(rows,end)
    def test_risk_and_rounding(self):
        for side in ("BUY","SELL"):
            api=Fake()
            p=plan(dict(side=side,atr=.001,reference=.092),.092,api.filters('XAUUSDT'),quote=api.market('XAUUSDT'),fees=api.commission_rates('XAUUSDT'))
            self.assertLessEqual(p["planned_loss"],1.5)
            self.assertLessEqual(p["notional"],120)
            self.assertEqual(p["quantity"]%1,0)
            self.assertEqual(p["stop"]<p["entry"],side=="BUY")
        self.assertEqual(floor_step(.12349,.0001),.1234)
    def test_reject_nan_leverage_and_too_small(self):
        sig=dict(side="BUY",atr=.001,reference=.092)
        for kwargs in ({"risk":float('nan')},{"leverage":20},{"risk":4},{"risk":.001}):
            with self.assertRaises(ValueError):plan(sig,.092,FILTERS,**kwargs)
    def test_no_chasing_and_no_signal(self):
        with self.assertRaises(ValueError):plan(dict(side="BUY",atr=.001,reference=.08),.092,FILTERS)
        with self.assertRaises(ValueError):plan(dict(side=None),.092,FILTERS)
    def test_mainnet_write_is_impossible(self):
        with patch("binance_trader.api.urlopen") as call:
            for method in ("POST","DELETE","PUT"):
                with self.assertRaises(APIError):Binance().request(method,"/fapi/v1/order",{},True)
            call.assert_not_called()
    def test_only_commodity_requests_allowed(self):
        self.assertEqual(SYMBOLS, ("XAUUSDT", "CLUSDT", "BZUSDT", "XAGUSDT", "NATGASUSDT"))
        with patch("binance_trader.api.urlopen") as call:
            for symbol in ("BTCUSDT", "DOGEUSDT", "ETHUSDT"):
                with self.assertRaises(ValueError): Binance().candles(symbol)
                with self.assertRaises(ValueError): Binance().filters(symbol)
            call.assert_not_called()

    def test_signature_does_not_leak(self):
        from urllib.error import URLError
        a=Binance(True,"secret-key","secret-value")
        a.synced=time.time()
        a._clock_anchor=(int(a.synced*1000),time.monotonic(),a.synced)
        with patch("binance_trader.api.urlopen",side_effect=URLError("secret-value")):
            with self.assertRaises(APIError) as err:a.request("GET","/fapi/v3/account",signed=True)
        self.assertNotIn("secret-value",str(err.exception))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=Fake()
        self.path=Path(self.temp.name)/"state.sqlite"
        self.engine=Engine(self.api,self.path)
        self.engine.news_provider=verified_news
        self.engine.quote=self.api.market("XAUUSDT")
        self.engine.updated=time.time()
    def tearDown(self):
        self.engine.db.close()
        self.temp.cleanup()
    def arm_signal(self):
        self.engine.sig=dict(side="SELL",atr=.001,reference=.092,candle=int(time.time()*1000)-3_600_000-1000)
        self.engine.news_provider=lambda symbol: verified_news(symbol, 'SELL')
        self.engine.quote=self.api.market("XAUUSDT")
    def test_paper_entry_close_costs_and_duplicate_candle(self):
        self.arm_signal();self.engine.maybe_enter()
        self.assertIsNotNone(self.engine.s["position"])
        self.assertLess(self.engine.s["wallet"],300)
        self.engine.close()
        balance=self.engine.s["wallet"]
        self.engine.maybe_enter()
        self.assertIsNone(self.engine.s["position"])
        self.assertLess(balance,300)
        self.assertEqual(self.api.calls,[])
    def test_stop_monitored_while_paused(self):
        self.arm_signal();self.engine.maybe_enter()
        self.api.price=.096
        self.engine.pause();self.engine.tick()
        self.assertIsNone(self.engine.s["position"])
        self.assertLess(self.engine.s["wallet"],300)
    def test_restart_pauses_and_preserves_position(self):
        self.arm_signal();self.engine.start();self.engine.maybe_enter()
        other=Engine(self.api,self.path)
        self.assertFalse(other.s["enabled"])
        self.assertIsNotNone(other.s["position"])
        other.db.close()
    def test_loss_latch_survives_restart(self):
        self.engine.s["wallet"]=254
        self.engine.loss_check();self.engine.save()
        other=Engine(self.api,self.path)
        self.assertTrue(other.s["total_latch"])
        other.quote=self.api.market("XAUUSDT");other.updated=time.time()
        with self.assertRaises(ValueError):other.start()
        other.db.close()
    def test_legacy_empty_migrates_preserving_risk(self):
        self.engine.s.update(symbol="DOGEUSDT", wallet=254, total_latch=True, enabled=True)
        self.engine.save()
        other=Engine(self.api,self.path)
        self.assertEqual(other.s["symbol"], "XAUUSDT")
        self.assertEqual(other.s["wallet"], 254)
        self.assertTrue(other.s["total_latch"])
        self.assertFalse(other.s["enabled"])
        other.db.close()
    def test_legacy_position_is_not_discarded(self):
        self.engine.s.update(symbol="DOGEUSDT", position={"symbol":"DOGEUSDT"})
        self.engine.save()
        with self.assertRaises(ValueError): Engine(self.api,self.path)
        saved=json.loads(self.engine.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
        self.assertEqual(saved["position"]["symbol"],"DOGEUSDT")
    def test_noncommodity_config_rejected(self):
        with self.assertRaises(ValueError): self.engine.configure(dict(symbol="DOGEUSDT",risk=1,leverage=2))

    def test_config_blocked_during_position(self):
        self.arm_signal();self.engine.maybe_enter()
        with self.assertRaises(ValueError):self.engine.configure(dict(symbol="BZUSDT",risk=1,leverage=2))
    def test_restart_with_pending_locks(self):
        self.engine.s["pending"]={"id":"unknown"};self.engine.save()
        other=Engine(self.api,self.path)
        self.assertTrue(other.s["halted"]);other.db.close()
    def demo(self):
        self.engine.db.close()
        self.path=Path(self.temp.name)/"demo.sqlite"
        self.api.demo=True
        self.engine=Engine(self.api,self.path,"demo")
        self.engine.news_provider=verified_news
        self.engine.quote=self.api.market("XAUUSDT")
        self.engine.updated=time.time()
    def test_demo_exchange_protection(self):
        self.demo();self.arm_signal();self.engine.maybe_enter()
        self.assertIsNone(self.engine.s["pending"])
        algos=[p for m,u,p in self.api.calls if m=="POST" and u.endswith("algoOrder")]
        self.assertEqual(len(algos),2)
        self.assertTrue(all(p["closePosition"]=="true" and p["workingType"]=="MARK_PRICE" for p in algos))
    def test_unknown_entry_never_retries(self):
        self.demo();self.arm_signal();self.api.unknown_entry=True
        with self.assertRaises(APIError):self.engine.maybe_enter()
        self.assertTrue(self.engine.s["halted"])
        self.assertIsNotNone(self.engine.s["pending"])
        self.engine.tick();self.engine.tick()
        self.assertEqual(len([1 for m,u,p in self.api.calls if m=="POST" and u.endswith("/order")]),1)
    def test_protection_failure_attempts_reduce_only_close(self):
        self.demo();self.arm_signal();self.api.reject_algo=True
        with self.assertRaises(APIError):self.engine.maybe_enter()
        orders=[p for m,u,p in self.api.calls if m=="POST" and u.endswith("/order")]
        self.assertEqual(len(orders),2)
        self.assertEqual(orders[-1]["reduceOnly"],"true")
        self.assertTrue(self.engine.s["halted"])
    def test_different_key_rejected(self):
        self.demo();self.engine.bind_account();self.api.key="other"
        with self.assertRaises(ValueError):Engine(self.api,self.path,"demo")
    def test_external_position_refuses_demo(self):
        self.demo();self.api.positions=[{"symbol":"BTCUSDT","positionAmt":"1"}]
        with self.assertRaises(ValueError):self.engine.start()
        self.assertFalse(any(m=="POST" for m,_,_ in self.api.calls))
    def test_stale_market_blocks_start(self):
        self.engine.updated=time.time()-30
        with self.assertRaises(ValueError):self.engine.start()


if __name__ == "__main__":unittest.main()
