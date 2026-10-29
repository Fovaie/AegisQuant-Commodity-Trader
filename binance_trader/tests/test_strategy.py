"""News direction and explicit closed-Kline fallback obey separate permissions."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from binance_trader.core import SYMBOLS
from binance_trader.engine import Engine
from binance_trader.strategy import decide, news_state
from test_trader import Fake


def verified_news(symbol='XAUUSDT', side='BUY', now=None):
    now = time.time() if now is None else now
    metal = symbol in ('XAUUSDT','XAGUSDT')
    rule = ('fed_rate_cut' if side == 'BUY' else 'fed_rate_hike') if metal else (
        'oil_supply_loss' if symbol in ('CLUSDT','BZUSDT') else 'us_gas_supply_loss')
    event = dict(direction=side, rule=rule, event_key='fixture-event', event_at=now-30,
                 reason='synthetic evidence', fingerprint='fixture-a', excerpt='Synthetic fixture only.',
                 source='Fed' if metal else 'BBC', origin='Fed' if metal else 'BBC', title='Fixture only',
                 url='https://www.federalreserve.gov/newsevents/pressreleases/monetary-fixture.htm' if metal else 'https://www.bbc.com/news/fixture',
                 published=now-30, primary=metal, verified=True)
    evidence = [event] if metal else [event,dict(event,source='Guardian',origin='Guardian',fingerprint='fixture-b',url='https://www.theguardian.com/business/fixture')]
    return dict(symbol=symbol,asof=now,expires=now+75,policy_version=2,entry_allowed=True,
                status='bullish' if side == 'BUY' else 'bearish',direction=side,reason='Synthetic verified news fixture',
                event_id='fixture-id',evidence=evidence,risks=[],
                drivers=[dict(key='fixture',label='Fixture factor',status='verified',reason='Synthetic factor')])


def risk(now, status='active', gate=None, acute_at=None, time_basis='event_time'):
    gate=gate or ('resolved' if status=='resolved' else 'cooldown')
    if gate=='cooldown' and acute_at is None:acute_at=now-30
    acute=acute_at is not None
    return dict(id='risk-fixture',category='supply',subject='Fixture site',status=status,
                blocking=gate=='cooldown',reason='Ongoing event fixture',source='BBC',
                url='https://www.bbc.com/news/fixture',first_seen=now-48*3600,last_seen=now,
                published=acute_at if acute else now-48*3600,event_at=None if time_basis=='first_evidence' else now-48*3600,
                gate_version=1,entry_gate=gate,gate_reason='Synthetic entry gate',
                acute_event_id='acute-fixture' if acute else None,
                acute_rule='synthetic-escalation' if acute else None,
                acute_event_at=acute_at,time_basis=time_basis if acute else None,
                cooldown_until=acute_at+3600 if acute else None)


def kline_news(symbol='XAUUSDT', now=None):
    return dict(verified_news(symbol, now=now), status='insufficient', direction=None,
                entry_allowed=False, kline_allowed=True, evidence=[], event_id=None,
                reason='新闻来源健康，无可执行方向，允许收盘K线突破')


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.now=int(time.time()*1000)
        self.sig=dict(side='BUY',candle=self.now-3601000,fast=102,slow=100,
                      reference=102,previous_close=101,atr=1)
    def news(self, side='BUY', symbol='XAUUSDT'):
        return verified_news(symbol,side,self.now/1000)
    def decision(self, news, sig=None, position=None, symbol='XAUUSDT'):
        return decide(self.sig if sig is None else sig,news,symbol,self.now,position)
    def bearish(self):
        return dict(self.sig,side='SELL',fast=98,reference=99,previous_close=100)

    def test_each_commodity_requires_matching_verified_direction(self):
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertEqual(self.decision(self.news(symbol=symbol),symbol=symbol)['action'],'BUY')
                self.assertEqual(self.decision(self.news('SELL',symbol),sig=self.bearish(),symbol=symbol)['action'],'SELL')
                self.assertEqual(self.decision(self.news('SELL',symbol),symbol=symbol)['action'],'WAIT')

    def test_missing_legacy_neutral_or_insufficient_never_grants_kline_only_entry(self):
        legacy=dict(symbol='XAUUSDT',asof=self.now/1000,expires=self.now/1000+75,status='neutral',direction=None,evidence=[])
        insufficient=dict(self.news(),status='insufficient',direction=None,entry_allowed=False,evidence=[],event_id=None)
        for value in (None,{},legacy,dict(self.news(),status='neutral',direction=None,entry_allowed=False),insufficient):
            with self.subTest(value=value):
                self.assertEqual(self.decision(value)['action'],'WAIT')
                self.assertIs(news_state(value,'XAUUSDT',self.now/1000)['entry_allowed'],False)

    def test_policy_permission_and_time_fields_must_have_real_types(self):
        patches=({'policy_version':None},{'policy_version':True},{'policy_version':'2'},
                 {'entry_allowed':1},{'entry_allowed':'true'},{'entry_allowed':None},
                 {'kline_allowed':1},{'kline_allowed':'true'},{'kline_allowed':None},
                 {'asof':True},{'expires':'9999999999'}, {'asof':float('nan')},
                 {'expires':self.now/1000-1},{'asof':self.now/1000-100},
                 {'asof':self.now/1000+60},{'expires':self.now/1000+10000})
        for changes in patches:
            with self.subTest(changes=changes):self.assertEqual(self.decision(dict(self.news(),**changes))['action'],'WAIT')
        for field in ('policy_version','entry_allowed','risks','drivers','evidence','direction'):
            value=self.news();value.pop(field)
            self.assertEqual(self.decision(value)['action'],'WAIT')

    def test_direction_needs_complete_verified_fresh_evidence(self):
        for changes in ({'verified':False},{'verified':1},{'primary':'true'},
                        {'event_at':self.now/1000-21601},{'event_at':self.now/1000+1},
                        {'published':self.now/1000+60},{'origin':'unknown'},
                        {'source':'BBC'},{'url':'https://example.com/fixture'},
                        {'url':'https://user:pass@www.federalreserve.gov/fixture'},
                        {'rule':'unsupported_official_rule'},{'direction':'SELL'}):
            value=self.news();value['evidence'][0].update(changes)
            with self.subTest(changes=changes):self.assertEqual(self.decision(value)['action'],'WAIT')
        for evidence in ([],{},[None],['headline'],[dict(verified=True)]):
            self.assertEqual(self.decision(dict(self.news(),evidence=evidence))['action'],'WAIT')
        self.assertEqual(self.decision(dict(self.news(),event_id=None))['action'],'WAIT')
        self.assertEqual(self.decision(dict(self.news(),symbol='CLUSDT'))['action'],'WAIT')

    def test_cross_confirmation_rejects_single_source_and_syndication(self):
        value=self.news(symbol='CLUSDT')
        for evidence in ([value['evidence'][0]],
                         [dict(e,origin='Reuters') for e in value['evidence']],
                         [dict(e,fingerprint='same-copy') for e in value['evidence']]):
            self.assertEqual(self.decision(dict(value,evidence=evidence),symbol='CLUSDT')['action'],'WAIT')
        first=value['evidence'][0]
        copied=[first,dict(first,fingerprint='extra-sentence'),
                dict(value['evidence'][1],fingerprint=first['fingerprint'])]
        self.assertEqual(self.decision(dict(value,evidence=copied),symbol='CLUSDT')['action'],'WAIT')
        official=self.news();official['symbol']='CLUSDT'
        self.assertEqual(self.decision(official,symbol='CLUSDT')['action'],'WAIT')

    def test_identified_agencies_can_confirm_but_unknown_or_same_agency_cannot(self):
        value=self.news(symbol='CLUSDT')
        value['evidence'][0]['origin']='AFP'
        value['evidence'][1]['origin']='PA'
        self.assertEqual(self.decision(value,symbol='CLUSDT')['action'],'BUY')
        for origins in (('AFP','AFP'),('PA','PA'),('Unknown','Guardian')):
            changed=copy.deepcopy(value)
            for item,origin in zip(changed['evidence'],origins):item['origin']=origin
            self.assertEqual(self.decision(changed,symbol='CLUSDT')['action'],'WAIT')

    def test_active_and_review_facts_block_only_during_valid_acute_cooldown(self):
        for status in ('active','review_required'):
            value=self.news();value['risks']=[risk(self.now/1000,status)]
            result=self.decision(value)
            self.assertEqual(result['action'],'WAIT')
            self.assertEqual(result['news']['status'],'blocked')
            self.assertFalse(result['news']['entry_allowed'])
        value=self.news();value['risks']=[risk(self.now/1000,'resolved')]
        self.assertEqual(self.decision(value)['action'],'BUY')

    def test_unresolved_background_allows_closed_kline_but_does_not_create_direction_or_exit(self):
        for symbol in SYMBOLS:
            for status in ('active','review_required'):
                value=kline_news(symbol,now=self.now/1000)
                value['risks']=[risk(self.now/1000,status,gate='background')]
                value['drivers'][0]['status']='background'
                for sig in (self.sig,self.bearish()):
                    with self.subTest(symbol=symbol,status=status,side=sig['side']):
                        result=self.decision(value,sig,symbol=symbol)
                        self.assertEqual(result['action'],sig['side'])
                        self.assertEqual(result['source'],'kline')
                        self.assertEqual(result['news']['risks'][0]['status'],status)
                        self.assertIsNone(result['news']['direction'])
                self.assertEqual(self.decision(value,dict(self.sig,side=None),symbol=symbol)['action'],'WAIT')
                self.assertEqual(self.decision(value,self.bearish(),{'side':'BUY'},symbol)['action'],'WAIT')

    def test_cooldown_expiry_needs_fresh_background_snapshot_not_local_reinterpretation(self):
        now=self.now/1000
        for basis in ('event_time','first_evidence'):
            with self.subTest(time_basis=basis):
                acute_at=now-3599
                old=dict(kline_news(now=now),status='blocked',kline_allowed=False,
                         risks=[risk(now,acute_at=acute_at,time_basis=basis)])
                self.assertFalse(news_state(old,'XAUUSDT',now)['kline_allowed'])
                after=now+2
                self.assertFalse(news_state(old,'XAUUSDT',after)['kline_allowed'])
                fresh=kline_news(now=after)
                fresh['risks']=[risk(after,gate='background',acute_at=acute_at,time_basis=basis)]
                fresh['drivers'][0]['status']='background'
                checked=news_state(fresh,'XAUUSDT',after)
                self.assertTrue(checked['kline_allowed'])
                self.assertEqual(checked['risks'][0]['status'],'active')
                self.assertEqual(decide(self.sig,fresh,'XAUUSDT',after*1000)['action'],'BUY')

    def test_risk_gate_requires_complete_consistent_typed_metadata(self):
        now=self.now/1000;valid=risk(now)
        changes=({'gate_version':True},{'gate_version':1.0},{'entry_gate':'clear'},
                 {'gate_reason':''},{'entry_gate':'background','blocking':False},
                 {'blocking':False},{'status':'resolved'},
                 {'acute_event_id':None},{'acute_rule':''},{'acute_event_at':True},
                 {'cooldown_until':float('nan')},{'cooldown_until':float('inf')},
                 {'cooldown_until':valid['cooldown_until']+1},{'time_basis':'published'},
                 {'acute_event_at':now+1,'cooldown_until':now+3601})
        variants=[dict(valid,**change) for change in changes]
        for field in ('gate_version','entry_gate','gate_reason','acute_event_id','acute_rule',
                      'acute_event_at','time_basis','cooldown_until'):
            item=dict(valid);item.pop(field);variants.append(item)
        variants.append(risk(now,gate='cooldown',acute_at=now-3601))
        variants.append(dict(risk(now,status='resolved'),acute_event_id='must-be-empty'))
        variants.append(dict(risk(now,gate='background'),time_basis='first_evidence'))
        for item in variants:
            with self.subTest(risk=item):
                value=kline_news(now=now);value['risks']=[item]
                result=self.decision(value)
                self.assertEqual(result['action'],'WAIT')
                self.assertEqual(result['news']['status'],'unavailable')

    def test_risk_and_driver_schema_is_strict_without_inventing_driver_coverage(self):
        valid=risk(self.now/1000)
        invalid_risks=(None,{},['risk'],[dict(valid,blocking='false')],
                       [dict(valid,blocking=False)],[dict(valid,status='resolved')],
                       [dict(valid,status='maybe')],[dict(valid,last_seen=None)],
                       [dict(valid,url='https://example.com/news')])
        for items in invalid_risks:
            self.assertEqual(self.decision(dict(self.news(),risks=items))['action'],'WAIT')
        for drivers in (None,{},[],['rates'],[dict(key='x',label='x',status='approved',reason='x')]):
            self.assertEqual(self.decision(dict(self.news(),drivers=drivers))['action'],'WAIT')
        for status in ('verified','no_recent_evidence','not_covered','background'):
            value=self.news();value['drivers'][0]['status']=status
            self.assertEqual(self.decision(value)['action'],'BUY')

    def test_verified_fed_does_not_override_fresh_unverified_dollar_driver(self):
        pending=dict(key='usd_real_yield',label='美元与实际利率',status='review_required',
                     reason='已发现近期美元报道，方向尚未核验')
        value=self.news();value['drivers'].append(pending)
        result=self.decision(value)
        self.assertEqual(result['action'],'WAIT')
        self.assertEqual(result['news']['status'],'blocked')
        self.assertIs(result['news']['entry_allowed'],False)
        self.assertIsNone(result['news']['direction'])
        reverse=self.news('SELL');reverse['drivers'].append(pending)
        self.assertEqual(self.decision(reverse,self.bearish(),{'side':'BUY'})['action'],'WAIT')

    def test_verified_news_and_momentum_still_require_closed_breakout_for_entry(self):
        sig=dict(self.sig,side=None)
        self.assertEqual(self.decision(self.news(),sig)['action'],'WAIT')
        for changed in (dict(sig,previous_close=103),dict(sig,fast=99),dict(sig,reference=0)):
            self.assertEqual(self.decision(self.news(),changed)['action'],'WAIT')

    def test_news_cannot_chase_late_unclosed_or_malformed_candle(self):
        for stamp in (self.now-3800000,self.now-1000,True,'123',None,float('nan')):
            self.assertEqual(self.decision(self.news(),dict(self.sig,candle=stamp))['action'],'WAIT')

    def test_explicit_healthy_insufficient_news_allows_only_closed_breakout(self):
        news=kline_news(now=self.now/1000)
        for sig in (self.sig,self.bearish()):
            result=self.decision(news,sig)
            self.assertEqual(result['action'],sig['side'])
            self.assertEqual(result['source'],'kline')
        self.assertEqual(self.decision(news,dict(self.sig,side=None))['action'],'WAIT')
        for stamp in (self.now-3800000,self.now-1000,True,None,float('nan')):
            self.assertEqual(self.decision(news,dict(self.sig,candle=stamp))['action'],'WAIT')
        late=self.decision(news,dict(self.sig,candle=self.now-3800000))
        self.assertEqual(late['source'],'kline')
        self.assertEqual(self.decision(news,self.bearish(),{'side':'BUY'})['action'],'WAIT')
        self.assertEqual(self.decision(news,self.sig,{'side':'SELL'})['action'],'WAIT')

    def test_kline_permission_never_overrides_invalid_news_or_risk(self):
        news=kline_news(now=self.now/1000)
        missing=dict(news);missing.pop('kline_allowed')
        self.assertIs(news_state(missing,'XAUUSDT',self.now/1000)['kline_allowed'],False)
        invalid=(missing,dict(news,kline_allowed=False),dict(news,kline_allowed='true'),
                 dict(news,status='blocked'),dict(news,status='unavailable'),
                 dict(news,expires=self.now/1000-1),dict(news,direction='BUY'),
                 dict(news,entry_allowed=True),dict(news,evidence=self.news()['evidence']),
                 dict(news,event_id='leftover-direction-event'),dict(news,event_id=False),
                 dict(news,risks=[risk(self.now/1000)]),
                 dict(self.news('SELL'),kline_allowed=True))
        for item in invalid:
            with self.subTest(item=item):
                result=self.decision(item)
                self.assertEqual(result['action'],'WAIT')
                self.assertIs(result['news']['kline_allowed'],False)

    def test_unconfirmed_driver_is_reference_for_explicit_kline_path(self):
        news=kline_news(now=self.now/1000)
        news['drivers']=[dict(key='usd',label='美元',status='review_required',reason='方向未确认')]
        for sig in (self.sig,self.bearish()):
            result=self.decision(news,sig)
            self.assertEqual(result['action'],sig['side'])
            self.assertEqual(result['source'],'kline')
            self.assertEqual(result['news']['drivers'][0]['status'],'review_required')
        self.assertEqual(self.decision(news,dict(self.sig,side=None))['action'],'WAIT')
        self.assertEqual(self.decision(news,self.bearish(),{'side':'BUY'})['action'],'WAIT')
        for blocked in (dict(news,expires=self.now/1000-1),
                        dict(news,risks=[risk(self.now/1000)])):
            self.assertEqual(self.decision(blocked)['action'],'WAIT')

    def test_only_trusted_opposite_news_and_price_confirmation_can_exit(self):
        position={'side':'BUY'}
        self.assertEqual(self.decision(self.news('SELL'),position=position)['action'],'WAIT')
        self.assertEqual(self.decision(self.news('SELL'),self.bearish(),position)['action'],'EXIT')
        bad=(None,dict(self.news('SELL'),entry_allowed=False),dict(self.news('SELL'),policy_version=1),
             dict(self.news('SELL'),evidence=[]),dict(self.news('SELL'),expires=self.now/1000-1),
             dict(self.news('SELL'),risks=[risk(self.now/1000)]))
        for value in bad:self.assertEqual(self.decision(value,self.bearish(),position)['action'],'WAIT')
        self.assertEqual(self.decision(self.news(),position=position)['action'],'WAIT')

    def test_news_validation_does_not_mutate_shared_analysis_snapshot(self):
        value=self.news();value['risks']=[risk(self.now/1000)]
        before=copy.deepcopy(value)
        self.decision(value)
        self.assertEqual(value,before)


class StrategyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.api=Fake()
        self.engine=Engine(self.api,Path(self.temp.name)/'paper.sqlite')
        self.now=int(time.time()*1000)
        self.engine.quote=self.api.market('XAUUSDT')
        self.engine.updated=time.time()
        self.engine.sig=dict(side='BUY',candle=self.now-3601000,fast=.094,slow=.091,
                             reference=.092,previous_close=.091,atr=.001)
        self.engine.news_provider=lambda symbol:verified_news(symbol,'BUY')
    def tearDown(self):self.engine.db.close();self.temp.cleanup()

    def test_news_connected_to_entry_exit_and_journal(self):
        self.engine.sig['side']=None
        self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])
        self.engine.sig['side']='BUY'
        self.engine.maybe_enter()
        self.assertEqual(self.engine.s['position']['side'],'BUY')
        self.assertEqual(self.engine.s['position']['decision']['source'],'news+kline')
        self.engine.sig.update(fast=.090,reference=.092,previous_close=.093)
        self.engine.last_candles=time.time()
        self.engine.news_provider=lambda symbol:verified_news(symbol,'SELL')
        self.engine.tick()
        self.assertIsNone(self.engine.s['position'])
        self.assertTrue(any('反转' in e['message'] for e in self.engine.s['events']))
        self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])

    def test_empty_news_repeated_breakout_never_reaches_account_or_orders(self):
        self.engine.news_provider=lambda symbol:None
        for _ in range(3):self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.engine.s['last_candle'],0)
        self.assertEqual(self.api.calls,[])

    def test_background_risk_can_start_and_open_without_claiming_event_resolved(self):
        value=kline_news()
        value['risks']=[risk(time.time(),status='review_required',gate='background')]
        value['drivers'][0]['status']='background'
        self.engine.news_provider=lambda symbol:value
        self.engine.start()
        self.engine.maybe_enter()
        position=self.engine.s['position']
        self.assertIsNotNone(position,self.engine.decision)
        self.assertEqual(position['decision']['source'],'kline')
        self.assertEqual(position['decision']['news']['risks'][0]['status'],'review_required')
        self.assertEqual(position['decision']['news']['risks'][0]['entry_gate'],'background')
        self.assertEqual(self.api.calls,[])

    def test_news_becoming_blocked_before_planning_prevents_order(self):
        good=verified_news()
        blocked=dict(good,status='blocked',direction=None,entry_allowed=False,risks=[risk(time.time())])
        with patch.object(self.engine,'news_provider',side_effect=[good,blocked]):self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.api.calls,[])

    def test_kline_fallback_becoming_blocked_before_planning_prevents_order(self):
        good=kline_news()
        good['risks']=[risk(time.time(),gate='background')]
        blocked=dict(good,status='blocked',kline_allowed=False,risks=[risk(time.time())])
        with patch.object(self.engine,'news_provider',side_effect=[good,blocked]):self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])
        self.assertEqual(self.api.calls,[])

    def test_demo_kline_fallback_revoked_immediately_before_submit_sends_no_write(self):
        self.engine.db.close();self.api.demo=True
        self.engine=Engine(self.api,Path(self.temp.name)/'demo.sqlite','demo')
        self.engine.quote=self.api.market('XAUUSDT')
        self.engine.sig=dict(side='BUY',candle=self.now-3601000,reference=.092,atr=.001)
        good=kline_news()
        good['risks']=[risk(time.time(),gate='background')]
        blocked=dict(good,status='blocked',kline_allowed=False,risks=[risk(time.time())])
        with patch.object(self.engine,'news_provider',side_effect=[good,good,blocked]):
            with self.assertRaisesRegex(ValueError,'提交前新闻或收盘信号失效'):self.engine.maybe_enter()
        self.assertIsNone(self.engine.s['position'])
        self.assertIsNone(self.engine.s['pending'])
        self.assertTrue(all(method=='GET' for method,_,_ in self.api.calls))

    def test_news_outage_and_candle_failure_do_not_prevent_existing_stop(self):
        self.engine.maybe_enter();self.assertIsNotNone(self.engine.s['position'])
        self.api.price=.089
        self.engine.news_provider=lambda symbol:(_ for _ in ()).throw(ValueError('news offline'))
        with patch.object(self.api,'candles',side_effect=ValueError('K线断线')):self.engine.tick()
        self.assertIsNone(self.engine.s['position'])
        self.assertFalse(self.engine.s['enabled'])

    def test_provider_exception_without_stop_keeps_position_protection_and_pauses(self):
        self.engine.maybe_enter();position_id=self.engine.s['position']['id']
        self.engine.last_candles=time.time()
        self.engine.news_provider=lambda symbol:(_ for _ in ()).throw(ValueError('news offline'))
        self.engine.tick()
        self.assertEqual(self.engine.s['position']['id'],position_id)
        self.assertFalse(self.engine.s['enabled'])
        self.assertEqual(self.api.calls,[])

    def test_demo_actual_funding_over_budget_exits_without_news_permission(self):
        self.engine.db.close();self.api.demo=True
        self.engine=Engine(self.api,Path(self.temp.name)/'demo.sqlite','demo')
        self.engine.quote=self.api.market('XAUUSDT')
        self.engine.news_provider=lambda symbol:verified_news(symbol)
        self.engine.sig=dict(side='BUY',candle=self.now-3601000,reference=.092,atr=.001)
        self.engine.maybe_enter();self.assertIsNotNone(self.engine.s['position'])
        p=self.engine.s['position'];p['income_check_due']=0
        self.engine.news_provider=lambda symbol:None
        charge=p['risk_audit']['funding_reserve']+.01
        with patch.object(self.api,'funding_income',return_value=[dict(time=self.now,income=-charge,tranId='fixture')]):self.engine.sync_demo()
        self.assertIsNone(self.engine.s['position'])
        self.assertTrue(self.engine.s['halted'])
        self.assertTrue(any(params.get('reduceOnly')=='true' for method,path,params in self.api.calls if method=='POST' and path.endswith('/order')))

if __name__=='__main__':unittest.main()
