"""Public statistical releases, parsed as numbers independently of news/AI."""
import calendar
import copy
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

H10='https://www.federalreserve.gov/releases/h10/current/'
H15='https://www.federalreserve.gov/releases/h15/'
WGC='https://www.gold.org/goldhub/research/gold-demand-trends'
SAFE='https://www.safe.gov.cn/safe/gfcbzc/'
OIL_CSV='https://ir.eia.gov/wpsr/table1.csv'
OIL_JSON='https://ir.eia.gov/wpsr/psw00.json'
GAS_STORAGE='https://ir.eia.gov/ngs/wngsr.json'
GAS_PROD='https://www.eia.gov/dnav/ng/hist/n9070us2m.htm'
GAS_LNG='https://www.eia.gov/dnav/ng/hist/n9133us2m.htm'
LNG_SUPPLEMENT='https://www.eia.gov/naturalgas/weekly/supplement/content/'
CPC='https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/'
SILVER='https://silverinstitute.org/all-world-silver-surveys-2025/'
HOSTS={'www.federalreserve.gov','www.gold.org','www.safe.gov.cn','ir.eia.gov','www.eia.gov','ftp.cpc.ncep.noaa.gov','silverinstitute.org'}
MAX_BYTES=2_000_000
METALS={'XAUUSDT','XAGUSDT'}
SPECS={
 'broad_usd':('usd_real_yield','美联储广义美元指数','指数（2006年1月=100）','daily','Fed H.10',H10,14),
 'real_yield_10y':('usd_real_yield','10年期TIPS实际收益率','%','daily','Fed H.15',H15,10),
 'nominal_yield_10y':('usd_real_yield','10年期美债名义收益率','%','daily','Fed H.15',H15,10),
 'effective_fed_funds':('fed_policy','有效联邦基金利率','%','daily','Fed H.15',H15,10),
 'central_bank_net_purchases':('gold_demand','全球央行及官方机构净购金','吨','quarterly','World Gold Council / Metals Focus',WGC,160),
 'jewellery_consumption':('gold_demand','全球金饰消费量','吨','quarterly','World Gold Council / Metals Focus',WGC,160),
 'bar_coin_demand':('gold_demand','全球金条与金币需求','吨','quarterly','World Gold Council / Metals Focus',WGC,160),
 'gold_etf_flows':('gold_demand','全球黄金ETF净流量','吨','quarterly','World Gold Council / Metals Focus',WGC,160),
 'china_gold_reserves':('gold_demand','中国央行黄金储备量','万盎司','monthly','国家外汇管理局（SAFE）',SAFE,75),
 'crude_stocks':('inventory','美国商业原油库存（不含SPR）','百万桶','weekly','EIA WPSR',OIL_CSV,21),
 'gasoline_stocks':('inventory','美国汽油库存','百万桶','weekly','EIA WPSR',OIL_CSV,21),
 'distillate_stocks':('inventory','美国馏分油库存','百万桶','weekly','EIA WPSR',OIL_CSV,21),
 'us_crude_production':('oil_supply','美国原油产量','千桶/日','weekly','EIA WPSR',OIL_CSV,21),
 'us_crude_imports':('oil_supply','美国原油进口','千桶/日','weekly','EIA WPSR',OIL_CSV,21),
 'us_crude_exports':('oil_supply','美国原油出口','千桶/日','weekly','EIA WPSR',OIL_CSV,21),
 'refinery_inputs':('oil_supply','美国炼厂原油投入','千桶/日','weekly','EIA WPSR',OIL_CSV,21),
 'gas_storage':('gas_inventory','美国本土48州工作气库存','Bcf','weekly','EIA WNGSR',GAS_STORAGE,21),
 'dry_gas_production':('us_gas_supply','美国干气产量（月度日均）','Bcf/日','monthly','EIA Natural Gas Monthly',GAS_PROD,110),
 'lng_exports':('us_lng','美国LNG出口（月度日均）','Bcf/日','monthly','EIA Natural Gas Monthly',GAS_LNG,110),
 'heating_degree_days':('gas_weather','美国人口加权供暖度日（近7日合计）','°F·日','weekly','NOAA CPC',CPC,10),
 'cooling_degree_days':('gas_weather','美国人口加权制冷度日（近7日合计）','°F·日','weekly','NOAA CPC',CPC,10),
 'ttf_weekly':('global_lng','欧洲TTF周均价格','USD/MMBtu','weekly','EIA WNGSR Supplement',LNG_SUPPLEMENT+'bullets_lng_2.html',21),
 'jkm_weekly':('global_lng','亚洲JKM周均价格','USD/MMBtu','weekly','EIA WNGSR Supplement',LNG_SUPPLEMENT+'bullets_lng_2.html',21),
 'silver_industrial':('silver_industry','全球白银工业需求','百万盎司','annual','Silver Institute / Metals Focus',SILVER,550),
 'silver_photovoltaic':('silver_industry','全球白银光伏需求','百万盎司','annual','Silver Institute / Metals Focus',SILVER,550),
 'silver_mine':('silver_supply','全球白银矿产供给','百万盎司','annual','Silver Institute / Metals Focus',SILVER,550),
 'silver_recycling':('silver_supply','全球白银回收供给','百万盎司','annual','Silver Institute / Metals Focus',SILVER,550),
}
GROUPS={'h10':['broad_usd'],'h15':['real_yield_10y','nominal_yield_10y','effective_fed_funds'],
        'wgc':['central_bank_net_purchases','jewellery_consumption','bar_coin_demand','gold_etf_flows'],
        'safe':['china_gold_reserves'],
        'oil':['crude_stocks','gasoline_stocks','distillate_stocks','us_crude_production','us_crude_imports','us_crude_exports','refinery_inputs'],
        'gas_storage':['gas_storage'],'gas_prod':['dry_gas_production'],'gas_lng':['lng_exports'],
        'weather':['heating_degree_days','cooling_degree_days'],'lng_prices':['ttf_weekly','jkm_weekly'],
        'silver':['silver_industrial','silver_photovoltaic','silver_mine','silver_recycling']}
