"""Bounded, read-only news evidence. Unverified text can veto, never authorize.

This is an explicit event rulebook, not general language understanding. The
engine must additionally require closed-candle price confirmation. A publisher
timestamp is not automatically treated as the time of the reported event.
"""
import copy
import hashlib
import html
import json
import re
import time
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .news_risk import (RiskLedger, relevant_context, risk_observations, driver_summary,
                        UNCERTAIN, HISTORICAL, canonical_origin, direct_assertion,
                        PATTERNS, RULE_DRIVERS, RISK_DRIVERS, DRIVERS, sentence_fingerprint,
                        retrospective_reference, select_factor_articles)

METALS = {'XAUUSDT', 'XAGUSDT'}
OIL = {'CLUSDT', 'BZUSDT'}
SYMBOLS = METALS | OIL | {'NATGASUSDT'}
TTL = 75
EVENT_AGE = 6 * 3600
ARTICLE_AGE = 48 * 3600
MAX_BYTES = 1_500_000
FEEDS = {
    'Fed': 'https://www.federalreserve.gov/feeds/press_monetary.xml',
    'EIA': 'https://www.eia.gov/rss/todayinenergy.xml',
    'BBC': 'https://feeds.bbci.co.uk/news/business/rss.xml',
    'Guardian': 'https://www.theguardian.com/business/rss',
    'BBC World': 'https://feeds.bbci.co.uk/news/world/rss.xml',
    'Guardian World': 'https://www.theguardian.com/world/rss',
    'Guardian Middle East': 'https://www.theguardian.com/world/middleeast/rss',
}
FEED_PUBLISHERS = {'BBC World':'BBC','Guardian World':'Guardian','Guardian Middle East':'Guardian'}
DOMAINS = {
    'www.federalreserve.gov': 'Fed', 'federalreserve.gov': 'Fed',
    'www.eia.gov': 'EIA', 'eia.gov': 'EIA',
    'www.bbc.com': 'BBC', 'www.bbc.co.uk': 'BBC', 'feeds.bbci.co.uk': 'BBC',
    'www.theguardian.com': 'Guardian',
}


def clean(value, limit=60000):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))).strip()[:limit]


def safe_url(url):
    try:
        p = urlparse(url)
        if p.scheme != 'https' or p.hostname not in DOMAINS or p.username or p.password or p.port not in (None, 443):
            return None
        return urlunparse((p.scheme, p.netloc, p.path, '', p.query, ''))
    except (ValueError, TypeError):
        return None


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl) or DOMAINS[urlparse(newurl).hostname] != DOMAINS.get(urlparse(req.full_url).hostname):
            raise ValueError('新闻跳转不能更换来源')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    if not safe_url(url):
        raise ValueError('非可信新闻网址')
    started = time.monotonic()
    request = Request(url, headers={'User-Agent': 'CommodityAnalysis/2.0 (personal research)',
                                   'Accept': 'application/rss+xml, application/xml, text/xml, text/html, */*;q=0.5'})
    with build_opener(SafeRedirect()).open(request, timeout=12) as response:
        if not safe_url(response.url) or DOMAINS[urlparse(response.url).hostname] != DOMAINS[urlparse(url).hostname]:
            raise ValueError('非可信新闻跳转')
        parts = []; size = 0
        while True:
            part = response.read(16384)
            size += len(part)
            if size > MAX_BYTES or time.monotonic() - started > 16:
                raise ValueError('新闻响应超出大小或时间限制')
            if not part:
                break
            parts.append(part)
        return b''.join(parts)


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, AttributeError):
            return None
    # A timezone-free publication date cannot establish a six-hour event age.
    return dt.timestamp() if dt.tzinfo else None


def relevant(text, symbol):
    t = text.lower()
    if relevant_context(text,symbol):return True
    if symbol in METALS:
        asset = r'\bgold\b' if symbol == 'XAUUSDT' else r'\bsilver\b'
        return bool(re.search(asset + r'|\bfederal reserve\b|\bfomc\b|\bfed\b|\bfederal funds\b', t) or
                    re.search(PATTERNS['usd_real_yield'],text,re.I) or
                    symbol=='XAUUSDT' and re.search(PATTERNS['gold_demand'],text,re.I))
    if symbol in OIL:
        return bool(re.search(r'\bcrude\b|\bwti\b|\bbrent\b|\bopec\b|\boil (?:production|exports?|supply|prices?|inventor|field)', t))
    return bool(re.search(r'\bnatural gas\b|\bhenry hub\b|\bu\.?s\.?\s+lng\b|\bamerican lng\b', t))


