"""Offline clock/signature regression tests; no account or exchange I/O."""
import hashlib
import hmac
import io
import json
import unittest
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse
from unittest.mock import patch

from binance_trader.api import APIError, Binance


class Clock:
    def __init__(self, wall=1900000000.0, mono=100.0, server=1791090000000):
        self.wall, self.mono, self.server = wall, mono, float(server)

    def advance(self, seconds, wall=None, mono=None):
        self.wall += seconds if wall is None else wall
        self.mono += seconds if mono is None else mono
        self.server += seconds * 1000


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode())
        self.headers = headers or {}
        self.status = 200


class Transport:
    def __init__(self, clock, samples=None, outcomes=None):
        self.clock = clock
        self.samples = list(samples or [])
        self.outcomes = list(outcomes or [])
        self.time_requests = []
        self.signed_requests = []

    def __call__(self, request, timeout=10):
        if urlparse(request.full_url).path == '/fapi/v1/time':
            self.time_requests.append(request)
            sample = self.samples.pop(0) if self.samples else {}
            if isinstance(sample, Exception): raise sample
            self.clock.advance(sample.get('delay', .02))
            self.clock.wall += sample.get('wall_jump', 0)
            server = sample.get('server', int(self.clock.server))
            headers = {'Date': formatdate(self.clock.server/1000, usegmt=True), 'Age':'0'}
            headers.update(sample.get('headers', {}))
            return Response({'serverTime':server}, headers)
        self.signed_requests.append(request)
        self.clock.advance(.01)
        outcome = self.outcomes.pop(0) if self.outcomes else {'ok':True}
        if isinstance(outcome, tuple):
            status, code = outcome
            raise HTTPError(request.full_url, status, 'remote text', {},
                            io.BytesIO(json.dumps({'code':code,'msg':'unit-secret-should-not-leak'}).encode()))
        if isinstance(outcome, Exception): raise outcome
        return Response(outcome)


def wire_query(request):
    return request.data.decode() if request.data is not None else urlparse(request.full_url).query


