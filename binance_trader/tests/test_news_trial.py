"""Synthetic fixtures and local HTTP only. No exchange or paid model requests."""
import copy
import json
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError

from binance_trader.news_trial import (NewsTrial, TrialClient, article_version, collect_inputs,
    public_articles, LUNA, SOL, measured, evidence_spans)
from binance_trader.news_trial_server import TrialSession, make_server
from binance_trader.ai_news import AINewsError
from binance_trader.core import SYMBOLS
from binance_trader.news_risk import DRIVERS
from test_ai_news import ARTICLE,CONFIG,NOW,envelope,MODEL
from test_ai_fundamentals import metric
from test_strategy import kline_news


def triage_row(article=ARTICLE,kind='material',symbols=None):
    return dict(id=article_version(article),kind=kind,symbols=list(SYMBOLS) if symbols is None else symbols,
        fact='合成测试：持续冲突，本条无法提供精确事件时间。',quote_id='q1',event_time=None)


class TrialTransportTests(unittest.TestCase):
    def client(self,result,model=LUNA,returned=None):
        self.calls=[]
        def transport(**kwargs):
            self.calls.append(kwargs)
            data=json.loads(envelope(result));data['model']=model if returned is None else returned
            return 200,json.dumps(data).encode()
        return TrialClient(dict(CONFIG,model=model,base_url='http://127.0.0.1:8080/v1'),transport)

    def test_triage_exact_source_and_model_identity(self):
        c=self.client({'articles':[triage_row()]})
        result=c.triage([ARTICLE],NOW)
        self.assertEqual(result[0]['kind'],'material');self.assertTrue(c.model_verified)
        request=json.loads(self.calls[0]['body'])
        self.assertEqual(request['reasoning_effort'],'low')
        self.assertEqual(request['model'],LUNA);self.assertFalse(request['stream'])
        self.assertNotIn(CONFIG['api_key'],json.dumps(request))
        self.assertNotIn('tools',request)

    def test_missing_duplicate_and_unknown_article_rejected(self):
        for rows in [[],[triage_row(),triage_row()],[dict(triage_row(),id='unknown')]]:
            with self.subTest(rows=rows),self.assertRaises(AINewsError):
                self.client({'articles':rows}).triage([ARTICLE],NOW)

    def test_negation_cannot_be_changed_in_quote_and_future_time_rejected(self):
        a=dict(ARTICLE,body='Officials denied that production had stopped. Output continued unchanged.')
        for change in [{'quote_id':'production had stopped yesterday'}, {'event_time':'2099-01-01T00:00:00Z'},
                       {'event_time':'2026-10-01T12:00:00'}, {'symbols':['BTCUSDT']},
                       {'kind':'irrelevant','symbols':list(SYMBOLS)}]:
            with self.subTest(change=change),self.assertRaises(AINewsError):
                self.client({'articles':[dict(triage_row(a),**change)]}).triage([a],NOW)

    def test_model_mismatch_and_missing_provenance_fail_closed(self):
        for returned in ['gpt-other','',SOL]:
            c=self.client({'articles':[triage_row()]},returned=returned)
            result=measured(lambda:c.triage([ARTICLE],NOW),c)
            self.assertEqual(result['status'],'error');self.assertFalse(result['model_verified'])
            self.assertIsNone(result['value'])

    def test_declared_dated_snapshot_accepted(self):
        c=self.client({'articles':[triage_row()]},returned=LUNA+'-2026-09-01')
        c.triage([ARTICLE],NOW);self.assertTrue(c.model_verified)

    def test_private_fields_and_conflicting_article_identity_rejected(self):
        for rows in [[dict(ARTICLE,balance=100)],[ARTICLE,ARTICLE],[dict(ARTICLE,published=NOW+1)]]:
            with self.assertRaises(AINewsError):public_articles(rows,NOW)

    def test_identity_ignores_polling_time_and_url_but_tracks_revision(self):
        self.assertEqual(article_version(ARTICLE),article_version(dict(ARTICLE,published=NOW,url=ARTICLE['url']+'?tracking=1')))
        self.assertNotEqual(article_version(ARTICLE),article_version(dict(ARTICLE,body=ARTICLE['body']+' Correction.')))

    def test_review_keeps_original_body_and_uses_host_validation(self):
        c=self.client(MODEL,model=SOL);c.hint=[triage_row()]
        out=c.analyze('XAUUSDT',[ARTICLE],NOW)
        sent=json.loads(json.loads(self.calls[0]['body'])['messages'][1]['content'])
        self.assertEqual(sent['articles'][0]['body'],ARTICLE['body'])
        self.assertIn('triage',sent);self.assertFalse(out['trading_authority'])

    def test_partial_validation_keeps_good_article_and_identifies_bad_field(self):
        second=dict(ARTICLE,id='second')
        bad=dict(triage_row(second),quote_id='invented')
        client=self.client({'articles':[triage_row(),bad]})
        result=measured(lambda:client.triage([ARTICLE,second],NOW),client)
        self.assertEqual(result['status'],'partial')
        self.assertEqual([r['id'] for r in result['value']],[article_version(ARTICLE)])
        issue=result['validation_issues'][0]
        self.assertEqual(issue['code'],'UNKNOWN_QUOTE_ID');self.assertEqual(issue['field'],'quote_id')
        self.assertEqual(issue['article_id'],'second');self.assertEqual(issue['url'],ARTICLE['url'])
        self.assertNotIn('invented',json.dumps(result['validation_issues']))

    def test_host_spans_cover_body_and_bound_quotes_without_model_copying(self):
        for body in [' '.join(['word']*70)+'.', '字'*700, 'Officials denied that production had stopped. Output continued unchanged.',
                     '  a \n b. More words.\nNext sentence.', 'word '*46+'word.longword', 'a'*239+' bc.' ]:
            with self.subTest(body=body):
                spans=evidence_spans(body)
                self.assertEqual(re.sub(r'\s+', '', ''.join(r['text'] for r in spans)),re.sub(r'\s+','',body))
                self.assertTrue(all(r['text'] in body and len(r['text'])<=240 and len(r['text'].split())<=25 for r in spans))
                article=dict(ARTICLE,body=body)
                client=self.client({'articles':[triage_row(article)]})
                result=client.triage([article],NOW)
                self.assertEqual(result[0]['quote'],spans[0]['text'])
                self.assertNotIn('quote_id',result[0])
                sent=json.loads(json.loads(self.calls[0]['body'])['messages'][1]['content'])
                self.assertEqual(sent['articles'][0]['body_evidence'],spans)
                self.assertNotIn('body',sent['articles'][0])
        client=self.client({'articles':[]})
        result=measured(lambda:client.triage([ARTICLE],NOW),client)
        self.assertEqual(result['validation_issues'][0]['code'],'MISSING_ARTICLE')

    def test_duplicate_cannot_keep_first_conflicting_interpretation(self):
        second=dict(ARTICLE,id='second')
        client=self.client({'articles':[triage_row(),dict(triage_row(),kind='background'),triage_row(second)]})
        result=measured(lambda:client.triage([ARTICLE,second],NOW),client)
        self.assertEqual(result['status'],'partial')
        self.assertEqual([r['id'] for r in result['value']],[article_version(second)])


