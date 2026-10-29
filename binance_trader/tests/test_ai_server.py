"""Local configuration routes: no remote model calls or exchange mutations."""
import http.client
import json
import re
import socket
import tempfile
import threading
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from binance_trader.__main__ import server_for
from binance_trader.analysis import Analysis
from binance_trader.engine import Engine
from test_trader import Fake


class AIServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Fake(), Path(self.temp.name)/'paper.sqlite')
        self.analysis = Analysis(self.engine)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.server = server_for(self.engine, self.port, analysis=self.analysis)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, html = self.request('/')
        token = re.search(b'name="csrf" content="([^"]+)"', html).group(1).decode()
        self.headers = {'Origin': 'http://127.0.0.1:'+str(self.port), 'X-CSRF-Token': token}

    def tearDown(self):
        self.analysis.stop()
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.engine.db.close(); self.temp.cleanup()

    def request(self, path, method='GET', data=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        c.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
        response = c.getresponse(); value = (response.status, response.read()); c.close()
        return value

    def config(self):
        return dict(base_url='https://api.openai.com/v1', model='fixture-model',
                    api_key='fixture-model-key-not-real-12345', enabled=True)

    def test_config_test_disconnect_are_local_and_do_not_enable_trading(self):
        with patch('binance_trader.ai_news.ModelClient.analyze') as analyze, \
             patch('binance_trader.ai_news.ModelClient.test_connection') as test:
            status, body = self.request('/api/ai/config', 'POST', self.config(), self.headers)
            self.assertEqual(status, 200, body)
            self.assertTrue(json.loads(body)['configured'])
            self.assertFalse(json.loads(body)['trading_authority'])
            status, body = self.request('/api/ai/test', 'POST', {}, self.headers)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['test_pending'])
            self.assertEqual(json.loads(body)['request_count'], 0)
            analyze.assert_not_called(); test.assert_not_called()
            self.assertEqual(self.engine.api.calls, [])
            self.assertFalse(self.engine.s['enabled'])
            status, body = self.request('/api/ai/disconnect', 'POST', {}, self.headers)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)['configured'])

    def test_model_key_is_not_in_public_snapshots_or_ledger(self):
        secret = self.config()['api_key']
        self.assertEqual(self.request('/api/ai/config', 'POST', self.config(), self.headers)[0], 200)
        for path in ('/api/ai/status', '/api/analysis', '/api/state'):
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertNotIn(secret.encode(), body)
            self.assertNotIn(b'"api_key"', body)
        for path in Path(self.temp.name).iterdir():
            if path.is_file(): self.assertNotIn(secret.encode(), path.read_bytes())

    def test_csrf_host_and_invalid_input_rejected_without_remote_calls(self):
        with patch('binance_trader.ai_news.ModelClient.test_connection') as test:
            self.assertEqual(self.request('/api/ai/config', 'POST', self.config())[0], 403)
            self.assertEqual(self.request('/api/ai/status', headers={'Host': 'evil.example'})[0], 403)
            for field, value in (('base_url', 'http://evil.example'), ('base_url', 'https://user:pass@api.openai.com/v1'),
                                 ('base_url', 'https://127.0.0.1/v1'), ('model', ''), ('api_key', '')):
                data = self.config(); data[field] = value
                status, _ = self.request('/api/ai/config', 'POST', data, self.headers)
                self.assertEqual(status, 400, (field, value))
            test.assert_not_called()
            self.assertFalse(self.analysis.ai.status()['configured'])

    def test_comparison_and_upgrade_are_bounded_local_actions_and_require_paused_config(self):
        from test_ai_news import ARTICLE
        self.assertEqual(self.request('/api/ai/config','POST',self.config(),self.headers)[0],200)
        now=time.time();self.analysis.ai.submit('XAUUSDT',[dict(ARTICLE,published=now-1)],now)
        with patch('binance_trader.ai_news.ModelClient.analyze') as analyze:
            for route in ('/api/ai/compare','/api/ai/model'):
                self.assertEqual(self.request(route,'POST',{'model':'gpt-6.1-sol'})[0],403)
            status,body=self.request('/api/ai/compare','POST',{},self.headers)
            self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['comparison']['status'],'pending')
            analyze.assert_not_called()
            self.engine.s['enabled']=True
            self.assertEqual(self.request('/api/ai/model','POST',{'model':'gpt-6.1-sol'},self.headers)[0],400)
            self.assertEqual(self.request('/api/ai/config','POST',self.config(),self.headers)[0],400)
            self.engine.s['enabled']=False
            status,body=self.request('/api/ai/model','POST',{'model':'gpt-6.1-sol'},self.headers)
            self.assertEqual(status,200,body);self.assertEqual(json.loads(body)['model'],'gpt-6.1-sol')
            self.assertNotIn(self.config()['api_key'].encode(),body)
            self.assertEqual(self.engine.api.calls,[]);self.assertFalse(self.engine.s['enabled'])


if __name__ == '__main__':
    unittest.main()
