import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.request import Request
from unittest.mock import patch

from binance_trader.news import (ARTICLE_AGE, EVENT_AGE, FEEDS, TTL, NewsService, SafeRedirect, article, decide, download,
                                 inspect_item, parse_feed, safe_url, unavailable, relevant)
from binance_trader.news_risk import select_factor_articles

NOW = 1791061200.0
FILLER = ' The release provides operational details and identifies the affected facility. Further background describes the regional energy market. The source documents the facts and dates in the original statement.'


def item(sentence, source='BBC', symbol='CLUSDT', age=60, title='Crude oil production halted today', author='', published=None):
    domain = {'BBC':'www.bbc.com', 'Guardian':'www.theguardian.com', 'Fed':'www.federalreserve.gov'}[source]
    path = '/news/test'
    if source == 'Fed':
        path = '/newsevents/pressreleases/monetary' + datetime.fromtimestamp(NOW-age, timezone.utc).strftime('%Y%m%d') + 'a.htm'
    value = dict(title=title, url='https://' + domain + path, published=NOW-age,
                 article_published=NOW-age if published is None else published, source=source,
                 fulltext=True, body=sentence+FILLER, author=author)
    return inspect_item(value, symbol, NOW)


def result(items, symbol='CLUSDT', healthy=True):
    return decide(symbol, items, {'healthy':healthy}, NOW)