class FakeClient:
    def __init__(self,owner,config,hint):
        self.owner,self._config,self.hint=owner,config,hint
        self.last_usage={'total_tokens':10};self.returned_model=config['model'];self.model_verified=True
    def triage(self,articles,now):
        self.owner.calls.append(('triage',copy.deepcopy(articles)))
        if self.owner.triage_error:raise AINewsError('EVIDENCE','test')
        rows=[triage_row(a,self.owner.kind) for a in articles]
        for row,article in zip(rows,articles):
            row.pop('quote_id');row['quote']=evidence_spans(article['body'])[0]['text']
        return rows
    def analyze(self,symbol,articles,now,fundamentals):
        self.owner.calls.append(('cascade' if self.hint is not None else 'direct',symbol))
        self.owner.received.append((symbol,copy.deepcopy(articles),copy.deepcopy(self.hint)))
        if self.owner.timeout:raise AINewsError('TIMEOUT','test secret')
        self.owner.now[0]+=self.owner.elapsed
        return dict(symbol=symbol,summary='合成样例：等待K线确认',direction='uncertain',events=[],drivers=[],
                    model=SOL,trading_authority=False,analyzed_at=now)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.received=[];self.now=[NOW];self.kind='material';self.triage_error=False;self.timeout=False;self.elapsed=0
        self.trial=NewsTrial(CONFIG,factory=lambda config,hint=None:FakeClient(self,config,hint),clock=lambda:self.now[0])
        self.inputs={s:dict(articles=[copy.deepcopy(ARTICLE)],metrics=[],healthy=True,news_decision=kline_news(s,now=NOW),signal={}) for s in SYMBOLS}

    def run_trial(self):return self.trial.run(self.inputs,self.now[0])

    def test_all_five_share_one_luna_and_compare_both_arms(self):
        out=self.run_trial()
        self.assertEqual(len(self.calls),11)
        self.assertEqual(sum(c[0]=='triage' for c in self.calls),1)
        self.assertEqual(set(r['symbol'] for r in out['rows']),set(SYMBOLS))
        self.assertTrue(all(r['direction_agreement'] for r in out['rows']))
        self.assertFalse(out['execution_connected']);self.assertFalse(out['trading_authority'])

    def test_duplicate_polling_never_rebills_including_failed_sol(self):
        for fail in (False,True):
            self.setUp();self.timeout=fail;self.run_trial();count=len(self.calls)
            self.now[0]+=30
            for row in self.inputs.values():row['articles'][0]['published']=self.now[0]-1
            out=self.run_trial();self.assertEqual(len(self.calls),count)
            self.assertEqual(out['luna']['status'],'skipped')

    def test_failed_luna_routes_originals_to_sol_once_and_never_rebills(self):
        self.triage_error=True;out=self.run_trial()
        self.assertEqual(out['luna']['status'],'error')
        self.assertEqual(sum(c[0]=='cascade' for c in self.calls),5)
        self.assertTrue(all(r['cascade']['route']=='original_text_fallback' for r in out['rows']))
        self.assertTrue(all(r['cascade']['article_count']==1 for r in out['rows']))
        self.assertTrue(all(articles==[ARTICLE] and hint==[] for _,articles,hint in self.received if hint is not None))
        count=len(self.calls);self.run_trial();self.assertEqual(len(self.calls),count)

    def test_later_triage_failure_invalidates_old_interpretation_and_preserves_new_original(self):
        self.run_trial();self.calls=[];self.received=[];self.triage_error=True
        revised=dict(ARTICLE,body=ARTICLE['body']+' Correction: this report has been withdrawn.')
        for row in self.inputs.values():row['articles']=[revised]
        out=self.run_trial()
        self.assertTrue(all(r['cascade']['route']=='original_text_fallback' for r in out['rows']))
        self.assertTrue(all(articles==[revised] and hint==[] for _,articles,hint in self.received if hint is not None))

    def test_luna_cannot_silently_veto_source_associated_new_article(self):
        self.run_trial();self.calls=[];self.received=[]
        revised=dict(ARTICLE,body=ARTICLE['body']+' New conflict escalation reported.')
        for source in self.inputs.values():source['articles']=[revised]
        def omitted(client,articles,now):
            return [dict(triage_row(a,kind='material',symbols=['CLUSDT','BZUSDT']),quote=evidence_spans(a['body'])[0]['text']) for a in articles]
        with patch.object(FakeClient,'triage',omitted):out=self.run_trial()
        self.assertEqual(sum(c[0]=='cascade' for c in self.calls),5)
        for row in out['rows']:
            if row['symbol'] in ('XAUUSDT','XAGUSDT','NATGASUSDT'):
                self.assertEqual(row['cascade']['route'],'original_text_routing_review')
                self.assertIn('新闻源关联与Luna品种判断不同，原文交Sol核对',row['review_reasons'])
        self.assertTrue(all(articles==[revised] for _,articles,_ in self.received))
        count=len(self.calls);self.run_trial();self.assertEqual(len(self.calls),count)

    def test_diagnosis_is_luna_only_and_full_run_reuses_triage_without_repeat(self):
        diagnostic=self.trial.run(self.inputs,NOW,mode='triage')
        self.assertEqual(diagnostic['kind'],'triage');self.assertEqual(diagnostic['rows'],[])
        self.assertEqual(len(self.calls),1)
        self.run_trial()
        self.assertEqual(sum(c[0]=='triage' for c in self.calls),1)
        self.assertEqual(sum(c[0]=='cascade' for c in self.calls),5)

    def test_completion_freshness_is_not_recomputed_after_other_arm_finishes(self):
        self.elapsed=70
        out=self.run_trial()
        # First direct call completed at +70s; other arm completed at +140s.
        self.assertTrue(out['rows'][0]['direct']['input_fresh_at_completion'])
        self.assertFalse(out['rows'][0]['direct']['input_fresh_at_projection'])

    def test_background_update_does_not_trigger_sol_but_correction_does(self):
        self.run_trial();self.calls=[];self.kind='background'
        for row in self.inputs.values():row['articles'][0]['body']+=' Background continues.'
        out=self.run_trial()
        self.assertEqual(sum(c[0]=='triage' for c in self.calls),1)
        self.assertFalse(any(c[0]=='cascade' for c in self.calls))
        self.assertTrue(all(r['cascade']['status']=='skipped' for r in out['rows']))
        self.calls=[];self.kind='correction'
        for row in self.inputs.values():row['articles'][0]['body']+=' Correction issued.'
        self.run_trial();self.assertEqual(sum(c[0]=='cascade' for c in self.calls),5)

    def test_feed_removal_does_not_create_new_model_request(self):
        extra=dict(ARTICLE,id='second',body=ARTICLE['body']+' Existing context.')
        for row in self.inputs.values():row['articles'].append(extra)
        self.run_trial();self.calls=[]
        for row in self.inputs.values():row['articles'].pop()
        self.run_trial();self.assertEqual(self.calls,[])

    def test_revised_metric_triggers_only_affected_symbol(self):
        self.inputs['XAUUSDT']['metrics']=[metric()];self.run_trial();self.calls=[]
        self.inputs['XAUUSDT']['metrics']=[metric(current=2.95)]
        self.run_trial();self.assertEqual(sorted(c[0] for c in self.calls),['cascade','direct'])
        self.assertTrue(all(c[1]=='XAUUSDT' for c in self.calls))

    def test_input_expired_during_analysis_cannot_publish_current_signal(self):
        self.elapsed=70
        out=self.run_trial()
        self.assertTrue(all(r['cascade']['research_action']=='WAIT' for r in out['rows']))
        self.assertFalse(out['rows'][-1]['direct']['input_fresh_at_completion'])
        self.assertIsNone(out['rows'][-1]['direct']['ai_filter'])

    def test_incomplete_stale_future_input_rejected(self):
        for value,stamp in [(dict(list(self.inputs.items())[:-1]),NOW),(self.inputs,NOW-121),(self.inputs,NOW+1)]:
            with self.assertRaises(AINewsError):self.trial.run(value,stamp)
        self.assertEqual(self.calls,[])

    def test_unhealthy_sources_skip_review_and_order_codes_are_not_reported_as_execution(self):
        for row in self.inputs.values():row['healthy']=False
        out=self.run_trial()
        self.assertFalse(any(c[0] in ('direct','cascade') for c in self.calls))
        self.assertTrue(all(r['direct']['research_action']=='WAIT' for r in out['rows']))

    def test_completed_analysis_reaches_pure_kline_filter_for_both_directions_all_five(self):
        from test_ai_direction import model_result
        for direction,side in [('bullish','BUY'),('bearish','SELL')]:
            self.setUp()
            for row in self.inputs.values():
                row['signal']=dict(side=side,candle=NOW*1000-3601000,
                    fast=102 if side=='BUY' else 98,slow=100,reference=102 if side=='BUY' else 98,
                    previous_close=101 if side=='BUY' else 99,atr=1)
            def analyze(client,symbol,articles,now,fundamentals):
                return dict(model_result(symbol,now,direction),model=SOL)
            with patch.object(FakeClient,'analyze',analyze):out=self.run_trial()
            self.assertTrue(all(r['cascade']['research_action']==side for r in out['rows']))
            self.assertFalse(out['execution_connected'])

    def test_opposite_model_direction_never_overrides_kline(self):
        from test_ai_direction import model_result
        for row in self.inputs.values():
            row['signal']=dict(side='BUY',candle=NOW*1000-3601000,fast=102,slow=100,
                reference=102,previous_close=101,atr=1)
        def analyze(client,symbol,articles,now,fundamentals):
            return dict(model_result(symbol,now,'bearish'),model=SOL)
        with patch.object(FakeClient,'analyze',analyze):out=self.run_trial()
        self.assertTrue(all(r['cascade']['research_action']=='WAIT' for r in out['rows']))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.session=TrialSession(self.temp.name)
        self.server=make_server(self.session,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        with urlopen(self.base) as r:page=r.read().decode()
        self.token=re.search(r'name="csrf-token" content="([^"]+)"',page).group(1)

    def tearDown(self):
        self.session.close();self.server.shutdown();self.server.server_close();self.thread.join(2);self.temp.cleanup()

    def post(self,path,data,headers=None):
        request=Request(self.base+path,data=json.dumps(data).encode(),headers=headers or
            {'Content-Type':'application/json','Origin':self.base,'X-CSRF-Token':self.token})
        try:
            with urlopen(request) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)

    def test_key_never_returned_or_persisted(self):
        status,result=self.post('/api/config',{'api_key':CONFIG['api_key']})
        self.assertEqual(status,200);self.assertTrue(result['configured'])
        self.assertNotIn(CONFIG['api_key'],json.dumps(result))
        from pathlib import Path
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_origin_csrf_and_no_trade_routes(self):
        self.assertEqual(self.post('/api/config',{'api_key':CONFIG['api_key']},{'Content-Type':'application/json'})[0],403)
        for path in ['/api/order','/api/leverage','/api/withdraw']:
            self.assertEqual(self.post(path,{})[0],404)
        self.assertEqual(self.post('/api/start',{})[0],400)

    def test_public_status_has_no_account_or_config_secrets(self):
        with urlopen(self.base+'/api/status') as r:result=json.load(r)
        self.assertEqual(result['status'],'waiting_key');self.assertFalse(result['execution_connected'])
        self.assertNotIn('api_key',json.dumps(result));self.assertNotIn('balance',json.dumps(result))

    def test_fixed_module_reload_keeps_key_in_memory_and_resets_protocol_cache(self):
        from types import SimpleNamespace
        from pathlib import Path
        made=[]
        class Runner:
            def __init__(self,config):self.config=dict(config);made.append(self)
            def run(self,inputs,asof,progress,mode):
                return dict(version=99,kind=mode,trading_authority=False,execution_connected=False)
        module=SimpleNamespace(NewsTrial=Runner,collect_inputs=lambda **kw:({},NOW),TRIAL_VERSION=99)
        self.session.configure(CONFIG['api_key'])
        with patch.object(Path,'read_bytes',return_value=b'changed fixed module'), \
             patch('binance_trader.news_trial_server.importlib.reload',return_value=module) as reload:
            self.session.start('triage')
            deadline=time.monotonic()+2
            while self.session.snapshot()['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(self.session.snapshot()['status'],'complete')
            reload.assert_called_once()
            self.assertEqual(made[0].config['api_key'],CONFIG['api_key'])
            self.assertNotIn(CONFIG['api_key'],json.dumps(self.session.snapshot()))
            self.assertEqual(self.session.snapshot()['version'],99)
            # Subsequent configuration continues using the loaded implementation.
            self.session.configure(CONFIG['api_key'])
            self.assertIsInstance(self.session.runner,Runner)


if __name__=='__main__':unittest.main()
