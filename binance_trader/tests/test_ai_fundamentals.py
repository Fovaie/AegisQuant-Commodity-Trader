"""Evidence binding and billing deduplication for numeric public data."""
import copy
import json
import unittest
from unittest.mock import patch
from binance_trader.ai_news import ModelClient, AINewsError
from binance_trader.ai_service import AIService
from binance_trader.news_risk import DRIVERS
from test_ai_news import CONFIG, Capture, ARTICLE, MODEL
from test_fundamentals import NOW, metric

def interpretation():
    return dict(summary='实际收益率上升，机会成本因素偏不利，不能单凭此因素预测价格。',direction='bearish',events=[],uncertainties=['未包含盘中利率。'],
        drivers=[dict(key=k,status='analyzed' if k=='usd_real_yield' else 'no_evidence',direction='bearish' if k=='usd_real_yield' else 'uncertain',
            summary='实际收益率较前值升高，需要与新闻及其他因素合看。' if k=='usd_real_yield' else '本批输入未包含该因素。',
            event_indices=[],metric_ids=['real_yield_10y'] if k=='usd_real_yield' else []) for k,_ in DRIVERS['XAUUSDT']])

class AIFundamentalTests(unittest.TestCase):
    def test_bad_news_citation_preserves_independent_statistics_and_does_not_retry(self):
        result=interpretation();result['events']=copy.deepcopy(MODEL['events'])
        result['events'][0]['quote']='An invented assertion not found in the body.'
        result['drivers'][0].update(status='analyzed',direction='bullish',event_indices=[0],summary='不应展示的无依据结论。')
        capture=Capture(result);now=[NOW]
        service=AIService(lambda cfg:ModelClient(cfg,capture),clock=lambda:now[0]);service.configure(**CONFIG)
        service.submit('XAUUSDT',[ARTICLE],NOW,fundamentals=[metric()]);self.assertTrue(service.run_once('XAUUSDT'))
        out=service.snapshot('XAUUSDT');self.assertEqual(out['status'],'ok')
        self.assertEqual(out['direction'],'uncertain');self.assertFalse(out['trading_authority'])
        self.assertEqual(out['drivers'][0]['status'],'unverified')
        self.assertEqual(out['drivers'][1]['summary'],result['drivers'][1]['summary'])
        self.assertEqual(out['drivers'][1]['metrics'][0]['value'],2.92)
        self.assertNotIn('不应展示',json.dumps(out,ensure_ascii=False))
        now[0]+=31;service.submit('XAUUSDT',[ARTICLE],now[0],fundamentals=[metric()])
        self.assertFalse(service.run_once('XAUUSDT'));self.assertEqual(len(capture.calls),1)
        result['drivers'][1]['event_indices']=[0]
        out=ModelClient(CONFIG,Capture(result)).analyze('XAUUSDT',[ARTICLE],NOW,fundamentals=[metric()])
        self.assertEqual(out['drivers'][1]['status'],'unverified','Any dependence on a bad citation invalidates the factor narrative')
        self.assertEqual(out['drivers'][1]['metrics'][0]['value'],2.92)

    def test_metric_only_input_is_analyzed_and_references_host_owned_values(self):
        capture=Capture(interpretation());out=ModelClient(CONFIG,capture).analyze('XAUUSDT',[],NOW,fundamentals=[metric()])
        row=next(r for r in out['drivers'] if r['key']=='usd_real_yield')
        self.assertEqual(row['metrics'][0]['value'],2.92)
        self.assertEqual(row['metrics'][0]['change'],4)
        self.assertEqual(row['sources'][0]['metric_id'],'real_yield_10y')
        self.assertEqual(row['sources'][0]['url'],metric()['url'])
        self.assertFalse(out['trading_authority'])
        sent=json.loads(json.loads(capture.calls[0]['body'])['messages'][1]['content'])
        self.assertEqual(sent['articles'],[]);self.assertEqual(sent['fundamentals'][0]['value'],2.92)
        self.assertNotIn(CONFIG['api_key'],json.dumps(sent))

    def test_fabricated_cross_factor_or_ignored_metrics_cannot_claim_success(self):
        for refs in ([],['invented'],['real_yield_10y','real_yield_10y'],[True]):
            result=interpretation();result['drivers'][1]['metric_ids']=refs
            with self.assertRaises(AINewsError):ModelClient(CONFIG,Capture(result)).analyze('XAUUSDT',[],NOW,fundamentals=[metric()])
        result=interpretation();result['drivers'][0].update(status='analyzed',metric_ids=['real_yield_10y'])
        with self.assertRaises(AINewsError):ModelClient(CONFIG,Capture(result)).analyze('XAUUSDT',[],NOW,fundamentals=[metric()])
        result=interpretation();result['direction']='uncertain';result['drivers'][1].update(status='no_evidence',metric_ids=[],direction='uncertain')
        with self.assertRaises(AINewsError):ModelClient(CONFIG,Capture(result)).analyze('XAUUSDT',[],NOW,fundamentals=[metric()])

    def test_unchanged_statistic_does_not_call_again_but_revision_does_once(self):
        now=[NOW];capture=Capture(interpretation());service=AIService(lambda cfg:ModelClient(cfg,capture),clock=lambda:now[0]);service.configure(**CONFIG)
        row=metric()
        def submit(rows=None,articles=None):service.submit('XAUUSDT',articles or [],now[0],fundamentals=[row] if rows is None else rows)
        submit();self.assertTrue(service.run_once('XAUUSDT'));self.assertEqual(service.snapshot('XAUUSDT')['status'],'ok')
        for _ in range(4):
            now[0]+=30;row.update(fetched_at=now[0],last_checked=now[0]);submit();self.assertFalse(service.run_once('XAUUSDT'))
        row=metric(current=2.95);submit();self.assertEqual(service.snapshot('XAUUSDT')['status'],'pending')
        self.assertTrue(service.run_once('XAUUSDT'))
        now[0]+=30;submit();self.assertFalse(service.run_once('XAUUSDT'));self.assertEqual(len(capture.calls),2)
        now[0]+=901;submit();self.assertEqual(service.snapshot('XAUUSDT')['status'],'ok');self.assertFalse(service.run_once('XAUUSDT'))

    def test_source_loss_never_keeps_numeric_result_current_or_rebills_recovery(self):
        now=[NOW];capture=Capture(interpretation());service=AIService(lambda cfg:ModelClient(cfg,capture),clock=lambda:now[0]);service.configure(**CONFIG)
        service.submit('XAUUSDT',[ARTICLE],now[0],fundamentals=[metric()]);service.run_once('XAUUSDT')
        self.assertEqual(service.snapshot('XAUUSDT',current_metrics=[])['status'],'historical')
        now[0]+=30;service.submit('XAUUSDT',[ARTICLE],now[0],fundamentals=[])
        self.assertEqual(service.snapshot('XAUUSDT')['status'],'historical');self.assertFalse(service.run_once('XAUUSDT'))
        now[0]+=30;service.submit('XAUUSDT',[ARTICLE],now[0],fundamentals=[metric()])
        self.assertFalse(service.run_once('XAUUSDT'));self.assertEqual(len(capture.calls),1)
        self.assertEqual(service.snapshot('XAUUSDT')['status'],'ok')

    def test_analysis_connects_all_numeric_factors_without_changing_engine_decision(self):
        from binance_trader.analysis import Analysis
        from binance_trader.fundamentals import Fundamentals
        from test_ai_integration import AIIntegrationTests
        from test_fundamentals import fixture_source
        case=AIIntegrationTests();case.setUp()
        clock_patch=patch('binance_trader.analysis.time.time',return_value=NOW);clock_patch.start()
        try:
            stats=Fundamentals(fetcher=fixture_source().__getitem__,clock=lambda:NOW);stats.refresh()
            case.analysis.fundamentals=stats
            result=interpretation();result['direction']='uncertain'
            for row in result['drivers']:
                row['metric_ids']=[m['id'] for m in stats.snapshot('XAUUSDT')['metrics'] if m['driver']==row['key']]
                row['status']='analyzed' if row['metric_ids'] else 'no_evidence'
                row['direction']='uncertain'
            capture=Capture(result);ai=AIService(lambda cfg:ModelClient(cfg,capture),clock=lambda:NOW);ai.configure(**CONFIG);case.analysis.ai=ai
            case.analysis.update('news','XAUUSDT',case.feed(articles=[]))
            before=copy.deepcopy(case.analysis.decision('XAUUSDT'));state=copy.deepcopy(case.engine.s)
            self.assertTrue(ai.run_once('XAUUSDT'))
            snap=case.analysis.snapshot('XAUUSDT')
            self.assertEqual(snap['ai']['status'],'ok');self.assertEqual(len(snap['fundamentals']['metrics']),9)
            self.assertEqual({k:v for k,v in snap['news']['decision'].items() if k!='ai'}, {k:v for k,v in before.items() if k!='ai'});self.assertEqual(case.engine.s,state)
            self.assertEqual(case.api.calls,[])
            self.assertEqual(len(json.loads(json.loads(capture.calls[0]['body'])['messages'][1]['content'])['fundamentals']),9)
        finally:clock_patch.stop();case.tearDown()

if __name__=='__main__':unittest.main()
