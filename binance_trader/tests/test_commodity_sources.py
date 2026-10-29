"""Source-shape fixtures: no exchange account, provider key or network calls."""
import copy
import csv
import io
import json
import unittest
from datetime import date,timedelta
from unittest.mock import patch
from binance_trader.commodity_sources import (parse_oil,parse_gas_storage,parse_gas_monthly,
    parse_weather,parse_lng_prices,parse_silver_table,fetch_commodity)
from binance_trader.fundamentals import (Fundamentals,model_metrics,validate_metrics,record,
    OIL_JSON,OIL_CSV,GAS_STORAGE,GAS_PROD,GAS_LNG,LNG_SUPPLEMENT,CPC,SILVER)
from binance_trader.analysis import factor_events
from test_fundamentals import NOW,table,fixture_source as metal_sources
from test_ai_news import ModelClient,CONFIG,Capture
from binance_trader.news_risk import DRIVERS

SILVER_URL='https://silverinstitute.org/wp-content/uploads/2026/04/World-Silver-Survey-2026.pdf'
SILVER_TABLE='''Silver Supply and Demand
Million ounces 2024 2025 2026F 2025 2026F
Mine Production 800.0 840.0 850.0 5% 1%
Recycling 180.0 190.0 210.0 5% 10%
Industrial (total) 680.0 650.0 630.0 -4% -3%
…of which photovoltaics 190.0 180.0 150.0 -5% -17%
'''

def oil_fixture():
    rows=[['STUB_1','9/25/26','9/18/26','Difference','Percent Change'],
        ['Commercial (Excluding SPR)','427.320','426.398','.922','.2'],
        ['Total Motor Gasoline','204.362','206.046','-1.683','-.8'],['Distillate Fuel Oil','105.180','107.431','-2.251','-2.1'],
        ['STUB_1','STUB_2','9/25/26','9/18/26','Difference']]
    for label,cur,prev in [('Domestic Production',13955,13939),('Imports',5698,5877),('Exports',3570,3281),('Crude Oil Input to Refineries',16257,16811)]:
        rows.append(['Crude Oil Supply ', '(1) '+label,str(cur),str(prev),str(cur-prev)])
    out=io.StringIO();csv.writer(out).writerows(rows)
    j={'metadata':{'release_date':'2026-09-30','time_period':{'end_date':'2026-09-25'}},'data':{'U.S.':{
        'sourcekey':'WCESTUS1','units':'thousand barrels','time_series':[
            {'date':'2026-09-25','value':427320,'suppression_flag':None},{'date':'2026-09-18','value':426398,'suppression_flag':None}]}}}
    return out.getvalue().encode(),json.dumps(j).encode()

def gas_fixture():
    return {'release_date':'2026-Oct-01 00:00:00','current_week':'2026-09-25','week_ago':'2026-09-18','periodicity':'weekly',
        'series':[{'series_id':'png.nw2_epg0_swo_r48_bcf.w','units':'billion cubic feet',
            'data':[['2026-09-25',3415],['2026-09-18',3351]],'reclassification_flag':['false','false'],'calculated':{'net_change':64}}]}

