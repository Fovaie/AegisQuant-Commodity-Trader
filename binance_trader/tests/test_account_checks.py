"""Account diagnostics through fake GET responses; never changes remote modes."""
import copy
import tempfile
import unittest
from pathlib import Path

from binance_trader.api import APIError
from binance_trader.engine import Engine
from test_live_engine import LiveFake


DEFAULT = object()


class AccountClient(LiveFake):
    def __init__(self, config=DEFAULT, dual=DEFAULT):
        super().__init__()
        self.config = ({'multiAssetsMargin':False, 'canTrade':True, 'dualSidePosition':False}
                       if config is DEFAULT else config)
        self.dual = {'dualSidePosition':False} if dual is DEFAULT else dual

    def account(self):
        self.calls.append(('GET','/fapi/v3/account',None))
        return super().account()

    def request(self, method, path, params=None, signed=False):
        if path in ('/fapi/v1/accountConfig','/fapi/v1/positionSide/dual'):
            self.calls.append((method,path,params))
            result = self.config if path.endswith('accountConfig') else self.dual
            if isinstance(result,Exception): raise result
            return copy.deepcopy(result)
        return super().request(method,path,params,signed)


class AccountChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original=AccountClient()
        self.engine=Engine(self.original,Path(self.temp.name)/'live.sqlite','live')

    def tearDown(self):
        self.engine.db.close();self.temp.cleanup()

    def rejected(self, client, exception=ValueError):
        before=copy.deepcopy(self.engine.s)
        before_client=self.engine.api
        before_connected=self.engine.live_connected
        stored=self.engine.db.execute('SELECT body FROM state').fetchone()[0]
        with self.assertRaises(exception) as failure:self.engine.attach_live(client)
        self.assertEqual(self.engine.s,before)
        self.assertEqual(self.engine.db.execute('SELECT body FROM state').fetchone()[0],stored)
        self.assertIs(self.engine.api,before_client)
        self.assertEqual(self.engine.live_connected,before_connected)
        self.assertTrue(client.calls)
        self.assertTrue(all(method=='GET' for method,_,_ in client.calls))
        return str(failure.exception)

    def test_missing_flags_are_unknown_instead_of_claiming_multi_asset_mode(self):
        for config in ({},{'canTrade':True},{'multiAssetsMargin':False}):
            with self.subTest(config=config):
                message=self.rejected(AccountClient(config=config))
                self.assertIn('字段缺失',message)
                self.assertIn('未确认',message)
                self.assertNotIn('币安确认当前为联合保证金',message)
                self.assertNotIn('canTrade=false',message)

    def test_non_object_configuration_is_a_format_error_without_payload_echo(self):
        for config in (None,[],False,'fixture-sensitive-payload'):
            with self.subTest(config=config):
                message=self.rejected(AccountClient(config=config))
                self.assertIn('响应格式异常',message)
                self.assertNotIn('fixture-sensitive-payload',message)
                self.assertNotIn('币安确认当前为联合保证金',message)

    def test_non_boolean_flags_are_not_coerced_to_modes_or_permissions(self):
        for field in ('multiAssetsMargin','canTrade'):
            for value in (None,'false','true',0,1,[],{}):
                with self.subTest(field=field,value=value):
                    config={'multiAssetsMargin':False,'canTrade':True};config[field]=value
                    message=self.rejected(AccountClient(config=config))
                    self.assertIn(field,message)
                    self.assertIn('字段格式异常',message)
                    self.assertNotIn('币安确认当前为联合保证金',message)
                    self.assertNotIn('canTrade=false',message)

    def test_verified_multi_asset_blocks_isolated_engine_without_modifying_account(self):
        message=self.rejected(AccountClient(config={'multiAssetsMargin':True,'canTrade':True}))
        self.assertIn('币安确认当前为联合保证金模式',message)
        self.assertIn('账户可交易：是',message)
        self.assertIn('持仓模式：单向',message)

    def test_cannot_trade_is_not_presented_as_proven_api_key_permission_failure(self):
        message=self.rejected(AccountClient(config={'multiAssetsMargin':False,'canTrade':False}))
        self.assertIn('账户可交易：否',message)
        self.assertIn('canTrade=false',message)
        self.assertIn('当前账户不允许交易',message)
        self.assertIn('不能仅凭此字段认定是API密钥权限问题',message)
        self.assertNotIn('币安确认当前为联合保证金',message)

    def test_independent_failures_are_aggregated_without_hiding_other_checks(self):
        message=self.rejected(AccountClient(config={'multiAssetsMargin':True,'canTrade':False},dual={'dualSidePosition':True}))
        self.assertIn('联合保证金',message)
        self.assertIn('账户可交易：否',message)
        self.assertIn('币安确认当前为双向持仓',message)

    def test_missing_or_invalid_position_mode_is_not_described_as_hedge_mode(self):
        for dual in ({},None,[],{'dualSidePosition':None},{'dualSidePosition':'false'}, {'dualSidePosition':0}):
            with self.subTest(dual=dual):
                message=self.rejected(AccountClient(dual=dual))
                self.assertIn('持仓模式：未确认',message)
                self.assertIn('dualSidePosition',message)
                self.assertNotIn('币安确认当前为双向持仓',message)

    def test_dedicated_position_endpoint_true_is_explicit_hedge_diagnostic(self):
        message=self.rejected(AccountClient(config={'multiAssetsMargin':False,'canTrade':True},dual={'dualSidePosition':True}))
        self.assertIn('持仓模式：双向',message)
        self.assertIn('币安确认当前为双向持仓',message)
        self.assertNotIn('币安确认当前为联合保证金',message)

    def test_conflicting_position_endpoints_do_not_advise_blind_mode_change(self):
        for config_mode, dedicated_mode in ((True,False),(False,True)):
            with self.subTest(config_mode=config_mode,dedicated_mode=dedicated_mode):
                message=self.rejected(AccountClient(config={'multiAssetsMargin':False,'canTrade':True,'dualSidePosition':config_mode},
                                                    dual={'dualSidePosition':dedicated_mode}))
                self.assertIn('接口返回不一致',message)
                self.assertIn('不能据此判断应修改哪项设置',message)
                self.assertNotIn('币安确认当前为双向持仓',message)

    def test_position_lookup_failure_preserves_error_or_aggregates_existing_issue(self):
        problem=APIError('币安API拒绝请求（HTTP 400，代码 -2015）',-2015,400)
        message=self.rejected(AccountClient(dual=problem),APIError)
        self.assertEqual(message,str(problem))
        message=self.rejected(AccountClient(config={'multiAssetsMargin':True,'canTrade':True},dual=problem))
        self.assertIn('币安确认当前为联合保证金',message)
        self.assertIn('持仓模式读取失败',message)
        self.assertIn('-2015',message)

    def test_valid_account_connects_with_gets_only_and_stays_paused(self):
        candidate=AccountClient()
        result=self.engine.attach_live(candidate)
        self.assertTrue(result['connected'])
        self.assertTrue(self.engine.live_connected)
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.engine.s['wallet'],300)
        self.assertTrue(self.engine.s['key_id'])
        self.assertTrue(all(method=='GET' for method,_,_ in candidate.calls))

    def test_failed_revalidation_preserves_existing_connection_and_financial_baseline(self):
        candidate=AccountClient();self.engine.attach_live(candidate)
        self.engine.s['has_traded']=True
        self.engine.s['remote_base']=350
        self.engine.s['capital_budget']=300
        self.engine.save()
        message=self.rejected(AccountClient(config={'multiAssetsMargin':False,'canTrade':False}))
        self.assertIn('canTrade=false',message)
        self.assertIs(self.engine.api,candidate)
        self.assertTrue(self.engine.live_connected)
        self.assertEqual(self.engine.s['remote_base'],350)