class TimeSyncTests(unittest.TestCase):
    def environment(self, samples=None, outcomes=None, **clock_kwargs):
        clock = Clock(**clock_kwargs)
        transport = Transport(clock, samples, outcomes)
        stack = ExitStack()
        stack.enter_context(patch('binance_trader.api.time.time', side_effect=lambda:clock.wall))
        stack.enter_context(patch('binance_trader.api.time.time_ns', side_effect=lambda:int(clock.wall*1_000_000_000)))
        stack.enter_context(patch('binance_trader.api.time.monotonic', side_effect=lambda:clock.mono))
        stack.enter_context(patch('binance_trader.api.urlopen', side_effect=transport))
        self.addCleanup(stack.close)
        return clock, transport

    def client(self, live=False):
        return Binance(False, 'unit-key-should-not-leak', 'unit-secret-should-not-leak', live_trading=live)

    def signed_time(self, request):
        return int(dict(parse_qsl(wire_query(request)))['timestamp'])

    def assert_signature(self, request, client):
        wire = wire_query(request)
        pairs = parse_qsl(wire)
        for key in ('timestamp','recvWindow','signature'):
            self.assertEqual(sum(k == key for k,_ in pairs),1)
        self.assertEqual(pairs[-1][0],'signature')
        unsigned, actual = wire.rsplit('&signature=',1)
        expected = hmac.new(client.secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(actual,expected)
        self.assertNotIn(client.key,wire)
        self.assertNotIn(client.secret,wire)

    def test_large_local_clock_offset_does_not_affect_signed_time(self):
        for wall in (1000.0, 2900000000.0):
            with self.subTest(wall=wall):
                clock, transport = self.environment(wall=wall)
                client=self.client()
                self.assertEqual(client.request('GET','/fapi/v3/account',signed=True),{'ok':True})
                stamp=self.signed_time(transport.signed_requests[-1])
                self.assertLessEqual(stamp,clock.server)
                self.assertLess(clock.server-stamp,1000)
                self.assert_signature(transport.signed_requests[-1],client)

    def test_monotonic_progress_within_cache_avoids_extra_sync(self):
        clock, transport=self.environment()
        client=self.client()
        first=client.signed_timestamp(); count=len(transport.time_requests)
        clock.advance(10)
        self.assertEqual(client.signed_timestamp()-first,10000)
        self.assertEqual(len(transport.time_requests),count)

    def test_legacy_diagnostic_fields_cannot_authorize_unsynced_signing(self):
        clock,transport=self.environment()
        client=self.client();client.synced=clock.wall+100000;client.offset=999999
        stamp=client.signed_timestamp()
        self.assertEqual(len(transport.time_requests),1)
        self.assertLessEqual(stamp,clock.server)

    def test_concurrent_callers_share_one_validated_clock_sample(self):
        _,transport=self.environment()
        client=self.client()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(lambda _:client.signed_timestamp(),range(8)))
        self.assertEqual(len(transport.time_requests),1)
        self.assertEqual(len(set(values)),1)

    def test_wall_jump_sleep_and_monotonic_reset_force_new_sync(self):
        clock, transport=self.environment()
        client=self.client(); client.signed_timestamp()
        for seconds, wall, mono in ((0,3600,0),(0,-7200,0),(3600,3600,0),(31,31,31),(0,0,-1000)):
            before=len(transport.time_requests)
            clock.advance(seconds,wall=wall,mono=mono)
            stamp=client.signed_timestamp()
            self.assertGreater(len(transport.time_requests),before)
            self.assertLessEqual(stamp,clock.server)
            self.assertLess(clock.server-stamp,1000)

    def test_asymmetric_roundtrip_does_not_push_timestamp_into_future(self):
        clock, transport=self.environment(samples=[{'delay':1.4}])
        # serverTime generated just before reception is the worst case for the
        # old midpoint-offset formula: it incorrectly added half the roundtrip.
        client=self.client(); stamp=client.signed_timestamp()
        self.assertEqual(len(transport.time_requests),1)
        self.assertLessEqual(stamp,clock.server-100)
        self.assertGreaterEqual(stamp,clock.server-250)

    def test_slow_samples_are_replaced_and_exhaustion_never_sends_signed_request(self):
        clock,transport=self.environment(samples=[{'delay':2.0},{'delay':.1}])
        client=self.client(); client.request('GET','/fapi/v3/account',signed=True)
        self.assertEqual(len(transport.time_requests),2)
        self.assertEqual(len(transport.signed_requests),1)
        self.assertLessEqual(self.signed_time(transport.signed_requests[0]),clock.server)
        transport.samples=[{'delay':2.0}]*3
        client._clock_anchor=None
        before=len(transport.signed_requests)
        with self.assertRaises(APIError):client.request('GET','/fapi/v3/account',signed=True)
        self.assertEqual(len(transport.signed_requests),before)
        self.assertEqual(len(transport.time_requests),5)

    def test_clock_jump_during_sample_is_rejected(self):
        _,transport=self.environment(samples=[{'wall_jump':5},{'wall_jump':-5},{'wall_jump':5}])
        with self.assertRaises(APIError):self.client().request('GET','/fapi/v3/account',signed=True)
        self.assertEqual(len(transport.time_requests),3)
        self.assertFalse(transport.signed_requests)

    def test_time_endpoint_network_failure_does_not_create_retry_storm(self):
        _,transport=self.environment(samples=[URLError('offline')]*3)
        with self.assertRaises(APIError):self.client().request('GET','/fapi/v3/account',signed=True)
        self.assertEqual(len(transport.time_requests),1)
        self.assertFalse(transport.signed_requests)

    def test_cached_or_inconsistent_server_time_never_authorizes_signature(self):
        for sample in ({'headers':{'Age':'10'}}, {'server':1791089000000}, {'server':True},
                       {'server':0}, {'server':-1}, {'server':1.5}, {'server':'1791090000000'}):
            with self.subTest(sample=sample):
                _,transport=self.environment(samples=[sample]*3)
                with self.assertRaises(APIError):self.client().request('GET','/fapi/v3/account',signed=True)
                self.assertFalse(transport.signed_requests)
                self.assertLessEqual(len(transport.time_requests),3)

    def test_time_requests_bypass_cache_and_have_no_credentials(self):
        clock,transport=self.environment()
        client=self.client();client.sync_time(force=True);clock.advance(.01);client.sync_time(force=True)
        self.assertEqual(len(transport.time_requests),2)
        cache_keys=[]
        for request in transport.time_requests:
            self.assertIn('no-cache',request.get_header('Cache-control',''))
            self.assertIsNone(request.get_header('X-mbx-apikey'))
            query=dict(parse_qsl(urlparse(request.full_url).query))
            self.assertIn('timestamp',query)
            self.assertNotIn('signature',query)
            cache_keys.append(query['timestamp'])
        self.assertNotEqual(cache_keys[0],cache_keys[1])

    def test_forced_sync_nonce_increases_even_when_nanosecond_clock_is_fixed(self):
        clock,transport=self.environment(samples=[{'delay':0},{'delay':0}])
        client=self.client()
        fixed_wall=clock.wall
        client.sync_time(force=True);client.sync_time(force=True)
        self.assertEqual(clock.wall,fixed_wall)
        nonces=[int(dict(parse_qsl(urlparse(request.full_url).query))['timestamp'])
                for request in transport.time_requests]
        self.assertEqual(len(nonces),2)
        self.assertGreater(nonces[1],nonces[0])

    def test_stale_age_sample_then_fresh_sample_recovers_before_signing(self):
        clock,transport=self.environment(samples=[{'headers':{'Age':'10'}},{}])
        client=self.client()
        self.assertEqual(client.request('GET','/fapi/v3/account',signed=True),{'ok':True})
        self.assertEqual(len(transport.time_requests),2)
        self.assertEqual(len(transport.signed_requests),1)
        request=transport.signed_requests[0]
        self.assertLessEqual(self.signed_time(request),clock.server)
        self.assertLess(clock.server-self.signed_time(request),1000)
        self.assert_signature(request,client)

    def test_read_timestamp_rejection_resynchronizes_and_resigns_once(self):
        _,transport=self.environment(outcomes=[(400,-1021),{'ok':True}])
        client=self.client()
        original={'symbol':'XAUUSDT','limit':5}
        self.assertEqual(client.request('GET','/fapi/v1/userTrades',original,signed=True),{'ok':True})
        self.assertEqual(original,{'symbol':'XAUUSDT','limit':5})
        self.assertEqual(len(transport.signed_requests),2)
        self.assertEqual(len(transport.time_requests),2)
        first,second=transport.signed_requests
        self.assertNotEqual(self.signed_time(first),self.signed_time(second))
        self.assert_signature(first,client);self.assert_signature(second,client)

    def test_second_timestamp_rejection_stops_and_sanitizes_error(self):
        _,transport=self.environment(outcomes=[(400,-1021),(400,-1021),(400,-1021)])
        client=self.client()
        with self.assertRaises(APIError) as captured:client.request('GET','/fapi/v3/account',signed=True)
        self.assertEqual(captured.exception.code,-1021)
        self.assertEqual(captured.exception.http_status,400)
        self.assertEqual(len(transport.signed_requests),2)
        self.assertEqual(len(transport.time_requests),2)
        self.assertNotIn(client.key,str(captured.exception));self.assertNotIn(client.secret,str(captured.exception))
        self.assertNotIn('signature=',str(captured.exception))

    def test_other_errors_and_ambiguous_read_timeout_are_not_retried(self):
        for outcome in ((400,-1022),(500,-1021),URLError('offline'),TimeoutError('timeout')):
            with self.subTest(outcome=outcome):
                _,transport=self.environment(outcomes=[outcome,{'ok':True}])
                with self.assertRaises(APIError):self.client().request('GET','/fapi/v3/account',signed=True)
                self.assertEqual(len(transport.signed_requests),1)

    def test_write_timestamp_rejection_and_ambiguous_timeout_never_resubmit(self):
        operations=(('POST','/fapi/v1/order',dict(symbol='XAUUSDT',side='BUY',type='MARKET',quantity='1',newClientOrderId='bt-clock-test',newOrderRespType='RESULT')),
                    ('POST','/fapi/v1/leverage',dict(symbol='XAUUSDT',leverage=2)),
                    ('DELETE','/fapi/v1/order',dict(symbol='XAUUSDT',origClientOrderId='bt-clock-test')))
        for method,path,params in operations:
            for outcome in ((400,-1021),URLError('timeout'),TimeoutError('ambiguous response')):
                with self.subTest(method=method,path=path,outcome=outcome):
                    _,transport=self.environment(outcomes=[outcome,{'ok':True}])
                    client=self.client(live=True)
                    with self.assertRaises(APIError):client.request(method,path,params,signed=True)
                    self.assertEqual(len(transport.signed_requests),1)
                    self.assertEqual(len(transport.time_requests),1)
                    if outcome == (400,-1021):self.assertIsNone(client._clock_anchor)

    def test_callers_cannot_inject_signature_or_clock_fields(self):
        _,transport=self.environment()
        client=self.client()
        for name in ('timestamp','recvWindow','signature'):
            with self.subTest(name=name),self.assertRaises((APIError,ValueError)):
                client.request('GET','/fapi/v3/account',{name:'attacker-value'},signed=True)
        self.assertFalse(transport.signed_requests)
        self.assertFalse(transport.time_requests)