def monthly_fixture(key):
    title='U.S. Dry Natural Gas Production' if key=='dry_gas_production' else 'Liquefied U.S. Natural Gas Exports'
    return (title+' (Million Cubic Feet) Release Date: 9/30/2026'+table([
        ['Year','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],
        ['2026','','','','','','3,300,000','3,410,000','','','','','']])).encode()

def weather_fixture(kind,missing=False):
    dates=[date(2026,9,21)+timedelta(days=i) for i in range(14)]
    return ('Product: Daily '+kind+' Degree Days\nRegions: CPC::Regions::CensusDivisions\nWeights: Population\nRegion|'+
        '|'.join(d.strftime('%Y%m%d') for d in dates)+'\nCONUS|'+'|'.join(['2']*7+['3']*6+(['-9999'] if missing else ['3']))).encode()

def lng_fixture():
    return json.dumps({'data-week-ending':'September 30, 2026','release-date':'October 1, 2026'}).encode(),b'''<li>For the week ending September 30:</li>
        <li>The price at the Title Transfer Facility (TTF) in Europe averaged $24.18/MMBtu, $0.98/per MMBtu lower than the previous week.</li>
        <li>The Japan-Korea Marker (JKM) price averaged $25.89/MMBtu, $0.52/MMBtu higher than the previous week.</li>'''

def sources():
    result=metal_sources();oc,oj=oil_fixture();lm,lc=lng_fixture()
    result.update({OIL_CSV:oc,OIL_JSON:oj,GAS_STORAGE:json.dumps(gas_fixture()).encode(),
        GAS_PROD:monthly_fixture('dry_gas_production'),GAS_LNG:monthly_fixture('lng_exports'),
        CPC+'2026/Population.Heating.txt':weather_fixture('Heating'),CPC+'2026/Population.Cooling.txt':weather_fixture('Cooling'),
        LNG_SUPPLEMENT+'release_dates.json':lm,LNG_SUPPLEMENT+'bullets_lng_2.html':lc,
        SILVER:('<a href="'+SILVER_URL+'">2026 survey</a>').encode(),SILVER_URL:b'synthetic-pdf'})
    return result

def populated():
    service=Fundamentals(fetcher=sources().__getitem__,clock=lambda:NOW)
    with patch('binance_trader.commodity_sources.parse_silver_pdf',side_effect=lambda b,u,n:parse_silver_table(SILVER_TABLE,u,n)):
        service.refresh()
    return service

class CommoditySourcesTests(unittest.TestCase):
    def test_oil_keeps_stocks_and_daily_flows_in_distinct_units(self):
        rows={r['id']:r for r in parse_oil(*oil_fixture(),NOW)}
        self.assertEqual(len(rows),7);self.assertEqual(rows['crude_stocks']['change'],.922)
        self.assertEqual(rows['crude_stocks']['unit'],'百万桶');self.assertEqual(rows['us_crude_production']['unit'],'千桶/日')
        self.assertEqual(rows['us_crude_production']['change'],16)
        self.assertEqual(rows['gasoline_stocks']['change'],-1.684,'Compute from published values; do not copy a separately rounded difference')

    def test_oil_rejects_mismatched_units_dates_suppression_and_duplicate_series(self):
        c,j=oil_fixture()
        for changed in (j.replace(b'thousand barrels',b'million barrels'),j.replace(b'427320',b'427.32'),j.replace(b'2026-09-25',b'2026-09-26'),j.replace(b'null',b'"--"')):
            with self.assertRaises((ValueError,KeyError)):parse_oil(c,changed,NOW)
        with self.assertRaises(ValueError):parse_oil(c+c.splitlines()[-1]+b'\n',j,NOW)

    def test_natural_gas_storage_reclassification_and_units_are_checked(self):
        d=gas_fixture();row=parse_gas_storage(json.dumps(d).encode(),NOW)[0]
        self.assertEqual((row['value'],row['change'],row['unit']),(3415,64,'Bcf'))
        for field,value in [('units','million cubic feet'),('reclassification_flag',['true','false'])]:
            changed=copy.deepcopy(d);changed['series'][0][field]=value
            with self.assertRaises(ValueError):parse_gas_storage(json.dumps(changed).encode(),NOW)

    def test_monthly_gas_is_normalized_for_month_length_and_not_confused_with_lng(self):
        row=parse_gas_monthly(monthly_fixture('dry_gas_production'),'dry_gas_production',NOW)[0]
        self.assertEqual((row['value'],row['previous_value'],row['change']),(110,110,0))
        self.assertEqual(row['observation_date'],'2026-07-31')
        with self.assertRaises(ValueError):parse_gas_monthly(monthly_fixture('dry_gas_production'),'lng_exports',NOW)
        with self.assertRaises(ValueError):parse_gas_monthly(monthly_fixture('lng_exports').replace(b'Million Cubic Feet',b'Billion Cubic Feet'),'lng_exports',NOW)

    def test_degree_days_are_national_weighted_observations_with_complete_nonoverlapping_weeks(self):
        row=parse_weather([weather_fixture('Heating')],'heating_degree_days',NOW,CPC+'2026/Population.Heating.txt')
        self.assertEqual((row['value'],row['previous_value'],row['change']),(21,14,7))
        for data in (weather_fixture('Heating',True),weather_fixture('Cooling'),weather_fixture('Heating').replace(b'CONUS',b'NY')):
            with self.assertRaises(ValueError):parse_weather([data],'heating_degree_days',NOW,CPC+'2026/Population.Heating.txt')

    def test_lng_foreign_prices_use_explicit_change_and_matching_reporting_week(self):
        m,c=lng_fixture();rows=parse_lng_prices(m,c,NOW)
        self.assertEqual(rows[0]['previous_value'],25.16);self.assertEqual(rows[1]['previous_value'],25.37)
        for changed in (c.replace(b'September 30',b'September 23'),c.replace(b'lower than',b'expected next'),c.replace(b'MMBtu',b'MWh')):
            with self.assertRaises(ValueError):parse_lng_prices(m,changed,NOW)

    def test_silver_forecasts_are_never_mislabelled_as_observed_years(self):
        rows={r['id']:r for r in parse_silver_table(SILVER_TABLE,SILVER_URL,NOW)}
        self.assertEqual(rows['silver_industrial']['value'],650);self.assertEqual(rows['silver_industrial']['previous_value'],680)
        self.assertEqual(rows['silver_mine']['observation_date'],'2025-12-31')
        for changed in (SILVER_TABLE.replace('2026F','2026'),SILVER_TABLE.replace('Million ounces','Tonnes'),SILVER_TABLE.replace('680.0 650.0','650.0')):
            with self.assertRaises(ValueError):parse_silver_table(changed,SILVER_URL,NOW)

    def test_all_five_symbols_have_valid_numeric_inputs_without_cross_commodity_leaks(self):
        service=populated()
        for symbol,count in [('XAUUSDT',9),('XAGUSDT',8),('CLUSDT',7),('BZUSDT',7),('NATGASUSDT',7)]:
            with self.subTest(symbol=symbol):
                metrics=model_metrics(service.snapshot(symbol));self.assertEqual(len(metrics),count)
                self.assertEqual(len(validate_metrics(metrics,symbol,NOW)),count)
                labels={k for k,_ in DRIVERS[symbol]};self.assertTrue(all(row['driver'] in labels for row in metrics))
        with self.assertRaises(ValueError):validate_metrics(model_metrics(service.snapshot('BZUSDT')),'XAUUSDT',NOW)
        row=model_metrics(service.snapshot('NATGASUSDT'))[0];row['url']=OIL_CSV
        with self.assertRaises(ValueError):validate_metrics([row],'NATGASUSDT',NOW)

    def test_luna_payload_and_numeric_evidence_validate_for_every_symbol(self):
        service=populated()
        for symbol in DRIVERS:
            metrics=model_metrics(service.snapshot(symbol))
            output=dict(summary='依据有效公开统计整理因素，低频资料仅作背景。',direction='uncertain',events=[],uncertainties=['数值不保证走势。'],drivers=[])
            for key,_ in DRIVERS[symbol]:
                ids=[r['id'] for r in metrics if r['driver']==key]
                output['drivers'].append(dict(key=key,status='analyzed' if ids else 'no_evidence',direction='uncertain',summary='所给统计支持背景分析。' if ids else '当前未提供此因素证据。',event_indices=[],metric_ids=ids))
            cap=Capture(output);result=ModelClient(CONFIG,cap).analyze(symbol,[],NOW,fundamentals=metrics)
            self.assertFalse(result['trading_authority']);self.assertEqual(len(result['drivers']),len(DRIVERS[symbol]))
            sent=json.loads(json.loads(cap.calls[0]['body'])['messages'][1]['content'])
            self.assertEqual({r['id'] for r in sent['fundamentals']},{r['id'] for r in metrics})

    def test_one_source_failure_does_not_erase_or_hide_other_commodity_data(self):
        service=populated();service.clock=lambda:NOW+1800
        fixture=sources();fixture[OIL_CSV]=b'malformed';service.fetcher=fixture.__getitem__;service.refresh()
        oil=service.snapshot('BZUSDT')['metrics'];self.assertTrue(all(r['status']=='stale' and 'value' in r for r in oil))
        self.assertEqual(len(model_metrics(service.snapshot('XAGUSDT'))),8)
        self.assertEqual(len(model_metrics(service.snapshot('NATGASUSDT'))),7)

    def test_geopolitical_and_shipping_events_retain_unverified_and_outage_states(self):
        risk=dict(subject='strait_of_hormuz',category='shipping_disruption',status='review_required',url='https://www.bbc.com/news/test',source='BBC',last_seen=NOW-10,published=NOW-3600)
        news=dict(symbol='BZUSDT',checked=NOW,coverage={'healthy':True},decision={'risks':[risk]})
        before=copy.deepcopy(news);events={r['key']:r for r in factor_events(news,NOW)}
        self.assertEqual(events['shipping']['total'],1);self.assertEqual(events['geopolitics']['records'][0]['status'],'review_required')
        self.assertEqual(news,before)
        self.assertTrue(all(r['status']=='unavailable' for r in factor_events(news,NOW+100)))
        news['decision']['risks'][0]['url']='javascript:alert(1)'
        self.assertTrue(all(r['total']==0 for r in factor_events(news,NOW)))

if __name__=='__main__':unittest.main()