INSTRUMENTS={driver:symbols for drivers,symbols in [
    (('usd_real_yield','fed_policy'),METALS),(('gold_demand',),{'XAUUSDT'}),
    (('silver_industry','silver_supply'),{'XAGUSDT'}),(('inventory','oil_supply'),{'CLUSDT','BZUSDT'}),
    (('gas_inventory','us_gas_supply','us_lng','gas_weather','global_lng'),{'NATGASUSDT'})] for driver in drivers}


def safe_data_url(url):
    try:
        p=urlsplit(url)
        if p.scheme=='https' and p.hostname in HOSTS and not p.username and not p.password and p.port in (None,443):return url
    except (ValueError,TypeError):pass
    return None


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not safe_data_url(newurl) or urlsplit(newurl).hostname!=urlsplit(req.full_url).hostname:raise ValueError('数据源跳转异常')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(url):
    if not safe_data_url(url):raise ValueError('数据源不在允许范围')
    started=time.monotonic();limit=20_000_000 if urlsplit(url).hostname=='silverinstitute.org' and url.lower().endswith('.pdf') else MAX_BYTES
    with build_opener(_Redirect()).open(Request(url,headers={'User-Agent':'CommodityAnalysis/3.0 (personal research)'}),timeout=12) as response:
        chunks=[];size=0
        while True:
            chunk=response.read(65536)
            if not chunk:break
            chunks.append(chunk);size+=len(chunk)
            if size>limit or time.monotonic()-started>25:raise ValueError('数据响应超出限制')
    return b''.join(chunks)


class Document(HTMLParser):
    def __init__(self,data):
        super().__init__();self.tables=[];self.table=None;self.row=None;self.cell=None;self.links=[];self.text=[]
        if len(data)>MAX_BYTES:raise ValueError('数据响应过大')
        self.feed(data.decode('utf-8-sig',errors='strict'))
    def handle_starttag(self,tag,attrs):
        if tag=='a':
            url=dict(attrs).get('href')
            if url:self.links.append(url)
        if tag=='table':self.table=[]
        elif tag=='tr':self.row=[]
        elif tag in ('td','th'):self.cell=[]
        elif tag=='br' and self.cell is not None:self.cell.append(' ')
    def handle_data(self,data):
        self.text.append(data)
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            if self.row is not None:self.row.append(' '.join(''.join(self.cell).split()))
            self.cell=None
        elif tag=='tr' and self.row is not None:
            if self.table is not None:self.table.append(self.row)
            self.row=None
        elif tag=='table' and self.table is not None:self.tables.append(self.table);self.table=None


