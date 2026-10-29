"""Identity and rotation contract: isolated ledgers, fake venue, no user secrets."""
import copy,hashlib,io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from binance_trader.api import Binance,APIError
from binance_trader.engine import Engine
from test_live_server import LiveServerClient
import test_live_server as server_fixture

class IdentityClient(LiveServerClient):
    def __init__(self,key='old-key',identity='same-account'):
        super().__init__(False,key,'fixture-secret',True)
        self.identity=hashlib.sha256(identity.encode()).hexdigest()
    def account_identity(self):
        self.calls.append(('GET','/api/v3/account',None));return self.identity

class KeyRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.client=IdentityClient()
        self.engine=Engine(self.client,Path(self.temp.name)/'live.sqlite','live')
        self.engine.attach_live(self.client);self.client.calls.clear()
    def tearDown(self):self.engine.db.close();self.temp.cleanup()
    def test_same_account_rotation_preserves_budget_latches_history_and_durable_state(self):
        e=self.engine;e.s.update(capital_budget=73,remote_base=321,loss_limit=10.95,daily_loss_limit=1.46,
            day_start=70,total_latch=True,daily_latch=True,last_candle=123,has_traded=False)
        e.bind_account_identity();before=copy.deepcopy(e.s);new=IdentityClient('new-key')
        result=e.rotate_live_key(new)
        self.assertTrue(result['rotated']);self.assertIs(e.api,new);self.assertFalse(e.s['enabled'])
        for k in ('capital_budget','remote_base','loss_limit','daily_loss_limit','day_start','total_latch','daily_latch','last_candle','has_traded','account_uid_hash'):
            self.assertEqual(e.s[k],before[k],k)
        disk=json.loads(e.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        self.assertEqual(disk['key_id'],hashlib.sha256(new.key.encode()).hexdigest())
        self.assertTrue(all(m=='GET' for m,_,_ in self.client.calls+new.calls))
        self.assertNotIn('fixture-secret',json.dumps(e.snapshot()))
    def test_unbound_or_other_account_cannot_replace_original_connection(self):
        e=self.engine;new=IdentityClient('new-key','other-account')
        with self.assertRaises(ValueError):e.rotate_live_key(new)
        e.bind_account_identity();before=copy.deepcopy(e.s)
        with self.assertRaises(ValueError):e.rotate_live_key(new)
        self.assertEqual(e.s,before);self.assertIs(e.api,self.client)
    def test_active_exposure_pending_or_enabled_rejects_before_identity_read(self):
        e=self.engine;e.bind_account_identity()
        for key,value in (('position',{'id':'fixture'}),('pending',{'id':'fixture'}),('enabled',True)):
            with self.subTest(key=key):
                old=e.s[key];e.s[key]=value;new=IdentityClient('new-key')
                with self.assertRaises(ValueError):e.rotate_live_key(new)
                self.assertEqual(new.calls,[]);e.s[key]=old
    def test_remote_exposure_or_order_rejected_and_disk_failure_rolls_back(self):
        e=self.engine;e.bind_account_identity();before=copy.deepcopy(e.s);new=IdentityClient('new-key')
        new.positions=[dict(symbol='XAUUSDT',positionAmt='1')]
        with self.assertRaises(ValueError):e.rotate_live_key(new)
        new.positions=[]
        with patch.object(new,'request',side_effect=lambda m,p,*a,**k:[{'clientOrderId':'external'}] if p.endswith('openOrders') else self.client.request(m,p,*a,**k)):
            with self.assertRaises(ValueError):e.rotate_live_key(new)
        with patch.object(e,'save',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.Error):e.rotate_live_key(new)
        self.assertEqual(e.s,before);self.assertIs(e.api,self.client)
        disk=json.loads(e.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]);self.assertEqual(disk['key_id'],before['key_id'])
    def test_bound_identity_allows_recovery_without_original_live_client(self):
        e=self.engine;e.bind_account_identity();e.live_connected=False
        e.api=Binance();new=IdentityClient('new-key');e.rotate_live_key(new)
        self.assertIs(e.api,new);self.assertTrue(e.live_connected)
    def test_identity_transport_is_fixed_read_only_signed_and_rejects_invalid_uid(self):
        b=Binance(False,'fixture-key','fixture-secret',True);seen=[]
        def opened(req,timeout):
            seen.append(req);return io.BytesIO(json.dumps({'uid':1234567}).encode())
        with patch.object(b,'signed_timestamp',return_value=1791100000000),patch('binance_trader.api.urlopen',side_effect=opened):
            value=b.account_identity()
        self.assertEqual(len(value),64);req=seen[0]
        self.assertEqual(req.method,'GET');self.assertTrue(req.full_url.startswith('https://api.binance.com/api/v3/account?'))
        self.assertIn('signature=',req.full_url);self.assertNotIn('fixture-secret',req.full_url)
        for uid in (None,True,'123',0,-1,2**63):
            with patch.object(b,'signed_timestamp',return_value=1),patch.object(b,'_send',return_value={'uid':uid}):
                with self.assertRaises(APIError):b.account_identity()
        with self.assertRaises(APIError):Binance(True,'key','secret').account_identity()
        with self.assertRaises(APIError):b._send('POST','/api/v3/account',{}, {},identity_read=True)
        with self.assertRaises(APIError):b.request('GET','/api/v3/account',signed=True)
    def test_identity_bind_disk_failure_does_not_create_anchor(self):
        e=self.engine;before=copy.deepcopy(e.s)
        with patch.object(e,'save',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.Error):e.bind_account_identity()
        self.assertEqual(e.s,before);self.assertIs(e.api,self.client)

class KeyRotationHTTPTests(unittest.TestCase):
    setUp=server_fixture.LiveServerTests.setUp
    tearDown=server_fixture.LiveServerTests.tearDown
    request=server_fixture.LiveServerTests.request
    context=server_fixture.LiveServerTests.context
    post=server_fixture.LiveServerTests.post
    switch=server_fixture.LiveServerTests.switch
    def test_identity_routes_require_current_context_and_explicit_rotation_confirmation(self):
        self.switch();old=IdentityClient('k'*64);self.live.attach_live(old)
        status,_=self.post('/api/credentials/rotate',{'mode':'live','key':'n'*64,'secret':'s'*64})
        self.assertEqual(status,400)
        context=self.context();status,result=self.post('/api/credentials/bind-account')
        self.assertEqual(status,200,result)
        status,result=self.post('/api/credentials/rotate',{'confirm_rotation':True,'mode':'live','key':'n'*64,'secret':'s'*64},context=context)
        self.assertEqual(status,400);self.assertIn('环境',result['error'])
        with patch('binance_trader.__main__.Binance',side_effect=lambda *a,**k:IdentityClient('n'*64)):
            status,result=self.post('/api/credentials/rotate',{'confirm_rotation':True,'mode':'live','key':'n'*64,'secret':'s'*64})
        self.assertEqual(status,200,result);self.assertTrue(result['rotated'])
        self.assertFalse(self.live.s['enabled'])
