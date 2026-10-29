"""Read-only analysis workers; independent from the execution engine."""
import copy
from concurrent.futures import ThreadPoolExecutor
import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from .api import Binance
from .core import SYMBOLS, ema, number, signal, validate_candles
from .news import NewsService, unavailable
from .ai_service import AIService
from .fundamentals import Fundamentals, model_metrics
from .news_risk import DRIVERS, RISK_DRIVERS
from .news import safe_url
from .ai_policy import project_ai
from .timeframes import timeframe, candles_for
from .ai_schedule import work_scope, major_events


def factor_events(news,now):
    """Read-only event projection: source availability is not fact validation."""
    healthy=not news.get('error') and news.get('coverage',{}).get('healthy') is True and 0<=now-news.get('checked',0)<=90
    result=[]
    for key,_ in DRIVERS[news['symbol']]:
        if key not in ('geopolitics','shipping','global_lng','gas_weather','silver_supply','oil_supply','us_lng'):continue
        records=[]
        for risk in news.get('decision',{}).get('risks',[]):
            if key not in RISK_DRIVERS.get(risk.get('category'),set()) or not safe_url(risk.get('url')):continue
            records.append({field:copy.deepcopy(risk.get(field)) for field in
                ('subject','category','status','source','url','published','first_seen','last_seen','entry_gate','cooldown_until')})
        records.sort(key=lambda r:r.get('last_seen') or 0,reverse=True)
        result.append(dict(key=key,status='connected' if healthy else 'unavailable',checked=news.get('checked'),
                           records=records[:6],total=len(records)))
    return result

QUERIES = {
    'XAUUSDT': 'gold prices Fed dollar', 'XAGUSDT': 'silver prices precious metals',
    'CLUSDT': 'WTI crude oil prices', 'BZUSDT': 'Brent crude oil prices',
    'NATGASUSDT': 'natural gas prices storage LNG weather',
}

def kline_analysis(rows, now, interval="1h"):
    validate_candles(rows)
    if len(rows) < 80 or int(rows[-1][6]) < now - 15000:
        raise ValueError('K线不足或已过期')
    closed = signal(rows, now, interval)
    values = [number(r[4]) for r in rows]
    fast, slow = ema(values, 20), ema(values, 60)
    return dict(interval=interval, price=values[-1], fast=fast, slow=slow,
        trend='偏多' if fast > slow else '偏空' if fast < slow else '横盘',
        upper=closed.get('upper'), lower=closed.get('lower'), atr=closed.get('atr'),
        confirmed=closed['reason'], side=closed['side'], candle=int(rows[-1][0]),
        rows=[dict(time=int(r[0]), close=number(r[4])) for r in rows[-80:]])

def driver(title):
    t = title.lower()
    if any(re.search(r'\b'+re.escape(w)+r'\b', t) for w in ('hormuz', 'war', 'sanction', 'sanctions', 'iran', 'supply disruption')):
        return '供应与地缘风险：关注供应受阻或恢复的实际进展，消息反转可放大波动。'
    if any(w in t for w in ('fed', 'rate', 'dollar', 'inflation')):
        return '利率与美元因素：需结合美元和实际利率走势判断，标题本身不足以确定多空。'
    if any(w in t for w in ('storage', 'inventory', 'inventories', 'eia')):
        return '库存因素：重点比较公布值与市场预期，不能只看库存增减方向。'
    if any(w in t for w in ('weather', 'cold', 'heat', 'lng')):
        return '需求因素：关注天气预报变化及LNG需求，需核实影响时段与地区。'
    return '价格与市场动态：需核实正文、事件时间及价格是否已反映消息。'

def parse_news(data, now):
    root = ET.fromstring(data)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('无效新闻源')
    items, seen = [], set()
    for item in root.findall('./channel/item'):
        title = (item.findtext('title') or '').strip()[:500]
        url = (item.findtext('link') or '').strip()
        if not title or title in seen or urlparse(url).scheme != 'https' or urlparse(url).hostname != 'news.google.com':
            continue
        try: published = parsedate_to_datetime(item.findtext('pubDate')).timestamp()
        except (ValueError, TypeError, AttributeError): continue
        if not 0 <= now-published <= 48*3600: continue
        seen.add(title)
        items.append(dict(title=title, url=url, published=published,
            source=(item.findtext('source') or 'Google News 索引')[:120], analysis=driver(title)))
    return sorted(items, key=lambda x:x['published'], reverse=True)[:6]

