"""Deterministic public-news lifecycle regressions; no network or accounts."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from binance_trader.news import FEEDS, NewsService, decide, event_time, inspect_item, safe_url
from binance_trader.news_risk import (RiskLedger, US, canonical_origin, driver_summary, region,
                                    relevant_context, risk_observations, subject_for_geo)


class NewsRiskTests(unittest.TestCase):
    def setUp(self):
        self.now=(int(time.time())//86400-1)*86400+12*3600

    def raw(self,body,source='BBC',published=None,fulltext=True,title=None,author=''):
        published=self.now-60 if published is None else published
        domain={'BBC':'www.bbc.com','Guardian':'www.theguardian.com','Fed':'www.federalreserve.gov'}[source]
        return dict(title=title or body,source=source,url='https://'+domain+'/news/'+str(published),
                    published=published,article_published=published if fulltext else None,
                    body=body if fulltext else '',fulltext=fulltext,author=author)

    def facts(self,body,symbol='CLUSDT',now=None,**kwargs):
        return risk_observations(self.raw(body,**kwargs),symbol,self.now if now is None else now,event_time)

    def seed(self,ledger,symbol='CLUSDT'):
        facts=self.facts('The war between Iran and Israel continues today.',symbol)
        facts+=self.facts('Hostilities between Israel and Iran continue today.',symbol,source='Guardian')
        rows=ledger.update(symbol,facts,self.now)
        self.assertEqual(rows[0]['status'],'active')
        return rows

    def test_war_survives_one_hour_seven_hours_leaving_rss_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';ledger=RiskLedger(path,safe_url)
            self.seed(ledger)
            for age in (3600,7*3600):
                rows=ledger.update('CLUSDT',[],self.now+age)
                self.assertFalse(rows[0]['blocking'])
                self.assertEqual(rows[0]['entry_gate'],'background')
                self.assertEqual(rows[0]['status'],'active')
            restored=RiskLedger(path,safe_url)
            self.assertFalse(restored.error)
            self.assertEqual(restored.snapshot('CLUSDT'),rows)
            decision=decide('CLUSDT',[],{'healthy':True},self.now+7*3600,risks=rows)
            self.assertEqual(decision['status'],'insufficient')
            self.assertTrue(decision['kline_allowed'])
            driver=next(d for d in decision['drivers'] if d['key']=='geopolitics')
            self.assertEqual(driver['status'],'background')
            self.assertIn('背景',driver['reason'])

    def test_same_event_requires_newer_independent_body_resolution(self):
        ledger=RiskLedger();self.seed(ledger)
        first=self.facts('The war between Iran and Israel has ended today.',published=self.now+100,now=self.now+110)
        rows=ledger.update('CLUSDT',first,self.now+110)
        self.assertNotEqual(rows[0]['status'],'resolved')
        second=self.facts('Hostilities between Israel and Iran have ceased today.',source='Guardian',published=self.now+105,now=self.now+110)
        rows=ledger.update('CLUSDT',second,self.now+110)
        self.assertEqual(rows[0]['status'],'resolved')
        self.assertFalse(rows[0]['blocking'])
        # Old RSS copies cannot re-open an event whose later end is confirmed.
        rows=ledger.update('CLUSDT',self.facts('The war between Iran and Israel continues today.'),self.now+120)
        self.assertFalse(rows[0]['blocking'])
        # A genuinely later escalation does re-open the same conflict.
        rows=ledger.update('CLUSDT',self.facts('The war between Iran and Israel continues today.',published=self.now+200,now=self.now+210),self.now+210)
        self.assertNotEqual(rows[0]['status'],'resolved')
        self.assertNotIn('resolved_at',rows[0])

    def test_title_only_and_single_source_end_cannot_release(self):
        for fulltext in (False,True):
            with self.subTest(fulltext=fulltext):
                ledger=RiskLedger();self.seed(ledger)
                facts=self.facts('The war between Iran and Israel has ended today.',published=self.now+100,now=self.now+110,fulltext=fulltext)
                rows=ledger.update('CLUSDT',facts,self.now+110)
                self.assertNotEqual(rows[0]['status'],'resolved')

    def test_different_conflict_and_local_ceasefire_cannot_release(self):
        ledger=RiskLedger();self.seed(ledger)
        for source in ('BBC','Guardian'):
            facts=self.facts('The war between Russia and Ukraine has ended today.',source=source,published=self.now+100,now=self.now+110)
            facts+=self.facts('A ceasefire between Iran and Israel took effect today.',source=source,published=self.now+100,now=self.now+110)
            rows=ledger.update('CLUSDT',facts,self.now+110)
        self.assertTrue(all(r['status']!='resolved' for r in rows))

    def test_hazard_highwater_survives_report_truncation_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';ledger=RiskLedger(path,safe_url);self.seed(ledger)
            initial=ledger.snapshot('CLUSDT')[0]['last_hazard_at']
            # Repeated, uncorroborated newer publications describing an older
            # alleged end cannot evict the last known hazard from the ledger.
            for count in range(30):
                obs=self.facts('The war between Iran and Israel has ended today.',published=self.now+100+count,now=self.now+200)[0]
                obs['event_at']=self.now-1000
                obs['fingerprint']=hashlib.sha256(str(count).encode()).hexdigest()
                ledger.update('CLUSDT',[obs],self.now+200)
            row=next(iter(ledger.symbols['CLUSDT'].values()))
            self.assertEqual(len(row['_reports']),24)
            self.assertTrue(all(r['action']=='resolved' for r in row['_reports']))
            self.assertEqual(row['last_hazard_at'],initial)
            self.assertTrue(row['blocking'])
            restored=RiskLedger(path,safe_url)
            self.assertFalse(restored.error)
            self.assertNotEqual(restored.update('CLUSDT',[],self.now+300)[0]['status'],'resolved')

    def test_older_or_conflicting_release_evidence_never_clears(self):
        ledger=RiskLedger();self.seed(ledger)
        facts=self.facts('The war between Iran and Israel has ended today.',published=self.now-1000)
        facts+=self.facts('Hostilities between Israel and Iran have ceased today.',source='Guardian',published=self.now-900)
        self.assertNotEqual(ledger.update('CLUSDT',facts,self.now)[0]['status'],'resolved')
        facts=self.facts('The war between Iran and Israel has ended today.',published=self.now+100,now=self.now+200)
        facts+=self.facts('Hostilities between Israel and Iran have ceased today.',source='Guardian',published=self.now+110,now=self.now+200)
        facts+=self.facts('The war between Iran and Israel continues today.',published=self.now+120,now=self.now+200)
        self.assertNotEqual(ledger.update('CLUSDT',facts,self.now+200)[0]['status'],'resolved')

    def test_facility_country_aliases_and_foreign_restore_do_not_cross(self):
        ledger=RiskLedger()
        risk=self.facts('Libyan oil production stopped today at El Sharara oilfield.')
        self.assertEqual(risk[0]['subject'],'libya:el_sharara')
        ledger.update('CLUSDT',risk,self.now)
        for source,verb in (('BBC','resumed'),('Guardian','restarted')):
            facts=self.facts('Iraqi oil production '+verb+' today at El Sharara oilfield.',source=source,published=self.now+100,now=self.now+110)
            ledger.update('CLUSDT',facts,self.now+110)
        self.assertNotEqual(ledger.snapshot('CLUSDT')[0]['status'],'resolved')
        for source,verb in (('BBC','resumed'),('Guardian','restarted')):
            facts=self.facts('Libyan oil production '+verb+' today at El Sharara oilfield.',source=source,published=self.now+200,now=self.now+210)
            rows=ledger.update('CLUSDT',facts,self.now+210)
        self.assertFalse(rows[0]['blocking'])

    def test_silver_mine_has_strict_recovery_lifecycle(self):
        ledger=RiskLedger()
        facts=self.facts('Mexican silver production stopped today at Santa mine.','XAGUSDT')
        self.assertEqual(facts[0]['subject'],'mexico:santa')
        ledger.update('XAGUSDT',facts,self.now)
        for source,verb in (('BBC','resumed'),('Guardian','restarted')):
            facts=self.facts('Mexican silver production '+verb+' today at Santa mine.','XAGUSDT',source=source,published=self.now+100,now=self.now+110)
            rows=ledger.update('XAGUSDT',facts,self.now+110)
        self.assertEqual(rows[0]['status'],'resolved')

    def test_global_lng_recovery_releases_risk_without_us_gas_direction(self):
        ledger=RiskLedger()
        body='Qatari LNG exports stopped today at Ras Laffan terminal.'
        facts=self.facts(body,'NATGASUSDT')
        self.assertEqual(facts[0]['category'],'global_lng_transmission')
        self.assertEqual(facts[0]['subject'],'qatar:ras_laffan')
        ledger.update('NATGASUSDT',facts,self.now)
        self.assertFalse(inspect_item(self.raw(body),'NATGASUSDT',self.now)['events'])
        for source,verb in (('BBC','resumed'),('Guardian','restarted')):
            body='Qatari LNG exports '+verb+' today at Ras Laffan terminal.'
            facts=self.facts(body,'NATGASUSDT',source=source,published=self.now+100,now=self.now+110)
            rows=ledger.update('NATGASUSDT',facts,self.now+110)
            self.assertFalse(inspect_item(self.raw(body,source,published=self.now+100),'NATGASUSDT',self.now+110)['events'])
        self.assertFalse(rows[0]['blocking'])

    def test_weather_and_us_lng_are_separate_factors(self):
        raw=self.raw('A winter storm in Texas intensified today. US LNG exports stopped today at Sabine Pass terminal.')
        facts=risk_observations(raw,'NATGASUSDT',self.now,event_time)
        self.assertEqual({f['category'] for f in facts},{'us_gas_weather','us_lng_exports'})
        self.assertEqual(next(f['subject'] for f in facts if f['category']=='us_lng_exports'),'us:sabine_pass')
        ledger=RiskLedger();ledger.update('NATGASUSDT',facts,self.now)
        self.assertEqual(ledger.snapshot('CLUSDT'),[])
        summary=driver_summary([], 'NATGASUSDT',ledger.snapshot('NATGASUSDT'),self.now)
        self.assertEqual(next(d['status'] for d in summary if d['key']=='gas_weather'),'background')
        self.assertEqual(next(d['status'] for d in summary if d['key']=='global_lng'),'background')

    def test_one_fed_fact_does_not_verify_usd_or_silver_industry(self):
        item=self.raw('The Federal Reserve cut interest rates today. US dollar and real yields rose today. Solar manufacturing demand fell today.',title='Fed cuts interest rates')
        prepared=inspect_item(item,'XAGUSDT',self.now)
        prepared['_context_text']=item['body']
        for event in prepared['events']:event['verified']=True
        summary=driver_summary([prepared],'XAGUSDT',[],self.now)
        self.assertEqual(next(d['status'] for d in summary if d['key']=='fed_policy'),'verified')
        for key in ('usd_real_yield','silver_industry'):
            self.assertEqual(next(d['status'] for d in summary if d['key']==key),'review_required')

    def test_unsupported_fresh_factor_switches_to_kline_without_claiming_verified_direction(self):
        body='The Fed cut interest rates today.'
        first=inspect_item(self.raw(body,title='Fed cuts rates'),'XAUUSDT',self.now)
        second=inspect_item(self.raw('The Federal Reserve lowered interest rates today.',source='Guardian',title='Fed decision'),'XAUUSDT',self.now)
        dollar=inspect_item(self.raw('US dollar and real yields rose today.',title='US dollar and real yields'),'XAUUSDT',self.now)
        decision=decide('XAUUSDT',[first,second,dollar],{'healthy':True},self.now)
        self.assertEqual(decision['status'],'insufficient')
        self.assertFalse(decision['entry_allowed'])
        self.assertTrue(decision['kline_allowed'])
        self.assertIsNone(decision['direction'])
        self.assertEqual(decision['evidence'],[])

    def test_old_policy_background_and_missing_driver_data_are_not_permanent_risks(self):
        raw=self.raw('The Federal Reserve cut interest rates today.',published=self.now-7*3600,title='Fed monetary policy')
        self.assertEqual(risk_observations(raw,'XAUUSDT',self.now,event_time),[])
        prepared=inspect_item(raw,'XAUUSDT',self.now)
        summary=driver_summary([prepared],'XAUUSDT',[],self.now)
        self.assertEqual(next(d['status'] for d in summary if d['key']=='fed_policy'),'no_recent_evidence')
        self.assertEqual(decide('XAUUSDT',[prepared],{'healthy':True},self.now)['status'],'insufficient')

    def test_expanded_sources_are_cached_across_five_symbols_and_expire(self):
        calls=[]
        def fetch(url):calls.append(url);return b'<rss><channel/></rss>'
        service=NewsService(fetch)
        for symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT'):
            result=service.fetch(symbol,self.now)
            self.assertEqual(result['decision']['status'],'insufficient')
            self.assertFalse(result['decision']['entry_allowed'])
            self.assertEqual(len(result['coverage']['successful']),6)
        self.assertEqual(set(calls),set(FEEDS.values()))
        self.assertEqual(len(calls),len(FEEDS))
        service.fetch('BZUSDT',self.now+26)
        self.assertEqual(len(calls),len(FEEDS)+6)

    def test_corrupt_ledger_is_never_overwritten_or_treated_as_no_risk(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';path.write_text('{broken')
            service=NewsService(lambda url:b'<rss><channel/></rss>',state_path=path)
            result=service.fetch('CLUSDT',self.now)
            self.assertEqual(result['decision']['status'],'unavailable')
            self.assertIn('账本',result['decision']['reason'])
            self.assertTrue(result['coverage']['risk_state_error'])
            self.assertEqual(path.read_text(),'{broken')

    def test_save_failure_blocks_and_durable_file_contains_metadata_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';ledger=RiskLedger(path,safe_url);self.seed(ledger)
            data=path.read_text()
            self.assertNotIn('body',data)
            self.assertNotIn('The war between',data)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with patch('binance_trader.news_risk.os.replace',side_effect=OSError('disk failure')):
                ledger.update('CLUSDT',[],self.now+10)
            self.assertTrue(ledger.error)
            self.assertEqual(path.read_text(),data)

    def test_unknown_or_multiple_agencies_never_form_independent_authority(self):
        for author in ('a news agency','Reuters and AFP','republished from a wire service'):
            raw=self.raw('The Fed cut interest rates today.',author=author,title='Fed rates')
            self.assertEqual(canonical_origin(raw),'Unknown')
            parsed=inspect_item(raw,'XAUUSDT',self.now)
            self.assertFalse(parsed['events'])
            self.assertTrue(parsed['uncertain'])

    def test_risk_discovery_relevance_covers_five_commodities_without_direction(self):
        cases={'XAUUSDT':'War between Iran and Israel threatens safe haven demand',
               'XAGUSDT':'Solar manufacturing and industrial demand',
               'CLUSDT':'Shipping through Hormuz is disrupted',
               'BZUSDT':'Red Sea tanker traffic is blocked',
               'NATGASUSDT':'Qatar LNG exports stopped'}
        for symbol,title in cases.items():
            with self.subTest(symbol=symbol):
                self.assertTrue(relevant_context(title,symbol))
                parsed=inspect_item(self.raw(title,title=title),'NATGASUSDT' if symbol=='NATGASUSDT' else symbol,self.now)
                self.assertFalse(parsed['events'])

    def test_pronoun_us_and_commentator_nationality_are_not_war_participants(self):
        cases=(
            'The speaker defends the decision to enter war in a podcast with a US host.',
            'You can count on us today; national security depends on the outcome of the war.',
            'The politician explained to us why the second world war began.',
            'US historians discussed the second world war today.',
            'A US podcaster debated the first world war today.',
        )
        for sentence in cases:
            with self.subTest(sentence=sentence):
                self.assertEqual(self.facts(sentence),[])
        self.assertIsNone(subject_for_geo('The war has affected us today.'))
        self.assertEqual(subject_for_geo('The war between the United States and Iran continues today.'),'iran_us_conflict')

    def test_clear_current_conflict_relationships_still_create_risk(self):
        for sentence in ('The war in Iran continues today.',
                         'The war with Iran continues today.',
                         'The war between Iran and Israel continues today.',
                         "Iran's war continues today.",
                         'US launched airstrikes today.',
                         'U.S. launched airstrikes today.',
                         'The United States launched airstrikes today.',
                         'Russia strikes Ukraine today.'):
            with self.subTest(sentence=sentence):
                self.assertTrue(self.facts(sentence))

    def test_us_pronoun_does_not_locate_weather_or_supply_in_america(self):
        self.assertFalse(US.search('The hurricane alarm woke us today.'))
        self.assertIsNone(region('The terminal supplied us with LNG.'))
        self.assertEqual(self.facts('The hurricane alarm woke us today.','NATGASUSDT'),[])
        self.assertFalse(relevant_context('The hurricane alarm woke us today.','NATGASUSDT'))
        for location in ('US','U.S.','United States','American','Texas','Louisiana'):
            with self.subTest(location=location):
                self.assertEqual(region(location),'us')
                self.assertTrue(US.search(location))
                self.assertTrue(relevant_context('The hurricane in '+location+' intensified today.','NATGASUSDT'))

    def test_acute_first_evidence_expires_without_resolving_the_conflict(self):
        ledger=RiskLedger()
        facts=self.facts('The war between Iran and Israel escalated today.')
        row=ledger.update('CLUSDT',facts,self.now)[0]
        self.assertEqual(row['entry_gate'],'cooldown')
        self.assertTrue(row['blocking'])
        self.assertEqual(row['gate_version'],1)
        self.assertEqual(row['time_basis'],'first_evidence')
        self.assertEqual(row['acute_event_at'],self.now)
        self.assertEqual(row['cooldown_until'],self.now+3600)
        self.assertEqual(ledger.snapshot('CLUSDT',self.now+3599)[0]['entry_gate'],'cooldown')
        expired=ledger.snapshot('CLUSDT',self.now+3600)[0]
        self.assertEqual(expired['entry_gate'],'background')
        self.assertFalse(expired['blocking'])
        self.assertNotEqual(expired['status'],'resolved')
        self.assertEqual(expired['acute_event_id'],row['acute_event_id'])

    def test_rewrite_retitle_republication_and_restart_cannot_extend_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';ledger=RiskLedger(path,safe_url)
            first=self.facts('The war between Iran and Israel escalated today.')
            initial=ledger.update('CLUSDT',first,self.now)[0]
            for elapsed,source,body in (
                (100,'BBC','The war between Iran and Israel escalated today.'),
                (200,'Guardian','Hostilities between Israel and Iran escalated today.'),
                (3800,'BBC','The conflict between Iran and Israel escalated today.'),
            ):
                ledger=RiskLedger(path,safe_url)
                self.assertFalse(ledger.error)
                facts=self.facts(body,source=source,published=self.now+elapsed-10,
                                 now=self.now+elapsed,title='New headline about the Iran war')
                row=ledger.update('CLUSDT',facts,self.now+elapsed)[0]
                self.assertEqual(row['acute_event_id'],initial['acute_event_id'])
                self.assertEqual(row['cooldown_until'],initial['cooldown_until'])
            self.assertEqual(row['entry_gate'],'background')

    def test_same_fingerprint_on_next_day_does_not_restart_window(self):
        ledger=RiskLedger()
        body='The war between Iran and Israel escalated today.'
        original=ledger.update('CLUSDT',self.facts(body),self.now)[0]
        later=self.now+86400
        # In-memory lifecycle only; no future timestamps are persisted.
        copied=self.facts(body,source='Guardian',published=later-10,now=later)
        row=ledger.update('CLUSDT',copied,later)[0]
        self.assertEqual(row['acute_event_id'],original['acute_event_id'])
        self.assertEqual(row['entry_gate'],'background')

    def test_exact_later_event_can_create_a_new_cooldown(self):
        ledger=RiskLedger()
        def body(at):
            return 'The war between Iran and Israel escalated at '+datetime.fromtimestamp(at,timezone.utc).isoformat().replace('+00:00','Z')+'.'
        first_at=self.now-900
        first=ledger.update('CLUSDT',self.facts(body(first_at)),self.now)[0]
        self.assertEqual(first['time_basis'],'event_time')
        self.assertEqual(first['acute_event_at'],first_at)
        self.assertEqual(first['cooldown_until'],first_at+3600)
        new_at=self.now+1000
        new=ledger.update('CLUSDT',self.facts(body(new_at),published=new_at+10,now=new_at+20),new_at+20)[0]
        self.assertNotEqual(new['acute_event_id'],first['acute_event_id'])
        self.assertEqual(new['cooldown_until'],new_at+3600)

    def test_offset_time_equivalence_does_not_start_another_event(self):
        ledger=RiskLedger();at=self.now-900
        iso=datetime.fromtimestamp(at,timezone.utc).isoformat()
        first=ledger.update('CLUSDT',self.facts('The war in Iran escalated at '+iso+'.'),self.now)[0]
        second=self.facts('The war in Iran escalated at '+iso.replace('+00:00','Z')+'.',source='Guardian',published=self.now+10,now=self.now+20)
        row=ledger.update('CLUSDT',second,self.now+20)[0]
        self.assertEqual(row['acute_event_id'],first['acute_event_id'])

    def test_ambiguous_old_or_conflicting_event_time_cannot_create_acute_window(self):
        cases=(
            ('The war in Iran continues today.',self.now-60),
            ('The war in Iran escalated today.',self.now-3601),
            ('The war in Iran escalated today, on 2020-01-01.',self.now-60),
            ('The war in Iran escalated today, on January 1, 2020.',self.now-60),
            ('The war in Iran escalated yesterday.',self.now-60),
            ('The war in Iran escalated.',self.now-60),
        )
        for body,published in cases:
            with self.subTest(body=body):
                facts=self.facts(body,published=published)
                self.assertFalse(any(f.get('acute') for f in facts))
                self.assertFalse(any(r['blocking'] for r in RiskLedger().update('CLUSDT',facts,self.now)))

    def test_existing_schema_two_preserves_background_and_old_first_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';ledger=RiskLedger(path,safe_url)
            body='The war in Iran escalated today.'
            first=ledger.update('CLUSDT',self.facts(body),self.now)[0]
            data=json.loads(path.read_text())
            for row in data['symbols']['CLUSDT'].values():row.pop('_acute_events',None)
            path.write_text(json.dumps(data))
            restored=RiskLedger(path,safe_url)
            self.assertFalse(restored.error)
            self.assertEqual(restored.snapshot('CLUSDT',self.now+4000)[0]['entry_gate'],'background')
            repeated=self.facts(body,source='Guardian',published=self.now+3990,now=self.now+4000)
            row=restored.update('CLUSDT',repeated,self.now+4000)[0]
            self.assertEqual(row['acute_event_at'],self.now)
            self.assertEqual(row['cooldown_until'],first['cooldown_until'])
            self.assertEqual(row['entry_gate'],'background')

    def test_old_unresolved_fact_does_not_require_a_false_resolution_to_trade(self):
        ledger=RiskLedger()
        past=self.now-365*86400
        facts=self.facts('The war between Iran and Israel continues.',published=past,now=past)
        ledger.update('CLUSDT',facts,past)
        row=ledger.update('CLUSDT',[],self.now)[0]
        self.assertNotEqual(row['status'],'resolved')
        self.assertEqual(row['entry_gate'],'background')
        d=decide('CLUSDT',[],{'healthy':True},self.now,risks=[row])
        self.assertTrue(d['kline_allowed'])

    def test_resolved_risk_clears_acute_projection_not_audit_registry(self):
        ledger=RiskLedger()
        ledger.update('CLUSDT',self.facts('The war between Iran and Israel escalated today.'),self.now)
        facts=self.facts('The war between Iran and Israel has ended today.',published=self.now+100,now=self.now+200)
        facts+=self.facts('Hostilities between Israel and Iran have ceased today.',source='Guardian',published=self.now+110,now=self.now+200)
        row=ledger.update('CLUSDT',facts,self.now+200)[0]
        self.assertEqual(row['entry_gate'],'resolved')
        for key in ('acute_event_id','acute_rule','acute_event_at','time_basis','cooldown_until'):self.assertIsNone(row[key])
        self.assertTrue(next(iter(ledger.symbols['CLUSDT'].values()))['_acute_events'])

    def test_corrupt_acute_clock_is_rejected_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'risks.json';ledger=RiskLedger(path,safe_url)
            ledger.update('CLUSDT',self.facts('The war in Iran escalated today.'),self.now)
            data=json.loads(path.read_text())
            row=next(iter(data['symbols']['CLUSDT'].values()))
            event=next(iter(row['_acute_events'].values()))
            event['cooldown_until']+=1
            path.write_text(json.dumps(data))
            self.assertTrue(RiskLedger(path,safe_url).error)


if __name__=='__main__':unittest.main()