def high_impact(text):
    return bool(re.search(r'\b(?:halt\w*|shut\w*|disrupt\w*|restart\w*|resum\w*|restor\w*|attack\w*|sanction\w*|strike\w*|war|hurricane|explosion|inventor\w*|storage|interest rates?|federal funds|rate (?:cut|hike)|fomc|monetary policy)\b', text.lower()))


def parse_feed(data, source, now):
    if len(data) > MAX_BYTES or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('新闻源格式不安全')
    root = ET.fromstring(data)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('无效RSS新闻源')
    result = []; seen = set()
    for node in root.findall('./channel/item')[:150]:
        url = safe_url((node.findtext('link') or '').strip())
        title = clean(node.findtext('title'), 500)
        published = timestamp(node.findtext('pubDate'))
        if not url or DOMAINS.get(urlparse(url).hostname) != source or not title or published is None:
            continue
        if not 0 <= now - published <= ARTICLE_AGE or url in seen:
            continue
        seen.add(url)
        identity = (node.findtext('guid') or '').strip() or url
        news_id = hashlib.sha256((source + '\n' + identity).encode()).hexdigest()[:24]
        result.append(dict(title=title, url=url, published=published, source=source, news_id=news_id,
                           summary=clean(node.findtext('description'), 1000)))
    return result


class ArticleParser(HTMLParser):
    """Extract article paragraphs or JSON-LD articleBody, never navigation text."""
    def __init__(self, primary=False):
        super().__init__(convert_charrefs=True)
        self.primary = primary; self.stack = []; self.paragraphs = []
        self.current = None; self.json_text = None; self.jsons = []
        self.dates = []; self.author = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = attrs.get('property', attrs.get('name', '')).lower()
            if name in ('article:published_time', 'datepublished', 'date', 'dc.date', 'dcterms.created'):
                self.dates.append(attrs.get('content'))
            if name == 'author':
                self.author = clean(attrs.get('content'), 150)
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.json_text = []
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr'):
            self.stack.append(tag)
        if tag == 'p' and not any(t in self.stack for t in ('nav', 'aside', 'footer', 'header', 'script', 'style')):
            if self.primary or 'article' in self.stack:
                self.current = []

    def handle_endtag(self, tag):
        if tag == 'script' and self.json_text is not None:
            try:
                self.jsons.append(json.loads(''.join(self.json_text)))
            except (ValueError, TypeError):
                pass
            self.json_text = None
        if tag == 'p' and self.current is not None:
            self.paragraphs.append(clean(' '.join(self.current)))
            self.current = None
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if self.json_text is not None:
            self.json_text.append(data)
        elif self.current is not None and not any(t in self.stack for t in ('script', 'style', 'nav', 'aside')):
            self.current.append(data)


def article(data, source):
    p = ArticleParser(primary=source in ('Fed', 'EIA'))
    p.feed(data.decode('utf-8', errors='replace'))
    bodies = []; dates = list(p.dates); author = p.author
    def walk(value):
        nonlocal author
        if isinstance(value, list):
            for v in value: walk(v)
        elif isinstance(value, dict):
            kind = value.get('@type', '')
            kinds = kind if isinstance(kind, list) else [kind]
            if any(k in ('NewsArticle', 'Article', 'ReportageNewsArticle') for k in kinds):
                if isinstance(value.get('articleBody'), str): bodies.append(clean(value['articleBody']))
                if value.get('datePublished'): dates.append(value['datePublished'])
                a = value.get('author', '')
                if isinstance(a, dict): a = a.get('name', '')
                if isinstance(a, str): author = clean(a, 150)
            for key, v in value.items():
                if key in ('@graph', 'mainEntity', 'itemListElement'): walk(v)
    for v in p.jsons: walk(v)
    body = max(bodies, key=len) if bodies else clean(' '.join(p.paragraphs))
    parsed_dates = [timestamp(d) for d in dates]
    parsed_dates = [d for d in parsed_dates if d is not None]
    return dict(body=body, article_published=min(parsed_dates) if parsed_dates else None, author=author,
                fulltext=len(body) >= 240)