class Analysis:
    def __init__(self, engine, state_path=None, ai=None, fundamentals=None):
        self.engine = engine
        self.api = Binance(engine.api.demo)
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cache = {'kline': {}, 'news': {}}
        self.news_by_symbol = {}
        self.klines_by_symbol = {}
        self.markets_by_symbol = {}
        self.ai_cursor = 0
        self.news = NewsService(state_path=state_path)
        self.ai = ai if ai is not None else AIService()
        self.ai.scope_provider = lambda: work_scope(self.engine.s)
        self.fundamentals = fundamentals if fundamentals is not None else Fundamentals()
        self.threads = []
    def symbol(self):
        # Atomic scalar read avoids waiting behind execution network calls.
        return self.engine.s['symbol']
    def interval(self):
        return self.engine.s.get("interval","1h")
    def snapshot(self, symbol=None):
        symbol = self.symbol() if symbol is None else symbol
        if symbol not in SYMBOLS:
            raise ValueError('不支持的品种')
        with self.lock:
            kline = self.klines_by_symbol.get(symbol, self.cache['kline'])
            result = {'kline': copy.deepcopy(kline) if kline.get('symbol') == symbol else {'symbol': symbol},
                      'news': copy.deepcopy(self.news_by_symbol.get(symbol, {'symbol': symbol}))}
            if kline.get('interval','1h') != self.interval():
                result['kline'] = {'symbol':symbol,'interval':self.interval(),'error':'周期已修改，等待新周期K线'}
            result['news']['decision'] = self._decision(symbol, time.time())
        result['fundamentals'] = self.fundamentals.snapshot(symbol)
        result['ai'] = self.ai.snapshot(symbol, current_metrics=sorted(model_metrics(result['fundamentals']),key=lambda row:row['id']))
        result['news']['decision']['ai'] = project_ai(result['ai'], symbol, time.time())
        result['fundamentals']['factors'] = [dict(key=key,label=label) for key,label in DRIVERS[symbol]]
        result['fundamentals']['events'] = factor_events(result['news'],time.time())
        return result
    def _decision(self, symbol, now):
        news = self.news_by_symbol.get(symbol, {})
        value = news.get('decision', {})
        def blocked(reason):
            result = unavailable(symbol, now, reason)
            # A data outage expires execution permission, not the risk history.
            result.update(risks=copy.deepcopy(value.get('risks', [])),
                          drivers=copy.deepcopy(value.get('drivers', [])))
            return result
        if news.get('symbol') != symbol or value.get('symbol') != symbol:
            return blocked('当前品种新闻尚未核验，暂停新仓')
        if news.get('error') or not news.get('coverage', {}).get('healthy'):
            reason = news.get('coverage', {}).get('risk_state_error')
            return blocked(reason or value.get('reason') or '新闻源连接失败或覆盖不足，暂停新仓')
        if not value.get('asof', now+1) <= now < value.get('expires', 0):
            return blocked('新闻证据已过期，暂停新仓')
        return copy.deepcopy(value)
    def decision(self, symbol):
        """Nonblocking engine input: network work is isolated in the news worker."""
        with self.lock:
            value = self._decision(symbol, time.time())
        metrics = sorted(model_metrics(self.fundamentals.snapshot(symbol)), key=lambda row:row['id'])
        value['ai'] = project_ai(self.ai.snapshot(symbol, current_metrics=metrics), symbol, time.time())
        return value
    def update(self, kind, symbol, data=None, error='', rows=None, closed_signal=None):
        with self.lock:
            old = self.news_by_symbol.get(symbol, {}) if kind == 'news' else self.klines_by_symbol.get(symbol,{})
            value = dict(old) if old.get('symbol') == symbol else {}
            value.update(symbol=symbol, checked=time.time(), error=error)
            if data is not None:
                if kind == 'news':
                    value.update(data=data['items'], decision=data['decision'], coverage=data['coverage'], updated=time.time())
                else:
                    value.update(data=data, interval=data.get('interval','1h'), updated=time.time())
                    if rows is not None:value.update(rows=rows,signal=closed_signal)
            if kind == 'news' and error:
                previous = value.get('decision', {})
                value['decision'] = dict(unavailable(symbol, time.time(), '新闻更新失败，暂停新仓'),
                    risks=copy.deepcopy(previous.get('risks', [])), drivers=copy.deepcopy(previous.get('drivers', [])))
            if kind == 'news':
                self.news_by_symbol[symbol] = value
            else:
                self.klines_by_symbol[symbol] = value
            self.cache[kind] = value
        # Model input is copied outside the analysis lock. submit only queues
        # public text; all network/model work is isolated in ai_loop.
        if kind == 'news':
            try:
                metrics=model_metrics(self.fundamentals.snapshot(symbol))
                healthy=not error and bool((data or {}).get('coverage', {}).get('healthy'))
                self.ai.submit(symbol, (data or {}).get('_ai_articles', []) if healthy else [], value['checked'],
                               healthy=healthy or bool(metrics), fundamentals=metrics,
                               major_events=major_events(data or {}, value['checked']) if healthy else [])
            except Exception:
                # Optional AI failures must never become a rule-news outage.
                self.ai.input_error(symbol)
    def fetch(self, kind, symbol):
        if symbol not in SYMBOLS: raise ValueError('不支持的品种')
        if kind == 'kline':
            return kline_analysis(candles_for(self.api,symbol,self.interval()), int(time.time()*1000), self.interval())
        return self.news.fetch(symbol)

    def candidates(self):
        with self.lock:
            result = copy.deepcopy(self.markets_by_symbol)
            for symbol in SYMBOLS:
                k = self.klines_by_symbol.get(symbol,{})
                value = result.setdefault(symbol,dict(symbol=symbol,error='等待行情'))
                value.update(kline_checked=k.get('updated',0),signal=k.get('signal',{}),rows=k.get('rows',[]))
                if k.get('error'): value['error']=k['error']
                if k.get('interval','1h') != self.interval(): value['error']='等待当前周期K线'
            return result

    def kline_once(self,symbol):
        interval=self.interval()
        try:
            rows=candles_for(self.api,symbol,interval);now=int(time.time()*1000)
            data,sig=kline_analysis(rows,now,interval),signal(rows,now,interval)
            if interval!=self.interval():return
            self.update('kline',symbol,data,rows=rows,closed_signal=sig)
        except Exception:
            self.update('kline',symbol,error='K线读取失败，暂不参与自动选品')

    def market_once(self,symbol):
        try:value=dict(symbol=symbol,quote=self.api.market(symbol),book=self.api.order_book(symbol),error='')
        except Exception:value=dict(symbol=symbol,error='行情或盘口读取失败，暂不参与自动选品')
        value['checked']=time.time()
        with self.lock:self.markets_by_symbol[symbol]=value

    def kline_cycle(self):
        with ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(self.kline_once,SYMBOLS))

    def market_cycle(self):
        with ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(self.market_once,SYMBOLS))

    def symbol_worker(self,kind,symbol,interval):
        # Exactly one request chain per symbol/feed; a slow symbol cannot
        # prevent another from beginning its next refresh or spawn a backlog.
        fetch=self.market_once if kind=='market' else self.kline_once
        while not self.done.is_set():
            start=time.monotonic();fetch(symbol)
            self.done.wait(max(.05,interval-(time.monotonic()-start)))

    def market_loop(self):
        while not self.done.is_set():
            start=time.monotonic();self.market_cycle()
            self.done.wait(max(.25,5-(time.monotonic()-start)))
    def news_cycle(self):
        # Scan every allowed commodity without changing the trading settings.
        # NewsService reuses bounded feed/article caches across these reads.
        current = self.symbol()
        for symbol in [current] + [s for s in SYMBOLS if s != current]:
            if self.done.is_set():
                break
            try:
                self.update('news', symbol, self.fetch('news', symbol))
            except Exception:
                self.update('news', symbol, error='新闻核验失败，暂停新仓；保留持续风险记录')

    def news_worker(self, symbol):
        # Each symbol publishes independently; a slow source must not queue the
        # other commodities behind it. NewsService coalesces shared downloads.
        while not self.done.is_set():
            start=time.monotonic()
            try:self.update('news',symbol,self.fetch('news',symbol))
            except Exception:self.update('news',symbol,error='新闻核验失败，保留持续风险记录')
            self.done.wait(max(.25,10-(time.monotonic()-start)))

    def loop(self, kind, interval):
        previous = None; due = 0
        while not self.done.is_set():
            symbol = self.symbol()
            if symbol != previous or time.monotonic() >= due:
                start = time.monotonic(); previous = symbol
                if kind == 'news':
                    self.news_cycle()
                else:
                    self.kline_cycle()
                due = start + interval
            self.done.wait(max(.05,min(.25,due-time.monotonic())))
    def start(self):
        for kind,interval in (('market',5),('kline',1)):
            for symbol in SYMBOLS:
                t=threading.Thread(target=self.symbol_worker,args=(kind,symbol,interval),daemon=True)
                t.start();self.threads.append(t)
        t=threading.Thread(target=self.fundamentals_loop,daemon=True)
        t.start();self.threads.append(t)
        for symbol in SYMBOLS:
            t=threading.Thread(target=self.news_worker,args=(symbol,),daemon=True)
            t.start();self.threads.append(t)
        for _ in range(2):
            t = threading.Thread(target=self.ai_loop, daemon=True)
            t.start();self.threads.append(t)
    def fundamentals_loop(self):
        while not self.done.is_set():
            self.fundamentals.refresh()
            self.done.wait(30)
    def ai_loop(self):
        while not self.done.is_set():
            try:
                if not self.ai_cycle():
                    self.ai.run_comparison_once()
            except Exception:
                self.ai.input_error(self.symbol())
            self.done.wait(.25)
    def ai_cycle(self):
        # Always give held/pending exposure first access to a free model slot.
        # AIService rechecks admission at reservation time, including direct calls.
        state=self.engine.s
        exposure=state.get('position') or state.get('pending')
        if exposure:
            focus=exposure.get('symbol',self.symbol())
            if focus in SYMBOLS and self.ai.run_once(focus):return True
        # Round-robin prevents frequent revisions in one commodity starving others.
        if self.engine.s.get('selection_mode','manual') != 'auto':
            return self.ai.run_once(self.symbol())
        for offset in range(len(SYMBOLS)):
            index=(self.ai_cursor+offset)%len(SYMBOLS)
            if self.ai.run_once(SYMBOLS[index]):
                self.ai_cursor=(index+1)%len(SYMBOLS)
                return True
        return False
    def stop(self):
        self.done.set()
        self.ai.stop()

    def join(self):
        for thread in self.threads:
            thread.join()
