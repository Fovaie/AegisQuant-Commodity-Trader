"""Manual entries must pass the same book guard as automatic selection."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from binance_trader.api import APIError
from binance_trader.engine import Engine
from test_trader import Fake, verified_news
from test_live_engine import LiveFake

class ManualDepthTests(unittest.TestCase):
    def make(self,directory,mode,side='BUY'):
        api=LiveFake() if mode=='live' else Fake()
        api.demo=mode=='demo'
        engine=Engine(api,Path(directory)/(mode+'.sqlite'),mode)
        engine.news_provider=lambda symbol:verified_news(symbol,side)
        engine.quote=api.market('XAUUSDT');engine.updated=time.time()
        engine.sig=dict(side=side,atr=.001,reference=api.price,previous_close=.091 if side=='BUY' else .093,
                        fast=.092 if side=='BUY' else .09,slow=.09 if side=='BUY' else .092,
                        candle=int(time.time()*1000)-3601000)
        if mode=='live':engine.attach_live(api);engine.start()
        return engine,api
    def entries(self,api):return [p for m,path,p in api.calls if m=='POST' and path=='/fapi/v1/order' and p.get('reduceOnly')!='true']
    def test_bad_spread_depth_slippage_and_stale_books_block_every_manual_mode_and_side(self):
        for mode in ('paper','demo','live'):
            for side in ('BUY','SELL'):
                for problem in ('spread','depth','slippage','stale','empty'):
                    with self.subTest(mode=mode,side=side,problem=problem),tempfile.TemporaryDirectory() as directory:
                        e,api=self.make(directory,mode,side)
                        try:
                            book=api.order_book('XAUUSDT')
                            if problem=='spread':book.update(bids=[[str(api.price*.99),'1000000']],asks=[[str(api.price*1.01),'1000000']])
                            elif problem=='depth':book.update(bids=[[str(api.price*.9999),'0.00000001']],asks=[[str(api.price*1.0001),'0.00000001']])
                            elif problem=='slippage':
                                book.update(bids=[[str(api.price*(1.0007 if side=='BUY' else .9992)),'1000000']],asks=[[str(api.price*(1.0008 if side=='BUY' else .9993)),'1000000']])
                            elif problem=='stale':book['T']-=16000
                            else:book['asks']=[]
                            with patch.object(api,'order_book',return_value=book) as reads:e.maybe_enter()
                            self.assertGreater(reads.call_count,0)
                            self.assertIsNone(e.s['position']);self.assertIsNone(e.s['pending'])
                            self.assertEqual(self.entries(api),[])
                            self.assertFalse(any(m!='GET' for m,_,_ in api.calls))
                            self.assertEqual(e.decision['action'],'WAIT')
                        finally:e.db.close()
    def test_healthy_book_allows_both_manual_directions_and_all_modes(self):
        for mode in ('paper','demo','live'):
            for side in ('BUY','SELL'):
                with self.subTest(mode=mode,side=side),tempfile.TemporaryDirectory() as directory:
                    e,api=self.make(directory,mode,side)
                    try:
                        with patch.object(api,'order_book',wraps=api.order_book) as book:e.maybe_enter()
                        self.assertIsNotNone(e.s['position']);self.assertEqual(e.s['position']['side'],side)
                        self.assertGreaterEqual(book.call_count,1 if mode=='paper' else 3)
                        self.assertEqual(len(self.entries(api)),0 if mode=='paper' else 1)
                        if mode!='paper':self.assertEqual(len(e.s['position']['algos']),2)
                    finally:e.db.close()
    def test_book_deterioration_after_leverage_blocks_order_and_retains_preparing_intent(self):
        for mode in ('demo','live'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                e,api=self.make(directory,mode)
                original=api.request
                def request(method,path,params=None,signed=False):
                    result=original(method,path,params,signed)
                    if method=='POST' and path=='/fapi/v1/leverage':api.order_book=lambda symbol:dict(T=int(time.time()*1000),bids=[['.09','1000000']],asks=[['.094','1000000']])
                    return result
                try:
                    with patch.object(api,'request',side_effect=request):
                        with self.assertRaises(ValueError):e.maybe_enter()
                    self.assertEqual(self.entries(api),[]);self.assertIsNone(e.s['position'])
                    self.assertEqual(e.s['pending']['phase'],'preparing');self.assertFalse(e.s['enabled'])
                    review=e.inspect_pending();self.assertTrue(review['can_resolve'],review)
                finally:e.db.close()
    def test_unavailable_book_never_permits_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            e,api=self.make(directory,'live')
            try:
                with patch.object(api,'order_book',side_effect=APIError('offline')):
                    with self.assertRaises(APIError):e.maybe_enter()
                self.assertEqual(self.entries(api),[]);self.assertIsNone(e.s['pending'])
            finally:e.db.close()
    def test_close_does_not_depend_on_available_entry_book(self):
        with tempfile.TemporaryDirectory() as directory:
            e,api=self.make(directory,'live')
            try:
                e.maybe_enter()
                with patch.object(api,'order_book',side_effect=APIError('offline')) as book:e.close()
                book.assert_not_called();self.assertIsNone(e.s['position'])
            finally:e.db.close()

if __name__=='__main__':unittest.main()
