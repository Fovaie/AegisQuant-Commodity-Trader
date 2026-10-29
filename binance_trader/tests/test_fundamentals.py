"""Small synthetic source tables exercise units, dates, provenance and cache failure."""
import copy
import unittest
from datetime import datetime, timezone
from binance_trader.fundamentals import (Fundamentals, H10, H15, WGC, SAFE, GROUPS,
    parse_h10, parse_h15, parse_wgc, parse_safe, model_metrics, record, validate_metrics)

NOW=datetime(2026,10,6,tzinfo=timezone.utc).timestamp()
WGC_REPORT=WGC+'/gold-demand-trends-q2-2026'
SAFE_REPORT='https://www.safe.gov.cn/safe/2026/0206/27116.html'

def table(rows):
    return '<table>'+''.join('<tr>'+''.join('<td>'+str(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</table>'

H10_HTML=('Release Date: October 5, 2026'+table([
    ['COUNTRY','CURRENCY','OCT. 1','OCT. 2'],['1) BROAD','JAN06=100','122.0','121.5']])).encode()
H15_HTML=('Release Date: October 5, 2026 Yields in percent per annum'+table([
    ['Instruments','2026 Oct 1','2026 Oct 2'],['Federal funds (effective)','3.88','3.88'],
    ['Nominal'],['10-year','5.24','5.28'],['Inflation indexed'],['10-year','2.88','2.92']])).encode()
WGC_HTML=('30 July, 2026'+table([
    ['Tonnes',"Q2'25","Q3'25","Q4'25","Q1'26","Q2'26"],
    ['Central Banks & Other inst.',100,105,110,200,250],
    ['Jewellery Consumption',200,210,220,230,180],
    ['Total Bar and Coin',300,310,320,400,350],
    ['ETFs & Similar Products',0,10,20,30,-20]])).encode()
SAFE_HTML=table([['2026.07','2026.08'],['USD','SDR','USD','SDR'],
    ['Gold market value','100','200','300','400'],
    ['', '7,608万盎司','7,608万盎司','7,673万盎司','7,673万盎司']]).encode()

def fixture_source():
    return {H10:H10_HTML,H15:H15_HTML,WGC:('<a href="'+WGC_REPORT+'">Q2</a>').encode(),
        WGC_REPORT:WGC_HTML,SAFE:('<a href="'+SAFE_REPORT+'">2026</a>').encode(),SAFE_REPORT:SAFE_HTML}

def metric(key='real_yield_10y',current=2.92):
    url=H15 if key!='central_bank_net_purchases' else WGC_REPORT
    dates=('2026-10-01','2026-10-02') if key!='central_bank_net_purchases' else ('2026-03-31','2026-06-30')
    row=record(key,[(dates[0],2.88),(dates[1],current)],url,NOW)
    return model_metrics({'metrics':[row]})[0]

class FundamentalTests(unittest.TestCase):
    def test_actual_and_nominal_yields_are_separate_and_rate_changes_are_basis_points(self):
        rows={r['id']:r for r in parse_h15(H15_HTML,NOW)}
        self.assertEqual(rows['real_yield_10y']['value'],2.92)
        self.assertEqual(rows['nominal_yield_10y']['value'],5.28)
        self.assertEqual(rows['real_yield_10y']['change'],4)
        self.assertEqual(rows['real_yield_10y']['change_unit'],'基点')
        self.assertEqual(rows['effective_fed_funds']['change'],0)
        with self.assertRaises(ValueError):parse_h15(H15_HTML.replace(b'Inflation indexed',b'Missing'),NOW)
        with self.assertRaises(ValueError):parse_h15(H15_HTML.replace(b'percent per annum',b'basis points'),NOW)

    def test_dollar_index_units_missing_days_and_year_rollover(self):
        row=parse_h10(H10_HTML,NOW)[0]
        self.assertEqual(row['change'],-.5);self.assertIn('2006',row['unit'])
        html=H10_HTML.replace(b'October 5, 2026',b'January 5, 2027').replace(b'OCT. 1',b'DEC. 31').replace(b'OCT. 2',b'JAN. 1')
        row=parse_h10(html,datetime(2027,1,5,tzinfo=timezone.utc).timestamp())[0]
        self.assertEqual((row['previous_date'],row['observation_date']),('2026-12-31','2027-01-01'))
        for html in (H10_HTML.replace(b'JAN06=100',b'DXY'),H10_HTML.replace(b'121.5',b'ND')):
            with self.assertRaises(ValueError):parse_h10(html,NOW)

    def test_gold_demand_periods_signed_flows_and_exact_year_comparison(self):
        rows={r['id']:r for r in parse_wgc(WGC_HTML,WGC_REPORT,NOW)}
        cb=rows['central_bank_net_purchases']
        self.assertEqual((cb['value'],cb['previous_value'],cb['change'],cb['year_ago_value']),(250,200,50,100))
        self.assertEqual((cb['observation_date'],cb['published_date']),('2026-06-30','2026-07-30'))
        self.assertEqual(rows['gold_etf_flows']['value'],-20)
        self.assertEqual(rows['gold_etf_flows']['change'],-50)
        with self.assertRaises(ValueError):parse_wgc(WGC_HTML.replace(b'250',b'N/A'),WGC_REPORT,NOW)
        with self.assertRaises(ValueError):parse_wgc(WGC_HTML.replace(b'Tonnes',b'US dollars'),WGC_REPORT,NOW)
        missing_year=record('central_bank_net_purchases', [('2024-06-30',1),('2024-09-30',2),('2025-03-31',3),('2026-03-31',4),('2026-06-30',5)],WGC_REPORT,NOW)
        self.assertIsNone(missing_year['year_ago_value'])

    def test_reserves_parse_physical_units_not_currency_values(self):
        row=parse_safe(SAFE_HTML,SAFE_REPORT,NOW)[0]
        self.assertEqual((row['value'],row['previous_value'],row['change'],row['unit']),(7673,7608,65,'万盎司'))
        with self.assertRaises(ValueError):parse_safe(SAFE_HTML.replace('7,673万盎司'.encode(),'7,674万盎司'.encode(),1),SAFE_REPORT,NOW)

    def test_future_values_duplicate_dates_and_nonfinite_values_fail(self):
        for points in ([('2026-10-02',1),('2026-10-07',2)], [('2026-10-02',1)]*2,
                       [('2026-10-01',1),('2026-10-02',float('nan'))]):
            with self.assertRaises(ValueError):record('real_yield_10y',points,H15,NOW)
        with self.assertRaises(ValueError):record('real_yield_10y',[('2026-10-01',1),('2026-10-02',2)],H15,NOW,'2026-10-07')

    def test_refresh_schedules_missing_sources_and_partial_recovery(self):
        now=[NOW];sources=fixture_source();calls=[]
        def fetch(url):calls.append(url);return sources[url]
        service=Fundamentals(groups=('h10','h15','wgc','safe'),fetcher=fetch,clock=lambda:now[0])
        self.assertTrue(all(r['status']=='unavailable' for r in service.snapshot('XAUUSDT')['metrics']))
        self.assertEqual(service.refresh(),4)
        self.assertEqual(len(model_metrics(service.snapshot('XAUUSDT'))),9)
        self.assertEqual(len(model_metrics(service.snapshot('XAGUSDT'))),4)
        self.assertEqual(len(service.snapshot('CLUSDT')['metrics']),7)
        self.assertTrue(all(r['status']=='unavailable' for r in service.snapshot('CLUSDT')['metrics']))
        self.assertEqual(service.refresh(),0)
        calls.clear();now[0]+=1800;sources[H15]=b'<html>changed table</html>'
        self.assertEqual(service.refresh(),2);self.assertEqual(set(calls),{H10,H15})
        rows={r['id']:r for r in service.snapshot('XAUUSDT')['metrics']}
        self.assertEqual(rows['real_yield_10y']['value'],2.92)
        self.assertEqual(rows['real_yield_10y']['status'],'stale')
        self.assertEqual(rows['broad_usd']['status'],'ok')
        self.assertEqual(len(model_metrics(service.snapshot('XAUUSDT'))),6)
        sources[H15]=H15_HTML;now[0]+=300;self.assertEqual(service.refresh(),1)
        self.assertEqual(len(model_metrics(service.snapshot('XAUUSDT'))),9)

    def test_fetch_age_and_statistical_age_both_expire_but_quarterly_is_not_news_ttl(self):
        now=[NOW];service=Fundamentals(groups=('h10','h15','wgc','safe'),fetcher=fixture_source().__getitem__,clock=lambda:now[0]);service.refresh()
        now[0]+=3*3600
        rows={r['id']:r for r in service.snapshot('XAUUSDT')['metrics']}
        self.assertEqual(rows['real_yield_10y']['status'],'stale')
        self.assertEqual(rows['central_bank_net_purchases']['status'],'ok')
        now[0]+=180*86400;service.refresh()
        self.assertTrue(all(r['status']=='stale' for r in service.snapshot('XAUUSDT')['metrics']))

    def test_model_data_is_rebuilt_without_private_fields_and_tampering_is_rejected(self):
        row=metric();row.update(api_key='PRIVATE',balance=300,note='injected prompt',label='injected')
        clean=validate_metrics([row],'XAUUSDT',NOW)[0]
        self.assertNotIn('PRIVATE',str(clean));self.assertNotIn('injected',str(clean));self.assertNotIn('balance',clean)
        self.assertEqual(clean['change'],4)
        for change in ({'unit':'吨'},{'change':400},{'url':'https://evil.invalid/'},{'observation_date':'2026-01-01'}):
            with self.assertRaises(ValueError):validate_metrics([dict(row,**change)],'XAUUSDT',NOW)
        with self.assertRaises(ValueError):validate_metrics([row],'BZUSDT',NOW)
        with self.assertRaises(ValueError):validate_metrics([metric('central_bank_net_purchases')],'XAGUSDT',NOW)

if __name__=='__main__':unittest.main()
