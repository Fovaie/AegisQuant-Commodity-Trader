"""Source-specific public data parsers for oil, natural gas and silver."""
import calendar
import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin
from .fundamentals import (Document, record, number, OIL_CSV, OIL_JSON, GAS_STORAGE,
    GAS_PROD, GAS_LNG, CPC, SILVER, LNG_SUPPLEMENT)


def parse_oil(csv_data,json_data,now):
    meta=json.loads(json_data.decode('utf-8-sig'))
    release=meta['metadata']['release_date'];series=meta['data']['U.S.']
    if series['sourcekey']!='WCESTUS1' or series['units']!='thousand barrels':raise ValueError('原油库存系列或单位变更')
    # EIA CSV contains a non-UTF8 missing-value glyph in unrelated rows.
    rows=list(csv.reader(io.StringIO(csv_data.decode('cp1252'))))
    header=rows[0]
    if header[0]!='STUB_1' or header[3]!='Difference':raise ValueError('石油周报表头变更')
    current,previous=[datetime.strptime(v,'%m/%d/%y').date().isoformat() for v in header[1:3]]
    if (date.fromisoformat(current)-date.fromisoformat(previous)).days!=7:raise ValueError('石油周报期次不连续')
    if meta['metadata']['time_period']['end_date']!=current:raise ValueError('石油源发布时间不同步')
    points={r['date']:r['value'] for r in series['time_series'] if r['suppression_flag'] is None}
    stock_keys={'Commercial (Excluding SPR)':'crude_stocks','Total Motor Gasoline':'gasoline_stocks','Distillate Fuel Oil':'distillate_stocks'}
    supply_keys={'Domestic Production':'us_crude_production','Imports':'us_crude_imports','Exports':'us_crude_exports','Crude Oil Input to Refineries':'refinery_inputs'}
    found={};supply=False
    for row in rows[1:]:
        if row[:2]==['STUB_1','STUB_2']:
            if row[2:4]!=header[1:3]:raise ValueError('石油库存与产量期次不符')
            supply=True;continue
        key=stock_keys.get(row[0]) if row and not supply else None
        index=1
        if supply and len(row)>4 and row[0].strip()=='Crude Oil Supply':
            key=supply_keys.get(re.sub(r'^\(\d+\)\s*','',row[1]).strip());index=2
        if not key:continue
        if key in found:raise ValueError('石油指标重复')
        cur,prev=number(row[index]),number(row[index+1])
        if cur<0 or prev<0:raise ValueError('库存或产量为负')
        if key=='crude_stocks' and any(abs(v*1000-points.get(d,-1))>.6 for d,v in ((current,cur),(previous,prev))):raise ValueError('百万桶与千桶交叉核对不符')
        found[key]=record(key,[(previous,prev),(current,cur)],OIL_CSV,now,release)
    if set(found)!=set(stock_keys.values())|set(supply_keys.values()):raise ValueError('石油指标缺失')
    return list(found.values())


def parse_gas_storage(data,now):
    doc=json.loads(data.decode('utf-8-sig'))
    rows=[r for r in doc['series'] if r.get('series_id')=='png.nw2_epg0_swo_r48_bcf.w']
    if len(rows)!=1 or rows[0]['units']!='billion cubic feet' or doc['periodicity']!='weekly':raise ValueError('工作气库存系列不符')
    row=rows[0];values=dict(row['data']);days=[doc['week_ago'],doc['current_week']]
    if len(values)!=len(row['data']) or any(number(str(values[d]))<0 for d in days):raise ValueError('库存日期重复或数值无效')
    if (date.fromisoformat(days[1])-date.fromisoformat(days[0])).days!=7:raise ValueError('天然气库存期次不连续')
    for day in days:
        i=[p[0] for p in row['data']].index(day)
        if row['reclassification_flag'][i]!='false':raise ValueError('天然气库存发生重分类，不能当作注采变化')
    release=datetime.strptime(doc['release_date'],'%Y-%b-%d %H:%M:%S').date().isoformat()
    result=record('gas_storage',[(d,number(str(values[d]))) for d in days],GAS_STORAGE,now,release)
    if result['change']!=row['calculated']['net_change']:raise ValueError('库存变化不一致')
    return [result]