def number(value):
    value=value.replace(',','').replace('−','-').strip()
    if not re.fullmatch(r'-?\d+(?:\.\d+)?',value):raise ValueError('统计值缺失或格式不符')
    result=float(value)
    if not math.isfinite(result) or abs(result)>1e9:raise ValueError('统计值越界')
    return result


def record(key,points,url,now,published=None):
    spec=SPECS[key];today=datetime.fromtimestamp(now,timezone.utc).date()
    if len(points)<2:raise ValueError('不足两个可比较观测值')
    if len({p[0] for p in points})!=len(points):raise ValueError('统计日期重复')
    points=sorted(points)
    if any(date.fromisoformat(d)>today for d,_ in points):raise ValueError('统计日期在未来')
    if published and (date.fromisoformat(published)>today or points[-1][0]>published):raise ValueError('发布日与统计期不符')
    if any(type(v) not in (int,float) or not math.isfinite(v) for _,v in points):raise ValueError('统计值无效')
    current,previous=points[-1],points[-2]
    if spec[2]=='%' and any(not -20<=v<=40 for _,v in points):raise ValueError('收益率超出校验范围')
    if key in ('broad_usd','china_gold_reserves') and any(v<=0 for _,v in points):raise ValueError('指数或储备值无效')
    delta=current[1]-previous[1]
    change=delta*100 if spec[2]=='%' else delta
    return dict(id=key,driver=spec[0],label=spec[1],unit=spec[2],frequency=spec[3],source=spec[4],url=url,
                observation_date=current[0],value=current[1],previous_date=previous[0],previous_value=previous[1],
                change=round(change,6),change_unit='基点' if spec[2]=='%' else spec[2],
                change_percent=round(delta/previous[1]*100,6) if previous[1]>0 else None,
                published_date=published,fetched_at=now,last_checked=now,error='',status='ok',
                year_ago_value=dict(points).get(str(int(current[0][:4])-1)+current[0][4:]) if spec[3]=='quarterly' else None)


def release_date(doc):
    match=re.search(r'Release [Dd]ate:\s*([A-Za-z]+ \d{1,2}, \d{4})',' '.join(doc.text))
    if not match:raise ValueError('缺少发布日')
    return datetime.strptime(match[1],'%B %d, %Y').date()


def parse_h10(data,now):
    doc=Document(data);release=release_date(doc)
    table=next((t for t in doc.tables if t and t[0][:2]==['COUNTRY','CURRENCY']),None)
    if not table:raise ValueError('H.10表结构变更')
    days=[]
    for value in table[0][2:]:
        stamp=datetime.strptime(value.replace('.','')+' '+str(release.year),'%b %d %Y').date()
        if stamp>release:stamp=stamp.replace(year=stamp.year-1)
        days.append(stamp.isoformat())
    row=next(r for r in table if r and re.fullmatch(r'1\)\s*BROAD',r[0]))
    if row[1]!='JAN06=100' or len(row)!=len(days)+2:raise ValueError('H.10单位或列不符')
    points=[(d,number(v)) for d,v in zip(days,row[2:]) if v not in ('ND','n.a.','')]
    return [record('broad_usd',points,H10,now,release.isoformat())]


def parse_h15(data,now):
    doc=Document(data);release=release_date(doc)
    if 'Yields in percent per annum' not in ' '.join(doc.text):raise ValueError('H.15收益率单位不符')
    table=next((t for t in doc.tables if t and t[0] and t[0][0]=='Instruments'),None)
    if not table:raise ValueError('H.15表结构变更')
    days=[datetime.strptime(re.sub(r'\s+','',v),'%Y%b%d').date().isoformat() for v in table[0][1:]]
    found={};section=None
    for row in table[1:]:
        if not row:continue
        label=row[0]
        if label.startswith('Nominal'):section='nominal'
        elif label.startswith('Inflation indexed'):section='real'
        key=None
        if label.startswith('Federal funds (effective)'):key='effective_fed_funds'
        elif label=='10-year':key={'nominal':'nominal_yield_10y','real':'real_yield_10y'}.get(section)
        if key:
            if key in found or len(row)!=len(days)+1:raise ValueError('H.15列数或行重复')
            points=[(d,number(v)) for d,v in zip(days,row[1:]) if v not in ('ND','n.a.','')]
            found[key]=record(key,points,H15,now,release.isoformat())
    if set(found)!=set(GROUPS['h15']):raise ValueError('H.15指标不完整')
    return list(found.values())