class NewsTests(unittest.TestCase):
    def test_macro_and_physical_demand_news_are_not_excluded_before_model_reading(self):
        for title in ('Greenback weakens as TIPS yields fall','DXY and breakeven inflation update',
                      'PBoC adds bullion to its reserves','Central banks increase gold purchases'):
            self.assertTrue(relevant(title,'XAUUSDT'),title)
        recent=[dict(id=str(i),title='Iran war update',published=NOW-i) for i in range(20)]
        rare=[dict(id='dollar',title='Real yields and greenback fall',published=NOW-21),
              dict(id='fed',title='Federal Reserve policy outlook',published=NOW-22),
              dict(id='demand',title='PBoC bullion purchases',published=NOW-23)]
        chosen=select_factor_articles(recent+rare,'XAUUSDT',8)
        self.assertEqual(len(chosen),8)
        self.assertTrue({'dollar','fed','demand'}<={r['id'] for r in chosen})
        self.assertEqual(chosen,select_factor_articles(list(reversed(recent+rare)),'XAUUSDT',8))

    def test_public_request_supports_xml_content_negotiation(self):
        with patch('binance_trader.news.build_opener') as factory:
            opener=factory.return_value
            response=opener.open.return_value.__enter__.return_value
            response.url=FEEDS['EIA'];response.read.side_effect=[b'<rss/>',b'']
            self.assertEqual(download(FEEDS['EIA']),b'<rss/>')
            request=opener.open.call_args.args[0]
            self.assertIn('text/xml',request.get_header('Accept'))
            self.assertIn('*/*',request.get_header('Accept'))
            self.assertTrue(request.get_header('User-agent').startswith('CommodityAnalysis/'))

    def test_two_independent_current_supply_reports_confirm(self):
        a = item('Libyan crude oil production was halted today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.')
        b = item('Libya oil production stopped today at El Sharara oilfield after pipeline damage.', source='Guardian')
        d = result([a,b])
        self.assertEqual(d['status'], 'bullish')
        self.assertEqual(d['direction'], 'BUY')
        self.assertTrue(d['event_id'])
        self.assertTrue(d['entry_allowed'])
        self.assertFalse(d['kline_allowed'])

    def test_single_source_and_republication_do_not_confirm(self):
        a = item('Libyan crude oil production was halted today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.')
        b = item('Libyan crude oil production was halted today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.', source='Guardian')
        self.assertEqual(result([a])['status'], 'insufficient')
        self.assertTrue(result([a])['kline_allowed'])
        self.assertEqual(result([a,b])['status'], 'insufficient')
        self.assertEqual(result([a,b])['evidence'], [])
        self.assertIsNone(result([a,b])['event_id'])
        self.assertIsNone(result([a,b])['direction'])
        a = item('Libyan crude oil production was halted today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.', author='Reuters')
        b = item('Libya oil production stopped today at El Sharara oilfield after pipeline damage.', source='Guardian', author='Reuters')
        self.assertEqual(result([a,b])['status'], 'insufficient')

    def test_different_incidents_are_not_corroboration(self):
        a = item('Libyan crude oil production was halted today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.')
        b = item('Iraq oil production stopped today at El Sharara oilfield after pipeline damage.', source='Guardian')
        self.assertEqual(result([a,b])['status'], 'insufficient')
        b = item('Libya oil production stopped today at El Feel oilfield after pipeline damage.', source='Guardian')
        self.assertEqual(result([a,b])['status'], 'insufficient')

    def test_predicate_must_belong_to_commodity_and_named_facility(self):
        for sentence in ('Libya oil supply increased today as attacks stopped at El Sharara oilfield.',
                         'Libya crude oil production expanded today but exports halted at El Sharara oilfield.',
                         'Libyan crude oil production halted today after a pipeline outage.'):
            self.assertFalse(item(sentence)['events'])

    def test_negation_conditional_and_rumor_never_authorize_news_direction(self):
        for sentence in ('Libyan crude oil production was not halted today.',
                         'Libyan crude oil production could be halted today.',
                         'Rumors claim Libyan crude oil production was halted today.',
                         'If Libyan crude oil production halted today, supplies would decrease.'):
            with self.subTest(sentence=sentence):
                a = item(sentence); b = item(sentence, source='Guardian')
                self.assertEqual(result([a,b])['status'], 'insufficient')
                self.assertIsNone(result([a,b])['direction'])
                self.assertFalse(result([a,b])['entry_allowed'])
                self.assertEqual(result([a,b])['evidence'],[])
                self.assertTrue(result([a,b])['kline_allowed'])

    def test_publication_does_not_establish_event_time(self):
        for sentence in ('Libyan crude oil production was halted.',
                         'Libyan crude oil production was halted last week.',
                         'Libyan crude oil production was halted in 2024.'):
            self.assertIsNone(result([item(sentence)])['direction'])

    def test_original_publication_and_evidence_age(self):
        a = item('Libyan crude oil production halted today.', age=EVENT_AGE+1)
        self.assertEqual(result([a])['status'], 'insufficient')
        a = item('Libyan crude oil production halted today.', published=NOW-EVENT_AGE-1)
        self.assertEqual(result([a])['status'], 'insufficient')

    def test_headline_alone_is_display_only_and_blocks_material_risk(self):
        a = inspect_item(dict(title='Crude oil production halted today',url='https://www.bbc.com/news/a',
                              source='BBC',published=NOW-10,fulltext=False), 'CLUSDT', NOW)
        self.assertEqual(a['evidence_level'], '仅标题/摘要')
        self.assertEqual(result([a])['status'], 'blocked')
        self.assertFalse(result([a])['kline_allowed'])

    def test_conflicting_verified_facts_block(self):
        a = item('Libyan crude oil production was halted today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.')
        b = item('Libya oil production resumed today at El Sharara oilfield after pipeline repairs.', source='Guardian')
        self.assertEqual(result([a,b])['status'], 'blocked')
        self.assertFalse(result([a,b])['kline_allowed'])

    def test_primary_fed_release_can_confirm_without_media(self):
        a = item('The Committee decided to lower the target range for the federal funds rate by 1/4 percentage point.',
                 source='Fed', symbol='XAUUSDT', title='Federal Reserve issues FOMC statement')
        d = result([a], 'XAUUSDT')
        self.assertEqual(d['status'], 'bullish')
        self.assertEqual(d['confidence'], '官方原始公告')
        a = item('The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point.',
                 source='Fed', symbol='XAGUSDT', title='Federal Reserve issues FOMC statement')
        self.assertEqual(result([a], 'XAGUSDT')['status'], 'bearish')

    def test_cross_commodity_information_does_not_authorize(self):
        a = item('Libyan crude oil production was halted today.', symbol='NATGASUSDT')
        self.assertEqual(result([a], 'NATGASUSDT')['status'], 'insufficient')
        a = item('US LNG exports halted today at Sabine Pass terminal after an outage.', symbol='NATGASUSDT', title='US LNG exports halted')
        self.assertEqual(a['events'][0]['direction'], 'SELL')
        a = item('European natural gas production halted today.', symbol='NATGASUSDT', title='European natural gas production halted')
        self.assertFalse(a['events'])

    def test_inventory_requires_actual_and_consensus_matching_units(self):
        sentence = 'US crude oil inventories fell by 5 million barrels today, compared with an expected fall of 2 million barrels.'
        a = item(sentence, title='Crude oil inventories fall')
        self.assertEqual(a['events'][0]['direction'], 'BUY')
        b = item(sentence.replace('5 million', '1 million'), title='Crude oil inventories fall')
        self.assertEqual(b['events'][0]['direction'], 'SELL')
        for sentence in ('US crude oil inventories fell by 5 million barrels today.',
                         'US crude oil inventories fell by 5 million barrels today, compared with an expected fall of 2 billion cubic feet.',
                         'Analysts forecast US crude oil inventories fell by 5 million barrels today, compared with an expected fall of 2 million barrels.'):
            self.assertFalse(item(sentence, title='Crude oil inventories')['events'])

    def test_unavailable_coverage_never_neutral(self):
        self.assertEqual(result([], healthy=False)['status'], 'unavailable')
        self.assertEqual(result([])['status'], 'insufficient')
        self.assertFalse(result([])['entry_allowed'])
        self.assertTrue(result([])['kline_allowed'])
        self.assertFalse(result([], healthy=False)['kline_allowed'])
        self.assertFalse(unavailable('CLUSDT',NOW)['kline_allowed'])

    def test_html_only_extracts_article_and_original_date(self):
        data = b'<meta property="article:published_time" content="2026-10-03T12:00:00Z"><nav><p>Libyan crude oil production halted today.</p></nav><article><p>Actual body is neutral.</p><script>BUY ALL NOW</script></article>'
        parsed = article(data, 'BBC')
        self.assertEqual(parsed['body'], 'Actual body is neutral.')
        self.assertIsNotNone(parsed['article_published'])
        self.assertNotIn('BUY', parsed['body'])
        body = 'Confirmed report. ' * 30
        jsonld = {'@type':'NewsArticle','articleBody':body,'datePublished':'2026-10-03T12:00:00Z','dateModified':'2026-10-04T12:00:00Z'}
        parsed = article(('<script type="application/ld+json">'+json.dumps(jsonld)+'</script>').encode(), 'BBC')
        self.assertTrue(parsed['fulltext'])
        self.assertEqual(parsed['article_published'], datetime(2026,10,3,12,tzinfo=timezone.utc).timestamp())

    def test_url_and_xml_boundaries(self):
        for url in ('http://www.bbc.com/a','https://www.bbc.com.evil.test/a','https://www.bbc.com@127.0.0.1/a',
                    'https://127.0.0.1/a','https://www.bbc.com:1234/a','javascript:alert(1)'):
            self.assertIsNone(safe_url(url))
        with self.assertRaises(ValueError): parse_feed(b'<!DOCTYPE rss><rss><channel/></rss>','BBC',NOW)
        def node(url, date):return '<item><title>Gold prices</title><link>'+url+'</link><pubDate>'+formatdate(date,usegmt=True)+'</pubDate></item>'
        good=node('https://www.bbc.com/news/one', NOW-60)
        xml=('<rss><channel>'+good+good+node('https://www.theguardian.com/a',NOW-60)+node('https://www.bbc.com/news/old',NOW-ARTICLE_AGE-1)+'</channel></rss>').encode()
        self.assertEqual(len(parse_feed(xml,'BBC',NOW)),1)
        with self.assertRaises(ValueError):
            SafeRedirect().redirect_request(Request('https://www.federalreserve.gov/a'),None,302,'redirect',{},'https://www.bbc.com/news/test')

    def test_news_id_uses_publisher_guid_and_falls_back_to_link(self):
        def identity(url, guid='', source='BBC'):
            xml=('<rss><channel><item><title>Gold prices</title><link>'+url+'</link><guid>'+guid+
                 '</guid><pubDate>'+formatdate(NOW-60,usegmt=True)+'</pubDate></item></channel></rss>').encode()
            return parse_feed(xml,source,NOW)[0]['news_id']
        a='https://www.bbc.com/news/one'
        b='https://www.bbc.com/news/one?at_medium=rss'
        self.assertEqual(identity(a,'stable-guid'),identity(b,'stable-guid'))
        self.assertNotEqual(identity(a,'stable-guid'),identity(a,'new-guid'))
        self.assertNotEqual(identity(a,'same-guid'),identity('https://www.theguardian.com/story','same-guid','Guardian'))
        self.assertEqual(identity(a),identity(a))
        self.assertNotEqual(identity(a),identity('https://www.bbc.com/news/two'))

    def test_pipeline_feed_outage_and_bounded_display(self):
        def unavailable_fetch(url): raise OSError('offline')
        d = NewsService(unavailable_fetch).fetch('CLUSDT', NOW)
        self.assertEqual(d['decision']['status'], 'unavailable')
        self.assertFalse(d['decision']['kline_allowed'])
        self.assertEqual(len(d['coverage']['failed']), 6)
        empty = b'<rss><channel></channel></rss>'
        d = NewsService(lambda url: empty).fetch('CLUSDT', NOW)
        self.assertEqual(d['decision']['status'], 'insufficient')
        self.assertTrue(d['decision']['kline_allowed'])
        def partial(url):
            if url == FEEDS['EIA']: raise OSError('offline')
            return empty
        self.assertEqual(NewsService(partial).fetch('CLUSDT', NOW)['decision']['status'],'unavailable')

    def test_fresh_unread_article_does_not_become_healthy_neutral(self):
        def fetch(url):
            if url == FEEDS['BBC']:
                return ('<rss><channel><item><title>Gold price update</title><link>https://www.bbc.com/news/test</link><pubDate>'+formatdate(NOW-60,usegmt=True)+'</pubDate></item></channel></rss>').encode()
            if url in FEEDS.values(): return b'<rss><channel/></rss>'
            raise OSError('paywall or offline')
        d=NewsService(fetch).fetch('XAUUSDT',NOW)
        self.assertEqual(d['decision']['status'],'unavailable')
        self.assertFalse(d['decision']['kline_allowed'])
        self.assertTrue(d['coverage']['unread'])

    def test_pipeline_fulltext_and_no_body_leak(self):
        date = formatdate(NOW-60, usegmt=True)
        iso = datetime.fromtimestamp(NOW-60,timezone.utc).isoformat()
        def fetch(url):
            if url in FEEDS.values():
                if url == FEEDS['EIA']: return b'<rss><channel/></rss>'
                domain = 'www.bbc.com' if url in (FEEDS['BBC'],FEEDS['BBC World']) else 'www.theguardian.com'
                return ('<rss><channel><item><title>Libya crude oil production halted today</title><link>https://'+domain+'/news/test</link><pubDate>'+date+'</pubDate></item></channel></rss>').encode()
            sentence = 'Libyan crude oil production was halted today at El Sharara oilfield after pipeline damage.' if 'bbc' in url else 'Libya oil production stopped today at El Sharara oilfield at El Sharara oilfield after a pipeline outage.'
            return ('<meta property="article:published_time" content="'+iso+'"><article><p>'+sentence+FILLER+'</p></article>').encode()
        d = NewsService(fetch).fetch('CLUSDT', NOW)
        self.assertEqual(d['decision']['status'],'blocked')
        self.assertFalse(d['decision']['entry_allowed'])
        self.assertFalse(d['decision']['kline_allowed'])
        self.assertTrue(d['decision']['risks'])
        self.assertEqual(len(d['items']),2,'Business/world copies are one publisher and one article')
        self.assertTrue(all('body' not in row for row in d['items']))
        self.assertTrue(all(len(e['excerpt'].split()) <= 22 for e in d['decision']['evidence']))

    def test_insufficient_early_path_checks_existing_risks_and_drivers(self):
        risk={'blocking':True}
        d=decide('CLUSDT',[],{'healthy':True},NOW,risks=[risk])
        self.assertEqual(d['status'],'blocked')
        self.assertFalse(d['kline_allowed'])
        driver={'key':'usd_real_yield','label':'美元实际利率','status':'review_required','reason':'待核验'}
        d=decide('XAUUSDT',[],{'healthy':True},NOW,drivers=[driver])
        self.assertEqual(d['status'],'insufficient')
        self.assertTrue(d['kline_allowed'])
        self.assertEqual(d['drivers'][0]['status'],'review_required')

    def test_single_source_complete_fact_falls_back_without_direction(self):
        value=item('The Fed cut interest rates today.',symbol='XAUUSDT',title='Fed policy update')
        d=result([value],'XAUUSDT')
        self.assertEqual(d['status'],'insufficient')
        self.assertTrue(d['kline_allowed'])
        self.assertFalse(d['entry_allowed'])
        self.assertIsNone(d['direction'])
        self.assertIsNone(d['event_id'])
        self.assertEqual(d['evidence'],[])
        driver=next(row for row in d['drivers'] if row['key']=='fed_policy')
        self.assertEqual(driver['status'],'no_recent_evidence')
        self.assertIn('独立来源',driver['reason'])

    def test_single_source_with_another_unmodelled_driver_can_use_closed_kline(self):
        fed=item('The Fed cut interest rates today.',symbol='XAUUSDT',title='Fed policy update')
        dollar=item('US dollar and real yields rose today.',symbol='XAUUSDT',title='US dollar and real yields')
        d=result([fed,dollar],'XAUUSDT')
        self.assertEqual(d['status'],'insufficient')
        self.assertTrue(d['kline_allowed'])
        self.assertFalse(d['entry_allowed'])
        self.assertIsNone(d['direction'])
        # A second unsupported report also remains advisory, not directional.
        context=item('The Federal Reserve held a meeting today.',symbol='XAUUSDT',title='Fed discussions')
        d=result([fed,context],'XAUUSDT')
        self.assertEqual(d['status'],'insufficient')
        self.assertTrue(d['kline_allowed'])

    def test_no_direction_with_fresh_unmodelled_driver_permits_kline_fallback(self):
        dollar=item('US dollar and real yields rose today.',symbol='XAUUSDT',title='US dollar and real yields')
        d=result([dollar],'XAUUSDT')
        self.assertEqual(d['status'],'insufficient')
        self.assertTrue(d['kline_allowed'])

    def test_stale_invalid_or_future_source_check_never_grants_fallback(self):
        for checked in (NOW-TTL,NOW-TTL-1,NOW+1,None,True,float('nan')):
            with self.subTest(checked=checked):
                d=decide('CLUSDT',[],{'healthy':True,'checked':checked},NOW)
                self.assertEqual(d['status'],'unavailable')
                self.assertFalse(d['kline_allowed'])

    def test_pipeline_single_source_inventory_fallback_preserves_driver_reason(self):
        date=formatdate(NOW-60,usegmt=True)
        iso=datetime.fromtimestamp(NOW-60,timezone.utc).isoformat()
        body='US crude oil inventories fell by 5 million barrels today, compared with an expected fall of 2 million barrels.'
        def fetch(url):
            if url==FEEDS['BBC']:
                return ('<rss><channel><item><title>Crude oil inventories</title><link>https://www.bbc.com/news/test</link><pubDate>'+date+'</pubDate></item></channel></rss>').encode()
            if url in FEEDS.values():return b'<rss><channel/></rss>'
            return ('<meta property="article:published_time" content="'+iso+'"><article><p>'+body+FILLER+'</p></article>').encode()
        payload=NewsService(fetch).fetch('CLUSDT',NOW)
        d=payload['decision']
        self.assertTrue(d['kline_allowed'])
        self.assertEqual(d['status'],'insufficient')
        self.assertEqual(d['evidence'],[])
        self.assertTrue(payload['items'][0]['events'],'Article facts remain available for display')
        self.assertEqual(next(row['status'] for row in d['drivers'] if row['key']=='inventory'),'no_recent_evidence')

    def war_feed(self,body,published=NOW-60,title='War in Iran update'):
        date=formatdate(published,usegmt=True)
        iso=datetime.fromtimestamp(published,timezone.utc).isoformat()
        def fetch(url):
            if url==FEEDS['BBC']:
                return ('<rss><channel><item><title>'+title+'</title><link>https://www.bbc.com/news/war</link><pubDate>'+date+'</pubDate></item></channel></rss>').encode()
            if url in FEEDS.values():return b'<rss><channel/></rss>'
            return ('<meta property="article:published_time" content="'+iso+'"><article><p>'+body+FILLER+FILLER+'</p></article>').encode()
        return fetch

    def test_ongoing_war_is_background_through_the_whole_service(self):
        for symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT'):
            with self.subTest(symbol=symbol):
                payload=NewsService(self.war_feed('The war in Iran continues today.')).fetch(symbol,NOW)
                self.assertTrue(payload['coverage']['healthy'])
                self.assertTrue(payload['decision']['kline_allowed'])
                self.assertFalse(payload['items'][0]['uncertain'])
                self.assertTrue(payload['decision']['risks'])
                self.assertTrue(all(r['entry_gate']=='background' and r['status']!='resolved' for r in payload['decision']['risks']))
                self.assertEqual(next(d['status'] for d in payload['decision']['drivers'] if d['key']=='geopolitics'),'background')

    def test_full_reports_without_rule_direction_allow_kline_for_all_five_products(self):
        cases={
            'XAUUSDT':'The Federal Reserve could cut interest rates if inflation falls. US dollar and real yields rose today.',
            'XAGUSDT':'Silver industrial demand remains uncertain. Analysts discuss whether the Fed could cut interest rates.',
            'CLUSDT':'Crude oil inventories could fall next week. Analysts discuss oil supply forecasts.',
            'BZUSDT':'Crude oil supply forecasts remain uncertain. Oil production could rise next month.',
            'NATGASUSDT':'US natural gas storage could fall next week. Weather forecasts remain uncertain.',
        }
        for symbol,body in cases.items():
            with self.subTest(symbol=symbol):
                payload=NewsService(self.war_feed(body,title=body)).fetch(symbol,NOW)
                decision=payload['decision']
                self.assertTrue(payload['coverage']['healthy'])
                self.assertEqual(decision['status'],'insufficient')
                self.assertTrue(decision['kline_allowed'],decision)
                self.assertFalse(decision['entry_allowed'])
                self.assertIsNone(decision['direction'])
                self.assertEqual(decision['evidence'],[])
                self.assertTrue(any(d['status']=='review_required' for d in decision['drivers']))

    def test_acute_cooldown_expiry_does_not_get_reblocked_by_six_hour_news_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risk.json'
            fetch=self.war_feed('The war in Iran escalated today.')
            service=NewsService(fetch,state_path=path)
            initial=service.fetch('CLUSDT',NOW)
            self.assertEqual(initial['decision']['status'],'blocked')
            risk=initial['decision']['risks'][0]
            self.assertEqual(risk['entry_gate'],'cooldown')
            self.assertEqual(risk['time_basis'],'first_evidence')
            service=NewsService(fetch,state_path=path)
            self.assertFalse(service.risk_ledger.error)
            expired=service.fetch('CLUSDT',NOW+3601)
            self.assertLess(NOW+3601-expired['items'][0]['article_published'],EVENT_AGE)
            self.assertTrue(expired['decision']['kline_allowed'],expired['decision']['reason'])
            self.assertFalse(expired['items'][0]['uncertain'])
            self.assertEqual(expired['decision']['risks'][0]['entry_gate'],'background')
            self.assertNotEqual(expired['decision']['risks'][0]['status'],'resolved')
            self.assertEqual(expired['decision']['risks'][0]['cooldown_until'],risk['cooldown_until'])

    def test_background_war_keeps_unmodelled_inventory_as_reference(self):
        for separator in ('. ', ', while '):
            body='The war in Iran continues today'+separator+'US crude oil inventories fell by 5 million barrels today.'
            payload=NewsService(self.war_feed(body)).fetch('CLUSDT',NOW)
            self.assertEqual(payload['decision']['status'],'insufficient')
            self.assertTrue(payload['decision']['kline_allowed'])
            self.assertFalse(payload['decision']['entry_allowed'])
            self.assertTrue(payload['items'][0]['uncertain'])

    def test_background_does_not_override_unread_or_failed_source(self):
        fetch=self.war_feed('The war in Iran continues today.')
        def failed(url):
            if url==FEEDS['EIA']:raise OSError('offline')
            return fetch(url)
        payload=NewsService(failed).fetch('CLUSDT',NOW)
        self.assertEqual(payload['decision']['status'],'unavailable')
        self.assertFalse(payload['decision']['kline_allowed'])

    def test_missile_strikes_and_us_lng_all_drivers_release_after_cooldown(self):
        cases=(('CLUSDT','Iran launched new missile strikes against Israel today.','Iran missile strikes'),
               ('NATGASUSDT','US LNG exports were halted at the Freeport terminal today.','US LNG exports halted'))
        for symbol,body,title in cases:
            with self.subTest(symbol=symbol):
                service=NewsService(self.war_feed(body,title=title))
                first=service.fetch(symbol,NOW)
                self.assertEqual(first['decision']['status'],'blocked')
                self.assertTrue(any(r['entry_gate']=='cooldown' for r in first['decision']['risks']))
                later=service.fetch(symbol,NOW+3601)
                self.assertTrue(later['decision']['kline_allowed'],later['decision']['reason'])
                self.assertFalse(later['items'][0]['uncertain'])
                self.assertFalse(any(d['status']=='review_required' for d in later['decision']['drivers']))

    def test_dated_social_research_is_background_but_mixed_new_events_are_not(self):
        # Synthetic paraphrases of the dated-survey case found during public QA.
        background=('Survey respondents described their attachment to Israel before the Gaza war. '
                    'Researchers examined family impacts of the 7 October attacks. '
                    'The research period preceded the Iran war.')
        title='Survey on Israel since the 7 October attacks and Gaza war'
        service=NewsService(self.war_feed(background,title=title))
        payload=service.fetch('XAUUSDT',NOW)
        self.assertTrue(payload['decision']['kline_allowed'],payload['decision']['reason'])
        self.assertFalse(payload['items'][0]['uncertain'])
        for fresh in ('Iran launched new missile strikes against Israel today.',
                      'Since the 7 October attacks, Iran attacked Israel today.'):
            with self.subTest(fresh=fresh):
                mixed=NewsService(self.war_feed(background+' '+fresh,title=title)).fetch('XAUUSDT',NOW)
                self.assertEqual(mixed['decision']['status'],'blocked')
                self.assertFalse(mixed['decision']['kline_allowed'])
        # Without a location/participant relationship this remains unconfirmed
        # reference text, not a verified acute event or a directional trade.
        mixed=NewsService(self.war_feed(background+' Since the 7 October attacks, missile strikes occurred today.',title=title)).fetch('XAUUSDT',NOW)
        self.assertTrue(any(d['status']=='review_required' for d in mixed['decision']['drivers']))
        self.assertEqual(mixed['decision']['status'],'insufficient')
        self.assertTrue(mixed['decision']['kline_allowed'])
        self.assertFalse(mixed['decision']['entry_allowed'])