def event_time(sentence, item, now):
    """A fresh report must explicitly locate the event in time."""
    published = item.get('article_published')
    if published is None or not 0 <= now - published <= EVENT_AGE:
        return None
    if HISTORICAL.search(sentence):
        return None
    date=datetime.fromtimestamp(published,timezone.utc)
    dates=re.findall(r'\b20\d{2}-\d{2}-\d{2}\b',sentence)
    if any(d!=date.strftime('%Y-%m-%d') for d in dates):return None
    weekdays=re.findall(r'\bon (Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b',sentence,re.I)
    if any(day.lower()!=date.strftime('%A').lower() for day in weekdays):return None
    names=re.findall(r'\bon (January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})(?:,? (20\d{2}))?\b',sentence,re.I)
    for month,day,year in names:
        try:
            if datetime.strptime(f'{month} {day} {year or date.year}','%B %d %Y').date()!=date.date():return None
        except ValueError:return None
    # Original FOMC policy releases announce the decision being taken now.
    if item['source'] == 'Fed' and re.search(r'/newsevents/pressreleases/monetary\d{8}[a-z]\.htm', item['url']):
        date = re.search(r'monetary(\d{8})', item['url']).group(1)
        if datetime.fromtimestamp(published, timezone.utc).strftime('%Y%m%d') == date:
            return published
    if re.search(r'\b(?:today|this morning|this afternoon)\b', sentence, re.I):
        return published
    # Explicit ISO event dates are accepted only on the publication's UTC date.
    if dates:return published
    # A named weekday/date must match the original publication's UTC date.
    # Never treat a bare past date elsewhere in a story as a new event.
    if weekdays or names:return published
    return None