def parse_wgc(data,url,now):
    doc=Document(data)
    table=next((t for t in doc.tables if t and sum(bool(re.fullmatch(r"Q[1-4]['’]\d{2}",v)) for v in t[0])>=2),None)
    if not table:raise ValueError('黄金需求统计表未找到')
    if table[0][0]!='Tonnes' and 'Table 1: Quarterly gold supply and demand by sector, tonnes' not in ' '.join(doc.text):raise ValueError('黄金需求统计单位不符')
    columns=[]
    for i,value in enumerate(table[0]):
        m=re.fullmatch(r"Q([1-4])['’](\d{2})",value)
        if m:
            year=2000+int(m[2]);month=int(m[1])*3
            columns.append((i,date(year,month,calendar.monthrange(year,month)[1]).isoformat()))
    names={'Central Banks & Other inst.':'central_bank_net_purchases','Jewellery Consumption':'jewellery_consumption',
           'Total Bar and Coin':'bar_coin_demand','ETFs & Similar Products':'gold_etf_flows'}
    text=' '.join(doc.text);pub=re.search(r'\b(\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December), \d{4})\b',text)
    published=datetime.strptime(pub[1],'%d %B, %Y').date().isoformat() if pub else None
    found={}
    for row in table[1:]:
        if row and row[0] in names:
            if len(row)!=len(table[0]):raise ValueError('黄金需求统计列不符')
            key=names[row[0]]
            if key in found:raise ValueError('黄金需求行重复')
            found[key]=record(key,[(d,number(row[i])) for i,d in columns],url,now,published)
    if set(found)!=set(GROUPS['wgc']):raise ValueError('黄金需求分类缺失')
    return list(found.values())


def parse_safe(data,url,now):
    doc=Document(data)
    for table in doc.tables:
        header=next((r for r in table if sum(bool(re.fullmatch(r'20\d{2}\.\d{2}',c)) for c in r)>=2),None)
        quantities=next((r for r in table if sum('万盎司' in c for c in r)>=2),None)
        if not header or not quantities:continue
        months=[c for c in header if re.fullmatch(r'20\d{2}\.\d{2}',c)]
        cells=[c for c in quantities if '万盎司' in c or not c.strip()]
        # The USD and SDR columns duplicate the SAME physical balance. They
        # must agree; neither market value nor FX valuation is a gold purchase.
        if len(cells)==2*len(months)+1 and cells[0]=='':cells=cells[1:]
        if len(cells)!=2*len(months):raise ValueError('黄金储备列数不符')
        points=[]
        for month,left,right in zip(months,cells[::2],cells[1::2]):
            if not left and not right:continue
            if left!=right or not re.fullmatch(r'[\d,.]+万盎司',left):raise ValueError('黄金储备重复单位列不一致')
            year,m=map(int,month.split('.'))
            points.append((date(year,m,calendar.monthrange(year,m)[1]).isoformat(),number(left.replace('万盎司',''))))
        return [record('china_gold_reserves',points,url,now)]
    raise ValueError('未找到实物黄金储备量')