def parse_gas_monthly(data,key,now):
    doc=Document(data);text=' '.join(doc.text)
    title={'dry_gas_production':'U.S. Dry Natural Gas Production','lng_exports':'Liquefied U.S. Natural Gas Exports'}[key]
    if not re.search(re.escape(title)+r'\s*\(Million Cubic Feet\)',text):raise ValueError('天然气系列或单位变更')
    table=next(t for t in doc.tables if t and t[0]==['Year','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'])
    pub=re.search(r'Release Date:\s*(\d+/\d+/\d{4})',text)
    if not pub:raise ValueError('天然气月报缺少发布日期')
    points=[]
    for row in table[1:]:
        if not row or not re.fullmatch(r'20\d{2}',row[0]):continue
        if len(row)!=13:raise ValueError('月报列数异常')
        for month,cell in enumerate(row[1:],1):
            if not cell.strip():continue
            days=calendar.monthrange(int(row[0]),month)[1];value=number(cell)
            if value<0:raise ValueError('月度气量为负')
            points.append((date(int(row[0]),month,days).isoformat(),round(value/1000/days,6)))
    return [record(key,points,GAS_PROD if key=='dry_gas_production' else GAS_LNG,now,datetime.strptime(pub[1],'%m/%d/%Y').date().isoformat())]


def degree_points(data,kind):
    lines=data.decode('ascii').splitlines()
    if lines[:3]!=['Product: Daily '+kind+' Degree Days','Regions: CPC::Regions::CensusDivisions','Weights: Population']:raise ValueError('度日类型、地区或权重不符')
    header=lines[3].split('|');national=[r.split('|') for r in lines[4:] if r.startswith('CONUS|')]
    if len(national)!=1 or header[0]!='Region' or len(header)!=len(national[0]):raise ValueError('全美度日列不符')
    return [(datetime.strptime(d,'%Y%m%d').date(),number(v)) for d,v in zip(header[1:],national[0][1:])]


def parse_weather(pages,key,now,url):
    kind='Heating' if key=='heating_degree_days' else 'Cooling'
    points=sorted(p for page in pages for p in degree_points(page,kind))
    if len({d for d,_ in points})!=len(points):raise ValueError('度日日期重复')
    points=points[-14:]
    if len(points)!=14 or any(v<0 or v>100 or points[i][0]!=points[0][0]+timedelta(days=i) for i,(_,v) in enumerate(points)):raise ValueError('度日缺测或不连续')
    sums=[(points[6][0].isoformat(),sum(v for _,v in points[:7])),(points[13][0].isoformat(),sum(v for _,v in points[7:]))]
    return record(key,sums,url,now)


def parse_lng_prices(metadata,data,now):
    meta=json.loads(metadata);end=datetime.strptime(meta['data-week-ending'],'%B %d, %Y').date()
    pub=datetime.strptime(meta['release-date'],'%B %d, %Y').date().isoformat()
    text=' '.join(Document(data).text);text=' '.join(text.split())
    if 'For the week ending '+end.strftime('%B ')+str(end.day)+':' not in text:raise ValueError('LNG正文与发布期不符')
    rows=[]
    for key,marker in [('ttf_weekly','TTF'),('jkm_weekly','JKM')]:
        # Require explicit direction and amount. Never fill a missing prior
        # price using forecasts, an article date, or an assumed market move.
        match=re.search(r'\('+marker+r'\)[^.$]{0,80}(?:averaged|average of) \$(\d+\.\d+)/MMBtu,? \$(\d+\.\d+)/(?:per )?MMBtu (lower|higher) than the previous week',text)
        if not match:raise ValueError('LNG价格或前周变化不完整')
        value,delta=number(match[1]),number(match[2]);previous=value+delta if match[3]=='lower' else value-delta
        if min(value,previous)<0:raise ValueError('LNG价格无效')
        rows.append(record(key,[((end-timedelta(days=7)).isoformat(),round(previous,6)),(end.isoformat(),value)],LNG_SUPPLEMENT+'bullets_lng_2.html',now,pub))
    return rows


