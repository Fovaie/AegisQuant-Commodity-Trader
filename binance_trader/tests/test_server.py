import http.client
import json
import re
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from binance_trader.api import APIError
from pathlib import Path
from binance_trader.__main__ import server_for
from binance_trader.engine import Engine
from binance_trader.analysis import Analysis
from test_trader import Fake, verified_news


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.engine=Engine(Fake(),Path(self.temp.name)/"paper.sqlite")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0));self.port=sock.getsockname()[1]
        self.analysis=Analysis(self.engine)
        self.server=server_for(self.engine,self.port,analysis=self.analysis)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.engine.db.close();self.temp.cleanup()
    def request(self,path="/",method="GET",headers=None,body=None):
        c=http.client.HTTPConnection("127.0.0.1",self.port,timeout=3)
        c.request(method,path,body=body,headers=headers or {})
        r=c.getresponse();result=(r.status,r.read(),dict(r.getheaders()));c.close()
        return result
    def test_english_presentation_asset_is_served_without_weakening_origin_controls(self):
        status,body,headers=self.request('/i18n.js')
        self.assertEqual(status,200)
        self.assertIn('text/javascript',headers['Content-Type'])
        self.assertIn(b'AegisI18n',body)
        self.assertIn(b"script-src 'self'",headers['Content-Security-Policy'].encode())
        self.assertEqual(self.request('/i18n.js',headers={'Host':'evil.example'})[0],403)

    def test_loopback_host_and_cross_origin_rejected(self):
        status,_,_=self.request(headers={"Host":"evil.example"})
        self.assertEqual(status,403)
        _,html,_=self.request()
        token=re.search(b'name="csrf" content="([^"]+)"',html).group(1).decode()
        for origin in ("https://evil.example",""):
            status,_,_=self.request("/api/pause","POST",{"Origin":origin,"X-CSRF-Token":token},"{}")
            self.assertEqual(status,403)
    def test_csrf_correct_and_secrets_absent(self):
        _,html,headers=self.request()
        self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])
        token=re.search(b'name="csrf" content="([^"]+)"',html).group(1).decode()
        status,_,_=self.request("/api/pause","POST",{"Origin":"http://127.0.0.1:"+str(self.port),"X-CSRF-Token":token},"{}")
        self.assertEqual(status,200)
        _,body,_=self.request("/api/state")
        self.assertNotIn(b'test-secret',body)
        self.assertFalse(json.loads(body)["state"]["enabled"])
    def auth_headers(self):
        _,html,_=self.request()
        token=re.search(b'name="csrf" content="([^"]+)"',html).group(1).decode()
        return {"Origin":"http://127.0.0.1:"+str(self.port),"X-CSRF-Token":token}
    def test_connect_account_only_in_memory(self):
        headers=self.auth_headers()
        payload=json.dumps({"key":"k"*64,"secret":"s"*64})
        account={"totalWalletBalance":"301", "availableBalance":"299", "totalUnrealizedProfit":"0", "positions":[]}
        with patch("binance_trader.__main__.Binance") as client:
            client.return_value.account.return_value=account
            status,body,_=self.request("/api/connect","POST",headers,payload)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["balance"],"301")
            client.assert_called_once_with(False,"k"*64,"s"*64)
            client.return_value.account.assert_called_once_with()
            _,state,_=self.request("/api/state")
            self.assertTrue(json.loads(state)["readonly_connected"])
            self.assertFalse(json.loads(state)["state"]["enabled"])
            self.assertNotIn(b's'*64,body+state)
            self.assertNotIn('s'*64,self.engine.db.execute("SELECT body FROM state").fetchone()[0])
            client.return_value.account.side_effect=APIError("验证失败")
            self.assertEqual(self.request("/api/connect","POST",headers,payload)[0],400)
            client.return_value.account.side_effect=None
            self.assertEqual(self.request("/api/account","POST",headers,"{}")[0],200)
    def test_connect_rejects_csrf_and_invalid_inputs(self):
        headers=self.auth_headers()
        with patch("binance_trader.__main__.Binance") as client:
            self.assertEqual(self.request("/api/connect","POST",{},"{}")[0],403)
            for body in ('[]','{"key":1,"secret":2}','{"key":"short","secret":"short"}'):
                self.assertEqual(self.request("/api/connect","POST",headers,body)[0],400)
            client.assert_not_called()

    def test_leverage_endpoint_requires_connection(self):
        status,body,_=self.request("/api/leverage","POST",self.auth_headers(),json.dumps({"symbol":"XAUUSDT"}))
        self.assertEqual(status,400);self.assertIn("请先连接",json.loads(body)["error"])
    def test_config_uses_server_limit_not_client_limit(self):
        self.engine.leverage_limits=lambda symbol,force=False: {"brackets":[{"leverage":50,"cap":10000,"floor":0}]}
        headers=self.auth_headers()
        for leverage,expected in ((50,200),(51,400),(2.5,400)):
            status,_,_=self.request("/api/config","POST",headers,json.dumps({"symbol":"XAUUSDT","risk":1.5,"leverage":leverage,"maximum":1000}))
            self.assertEqual(status,expected)
        self.assertEqual(self.engine.s["leverage"],50)

    def test_no_traversal_or_credentials_route(self):
        for path in ("/../core.py","/data/binance/demo.credentials.json","/api/order"):
            self.assertEqual(self.request(path)[0],404)

    def test_analysis_symbol_query_is_read_only_and_allowlisted(self):
        self.analysis.update('news','BZUSDT',dict(items=[],decision=verified_news('BZUSDT'),coverage={'healthy':True}))
        selected=self.engine.s['symbol'];calls=list(self.engine.api.calls)
        status,body,_=self.request('/api/analysis?symbol=BZUSDT')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['news']['symbol'],'BZUSDT')
        self.assertEqual(self.engine.s['symbol'],selected)
        self.assertEqual(self.engine.api.calls,calls)
        for query in ('symbol=BTCUSDT','symbol=','symbol=BZUSDT&symbol=CLUSDT','symbol=BZUSDT&start=true'):
            self.assertEqual(self.request('/api/analysis?'+query)[0],400)


if __name__ == "__main__":unittest.main()