def series_note(row):
    key=row['id'];delta=row.get('change')
    if row.get('status')!='ok':return '数据缺失、读取失败或超出统计时效；保留原值但不据此生成当前方向。'
    if key=='broad_usd':return '较前一观测日美元'+('走强' if delta>0 else '走弱' if delta<0 else '持平')+'；广义贸易加权指数，不是ICE DXY，也不是实时汇率。'
    if key=='real_yield_10y':return '实际收益率'+('上升，持有黄金的机会成本因素偏不利' if delta>0 else '下降，机会成本因素偏支持黄金' if delta<0 else '持平，未给出增量利率方向')+'；这只是单一因素。'
    if key=='nominal_yield_10y':return '名义收益率仅作对照，不当作实际收益率。'
    if key=='effective_fed_funds':return '实际成交的隔夜有效利率，不等于联储目标区间，也不代表未来政策决定。'
    if key=='china_gold_reserves':return '实物储备余额的月度变化；不把美元估值上涨当成购金，也不把储备变化直接等同市场净买入。'
    if key=='central_bank_net_purchases':return '全球官方部门季度净'+('购金' if row['value']>0 else '售金' if row['value']<0 else '变化为零')+'；属于低频需求背景，可能修订，不是当前买入信号。'
    if row['driver']=='inventory':return '周度实际库存及与上周的变化；未接入市场一致预期，不把增减直接称为超预期。美国库存对布伦特仅是跨市场参考。'
    if row['driver']=='oil_supply':return '美国周度供给估计，不等于OPEC或全球供给；产量、进口、出口与炼厂需求要合并判断。'
    if key=='gas_storage':return '本土48州工作气库存与上周变化；历史均值不是市场预期，重分类时库存变化也不等于实际注采流量。'
    if key in ('dry_gas_production','lng_exports'):return '原始月度百万立方英尺除以1000及当月天数得到Bcf/日，按日均比较月份；月度数据存在公布滞后。'
    if row['driver']=='gas_weather':return 'NOAA人口加权CONUS实际度日，近7日与前7日比较；不是天气预报或天然气用量，不能将单点城市温度代表全美。'
    if row['driver']=='global_lng':return 'EIA公布的海外周均价格；前值按原文明示的周变化反算。TTF/JKM不等于Henry Hub，也不是美国出口利润或即时套利信号。'
    if row['frequency']=='annual':return 'Silver Institute / Metals Focus年度估计；使用历史年度列并排除F预测列，同一报告中的前值可能修订，不是盘中供需。'
    return '季度实物需求量/ETF净流量及环比变化；保留统计期，不能外推为当前交易日需求。'


class Fundamentals:
    def __init__(self,fetcher=download,clock=time.time,groups=None):
        self.fetcher=fetcher;self.clock=clock;self.lock=threading.Lock();self.rows={};self.due={}
        self.groups=tuple(GROUPS if groups is None else groups)
    def _fetch(self,group,now):
        if group not in ('h10','h15','wgc','safe'):
            from .commodity_sources import fetch_commodity
            return fetch_commodity(group,self.fetcher,now)
        if group=='h10':return parse_h10(self.fetcher(H10),now)
        if group=='h15':return parse_h15(self.fetcher(H15),now)
        index=WGC if group=='wgc' else SAFE;doc=Document(self.fetcher(index))
        links=[urljoin(index,u) for u in doc.links]
        if group=='wgc':
            matches=[(int(m[2]),int(m[1]),u) for u in links for m in [re.fullmatch(re.escape(WGC)+r'/gold-demand-trends-q([1-4])-(20\d{2})',u)] if m]
            if not matches:raise ValueError('未找到最新季度报告')
            url=max(matches)[2]
            return parse_wgc(self.fetcher(url),url,now)
        # Only links in the official reserve-assets index can select a report.
        candidates=[u for u in links if re.fullmatch(r'https://www.safe.gov.cn/safe/20\d{2}/\d{4}/\d+\.html',u)]
        if not candidates:raise ValueError('未找到储备资产报告')
        url=max(candidates)
        return parse_safe(self.fetcher(url),url,now)
    def refresh(self):
        now=self.clock()
        with self.lock:groups=[g for g in self.groups if now>=self.due.get(g,0)]
        def fetch(group):
            try:return group,self._fetch(group,now),''
            except Exception:return group,[],'数据源读取或结构校验失败'
        with ThreadPoolExecutor(max_workers=4) as pool:
            # Publish completed sources immediately; one slow annual report
            # must not hold back already available weekly inventory data.
            for future in as_completed([pool.submit(fetch,g) for g in groups]):
                group,rows,error=future.result()
                with self.lock:
                    self.due[group]=now+(300 if error else 86400 if group=='silver' else 1800 if group in ('h10','h15','oil','gas_storage','lng_prices') else 21600)
                    if not error:
                        for row in rows:self.rows[row['id']]=row
                    else:
                        for key in GROUPS[group]:
                            old=self.rows.get(key,{})
                            self.rows[key]=dict(old,id=key,error=error,last_checked=now)
        return len(groups)
    def snapshot(self,symbol):
        now=self.clock();today=datetime.fromtimestamp(now,timezone.utc).date();rows=[]
        with self.lock:stored=copy.deepcopy(self.rows)
        keys=[k for k,s in SPECS.items() if symbol in INSTRUMENTS[s[0]]]
        for key in keys:
            spec=SPECS[key];row=stored.get(key,{})
            value=dict(id=key,driver=spec[0],label=spec[1],unit=spec[2],frequency=spec[3],source=spec[4],url=spec[5])
            value.update(row)
            age=(today-date.fromisoformat(row['observation_date'])).days if row.get('observation_date') else None
            max_fetch_age=172800 if spec[3]=='annual' else 43200 if spec[3] in ('monthly','quarterly') or spec[0]=='gas_weather' else 7200
            overdue=bool(row.get('fetched_at') and now-row['fetched_at']>max_fetch_age)
            value['status']='unavailable' if not row.get('observation_date') else 'stale' if row.get('error') or age>spec[6] or age<0 or overdue else 'ok'
            value['note']=series_note(value);rows.append(value)
        return dict(symbol=symbol,metrics=rows,checked=max((r.get('last_checked',0) for r in rows),default=0),
                    scope='按日、周、月、季、年分别标记原始统计；事件数据单独显示。缺失、失败与过期不视为中性或已核验。')