def event_rule(sentence, symbol, source):
    """Return a factual supply/policy factor; price confirmation is separate."""
    t = sentence.lower()
    # Consensus is accepted only when the same sentence supplies explicit actual
    # and expected numerical changes, in the same commodity and unit. The parser
    # never substitutes last week's value for consensus or invents expectations.
    inv = re.match(r'(?P<asset>us crude oil inventories|us natural gas storage) (?P<act>rose|fell|increased|decreased) by (?P<actual>\d+(?:\.\d+)?) (?P<unit>million barrels|billion cubic feet) (?:today|on 20\d{2}-\d{2}-\d{2}),? (?:compared with|versus) (?:an? )?(?:expected|forecast) (?P<expact>rise|fall|increase|decrease) of (?P<expected>\d+(?:\.\d+)?) (?P=unit)', t)
    tail=t[inv.end():].replace('according to the release','') if inv else ''
    if inv and direct_assertion(tail):
        oil = inv['asset'] == 'us crude oil inventories' and inv['unit'] == 'million barrels'
        gas = inv['asset'] == 'us natural gas storage' and inv['unit'] == 'billion cubic feet'
        if (oil and symbol in OIL) or (gas and symbol == 'NATGASUSDT'):
            actual = float(inv['actual']) * (-1 if inv['act'] in ('fell', 'decreased') else 1)
            expected = float(inv['expected']) * (-1 if inv['expact'] in ('fall', 'decrease') else 1)
            if actual != expected:
                rule = ('us_oil_inventory_surprise' if oil else 'us_gas_storage_surprise') + f'_{actual:g}_{expected:g}'
                return ('BUY' if actual < expected else 'SELL', rule, f'同文公布变化 {actual:g}，预期 {expected:g}（{inv["unit"]}）；库存相对预期偏低' if actual < expected else f'同文公布变化 {actual:g}，预期 {expected:g}（{inv["unit"]}）；库存相对预期偏高')
    if not direct_assertion(t):
        return None
    if symbol in METALS:
        if source == 'Fed' and re.match(r'(?:the )?committee (?:has )?decided to (?:lower|reduce|raise|increase) the target range for the federal funds rate', t):
            down = bool(re.search(r'decided to (?:lower|reduce)', t))
            return ('BUY' if down else 'SELL', 'fed_rate_cut' if down else 'fed_rate_hike', '已公布的联储利率决定；仍须价格确认')
        if re.match(r'(?:the )?(?:federal reserve|fed) (?:has )?(?:cut|lowered|raised|increased) (?:its |the )?(?:interest rates?|federal funds rate)', t):
            down = bool(re.search(r'(?:fed|reserve) (?:has )?(?:cut|lowered)', t))
            return ('BUY' if down else 'SELL', 'fed_rate_cut' if down else 'fed_rate_hike', '已发生的联储利率调整；仍须价格确认')
    subject = r'(?:(?:libya|libyan|iraq|iraqi|iran|iranian|russia|russian|saudi|kuwait|nigeria|nigerian|venezuela|norway|norwegian|canada|canadian|kazakhstan|us|american) )?(?:crude oil|oil) (?:production|exports?|supply)' if symbol in OIL else r'(?:u\.?s\.?|american) natural gas production'
    # Require the action to be the predicate of the commodity subject. Arbitrary
    # intervening prose can change the actor (e.g. supply rose as attacks stopped).
    auxiliary = r' (?:(?:was|were|is|are|has been|have been) )?'
    if symbol in OIL or symbol == 'NATGASUSDT':
        if re.match(subject + auxiliary + r'(?:halted|stopped|shut down|suspended|disrupted)\b', t):
            return ('BUY', 'oil_supply_loss' if symbol in OIL else 'us_gas_supply_loss', '已发生的供应中断')
        if re.match(subject + auxiliary + r'(?:resumed|restarted|restored)\b', t):
            return ('SELL', 'oil_supply_restore' if symbol in OIL else 'us_gas_supply_restore', '已发生的供应恢复')
    if symbol == 'NATGASUSDT':
        if re.match(r'(?:u\.?s\.?|american) lng exports?' + auxiliary + r'(?:halted|stopped|suspended)\b', t):
            return ('SELL', 'us_lng_export_loss', '美国LNG出口中断，国内需求减少')
        if re.match(r'(?:u\.?s\.?|american) lng exports?' + auxiliary + r'(?:resumed|restarted|restored)\b', t):
            return ('BUY', 'us_lng_export_restore', '美国LNG出口恢复，国内需求增加')
    # Inventory changes without a validated actual/consensus pair are abstentions.
    # Weather forecasts, price recaps and geopolitical adjectives are not orders.
    return None


def event_subject(sentence, rule):
    if rule.startswith('fed_'): return 'fomc'
    if 'inventory_surprise' in rule or 'storage_surprise' in rule: return 'us'
    # Explicit named facility, not a generic "a pipeline" or country alone.
    sites = re.findall(r'\b(?:at|from) (?:the )?([A-Z][A-Za-z\'-]*(?: [A-Z][A-Za-z\'-]*){0,3}) (?:oilfield|field|terminal|pipeline|plant)\b', sentence)
    if len(set(sites)) != 1: return None
    site = sites[0].lower().replace(' ', '_')
    if rule.startswith('us_'): return 'us:' + site
    # Two different supply incidents are not two confirmations of one incident.
    entities = re.findall(r'\b(?:libya|libyan|iraq|iraqi|iran|iranian|russia|russian|saudi arabia|saudi|kuwait|nigeria|nigerian|venezuela|norway|norwegian|canada|canadian|kazakhstan|hormuz|united states|us)\b', sentence.lower())
    aliases = {'libyan':'libya','iraqi':'iraq','iranian':'iran','russian':'russia','saudi arabia':'saudi',
               'nigerian':'nigeria','norwegian':'norway','canadian':'canada','united states':'us'}
    entities = {aliases.get(e, e) for e in entities}
    return next(iter(entities)) + ':' + site if len(entities) == 1 else None


