"""Credential lifecycle uses fake providers and an isolated, in-memory Keychain."""
import copy
import http.client
import json
import re
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from binance_trader.ai_service import AIService
from binance_trader.credential_store import CredentialStore, CredentialError, restore_connections
from binance_trader.credential_migration import migration_server
from binance_trader.engine import Engine
from binance_trader.runtime import Runtime
from binance_trader.__main__ import server_for
from test_live_server import LiveServerClient

KEY='fixture-key-'+('k'*40)
SECRET='fixture-secret-'+('s'*40)
MODEL_KEY='fixture-model-key'


class MemoryKeychain:
    def __init__(self):self.rows={};self.calls=[];self.fail=False
    def __call__(self, args, **kwargs):
        # No secrets in command arguments, diagnostics or persisted files.
        assert len(args)==1
        body=json.loads(kwargs['input']);self.calls.append(body)
        key=(body['service'],body['slot'])
        if self.fail:return subprocess.CompletedProcess(args,0,json.dumps(dict(ok=False,code=-25308)).encode())
        if body['action']=='set':self.rows[key]=copy.deepcopy(body['value'])
        elif body['action']=='delete':self.rows.pop(key,None)
        return subprocess.CompletedProcess(args,0,json.dumps(dict(ok=True,value=self.rows.get(key))).encode())


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.helper=self.directory/'secure-store';self.helper.write_text('fixture');self.helper.chmod(0o700)
        self.backend=MemoryKeychain();self.store=CredentialStore(self.directory,runner=self.backend)
    def tearDown(self):self.temp.cleanup()

    def model_service(self):return AIService(client_factory=lambda config:Mock(),credential_store=self.store)

    def test_separate_process_write_read_restart_forget_and_no_plaintext(self):
        value=dict(key=KEY,secret=SECRET);self.store.save('live',value)
        other=CredentialStore(self.directory,runner=self.backend)
        self.assertEqual(other.load('live'),value)
        status=json.dumps(other.status());self.assertNotIn(KEY,status);self.assertNotIn(SECRET,status)
        self.assertEqual([p.name for p in self.directory.iterdir()],['secure-store'])
        other.forget('live');self.assertIsNone(self.store.load('live'))

    def test_directory_isolation_and_keychain_failure_do_not_erase(self):
        self.store.save('live',dict(key=KEY,secret=SECRET))
        other=CredentialStore(self.directory/'other',helper=self.helper,runner=self.backend)
        self.assertIsNone(other.load('live'))
        self.backend.fail=True
        with self.assertRaises(CredentialError):self.store.save('live',dict(key='replacement'))
        self.assertNotIn(SECRET,json.dumps(self.store.status()))
        self.backend.fail=False;self.assertEqual(self.store.load('live')['key'],KEY)

    def test_untrusted_helper_and_bad_slot_refused(self):
        with self.assertRaises(CredentialError):self.store.load('all')
        self.helper.chmod(0o777)
        with self.assertRaises(CredentialError):self.store.load('live')
        self.assertFalse(self.backend.calls)

    def test_shutdown_retains_model_but_explicit_disconnect_forgets(self):
        ai=self.model_service();ai.configure('http://127.0.0.1:8080/v1','gpt-6-luna',MODEL_KEY)
        ai.stop();self.assertEqual(self.store.load('model')['api_key'],MODEL_KEY)
        fresh=self.model_service();restore_connections(self.store,None,fresh)
        self.assertTrue(fresh.status()['enabled']);self.assertNotIn(MODEL_KEY,json.dumps(fresh.status()))
        fresh.disconnect();self.assertIsNone(self.store.load('model'))

    def test_failed_save_does_not_replace_active_model(self):
        ai=self.model_service();ai.configure('http://127.0.0.1:8080/v1','gpt-6-luna',MODEL_KEY)
        old=ai.client;self.backend.fail=True
        with self.assertRaises(CredentialError):ai.configure('http://127.0.0.1:8080/v1','gpt-6.1-sol','new-key')
        self.assertIs(ai.client,old);self.assertEqual(ai.status()['model'],'gpt-6-luna')

    def runtime(self):
        return Runtime(self.directory,initial_mode='live',engines={
            'paper':Engine(LiveServerClient(),self.directory/'paper.sqlite','paper'),
            'live':Engine(LiveServerClient(live_trading=True),self.directory/'live.sqlite','live')})

    def test_restore_live_uses_gets_only_and_never_arms(self):
        self.store.save('live',dict(key=KEY,secret=SECRET));runtime=self.runtime()
        try:
            restore_connections(self.store,runtime,self.model_service(),LiveServerClient)
            live=runtime.engines['live'];self.assertTrue(live.live_connected);self.assertFalse(live.s['enabled'])
            self.assertTrue(live.api.calls);self.assertTrue(all(c[0]=='GET' for c in live.api.calls))
            self.assertTrue(self.store.status()['restored']['live'])
        finally:runtime.close_databases()

    def test_failed_restore_keeps_saved_key_disconnected_paused(self):
        self.store.save('live',dict(key=KEY,secret=SECRET));runtime=self.runtime()
        try:
            with patch.object(LiveServerClient,'account',side_effect=ValueError(SECRET)):
                restore_connections(self.store,runtime,self.model_service(),LiveServerClient)
            self.assertFalse(runtime.engine.live_connected);self.assertFalse(runtime.s['enabled'])
            self.assertTrue(self.store.status()['saved']['live']);self.assertNotIn(SECRET,json.dumps(self.store.status()))
        finally:runtime.close_databases()


    def test_local_csrf_only_readonly_checks_and_no_secret_response(self):
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        clients=[]
        def factory(*args):
            c=LiveServerClient(*args);clients.append(c);return c
        server=migration_server(self.directory,port,self.store,factory,lambda cfg:Mock())
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            conn.request('GET' if body is None else 'POST',path,body,headers or {})
            r=conn.getresponse();result=(r.status,r.read());conn.close();return result
        try:
            _,html=request('/');token=re.search(b'name="csrf" content="([^"]+)"',html).group(1).decode()
            body=json.dumps(dict(slot='live',key=KEY,secret=SECRET))
            self.assertEqual(request('/api/save',body)[0],403)
            headers={'Origin':'http://127.0.0.1:'+str(port),'X-CSRF-Token':token,'Content-Type':'application/json'}
            code,result=request('/api/save',body,headers);self.assertEqual(code,200,result)
            self.assertFalse(clients[0].live_trading);self.assertNotIn(KEY,result.decode());self.assertNotIn(SECRET,result.decode())
            for path in ('/api/start','/api/close','/api/apply-leverage'):self.assertEqual(request(path,'{}',headers)[0],404)
            self.assertEqual(request('/api/status',headers={'Host':'evil.example'})[0],403)
        finally:server.shutdown();server.server_close();thread.join()

    def test_app_connect_persists_only_verified_keys_and_reports_partial_save(self):
        runtime=self.runtime()
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        server=server_for(runtime.engine,port,runtime=runtime,credential_store=self.store)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,payload=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            if payload is None:conn.request('GET',path)
            else:
                body=dict(payload,context_id=runtime.context_id,context_mode=runtime.mode)
                conn.request('POST',path,json.dumps(body),headers)
            r=conn.getresponse();result=r.status,r.read();conn.close();return result
        try:
            _,html=request('/');token=re.search(b'name="csrf" content="([^"]+)"',html).group(1).decode()
            headers={'Origin':'http://127.0.0.1:'+str(port),'X-CSRF-Token':token,'Content-Type':'application/json'}
            payload=dict(mode='live',key=KEY,secret=SECRET)
            with patch('binance_trader.__main__.Binance',side_effect=LiveServerClient):
                code,result=request('/api/connect',payload)
                self.assertEqual(code,200,result);self.assertTrue(json.loads(result)['credential_saved'])
                self.assertEqual(self.store.load('live'),dict(key=KEY,secret=SECRET))
                self.backend.fail=True
                code,result=request('/api/connect',payload)
                self.assertEqual(code,200,result);self.assertFalse(json.loads(result)['credential_saved'])
                self.assertTrue(runtime.engine.live_connected);self.assertFalse(runtime.s['enabled'])
            self.backend.fail=False
            _,state=request('/api/state');self.assertNotIn(SECRET,state.decode());self.assertNotIn(KEY,state.decode())
            runtime.s['pending']={'id':'fixture'}
            self.assertEqual(request('/api/credentials/forget',dict(slot='live'))[0],400)
            runtime.s['pending']=None
            self.assertEqual(request('/api/credentials/forget',dict(slot='live'))[0],200)
            self.assertIsNone(self.store.load('live'));self.assertTrue(runtime.engine.live_connected)
        finally:server.shutdown();server.server_close();thread.join();runtime.close_databases()