def model_metrics(snapshot):
    """Canonical public-only records; observation values, not poll timestamps."""
    fields=('id','driver','label','unit','frequency','source','url','observation_date','value','previous_date','previous_value',
            'change','change_unit','change_percent','year_ago_value','published_date','note')
    return [{k:row.get(k) for k in fields} for row in snapshot.get('metrics',[]) if row.get('status')=='ok']


def metric_version(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def validate_metrics(rows,symbol,now):
    """Rebuild public metrics; callers cannot inject extra fields or change units."""
    if not isinstance(rows,list) or len(rows)>len(SPECS):raise ValueError('统计指标输入无效')
    result=[];seen=set();today=datetime.fromtimestamp(now,timezone.utc).date()
    for row in rows:
        if not isinstance(row,dict) or row.get('id') not in SPECS:raise ValueError('未知统计指标')
        key=row['id'];spec=SPECS[key];url=row.get('url')
        if key in seen or symbol not in INSTRUMENTS[spec[0]]:raise ValueError('统计指标与品种不符')
        seen.add(key)
        if not safe_data_url(url) or urlsplit(url).hostname!=urlsplit(spec[5]).hostname or len(url)>500:raise ValueError('统计来源不符')
        if spec[5] not in (WGC,SAFE,CPC,SILVER) and url!=spec[5]:raise ValueError('统计系列来源不符')
        if spec[5]==CPC:
            kind='Heating' if key=='heating_degree_days' else 'Cooling'
            if not re.fullmatch(re.escape(CPC)+r'20\d{2}/Population\.'+kind+r'\.txt',url):raise ValueError('度日系列来源不符')
        rebuilt=record(key,[(row['previous_date'],row['previous_value']),(row['observation_date'],row['value'])],url,now,row.get('published_date'))
        if rebuilt['observation_date']!=row['observation_date'] or not 0<=(today-date.fromisoformat(rebuilt['observation_date'])).days<=spec[6]:raise ValueError('统计日期或时效不符')
        for field in ('driver','unit','frequency','change','change_unit'):
            if row.get(field)!=rebuilt[field]:raise ValueError('统计单位或变化值不符')
        yearago=row.get('year_ago_value')
        if yearago is not None and (type(yearago) not in (int,float) or not math.isfinite(yearago)):raise ValueError('同比数据无效')
        rebuilt['year_ago_value']=yearago if spec[3]=='quarterly' else None
        rebuilt['note']=series_note(rebuilt)
        result.extend(model_metrics({'metrics':[rebuilt]}))
    return sorted(result,key=lambda row:row['id'])