def inspect_item(item, symbol, now, contexts=None, risks=None):
    value = dict(item); value.pop('summary', None)
    contexts=risk_observations(item,symbol,now,event_time) if contexts is None else contexts
    if risks is None:risks=RiskLedger().update(symbol,contexts,now)
    gates={(r['category'],r['subject']):r.get('entry_gate') for r in risks}
    background=[c for c in contexts if gates.get((c['category'],c['subject']),'background')!='cooldown']
    driver_keys={key for key,_ in DRIVERS[symbol]}
    def background_sentence(sentence,title=False):
        candidates=background if title else [c for c in background if c['fingerprint']==sentence_fingerprint(sentence)]
        covered=set().union(*(RISK_DRIVERS.get(c['category'],set()) for c in candidates)) if candidates else set()
        matched={key for key in driver_keys if re.search(PATTERNS[key],sentence,re.I)}
        if matched=={'geopolitics'} and retrospective_reference(sentence):return True
        return bool(matched) and matched<=covered
    body=item.get('body','').replace('U.S.','US').replace('U.S','US')
    sentences=re.split(r'(?<=[.!?])\s+',body)
    driver_background=[]
    for key in driver_keys:
        matching=[(s,False) for s in sentences if re.search(PATTERNS[key],s,re.I)]
        if re.search(PATTERNS[key],item['title'],re.I):matching.append((item['title'],True))
        if matching and all(background_sentence(s,title) for s,title in matching):driver_background.append(key)
    origin = canonical_origin(item)
    value.update(evidence_level='正文已读取' if item.get('fulltext') else '仅标题/摘要', origin=origin,
                 analysis='未识别可执行事实；不据此产生方向', events=[], uncertain=False,
                 _background_drivers=driver_background)
    if not relevant(item['title'] + ' ' + item.get('body', ''), symbol):
        value['analysis'] = '与当前品种无直接关系'; return value
    if not item.get('fulltext'):
        value['uncertain'] = high_impact(item['title'])
        value['analysis'] = '正文未取得，标题不可授权交易' + ('；重大消息待核实，暂停新仓' if value['uncertain'] else '')
        return value
    if item.get('article_published') is None:
        value['uncertain'] = high_impact(item['title'])
        value['analysis'] = '正文发布时间无法核实，不产生交易方向'; return value
    if not 0 <= now - item['article_published'] <= EVENT_AGE:
        value['analysis'] = '事件证据超过6小时有效期；仅供背景参考'; return value
    background_only=bool(driver_background) and all(not high_impact(s) or background_sentence(s) for s in sentences)
    if origin=='Unknown' and not background_only:
        value.update(uncertain=True,analysis='报道标明转载或混合通讯社，原始来源无法独立归属；不采纳其交易方向，仅作参考')
        return value
    # Avoid U.S. abbreviations breaking sentence boundaries.
    risky_unresolved = False
    for sentence in sentences:
        if not relevant(sentence, symbol): continue
        matched = event_rule(sentence, symbol, item['source'])
        event_at = event_time(sentence, item, now)
        subject = event_subject(sentence, matched[1]) if matched else None
        if matched and event_at and subject:
            side, rule, reason = matched
            normalized = re.sub(r'[^a-z0-9 ]', '', sentence.lower())
            event_key = rule + ':' + subject + ':' + datetime.fromtimestamp(event_at, timezone.utc).strftime('%Y-%m-%d')
            value['events'].append(dict(direction=side, rule=rule, event_key=event_key,
                event_at=event_at, reason=reason, fingerprint=hashlib.sha256(normalized.encode()).hexdigest(),
                # A short excerpt only; full publisher text is never exposed in UI.
                excerpt=' '.join(sentence.split()[:22])))
        elif high_impact(sentence) and re.search(r'\b(?:halt\w*|shut\w*|disrupt\w*|restart\w*|resum\w*|restor\w*|attack\w*|cut|lower\w*|rais\w*|hike\w*|inventor\w*|storage)\b', sentence, re.I) and not HISTORICAL.search(sentence) and not background_sentence(sentence):
            risky_unresolved = True
    if not value['events'] and high_impact(item['title']) and not background_sentence(item['title'],True):
        risky_unresolved = True
    value['uncertain'] = risky_unresolved
    if value['events']:
        value['analysis'] = '；'.join(sorted(set(e['reason'] for e in value['events'])))
    if risky_unresolved:
        value['analysis'] += '；包含时点不明、条件、否定或规则未支持的内容，不采纳其交易方向；按K线与风险限制判断'
    elif driver_background and not value['events']:
        value['analysis']='已归类为持续背景或冷静期已到期的事件；事实仍保留，由已收盘K线与风控判断'
    # Body is processed locally, not sent back to the browser or stored in logs.
    value.pop('body', None)
    return value


