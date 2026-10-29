"""Regression cases from the independent news-risk audit; synthetic input only."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from binance_trader.news import decide, event_time, inspect_item, safe_url
from binance_trader.news_risk import RiskLedger, risk_observations


class NewsAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time() - 2

    def article(self, body, source='BBC', age=20, author=''):
        root = ('https://www.bbc.com/news/' if source == 'BBC'
                else 'https://www.theguardian.com/world/')
        return dict(title='Markets update', source=source,
                    url=root + str(age), published=self.now-age,
                    article_published=self.now-age, body=body,
                    fulltext=True, author=author)

    def observations(self, body, symbol='CLUSDT', **kwargs):
        return risk_observations(self.article(body, **kwargs), symbol,
                                 self.now, event_time)

    def seed_shipping_risk(self, path=None):
        ledger = RiskLedger(path, url_validator=safe_url)
        rows = ledger.update('CLUSDT', self.observations(
            'The strait of Hormuz remains closed today.', age=1000), self.now)
        self.assertFalse(ledger.error)
        self.assertNotEqual(rows[0]['status'],'resolved')
        return ledger

    def test_denied_fed_reports_cannot_form_two_source_buy_signal(self):
        pairs = (
            ('Reports that the Fed cut interest rates today are false.',
             'The report that the Federal Reserve lowered interest rates today was incorrect.'),
            ('The allegation that the Fed cut interest rates today is unverified.',
             'The unverified allegation that the Federal Reserve lowered interest rates today is incorrect.'),
        )
        for first, second in pairs:
            with self.subTest(first=first):
                items = [inspect_item(self.article(first), 'XAUUSDT', self.now),
                         inspect_item(self.article(second, 'Guardian'), 'XAUUSDT', self.now)]
                self.assertFalse(any(item['events'] for item in items))
                result = decide('XAUUSDT', items, {'healthy': True}, self.now)
                self.assertIs(result['entry_allowed'], False)
                self.assertIs(result['kline_allowed'], True)
                self.assertIsNone(result['direction'])
                self.assertEqual(result['evidence'], [])

    def test_multi_subject_clauses_do_not_release_an_unrelated_hazard(self):
        cases = (
            ('CLUSDT', 'The strait of Hormuz remains closed, although shipping through the Suez canal has resumed today.'),
            ('XAUUSDT', 'Iran tensions persist while hostilities in Ukraine have ended today.'),
            ('XAUUSDT', 'Reports that the war in Iran has ended today are false.'),
            ('XAUUSDT', 'Iran said the ceasefire has taken effect today in Ukraine.'),
            ('NATGASUSDT', 'A winter storm in Texas is ongoing, but extreme heat has ended today in Louisiana.'),
        )
        for symbol, body in cases:
            with self.subTest(body=body):
                facts = self.observations(body, symbol)
                self.assertFalse(any(f['action'] == 'resolved' and f['reliable'] for f in facts))

    def test_doubted_anonymous_or_false_release_never_clears_existing_risk(self):
        cases = (
            'Analysts doubt that the strait of Hormuz has reopened today.',
            'The strait of Hormuz has reopened today, according to an anonymous source.',
            'The strait of Hormuz has reopened today is a lie.',
        )
        for body in cases:
            with self.subTest(body=body):
                ledger = self.seed_shipping_risk()
                facts = self.observations(body)
                self.assertFalse(any(f['action'] == 'resolved' and f['reliable'] for f in facts))
                rows = ledger.update('CLUSDT', facts, self.now)
                self.assertTrue(all(row['status'] != 'resolved' for row in rows))

    def test_today_reporting_does_not_refresh_an_explicitly_past_event(self):
        cases = (
            ('XAUUSDT', 'Today traders discussed how the Fed cut interest rates yesterday.'),
            ('XAUUSDT', 'Today officials confirmed that the war between Iran and Israel has ended yesterday.'),
            ('CLUSDT', 'Today analysts discussed the fact that the strait of Hormuz has reopened on 2020-01-01.'),
        )
        for symbol, body in cases:
            with self.subTest(body=body):
                item = self.article(body)
                self.assertIsNone(event_time(body, item, self.now))
                self.assertFalse(inspect_item(item, symbol, self.now)['events'])
                self.assertFalse(any(f['action'] == 'resolved' and f['reliable']
                                     for f in risk_observations(item, symbol, self.now, event_time)))

    def test_extra_sentence_from_original_publisher_cannot_validate_a_copy(self):
        ledger = self.seed_shipping_risk()
        facts = self.observations(
            'The strait of Hormuz has reopened today. '
            'Shipping through the strait of Hormuz has resumed today.')
        facts += self.observations('The strait of Hormuz has reopened today.',
                                   source='Guardian', age=10)
        self.assertEqual(len({fact['origin'] for fact in facts}), 2)
        self.assertGreaterEqual(len({fact['fingerprint'] for fact in facts}), 2)
        rows = ledger.update('CLUSDT', facts, self.now)
        self.assertFalse(any(row['status'] == 'resolved' for row in rows))

    def test_agency_aliases_remain_one_origin_across_publishers(self):
        for first, second in (('AFP', 'Agence France-Presse'), ('PA Media', 'Press Association')):
            with self.subTest(agency=first):
                ledger = self.seed_shipping_risk()
                facts = self.observations('The strait of Hormuz has reopened today.', author=first)
                facts += self.observations('Shipping through the strait of Hormuz has resumed today.',
                                           source='Guardian', age=10, author=second)
                self.assertEqual(len({fact['origin'] for fact in facts}), 1)
                self.assertTrue(all(row['status'] != 'resolved' for row in ledger.update('CLUSDT', facts, self.now)))
                items = [inspect_item(self.article('The Fed cut interest rates today.', author=first), 'XAUUSDT', self.now),
                         inspect_item(self.article('The Federal Reserve lowered interest rates today.', 'Guardian', author=second), 'XAUUSDT', self.now)]
                decision=decide('XAUUSDT', items, {'healthy': True}, self.now)
                self.assertFalse(decision['entry_allowed'])
                self.assertIsNone(decision['direction'])
                self.assertEqual(decision['evidence'],[])
                self.assertEqual(decision['status'],'insufficient')
                self.assertIs(decision['kline_allowed'],True)

    def test_resolved_status_without_valid_newer_proof_is_corrupt_and_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic-risks.json'
            self.seed_shipping_risk(path)
            original = json.loads(path.read_text())
            for resolved_at in (self.now+100_000_000, float('nan'), self.now-10):
                with self.subTest(resolved_at=resolved_at):
                    corrupted = copy.deepcopy(original)
                    row = next(iter(corrupted['symbols']['CLUSDT'].values()))
                    row.update(status='resolved', blocking=False, resolved_at=resolved_at)
                    path.write_text(json.dumps(corrupted))
                    restored = RiskLedger(path, url_validator=safe_url)
                    self.assertTrue(restored.error)
                    risks = restored.update('CLUSDT', [], self.now)
                    result = decide('CLUSDT', [], {'healthy': not bool(restored.error)}, self.now, risks=risks)
                    self.assertIs(result['entry_allowed'], False)
                    self.assertIs(result['kline_allowed'], False)

    def test_valid_independent_resolution_survives_restart_without_new_headlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic-risks.json'
            ledger = self.seed_shipping_risk(path)
            facts = self.observations('The strait of Hormuz has reopened today.')
            facts += self.observations('Shipping through the strait of Hormuz has resumed today.',
                                       source='Guardian', age=10)
            rows = ledger.update('CLUSDT', facts, self.now)
            self.assertFalse(ledger.error)
            self.assertTrue(all(row['status'] == 'resolved' for row in rows))
            restored = RiskLedger(path, url_validator=safe_url)
            self.assertFalse(restored.error)
            self.assertTrue(all(not row['blocking'] for row in restored.update('CLUSDT', [], self.now)))


if __name__ == '__main__':
    unittest.main()