def parse_silver_table(text,url,now):
    header=re.search(r'^Million ounces (.+)$',text,re.M)
    if not header:raise ValueError('白银供需单位或表头缺失')
    years=header[1].split();forecast=next((i for i,y in enumerate(years) if re.fullmatch(r'20\d{2}F',y)),None)
    if forecast is None or forecast<2 or not all(re.fullmatch(r'20\d{2}',y) for y in years[:forecast]):raise ValueError('白银历史/预测列未区分')
    historical=years[:forecast]
    if len(set(historical))!=len(historical):raise ValueError('白银年度重复')
    names={'Mine Production':'silver_mine','Recycling':'silver_recycling','Industrial (total)':'silver_industrial','…of which photovoltaics':'silver_photovoltaic'}
    found={}
    for line in text.splitlines():
        line=line.strip()
        for label,key in names.items():
            if not line.startswith(label+' '):continue
            if key in found:raise ValueError('白银行重复')
            cells=re.sub(r'(?<=\d)\s*\.\s*(?=\d)','.',line[len(label):]).split()
            if len(cells)!=len(years) or any('%' in c for c in cells[:forecast+1]):raise ValueError('白银数值列错位')
            values=[number(c) for c in cells[:forecast]]
            if any(v<0 for v in values):raise ValueError('白银供需量为负')
            found[key]=record(key,[(y+'-12-31',v) for y,v in zip(historical,values)],url,now)
    if set(found)!=set(names.values()):raise ValueError('白银供需分类缺失')
    return list(found.values())


def parse_silver_pdf(data,url,now):
    from pypdf import PdfReader
    if len(data)>20_000_000 or not data.startswith(b'%PDF'):raise ValueError('白银报告格式异常')
    reader=PdfReader(io.BytesIO(data));found=[]
    if reader.is_encrypted or not 10<=len(reader.pages)<=150:raise ValueError('白银报告页数或加密状态异常')
    for page in reader.pages[:18]:
        text=page.extract_text()
        if 'Million ounces' in text and 'Industrial (total)' in text and 'Mine Production' in text:
            found.append(parse_silver_table(text,url,now))
    if len(found)!=1:raise ValueError('白银报告主表不唯一')
    return found[0]


def fetch_commodity(group,fetch,now):
    if group=='oil':return parse_oil(fetch(OIL_CSV),fetch(OIL_JSON),now)
    if group=='gas_storage':return parse_gas_storage(fetch(GAS_STORAGE),now)
    if group in ('gas_prod','gas_lng'):
        return parse_gas_monthly(fetch(GAS_PROD if group=='gas_prod' else GAS_LNG),'dry_gas_production' if group=='gas_prod' else 'lng_exports',now)
    if group=='weather':
        year=datetime.fromtimestamp(now,timezone.utc).year;rows=[]
        for key,kind in [('heating_degree_days','Heating'),('cooling_degree_days','Cooling')]:
            url=CPC+str(year)+'/Population.'+kind+'.txt';pages=[fetch(url)]
            if len(degree_points(pages[0],kind))<14:pages.insert(0,fetch(CPC+str(year-1)+'/Population.'+kind+'.txt'))
            rows.append(parse_weather(pages,key,now,url))
        return rows
    if group=='lng_prices':
        metadata=fetch(LNG_SUPPLEMENT+'release_dates.json');data=fetch(LNG_SUPPLEMENT+'bullets_lng_2.html')
        if json.loads(metadata)['release-date']!=json.loads(fetch(LNG_SUPPLEMENT+'release_dates.json'))['release-date']:raise ValueError('报告发布中，等待来源同步')
        return parse_lng_prices(metadata,data,now)
    if group=='silver':
        doc=Document(fetch(SILVER));matches=[]
        for link in doc.links:
            url=urljoin(SILVER,link);m=re.fullmatch(r'https://silverinstitute.org/wp-content/uploads/(20\d{2})/\d{2}/World[-_]Silver[-_]Survey[-_](20\d{2})\.pdf',url)
            if m and m[1]==m[2]:matches.append((int(m[1]),url))
        if not matches:raise ValueError('找不到白银年度报告')
        url=max(matches)[1];return parse_silver_pdf(fetch(url),url,now)
    raise ValueError('未知商品数据源')