def unavailable(symbol, now, reason='新闻证据尚未就绪'):
    return dict(symbol=symbol, asof=now, expires=now, status='unavailable', direction=None,
                confidence='无', reason=reason, evidence=[], event_id=None,policy_version=2,
                entry_allowed=False,kline_allowed=False,risks=[],drivers=[])


def assessed_drivers(items,symbol,risks,now,unconfirmed=False):
    drivers=driver_summary(items,symbol,risks,now)
    if not unconfirmed or any(r.get('blocking') is True for r in risks) or any(i.get('uncertain') for i in items):
        return drivers
    for driver in drivers:
        if driver['status']!='review_required':continue
        key=driver['key']
        matching=[i for i in items if i.get('article_published') is not None and
                  0<=now-i['article_published']<=EVENT_AGE and
                  re.search(PATTERNS[key],i.get('title','')+' '+i.get('_context_text',''),re.I)]
        # Lack of a second origin is not an unknown fact or a new hazard. Only
        # downgrade this specific gap when every matching report has a complete
        # supported event. Other unmodelled drivers remain visibly unconfirmed.
        if matching and all(any(0<=now-e.get('event_at',0)<=EVENT_AGE and
                                any(e.get('rule','').startswith(prefix) and mapped==key
                                    for prefix,mapped in RULE_DRIVERS.items())
                                for e in row.get('events',[])) for row in matching):
            driver.update(status='no_recent_evidence',
                          reason='已有完整事实报道，但方向缺少独立来源确认；不采纳报道方向，改由已收盘K线判断')
    return drivers


def decide(symbol, items, coverage, now, risks=None, drivers=None):
    base = unavailable(symbol, now)
    base['expires'] = now + TTL
    base.update(risks=risks or [],drivers=drivers or driver_summary(items,symbol,risks or [],now))
    checked=coverage.get('checked',now)
    if coverage.get('healthy') is not True or type(checked) not in (int,float) or not 0<=now-checked<TTL:
        base.update(reason='新闻源覆盖不足或连接失败，暂停新仓', expires=now)
        return base
    evidence = []; seen = set()
    for item in items:
        for event in item.get('events', []):
            if not 0 <= now - event['event_at'] <= EVENT_AGE: continue
            # Syndicated Reuters/AP text is a single origin even on two domains.
            origin = item.get('origin', item['source'])
            key = (event['event_key'], origin,event['fingerprint'])
            if key in seen: continue
            seen.add(key)
            evidence.append(dict(event, source=item['source'], origin=origin, title=item['title'],
                                 url=item['url'], published=item['published'], primary=item['source'] == 'Fed'))
    base['evidence'] = evidence[:12]
    if any(r.get('blocking') is True for r in base['risks']):
        base.update(status='blocked',reason='存在持续重大风险或未核实解除信息，暂停新仓；方向证据失效不代表风险消失')
        return base
    uncertain = any(i.get('uncertain') for i in items)
    if any(i.get('uncertain') and (not i.get('fulltext') or i.get('article_published') is None) for i in items):
        base.update(status='blocked', reason='重大报道的正文或发布时间缺失，等待数据恢复')
        return base
    directions = {e['direction'] for e in evidence}
    if len(directions) > 1:
        base.update(status='blocked', reason='有效新闻方向冲突，暂停新仓'); return base
    confirmed = []
    for event in evidence:
        group = [e for e in evidence if e['event_key'] == event['event_key']]
        # Identical event sentences republished by another site are not independent.
        independent = RiskLedger.independent(group)
        if event['primary'] or independent:
            confirmed.append(event)
    reviewed=copy.deepcopy(items)
    verified={(e['event_key'],e['origin'],e['fingerprint']) for e in confirmed}
    for row in reviewed:
        for event in row.get('events',[]):
            event['verified']=(event['event_key'],row.get('origin',row['source']),event['fingerprint']) in verified
    base['drivers']=drivers if drivers is not None else assessed_drivers(reviewed,symbol,base['risks'],now,unconfirmed=not confirmed)
    pending=[d['label'] for d in base['drivers'] if d['status']=='review_required']
    # An unconfirmed interpretation is not an entry prohibition. Explicit
    # producer permission still requires fresh sources and no acute cooldown;
    # consumers may then use only their independently confirmed closed Kline.
    if not confirmed or pending or uncertain:
        base.update(status='insufficient',direction=None,entry_allowed=False,kline_allowed=True,
                    reason='相关新闻方向尚未确认，仅作参考；按已收盘K线突破与风控判断' if pending or uncertain else
                           '新闻方向证据不足，改由已收盘K线突破与风控判断',confidence='证据不足',
                    evidence=[],event_id=None)
        return base
    side = confirmed[0]['direction']
    identity = '|'.join(sorted(set(e['event_key'] for e in confirmed)))
    base.update(status='bullish' if side == 'BUY' else 'bearish', direction=side,
                confidence='官方原始公告' if any(e['primary'] for e in confirmed) else '独立来源一致',
                reason='明确事实通过来源、事件时点及一致性核验，须与收盘K线共同确认',
                event_id=hashlib.sha256((symbol + side + identity).encode()).hexdigest()[:24],
                expires=min(now + TTL, min(e['event_at'] + EVENT_AGE for e in confirmed)),
                entry_allowed=True,evidence=[dict(e,verified=True) for e in confirmed][:12])
    return base


