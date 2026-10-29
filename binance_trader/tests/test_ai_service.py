"""Scheduling/cache contracts independent of provider output accuracy."""
import time
import copy
import unittest

from binance_trader.ai_service import AIService, INPUT_TTL, RESULT_TTL, REFRESH_SECONDS
from test_ai_integration import ModelHarness
from test_ai_news import Capture, CONFIG, MODEL, NOW, ARTICLE
from binance_trader.ai_news import ModelClient


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [time.time()]
        self.model = ModelHarness()
        self.ai = AIService(client_factory=self.model.factory, clock=lambda: self.now[0])
        self.ai.configure('https://api.openai.com/v1', 'fixture-model', 'fake-key-for-tests-only')
        self.articles = [dict(id='cache-article', url='https://www.bbc.com/news/fixture',
                             title='Fixture', source='BBC', origin='BBC',
                             published=self.now[0]-60, body='Public fixture text.')]

    def submit(self, healthy=True):
        self.ai.submit('XAUUSDT', self.articles, self.now[0], healthy=healthy)

    def test_long_quote_publishes_success_and_unchanged_news_does_not_request_again(self):
        self.now[0] = NOW
        words=' '.join('word'+str(i) for i in range(35))
        result=copy.deepcopy(MODEL)
        result['events'][0]['quote']=words
        capture=Capture(result)
        ai=AIService(client_factory=lambda config: ModelClient(config,capture),clock=lambda:self.now[0])
        ai.configure(**CONFIG)
        articles=[dict(ARTICLE,body=words)]
        ai.submit('XAUUSDT',articles,self.now[0])
        self.assertTrue(ai.run_once('XAUUSDT'))
        published=ai.snapshot('XAUUSDT')
        self.assertEqual(published['status'],'ok')
        self.assertEqual(published['summary'],MODEL['summary'])
        self.assertEqual(published['events'][0]['quote'],'')
        self.assertTrue(published['events'][0]['quote_omitted'])
        self.assertEqual([r['key'] for r in published['drivers']],[r['key'] for r in MODEL['drivers']])
        self.assertEqual(published['drivers'][0]['sources'][0]['url'],ARTICLE['url'])
        self.assertEqual(ai.status()['error'],'')
        for delay in (REFRESH_SECONDS,RESULT_TTL,86400):
            self.now[0] += delay
            ai.submit('XAUUSDT',articles,self.now[0])
            self.assertFalse(ai.run_once('XAUUSDT'))
        self.assertEqual(len(capture.calls),1)

    def test_unchanged_news_retains_direction_without_recalling_model(self):
        self.submit()
        self.assertTrue(self.ai.run_once('XAUUSDT'))
        first = self.ai.snapshot('XAUUSDT')
        self.now[0] += REFRESH_SECONDS+1
        self.submit()
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        cached = self.ai.snapshot('XAUUSDT')
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['analyzed_at'], first['analyzed_at'])
        self.now[0] = first['analyzed_at']+RESULT_TTL
        self.submit()
        expired = self.ai.snapshot('XAUUSDT')
        self.assertEqual(expired['status'], 'ok')
        self.assertEqual(expired['summary'], first['summary'])
        self.assertEqual(expired['direction'], first['direction'])
        self.assertEqual(expired['retained_input_checked_at'], self.now[0])
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.now[0] += 24*3600
        self.submit()
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.ai.snapshot('XAUUSDT')['analyzed_at'], first['analyzed_at'])

    def test_global_rate_limit_includes_connection_tests_and_changed_articles(self):
        self.submit(); self.ai.run_once('XAUUSDT')
        self.articles[0]['body'] = 'Changed public body.'
        self.submit(); self.ai.request_test()
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'pending')
        self.now[0] += REFRESH_SECONDS
        self.assertTrue(self.ai.run_once('XAUUSDT'))
        self.assertEqual(self.model.tests, 1)
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.now[0] += REFRESH_SECONDS
        self.submit()
        self.assertTrue(self.ai.run_once('XAUUSDT'))
        self.assertEqual(self.ai.status()['request_count'], 3)

    def test_news_outage_and_old_input_hide_old_result_and_do_not_call_model(self):
        self.submit(); self.ai.run_once('XAUUSDT')
        self.now[0] += INPUT_TTL+1
        stale = self.ai.snapshot('XAUUSDT')
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['events'], [])
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.submit(healthy=False)
        self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'stale')
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(len(self.model.calls), 1)

    def test_failed_version_does_not_auto_retry_but_explicit_reconfigure_can_retry(self):
        self.submit(); self.model.failure = RuntimeError('provider failed')
        self.ai.run_once('XAUUSDT')
        self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'error')
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.now[0] += REFRESH_SECONDS
        self.model.failure = None
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.now[0] += 3600
        self.submit()
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(len(self.model.calls), 1)
        self.ai.configure('https://api.openai.com/v1', 'fixture-model', 'fake-key-for-tests-only')
        self.assertTrue(self.ai.run_once('XAUUSDT'))
        result = self.ai.snapshot('XAUUSDT')
        self.assertEqual(result['status'], 'ok')
        result['summary'] = 'Caller mutation'
        result['uncertainties'].append('Caller mutation')
        self.assertNotEqual(self.ai.snapshot('XAUUSDT')['summary'], 'Caller mutation')
        self.assertNotIn('Caller mutation', self.ai.snapshot('XAUUSDT')['uncertainties'])

    def test_order_duplicates_timestamp_whitespace_removal_and_reappearance_do_not_rebill(self):
        original = self.articles[0].copy()
        other = dict(original, id='second-article', body='Second news story.')
        self.articles = [original, other, original.copy()]
        self.submit(); self.ai.run_once('XAUUSDT')
        self.assertEqual(len(self.model.calls[0]['articles']), 2)
        for rows in ([other, original], [original], [], [other, original],
                     [dict(original, published=self.now[0], body='Public  fixture\ntext.',
                           url=original['url']+'?utm_source=changed'), other]):
            self.now[0] += REFRESH_SECONDS
            self.articles = rows
            self.submit()
            self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'ok')

    def test_new_id_and_same_id_correction_each_call_once(self):
        self.submit(); self.ai.run_once('XAUUSDT')
        for changes in ({'id':'new-article'}, {'body':'Corrected: exports did not stop.'},
                        {'title':'Correction: no export halt'}):
            self.now[0] += REFRESH_SECONDS
            self.articles[0].update(changes)
            self.submit()
            self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'pending')
            self.assertTrue(self.ai.run_once('XAUUSDT'))
            self.now[0] += REFRESH_SECONDS
            self.submit()
            self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(len(self.model.calls), 4)

    def test_seen_versions_are_scoped_by_commodity_and_restored_after_source_recovery(self):
        self.submit(); self.ai.run_once('XAUUSDT')
        self.ai.input_error('XAUUSDT')
        self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'stale')
        self.now[0] += REFRESH_SECONDS
        self.submit()
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual(self.ai.snapshot('XAUUSDT')['status'], 'ok')
        self.ai.submit('CLUSDT', self.articles, self.now[0])
        self.assertTrue(self.ai.run_once('CLUSDT'))
        self.now[0] += REFRESH_SECONDS
        self.submit()
        self.assertFalse(self.ai.run_once('XAUUSDT'))
        self.assertEqual([c['symbol'] for c in self.model.calls], ['XAUUSDT','CLUSDT'])


if __name__ == '__main__':
    unittest.main()
