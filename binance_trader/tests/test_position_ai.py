"""Position-scoped model calls; fixtures only, no credentials or venue writes."""
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from binance_trader.ai_schedule import major_events, work_scope
from binance_trader.ai_service import AIService
from binance_trader.analysis import Analysis
from binance_trader.ai_policy import project_ai
from binance_trader.ai_news import AINewsError
from binance_trader.core import SYMBOLS
from test_ai_news import ARTICLE, CONFIG, NOW
from test_ai_integration import ModelHarness


def event_feed(now=NOW, body='Exports were halted today.', event_id='a'*24):
    article=dict(ARTICLE,body=body,published=now-30)
    event=dict(acute_event_id=event_id,acute_event_at=now-30,entry_gate='cooldown',
               blocking=True,subject='Fixture terminal',url=article['url'])
    return dict(coverage={'healthy':True},decision={'asof':now,'risks':[event]},_ai_articles=[article])


class PositionAITests(unittest.TestCase):
    def setUp(self):
        self.now=[NOW];self.model=ModelHarness()
        self.state=dict(symbol='XAUUSDT',selection_mode='auto',position=None,pending=None,enabled=False)
        def factory(config):
            client=self.model.factory(config)
            client.last_usage={'prompt_tokens':100,'completion_tokens':10}
            return client
        self.ai=AIService(factory,clock=lambda:self.now[0]);self.ai.configure(**CONFIG)
        self.ai.scope_provider=lambda:work_scope(self.state)

    def submit(self,symbol,body='Normal public update.',events=None):
        self.ai.submit(symbol,[dict(ARTICLE,body=body,published=self.now[0]-5)],self.now[0],major_events=events)

    def call(self,symbol):
        result=self.ai.run_once(symbol);self.now[0]+=2;return result

    def test_two_update_waves_reduce_calls_and_flat_resumes_latest_input(self):
        self.state['position']={'symbol':'XAUUSDT','side':'BUY','secret_fixture':'not for model'}
        for revision in range(2):
            for symbol in SYMBOLS:self.submit(symbol,'Normal revision '+str(revision))
            for symbol in SYMBOLS:
                self.assertEqual(self.call(symbol),symbol=='XAUUSDT')
        self.assertEqual(len(self.model.calls),2)  # Without position scope: 5 x 2 = 10.
        self.assertEqual(self.ai.status()['usage'],dict(analysis_calls=2,reported_calls=2,prompt_tokens=200,completion_tokens=20))
        for symbol in SYMBOLS[1:]:
            snap=self.ai.snapshot(symbol)
            self.assertEqual(snap['status'],'standby')
            self.assertEqual(project_ai(snap,symbol,self.now[0])['status'],'fallback')
            self.assertNotIn(symbol,self.ai.attempted)
        self.assertNotIn('secret_fixture',json.dumps(self.model.calls))
        self.state['position']=None
        self.assertEqual(self.ai.snapshot('BZUSDT')['queue_wait_ms'],0)
        for symbol in SYMBOLS[1:]:self.assertTrue(self.call(symbol))
        self.assertEqual(len(self.model.calls),6)
        for c in self.model.calls[2:]:self.assertEqual(c['articles'][0]['body'],'Normal revision 1')

    def test_major_event_once_corrections_wake_unrelated_news_do_not(self):
        self.state['position']={'symbol':'XAUUSDT','side':'BUY'}
        data=event_feed(self.now[0]);events=major_events(data,self.now[0])
        self.assertEqual(len(events),1)
        self.submit('BZUSDT',data['_ai_articles'][0]['body'],events);self.assertTrue(self.call('BZUSDT'))
        alert=next(r['alert'] for r in self.ai.monitoring_status()['rows'] if r['symbol']=='BZUSDT')
        self.assertEqual(alert['status'],'ready');self.assertFalse(alert['automatic_rotation'])
        self.submit('BZUSDT','Unrelated ordinary data changed.',events)
        self.assertFalse(self.call('BZUSDT'));self.assertEqual(self.ai.snapshot('BZUSDT')['status'],'standby')
        self.assertEqual(next(r['alert']['status'] for r in self.ai.monitoring_status()['rows'] if r['symbol']=='BZUSDT'),'historical')
        data=event_feed(self.now[0],body='Correction: exports were partially halted today.')
        revised=major_events(data,self.now[0]);self.assertNotEqual(events[0]['token'],revised[0]['token'])
        self.submit('BZUSDT',data['_ai_articles'][0]['body'],revised);self.assertTrue(self.call('BZUSDT'))
        self.assertFalse(self.call('BZUSDT'))
        self.assertEqual(self.state['position'],{'symbol':'XAUUSDT','side':'BUY'})

    def test_failed_major_event_not_retried_and_usage_missing_not_counted(self):
        self.state['position']={'symbol':'XAUUSDT'}
        self.ai.factory=self.model.factory  # This fixture provider reports no tokens.
        self.model.failure=AINewsError('TIMEOUT','Synthetic timeout')
        self.submit('CLUSDT','Event',major_events(event_feed(self.now[0]),self.now[0]))
        self.assertTrue(self.call('CLUSDT'));self.assertFalse(self.call('CLUSDT'))
        row=next(r for r in self.ai.monitoring_status()['rows'] if r['symbol']=='CLUSDT')
        self.assertEqual(row['alert']['status'],'error')
        self.assertEqual(self.ai.status()['usage']['reported_calls'],0)
        self.assertEqual(self.ai.status()['usage']['analysis_calls'],1)

    def test_held_symbol_priority_while_paused_and_pending_blocks_other_events(self):
        self.state.update(symbol='XAUUSDT',position={'symbol':'CLUSDT'},enabled=False)
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s=self.state)
        analysis=Analysis(engine,ai=self.ai)
        for s in SYMBOLS:self.submit(s)
        self.assertTrue(analysis.ai_cycle());self.assertEqual(self.model.calls[0]['symbol'],'CLUSDT')
        self.now[0]+=2;self.state['pending']={'kind':'exit','symbol':'CLUSDT'}
        self.submit('BZUSDT','Fresh event',major_events(event_feed(self.now[0]),self.now[0]))
        self.assertFalse(self.call('BZUSDT'))
        self.submit('CLUSDT','Own fresh update');self.assertTrue(self.call('CLUSDT'))
        self.state.update(pending=None,position=None,selection_mode='manual')
        self.assertFalse(self.call('BZUSDT'));self.assertTrue(self.call('XAUUSDT'))

    def test_entry_during_inflight_finishes_without_followup_calls(self):
        self.submit('BZUSDT');self.model.block=True
        worker=threading.Thread(target=self.ai.run_once,args=('BZUSDT',));worker.start()
        try:
            self.assertTrue(self.model.entered.wait(1))
            self.state['position']={'symbol':'XAUUSDT'};self.now[0]+=2
            self.submit('BZUSDT','Later normal update')
            self.model.release.set();worker.join(2);self.assertFalse(worker.is_alive())
            self.assertFalse(self.call('BZUSDT'));self.assertEqual(len(self.model.calls),1)
            self.assertEqual(self.ai.status()['active_count'],0)
        finally:self.model.release.set();worker.join(2)

    def test_invalid_scope_fails_closed_and_expired_event_not_woken(self):
        self.state['position']={'symbol':'XAUUSDT'}
        events=major_events(event_feed(self.now[0]),self.now[0]);self.now[0]+=3600
        self.submit('BZUSDT','Fresh text, old event',events);self.assertFalse(self.call('BZUSDT'))
        self.submit('XAUUSDT')
        with patch.object(self.ai,'scope_provider',side_effect=ValueError('fixture')):
            self.assertFalse(self.call('XAUUSDT'))

    def test_old_alert_not_revived_by_later_analysis_or_missing_event_evidence(self):
        self.state['position']={'symbol':'XAUUSDT'}
        events=major_events(event_feed(self.now[0]),self.now[0])
        self.submit('BZUSDT','Event',events);self.assertTrue(self.call('BZUSDT'))
        self.submit('BZUSDT','Event',[])  # Source/event evidence disappeared; input text alone is insufficient.
        row=lambda:next(r for r in self.ai.monitoring_status()['rows'] if r['symbol']=='BZUSDT')
        self.assertEqual(row()['alert']['status'],'historical')
        self.state['position']=None;self.submit('BZUSDT','New ordinary input',events)
        self.assertTrue(self.call('BZUSDT'))
        self.assertEqual(row()['alert']['status'],'historical')

    def test_detector_requires_fresh_body_and_handles_verified_facts(self):
        good=event_feed()
        for mutate in (
            lambda d:d['coverage'].update(healthy=False),
            lambda d:d['decision']['risks'][0].update(entry_gate='background',blocking=False),
            lambda d:d['decision']['risks'][0].update(acute_event_at=NOW-3601),
            lambda d:d['decision']['risks'][0].update(acute_event_at=NOW+1),
            lambda d:d['_ai_articles'][0].update(body=''),
            lambda d:d['_ai_articles'][0].update(published=NOW-3601),
            lambda d:d['decision'].update(asof=NOW-76),
        ):
            data=copy.deepcopy(good);mutate(data);self.assertEqual(major_events(data,NOW),[])
        data=copy.deepcopy(good)
        data['decision'].update(risks=[],entry_allowed=True,event_id='b'*24,evidence=[dict(
            verified=True,event_at=NOW-10,url=ARTICLE['url'],title='Official fact')])
        self.assertEqual(len(major_events(data,NOW)),1)
        duplicate=copy.deepcopy(data);duplicate['_ai_articles'][0]['body']=' Exports   were halted today. '
        self.assertEqual(major_events(data,NOW)[0]['token'],major_events(duplicate,NOW)[0]['token'])

    def test_analysis_update_passes_event_metadata_without_portfolio_to_model(self):
        self.state['position']={'symbol':'XAUUSDT','secret_fixture':'private'}
        engine=SimpleNamespace(api=SimpleNamespace(demo=False),s=self.state)
        analysis=Analysis(engine,ai=self.ai)
        data=event_feed();data['items']=[]
        with patch('binance_trader.analysis.time.time',return_value=NOW):
            analysis.update('news','BZUSDT',data)
        self.assertTrue(self.call('BZUSDT'))
        self.assertNotIn('secret_fixture',json.dumps(self.model.calls))


if __name__=='__main__':unittest.main()