class NewsService:
    def __init__(self, fetcher=download,state_path=None):
        self.fetcher = fetcher
        self.cache = {}
        self.feed_cache = {}
        self.cache_lock = threading.RLock()
        self.key_locks = {}
        self.download_slots = threading.BoundedSemaphore(8)
        self.ledger_lock = threading.Lock()
        self.risk_ledger=RiskLedger(state_path,url_validator=safe_url)

    def key_lock(self, key):
        with self.cache_lock:
            # Striping bounds lock memory even over long-running feed churn.
            key=hash(key)%128
            return self.key_locks.setdefault(key,threading.Lock())

    def load_feed(self,feed,now):
        with self.key_lock(('feed',feed)):
            return self._load_feed(feed,now)

    def _load_feed(self,feed,now):
        source=FEED_PUBLISHERS.get(feed,feed)
        with self.cache_lock: old=self.feed_cache.get(feed)
        if old and -2<=now-old[0]<10:return copy.deepcopy(old[1])
        try:
            with self.download_slots: raw=self.fetcher(FEEDS[feed])
            result=(feed,parse_feed(raw,source,now),'')
        except Exception as exc:result=(feed,[],type(exc).__name__)
        with self.cache_lock:self.feed_cache[feed]=(now,result)
        return result

    def load_article(self, item, now):
        with self.key_lock(('article',item['url'])):
            return self._load_article(item,now)

    def _load_article(self, item, now):
        url = item['url']
        with self.cache_lock: old = self.cache.get(url)
        if old and -2 <= now - old[0] < 60:
            return dict(item, **copy.deepcopy(old[1]))
        try:
            with self.download_slots: raw = self.fetcher(url)
            parsed = article(raw, item['source'])
            # Original release RSS is authoritative for Fed, but not media event time.
            if item['source'] == 'Fed' and parsed['article_published'] is None:
                parsed['article_published'] = item['published']
            if parsed['article_published'] is not None and parsed['article_published'] > now:
                parsed['article_published'] = None
            if parsed['article_published'] is not None and abs(parsed['article_published'] - item['published']) > 3600:
                parsed['article_published'] = min(parsed['article_published'], item['published'])
        except Exception:
            parsed = dict(fulltext=False, body='', article_published=None, author='')
        with self.cache_lock:self.cache[url] = (now, parsed)
        return dict(item, **parsed)

    def fetch(self, symbol, now=None):
        if symbol not in SYMBOLS: raise ValueError('不支持的新闻品种')
        started=time.monotonic()
        now = time.time() if now is None else now
        sources = ['Fed' if symbol in METALS else 'EIA','BBC','Guardian','BBC World','Guardian World','Guardian Middle East']
        with ThreadPoolExecutor(max_workers=6) as pool:
            feeds = list(pool.map(lambda source:self.load_feed(source,now),sources))
        successful = [s for s, _, error in feeds if not error]
        coverage = dict(healthy=len(successful) == len(sources), successful=successful,
                        failed=[s for s, _, error in feeds if error], checked=now,
                        errors={s:error for s, _, error in feeds if error},
                        scope='联储或EIA、BBC商业/世界、Guardian商业/世界/中东；栏目按同一出版社归并，读取成功不代表证据充分',
                        publishers=sorted({FEED_PUBLISHERS.get(s,s) for s in successful}))
        candidates = [i for _, rows, _ in feeds for i in rows if relevant(i['title'] + ' ' + i.get('summary', ''), symbol)]
        candidates=list({i['url']:i for i in candidates}.values())
        candidates.sort(key=lambda i: i['published'], reverse=True)
        # Limit network work. Unprocessed important headlines remain blocking evidence.
        selected = select_factor_articles(candidates,symbol,12)
        with ThreadPoolExecutor(max_workers=4) as pool:
            detailed = list(pool.map(lambda i: self.load_article(i, now), selected))
        selected_urls={i['url'] for i in selected}
        overflow=[dict(i,fulltext=False) for i in candidates if i['url'] not in selected_urls][:24]
        grouped=[risk_observations(i,symbol,now,event_time) for i in detailed+overflow]
        observations=[o for group in grouped for o in group]
        with self.ledger_lock:risks=self.risk_ledger.update(symbol,observations,now)
        items=[inspect_item(raw,symbol,now,contexts,risks) for raw,contexts in zip(detailed+overflow,grouped)]
        for value,raw in zip(items,detailed+overflow):value['_context_text']=raw.get('body','')
        unread = [i['url'] for i in items if now-i['published'] <= EVENT_AGE and
                  (not i.get('fulltext') or i.get('article_published') is None)]
        if unread:
            coverage.update(healthy=False, unread=unread)
        if len(candidates) > 36:
            coverage['healthy'] = False
            coverage['overflow'] = True
        # Cache is bounded and includes no API credentials or user account data.
        with self.cache_lock:
            self.cache = {u: v for u, v in self.cache.items() if now - v[0] < ARTICLE_AGE}
            if len(self.cache) > 100: self.cache = dict(sorted(self.cache.items(), key=lambda p: p[1][0])[-100:])
        if self.risk_ledger.error:
            coverage.update(healthy=False,risk_state_error=self.risk_ledger.error)
        decision = decide(symbol,items,coverage,now,risks=risks)
        if any(e.get('verified') is True for e in decision['evidence']):
            verified={e['event_key'] for e in decision['evidence'] if e.get('verified') is True}
            for i in items:
                for event in i.get('events',[]):event['verified']=event['event_key'] in verified
        decision['drivers']=assessed_drivers(items,symbol,risks,now,
                                            unconfirmed=decision['status']=='insufficient' and decision['kline_allowed'])
        if self.risk_ledger.error:decision['reason']=self.risk_ledger.error
        for i in items:i.pop('body', None);i.pop('_context_text',None);i.pop('_background_drivers',None)
        # A private handoff to the optional AI worker. Analysis strips this
        # field from its public snapshot; no portfolio or account data enters.
        ai_candidates=[i for i in detailed if i.get('fulltext') and i.get('article_published') is not None]
        ai_articles=[dict(id=i.get('news_id') or hashlib.sha256((i['source']+'\n'+i['url']).encode()).hexdigest()[:24],
                          url=i['url'], title=i['title'], source=i['source'],
                          origin=canonical_origin(i), published=i['article_published'],
                          body=i['body'][:5000]) for i in select_factor_articles(ai_candidates,symbol,8)]
        newest=max((i['published'] for i in candidates),default=None)
        coverage.update(poll_target_seconds=10,fetch_duration_ms=round((time.monotonic()-started)*1000),
                        newest_published=newest, newest_publication_age_seconds=now-newest if newest else None,
                        latency_note='发布时间距今不是事件发生延迟；源站发布及更新速度不受本机控制')
        return dict(items=items[:12], decision=decision, coverage=coverage, _ai_articles=ai_articles)
