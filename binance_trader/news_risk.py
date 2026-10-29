"""Public-news context and durable unresolved risk; never an order generator."""
import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone

SYMBOLS = {'XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT'}
SIX_HOURS = 6*3600
COOLDOWN_SECONDS=3600
ACUTE_FIELDS=('acute_event_id','acute_rule','acute_event_at','time_basis','cooldown_until')
SOURCE_HOSTS={'Fed':{'www.federalreserve.gov','federalreserve.gov'},'EIA':{'www.eia.gov','eia.gov'},
              'BBC':{'www.bbc.com','www.bbc.co.uk','feeds.bbci.co.uk'},'Guardian':{'www.theguardian.com'}}
UNCERTAIN = re.compile(r"\b(?:may|might|could|would|if|rumou?r\w*|unconfirmed|unverified|reportedly|claim\w*|false|incorrect|debunk\w*|doubt\w*|alleg\w*|anonymous|lie|lies|consider\w*|expect\w*|forecast\w*|predict\w*|plan\w*|propos\w*|potential|possib\w*|denied|denial|not|no|never|didn['’]t|hasn['’]t|isn['’]t)\b",re.I)
HISTORICAL = re.compile(r'\b(?:yesterday|last (?:night|week|month|year)|earlier this (?:week|month|year)|previously|in \d{4}|years? ago|months? ago|used to|recall\w*|retrospect\w*|tomorrow|next (?:week|month|year))\b',re.I)
GEO = re.compile(r'\b(?:iran|israel|ukraine|russia|hormuz|middle east|red sea|yemen)\b',re.I)
WAR = re.compile(r'\b(?:war|warfare|conflict|hostilities|invasion|ceasefire|military attacks?|missile strikes?|bombardment|airstrikes?)\b',re.I)
SHIPPING = re.compile(r'\b(?:hormuz|red sea|suez|bab.el.mandeb|tanker|shipping|maritime)\b',re.I)
DISRUPTION = re.compile(r'\b(?:closed|closure|blocked|blockade|halted|stopped|suspended|disrupted|disruption|attacks?|strikes?|war|outage|shutdown)\b',re.I)
WEATHER = re.compile(r'\b(?:extreme heat|heatwave|heat wave|arctic blast|winter storm|hurricane|freeze|freezing|extreme cold)\b',re.I)
# Preserve case for the abbreviation: the English pronoun "us" is not a country.
US_ABBREVIATION=re.compile(r'(?<!\w)(?:US|U\.S\.)(?!\w)')
US = re.compile(r'(?<!\w)(?:US|U\.S\.)(?!\w)|(?i:\b(?:united states|american|texas|louisiana|henry hub|gulf coast|gulf of mexico)\b)')
LNG = re.compile(r'\b(?:lng|liquefied natural gas)\b',re.I)
HISTORICAL_WAR=re.compile(r'\b(?:(?:first|second) world war|world war (?:ii|i|1|2)|wwii|wwi|vietnam war|korean war)\b',re.I)


def canonical_origin(item):
    """A second publisher is not a second reporter for syndicated agency copy."""
    attribution=(item.get('author','')+' '+item.get('body','')).lower()
    agencies={name for name,pattern in (
        ('Reuters',r'\breuters\b'),('AP',r'\bassociated press\b'),
        ('AFP',r'\b(?:afp|agence france[ -]presse)\b'),
        ('PA',r'\b(?:pa media|press association)\b')) if re.search(pattern,attribution)}
    if len(agencies)==1:return next(iter(agencies))
    if agencies or re.search(r'\b(?:news agenc(?:y|ies)|syndicated|republished|reprinted|wire service)\b',attribution):return 'Unknown'
    return item['source']


def direct_assertion(sentence):
    """Only simple declarative main clauses can grant authority or release risk."""
    return not (UNCERTAIN.search(sentence) or re.search(r'[?"“”]|\b(?:reports? that|according to|said|says|stated|discuss\w*|whether|that|but|although|while|remains|ongoing|persists?)\b',sentence,re.I))


def retrospective_reference(sentence):
    """A dated retrospective/survey reference is not a fresh attack report.

    This applies to a single sentence, and cannot absorb a fresh hazard clause
    or financial driver elsewhere in the article.
    """
    if re.search(r'\b(?:launched|began|begun|started|escalated|attacked|struck|occurred|invaded|halted|closed|suspended|disrupted)\b|\b(?:new|fresh|latest) (?:attacks?|strikes?|airstrikes?|blockade|sanctions)',sentence,re.I):return False
    month=r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
    dated_attacks=r'(?:the )?\d{1,2} '+month+r'(?: (?:19|20)\d{2})? attacks?\b'
    return bool(re.search(r'\b(?:covid|brexit|mental health|survey|respondents|research|study)\b',sentence,re.I) or
                re.search(r'\bsince '+dated_attacks,sentence,re.I) or
                re.search(dated_attacks,sentence,re.I) and re.search(r'\b(?:impact\w*|effects?|aftermath|anniversary|recalled|remembered|survey|research|respondents)\b',sentence,re.I) or
                re.search(r'\bbefore (?:the )?(?:(?:Israel.Gaza|Gaza|Iran|Ukraine|Iraq) )?war\b',sentence,re.I) and
                re.search(r'\b(?:survey|research|respondents|percentage|per cent|percent|emotionally|attitudes?|attachment)\b',sentence,re.I))


def relevant_context(text,symbol):
    """Broader recall for risk discovery than the strict directional rulebook."""
    t=text.lower()
    if symbol in ('XAUUSDT','XAGUSDT'):
        if WAR.search(t) and GEO.search(t):return True
        if re.search(r'\b(?:safe.haven|us dollar|dollar index|dxy|real yields?|real interest rates?|treasury yields?|central bank gold)\b',t):return True
        if symbol=='XAGUSDT' and re.search(r'\b(?:solar|photovoltaic|industrial demand|manufacturing|silver min\w*|silver supply)\b',t):return True
    if symbol in ('CLUSDT','BZUSDT'):
        return bool((WAR.search(t) and GEO.search(t)) or SHIPPING.search(t))
    if symbol=='NATGASUSDT':
        return bool(LNG.search(t) or (WEATHER.search(t) and US.search(text)) or
                    (SHIPPING.search(t) and (GEO.search(t) or LNG.search(t))))
    return False


def subject_for_geo(text):
    t=text.lower()
    places=set(re.findall(r'\b(?:iran|israel|ukraine|russia|yemen|united states)\b',t))
    places={'us' if p=='united states' else p for p in places}
    if US_ABBREVIATION.search(text):places.add('us')
    if places:return '_'.join(sorted(places))+'_conflict'
    if 'red sea' in t:return 'red_sea_conflict'
    if 'middle east' in t:return 'middle_east_conflict'
    return None


def geopolitical_context(sentence):
    """Require a conflict/participant relationship before persisting a hazard.

    Nationality of a commentator and historical-war discussion are not a
    present conflict. This discovery filter never establishes direction or
    lowers the separate proof requirements for resolving an existing risk.
    """
    text=HISTORICAL_WAR.sub('historical_event',sentence)
    text=re.sub(r'\bus\b','pronoun',text)  # case sensitive, before re.I patterns
    country=r'(?:iran|israel|ukraine|russia|yemen|united states|US|U\.S\.|middle east|red sea)'
    conflict=r'(?:war|warfare|conflict|hostilities|invasion|ceasefire|military attacks?|missile strikes?|airstrikes?|bombardment)'
    return bool(re.search(conflict+r' (?:in|with|between|against|on|over|by) (?:the )?'+country+r'\b',text,re.I) or
                re.search(r'\b'+country+r"(?:['’]s)? (?:"+conflict+r')\b',text,re.I) or
                re.search(r'\b'+country+r' (?:(?:has|have) )?(?:launched|launches|conducted|continues?|continued) (?:a |new |military |missile )*'+conflict+r'\b',text,re.I) or
                re.search(r'\b'+country+r' (?:strikes|attacked|invaded) '+country+r'\b',text,re.I))


def shipping_subject(text):
    t=text.lower()
    routes=[]
    if 'hormuz' in t:routes.append('strait_of_hormuz')
    if 'red sea' in t or re.search('bab.el.mandeb',t):routes.append('red_sea_route')
    if 'suez' in t:routes.append('suez_canal')
    return routes[0] if len(routes)==1 else 'multiple_routes' if routes else None


def region(text):
    aliases={'iranian':'iran','iraqi':'iraq','libyan':'libya','israeli':'israel','russian':'russia',
             'ukrainian':'ukraine','qatari':'qatar','nigerian':'nigeria','canadian':'canada','norwegian':'norway',
             'saudi':'saudi arabia','american':'us','united states':'us','texas':'us','louisiana':'us',
             'mexican':'mexico','peruvian':'peru','australian':'australia','chinese':'china'}
    countries={'iran','iraq','libya','israel','russia','ukraine','qatar','nigeria','canada','norway',
               'kazakhstan','saudi arabia','us','mexico','peru','australia','china'}
    values=set(re.findall(r'\b(?:'+'|'.join(sorted((countries|set(aliases))-{'us'},key=len,reverse=True))+r')\b',text.lower()))
    values={aliases.get(v,v) for v in values}
    if US_ABBREVIATION.search(text):values.add('us')
    return next(iter(values)) if len(values)==1 else None


def site(text):
    values=re.findall(r'\b(?:at|from) (?:the )?([A-Z][A-Za-z\'-]*(?: [A-Z][A-Za-z\'-]*){0,3}) (?:oilfield|field|terminal|pipeline|plant|mine)\b',text)
    return values[0].lower().replace(' ','_') if len(set(values))==1 else None


def sentence_fingerprint(sentence):
    return hashlib.sha256(re.sub(r'[^a-z0-9 ]','',sentence.lower()).encode()).hexdigest()


def acute_candidate(item,category,subject,sentence,action,now,day_verified=False):
    """Only a newly occurring hazard gets a bounded execution cooling period."""
    if action=='resolved' or not item.get('fulltext') or canonical_origin(item)=='Unknown':return None
    published=item.get('article_published')
    if type(published) not in (int,float) or not 0<=now-published<=SIX_HOURS:return None
    if UNCERTAIN.search(sentence) or HISTORICAL.search(sentence) or re.search(r'\b(?:ongoing|remains?|continues?|continuing|still|persists?|already)\b',sentence,re.I):return None
    patterns={
        'geopolitical_conflict':r'\b(?:began|begun|started|escalated|attacked|invaded|launched (?:a |new |fresh |military |missile )*(?:attack|strikes?|airstrikes?|invasion))\b',
        'shipping_disruption':r'\b(?:(?:(?:newly|just) )?closed|began (?:a )?blockade|(?:new|fresh) blockade|(?:shipping|transit|traffic) (?:was |has been )?(?:newly |just )?(?:halted|stopped|suspended))\b',
        'facility_supply':r'\b(?:production|supply|exports?) (?:was |were |has been |have been )?(?:newly |just )?(?:halted|stopped|suspended|disrupted)\b',
        'silver_supply':r'\b(?:production|supply) (?:was |has been )?(?:newly |just )?(?:halted|stopped|suspended|disrupted)\b',
        'us_lng_exports':r'\bexports? (?:was |were |has been |have been )?(?:newly |just )?(?:halted|stopped|suspended|disrupted)\b',
        'global_lng_transmission':r'\bexports? (?:was |were |has been |have been )?(?:newly |just )?(?:halted|stopped|suspended|disrupted)\b',
        'us_gas_weather':r'\b(?:made landfall|new (?:hurricane|winter storm|arctic blast)|(?:winter storm|freeze|heatwave|heat wave) (?:has )?(?:begun|started))\b',
    }
    if category not in patterns or not re.search(patterns[category],sentence,re.I):return None
    # Full date plus explicit zone only. "Today" locates an event day; it does
    # not give a precise event time and never reuses article publication as one.
    exact=re.findall(r'\b(20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}| UTC))\b',sentence)
    if exact:
        try:
            instants={datetime.fromisoformat(value.replace(' UTC','+00:00').replace('Z','+00:00')).timestamp() for value in exact}
        except ValueError:return None
        if len(instants)!=1:return None
        event_at=next(iter(instants))
        if not 0<event_at<=min(now,published+5):return None
        basis='event_time'
        day=datetime.fromtimestamp(event_at,timezone.utc).strftime('%Y-%m-%d')
    else:
        if re.search(r'\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:',sentence):return None
        if not day_verified or not re.search(r'\b(?:today|this morning|this afternoon|just now)\b',sentence,re.I) or now-published>COOLDOWN_SECONDS:return None
        event_at=None;basis='first_evidence'
        day=datetime.fromtimestamp(published,timezone.utc).strftime('%Y-%m-%d')
    return dict(rule=category+'_new_hazard',event_at=event_at,time_basis=basis,day=day)


def observation(item,category,subject,sentence,action,now,event_at=None,reliable=False):
    origin=canonical_origin(item)
    return dict(category=category,subject=subject,action=action,source=item['source'],origin=origin,
                url=item['url'],published=item.get('article_published') or item['published'],
                event_at=event_at,reliable=bool(reliable and origin!='Unknown'),fingerprint=sentence_fingerprint(sentence),
                seen=now,background=retrospective_reference(sentence),acute=acute_candidate(item,category,subject,sentence,action,now,day_verified=event_at is not None))


def risk_observations(item,symbol,now,event_clock):
    """Recognize affected subjects, without turning war/foreign gas into BUY."""
    body=item.get('body','').replace('U.S.','US')
    text=(item['title']+' '+body)
    full=bool(item.get('fulltext'))
    published=item.get('article_published')
    facts=[]
    # Include title for discovery, but only original dated full body can release risk.
    chunks=[(item['title'],False)]+[(s,True) for s in re.split(r'(?<=[.!?])\s+',body)]
    for sentence,in_body in chunks:
        start=len(facts)
        if HISTORICAL.search(sentence):continue
        uncertain=bool(UNCERTAIN.search(sentence))
        event_at=event_clock(sentence,item,now) if full and in_body else None
        reliable=bool(full and in_body and published is not None and event_at and not uncertain)
        geo=subject_for_geo(sentence)
        if symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT') and geo and geopolitical_context(sentence):
            # A local ceasefire is not proof that the whole conflict has ended.
            ending=re.match(r'\s*(?:the )?(?:war|hostilities) between (iran|israel|russia|ukraine|united states|us) and (iran|israel|russia|ukraine|united states|us) (?:has |have )?(?:ended|ceased)\b',sentence,re.I)
            ended=bool(ending and subject_for_geo(ending.group(0))==geo and direct_assertion(sentence))
            action='resolved' if ended and not uncertain else 'uncertain' if uncertain else 'active'
            facts.append(observation(item,'geopolitical_conflict',geo,sentence,action,now,event_at,reliable))
        route=shipping_subject(sentence)
        if route and SHIPPING.search(sentence) and symbol in ('XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT'):
            reopened=bool(re.match(r'\s*(?:the )?(?:(?:strait of hormuz|hormuz|red sea route|suez canal) (?:has )?reopened\b|(?:shipping|tanker traffic|transit) through (?:the )?(?:strait of hormuz|red sea|suez canal) (?:has )?(?:resumed|returned to normal)\b)',sentence,re.I))
            reopened=reopened and route!='multiple_routes' and direct_assertion(sentence)
            if DISRUPTION.search(sentence) or reopened:
                facts.append(observation(item,'shipping_disruption',route,sentence,
                    'resolved' if reopened and not uncertain else 'uncertain' if uncertain else 'active',now,event_at,reliable))
        facility=site(sentence);country=region(sentence)
        facility_subject=(country or 'unknown_region')+':'+facility if facility else None
        if symbol in ('CLUSDT','BZUSDT') and facility and re.search(r'\b(?:oil|crude)\b',sentence,re.I):
            lost=bool(re.search(r'\b(?:oil|crude oil) (?:production|exports?|supply) (?:(?:was|were|is|are|has been|have been) )?(?:halted|stopped|shut down|suspended|disrupted)\b',sentence,re.I))
            restored=bool(re.search(r'\b(?:oil|crude oil) (?:production|exports?|supply) (?:(?:was|were|is|are|has been|have been) )?(?:resumed|restarted|restored)\b',sentence,re.I))
            if lost or restored:
                restored=restored and direct_assertion(sentence) and bool(re.match(r'\s*(?:(?:libyan?|iraqi?|iranian?|russian?|nigerian?|canadian?|norwegian?|us|american|saudi) )?(?:crude oil|oil) (?:production|exports?|supply)\b',sentence,re.I))
                facts.append(observation(item,'facility_supply',facility_subject,sentence,'resolved' if restored and country else 'uncertain' if uncertain or not country else 'active',now,event_at,reliable and bool(country)))
        if symbol=='XAGUSDT' and facility and re.search(r'\bsilver\b',sentence,re.I):
            restored=bool(re.match(r'\s*(?:(?:mexican|peruvian|canadian|australian|chinese) )?silver (?:production|supply) (?:(?:was|is|has been) )?(?:resumed|restarted|restored)\b',sentence,re.I)) and direct_assertion(sentence)
            if DISRUPTION.search(sentence) or restored:
                facts.append(observation(item,'silver_supply',facility_subject,sentence,'resolved' if restored and country else 'uncertain' if uncertain or not country else 'active',now,event_at,reliable and bool(country)))
        if symbol=='NATGASUSDT':
            if WEATHER.search(sentence) and US.search(sentence):
                # Forecasts matter to demand but do not establish price direction.
                locations=set(re.findall(r'\b(?:texas|louisiana|gulf coast|gulf of mexico)\b',sentence.lower()))
                weather_region=next(iter(locations)) if len(locations)==1 else 'multiple_regions' if locations else 'us_unspecified'
                hazards=set(m.group(0).lower() for m in WEATHER.finditer(sentence))
                weather_kind=next(iter(hazards)) if len(hazards)==1 else 'multiple_hazards'
                weather_subject=weather_region+':'+weather_kind
                ended=bool(re.search(r'\b(?:winter storm|hurricane|heatwave|heat wave|freeze|extreme cold|extreme heat) (?:has )?ended\b',sentence,re.I))
                ended=ended and len(locations)==1 and len(hazards)==1 and direct_assertion(sentence) and bool(re.match(r'\s*(?:the )?(?:winter storm|hurricane|heatwave|heat wave|freeze|extreme cold|extreme heat) (?:has )?ended\b',sentence,re.I))
                facts.append(observation(item,'us_gas_weather',weather_subject,sentence,'resolved' if ended and not uncertain else 'uncertain' if uncertain else 'active',now,event_at,reliable))
            if LNG.search(sentence) and (DISRUPTION.search(sentence) or re.search(r'\b(?:resumed|restarted|restored)\b',sentence,re.I)):
                us=country=='us';subject=facility_subject if facility else ('us_lng_unspecified' if us else ('qatar_lng' if country=='qatar' else 'global_lng_unspecified'))
                restored=bool(re.match(r'\s*(?:(?:us|american|qatar|qatari|australia|australian|russia|russian|nigeria|nigerian) )?lng exports? (?:(?:was|were|is|are|has been|have been) )?(?:resumed|restarted|restored)\b',sentence,re.I)) and direct_assertion(sentence)
                # Global LNG dislocation is a regional-transmission uncertainty,
                # not an automatic bullish Henry Hub signal.
                facts.append(observation(item,'us_lng_exports' if us else 'global_lng_transmission',subject,sentence,
                    'resolved' if restored and facility and country else 'uncertain' if uncertain or not us else 'active',now,event_at,reliable and bool(country)))
        for fact in facts[start:]:
            fact['headline']=not in_body
            if not in_body:fact['acute']=None
    # Repetition in title/body is one report from one origin, not corroboration.
    body_subjects={(o['category'],o['subject']) for o in facts if not o['headline']}
    unique={ (o['category'],o['subject'],o['action'],o['fingerprint']):o for o in facts if not o['headline'] or (o['category'],o['subject']) not in body_subjects }
    return list(unique.values())


DRIVERS = {
    'XAUUSDT':[('geopolitics','地缘与避险'),('usd_real_yield','美元与实际利率'),('fed_policy','联储政策'),('gold_demand','央行与实物黄金需求')],
    'XAGUSDT':[('geopolitics','地缘与避险'),('usd_real_yield','美元与实际利率'),('fed_policy','联储政策'),('silver_industry','工业与光伏需求'),('silver_supply','白银矿业供给')],
    'CLUSDT':[('geopolitics','地缘冲突'),('shipping','航运与海峡'),('oil_supply','原油供给'),('inventory','美国库存与预期')],
    'BZUSDT':[('geopolitics','地缘冲突'),('shipping','航运与海峡'),('oil_supply','原油供给'),('inventory','美国库存与预期')],
    'NATGASUSDT':[('gas_weather','美国天气与需求'),('gas_inventory','美国天然气库存'),('us_gas_supply','美国天然气产量'),('us_lng','美国LNG出口'),('global_lng','全球LNG与美国基准传导')],
}
PATTERNS={
    'geopolitics':r'\b(?:war|conflict|hostilities|ceasefire|safe.haven|attacks?|missile strikes?|airstrikes?|invasion|bombardment)\b','shipping':r'\b(?:hormuz|shipping|tanker|red sea)\b',
    'usd_real_yield':r'\b(?:us dollar|dollar index|dxy|greenback|real yields?|real interest rates?|treasury yields?|inflation.linked (?:bonds?|yields?)|TIPS yields?|breakeven inflation)\b',
    'fed_policy':r'\b(?:fed|federal reserve|fomc|federal funds)\b','gold_demand':r'\b(?:central banks?|physical gold|gold demand|bullion|gold reserves?|gold purchases?|PBoC|peoples? bank of china)\b',
    'silver_industry':r'\b(?:solar|photovoltaic|industrial demand|manufacturing)\b','silver_supply':r'\bsilver (?:mine|mining|supply|production)',
    'oil_supply':r'\b(?:oil|crude) (?:production|supply|exports?)\b','inventory':r'\b(?:inventor\w*|stockpile\w*)\b',
    'gas_weather':r'\b(?:weather|heatwave|heat wave|winter storm|hurricane|freeze|cold)\b','gas_inventory':r'\b(?:storage|inventor\w*|injection|withdrawal)\b',
    'us_gas_supply':r'\b(?:us|u\.?s\.?|american) natural gas production\b','us_lng':r'\b(?:us|u\.?s\.?|american) lng\b','global_lng':r'\b(?:lng|liquefied natural gas|qatar|ttf|jkm)\b',
}
UNSUPPORTED={'usd_real_yield','gold_demand','silver_industry','silver_supply','gas_weather','global_lng'}
RISK_DRIVERS={
    'geopolitical_conflict':{'geopolitics'},'shipping_disruption':{'shipping','geopolitics','global_lng'},
    'facility_supply':{'oil_supply'},'silver_supply':{'silver_supply'},'us_gas_weather':{'gas_weather'},
    'us_lng_exports':{'us_lng','global_lng'},'global_lng_transmission':{'global_lng'},
}
RULE_DRIVERS={'fed_':'fed_policy','oil_supply_':'oil_supply','us_oil_inventory_surprise':'inventory',
              'us_gas_storage_surprise':'gas_inventory','us_gas_supply_':'us_gas_supply','us_lng_export_':'us_lng'}


def driver_summary(items,symbol,risks,now):
    result=[]
    for key,label in DRIVERS[symbol]:
        matches=[i for i in items if re.search(PATTERNS[key],i.get('title','')+' '+i.get('_context_text',''),re.I)]
        recent=[i for i in matches if i.get('article_published') is not None and 0<=now-i['article_published']<=SIX_HOURS]
        supported=[e for i in recent for e in i.get('events',[]) if e.get('verified') is True and any(e.get('rule','').startswith(prefix) and driver==key for prefix,driver in RULE_DRIVERS.items())]
        persistent=[r for r in risks if r.get('blocking') is True and key in RISK_DRIVERS.get(r.get('category'),set())]
        background=[r for r in risks if r.get('entry_gate')=='background' and key in RISK_DRIVERS.get(r.get('category'),set())]
        unclassified=[i for i in recent if key not in i.get('_background_drivers',[])]
        background_only=not unclassified and (background or recent)
        status='review_required' if persistent else 'verified' if supported else 'background' if background_only else 'review_required' if recent else 'not_covered' if key in UNSUPPORTED else 'no_recent_evidence'
        reason=('新突发事件冷静期内；'+ '、'.join(dict.fromkeys(r['source']+'：'+r['subject'] for r in persistent)) if persistent else
                '已提取并核验本因素的事件，仍须收盘K线确认' if supported else
                '持续风险事实作为背景保留；当前没有新的突发冷静期，不据此无限禁止K线交易' if background_only else
                '已发现相关报道（'+ '、'.join(dict.fromkeys(i['source'] for i in recent))+'），规则未确认本因素的交易方向，仅作参考' if recent else
                '固定新闻规则尚未覆盖本因素；结构化数据的实际接入状态见独立数据栏' if key in UNSUPPORTED else
                '本轮来源未提供近期可执行证据；不表示该因素无影响')
        if key=='global_lng':reason+='；海外LNG/TTF/JKM不能直接等同美国Henry Hub'
        result.append(dict(key=key,label=label,status=status,reason=reason))
    return result


def select_factor_articles(items, symbol, limit):
    """Keep a bounded batch without letting one busy news topic fill every slot.

    Keywords allocate reading capacity only. The model reads the complete
    selected excerpt and determines relevance/direction independently.
    """
    ordered=sorted(items,key=lambda item:item.get('published',0),reverse=True)
    factors=[key for key,_ in DRIVERS[symbol]]
    def matches(item,key):
        text=' '.join(str(item.get(field,'') or '') for field in ('title','summary','body'))
        return bool(re.search(PATTERNS[key],text,re.I))
    chosen=[]
    for key in factors:
        if any(matches(ordered[i],key) for i in chosen):continue
        index=next((i for i,item in enumerate(ordered) if i not in chosen and matches(item,key)),None)
        if index is not None and len(chosen)<limit:chosen.append(index)
    for i in range(len(ordered)):
        if len(chosen)>=limit:break
        if i not in chosen:chosen.append(i)
    return [ordered[i] for i in sorted(chosen)]


class RiskLedger:
    """Atomic disk ledger containing only public risk metadata and fingerprints."""
    def __init__(self,path=None,url_validator=None):
        self.path=Path(path) if path is not None else None
        self.url_validator=url_validator
        self.symbols={s:{} for s in SYMBOLS};self.error=''
        self._retryable_write_error=False
        if self.path is not None and self.path.exists():
            try:
                if self.path.stat().st_size>2_000_000:raise ValueError('oversize')
                data=json.loads(self.path.read_text())
                if data.get('schema')!=2 or set(data.get('symbols',{}))!=SYMBOLS:raise ValueError('schema')
                for symbol,rows in data['symbols'].items():
                    if not isinstance(rows,dict) or len(rows)>100:raise ValueError('shape')
                    for identity,row in rows.items():self.validate(identity,row,symbol)
                self.symbols=data['symbols']
            except Exception:
                self.error='持续新闻风险账本损坏或无法读取；禁止将风险视为已消失'

    def validate(self,identity,row,symbol=None):
        if not isinstance(row,dict) or row.get('id')!=identity or row.get('status') not in ('active','review_required','resolved'):raise ValueError('risk')
        if row.get('blocking') is not (row['status']!='resolved'):raise ValueError('blocking')
        for key in ('category','subject','reason','source','url'):
            if not isinstance(row.get(key),str) or not row[key] or len(row[key])>1500:raise ValueError('field')
        if self.url_validator and not self.url_validator(row['url']):raise ValueError('url')
        if urlparse(row['url']).hostname not in SOURCE_HOSTS.get(row['source'],set()):raise ValueError('publisher')
        for key in ('first_seen','last_seen','published','last_hazard_at'):
            value=row.get(key)
            if type(value) not in (int,float) or not 0<value<=time.time()+5:raise ValueError('date')
        if row['first_seen']>row['last_seen'] or row['published']>row['last_seen']+5 or row['last_hazard_at']>row['last_seen']+5:raise ValueError('date order')
        if row.get('event_at') is not None and (type(row['event_at']) not in (int,float) or not 0<row['event_at']<=row['published']+5):raise ValueError('event date')
        if symbol and identity!=hashlib.sha256((symbol+':'+row['category']+':'+row['subject']).encode()).hexdigest()[:24]:raise ValueError('identity')
        reports=row.get('_reports')
        if not isinstance(reports,list) or not 0<len(reports)<=24:raise ValueError('reports')
        def validate_report(report):
            if not isinstance(report,dict):raise ValueError('report')
            if report.get('action') not in ('active','uncertain','resolved') or type(report.get('reliable')) is not bool:raise ValueError('report')
            if report.get('category')!=row['category'] or report.get('subject')!=row['subject']:raise ValueError('identity')
            if report.get('origin') not in ('Fed','EIA','BBC','Guardian','Reuters','AP','AFP','PA','Unknown') or not isinstance(report.get('fingerprint'),str) or not re.fullmatch('[a-f0-9]{64}',report['fingerprint']):raise ValueError('origin')
            if report['origin']=='Unknown' and report['reliable']:raise ValueError('unknown agency')
            if self.url_validator and not self.url_validator(report.get('url')):raise ValueError('url')
            if urlparse(report.get('url','')).hostname not in SOURCE_HOSTS.get(report.get('source'),set()):raise ValueError('publisher')
            for key in ('published','seen'):
                if type(report.get(key)) not in (int,float) or not 0<report[key]<=row['last_seen']+5:raise ValueError('date')
            if report['published']>report['seen']+5:raise ValueError('date order')
            if report.get('event_at') is not None and (type(report['event_at']) not in (int,float) or not 0<report['event_at']<=report['published']+5):raise ValueError('date')
            if report['reliable'] and report.get('event_at') is None:raise ValueError('missing event date')
        mentions=row.get('_mentions',[])
        if not isinstance(mentions,list) or len(mentions)>24:raise ValueError('mentions')
        for mention in mentions:
            validate_report(mention)
            if mention.get('background') is not True:raise ValueError('background mention')
        for report in reports:validate_report(report)
        hazard=row.get('_last_hazard');validate_report(hazard)
        if hazard['action']=='resolved' or max(hazard['published'],hazard.get('event_at') or 0)!=row['last_hazard_at']:raise ValueError('hazard highwater')
        if any(r['action']!='resolved' and max(r['published'],r.get('event_at') or 0)>row['last_hazard_at'] for r in reports):raise ValueError('hazard lost')
        proof=row.get('_resolution_proof',[])
        if row['status']=='resolved':
            value=row.get('resolved_at')
            if type(value) not in (int,float) or not row['last_hazard_at']<value<=row['last_seen']+5:raise ValueError('resolution date')
            if not isinstance(proof,list) or not 2<=len(proof)<=24:raise ValueError('resolution proof')
            for report in proof:
                validate_report(report)
                if report['action']!='resolved' or not report['reliable'] or not report.get('event_at') or min(report['published'],report['event_at'])<=row['last_hazard_at']:raise ValueError('resolution proof')
            if not self.independent(proof) or len({int(r['event_at']//86400) for r in proof})!=1 or value!=max(r['event_at'] for r in proof):raise ValueError('resolution independence')
        elif proof or 'resolved_at' in row:raise ValueError('unexpected resolution')
        acute=row.get('_acute_events',{})
        if not isinstance(acute,dict) or len(acute)>256:raise ValueError('acute registry')
        for event_id,event in acute.items():
            if not isinstance(event,dict) or event.get('acute_event_id')!=event_id or not re.fullmatch('[a-f0-9]{24}',event_id):raise ValueError('acute identity')
            for key in ('acute_rule','day_key','fingerprint','source','url'):
                if not isinstance(event.get(key),str) or not event[key]:raise ValueError('acute field')
            if event['time_basis'] not in ('event_time','first_evidence'):raise ValueError('acute basis')
            for key in ('acute_event_at','cooldown_until','first_evidence_at'):
                if type(event.get(key)) not in (int,float) or not 0<event[key]<1e11:raise ValueError('acute date')
            if event['cooldown_until']!=event['acute_event_at']+COOLDOWN_SECONDS or event['acute_event_at']>event['first_evidence_at']+5 or event['first_evidence_at']>row['last_seen']+5:raise ValueError('acute clock')
            if event['time_basis']=='first_evidence' and event['acute_event_at']!=event['first_evidence_at']:raise ValueError('first evidence clock')
            if self.url_validator and not self.url_validator(event['url']):raise ValueError('acute url')
            if urlparse(event['url']).hostname not in SOURCE_HOSTS.get(event['source'],set()):raise ValueError('acute publisher')

    def register_acute(self,row,obs,now):
        candidate=obs.get('acute')
        if not candidate or obs['action']=='resolved':return
        events=row.setdefault('_acute_events',{})
        day_key=obs['category']+':'+obs['subject']+':'+candidate['rule']+':'+candidate['day']
        # Exact copied facts and imprecise same-subject/day rewrites cannot
        # restart the timer. A second same-day event requires a later exact time.
        if any(e['fingerprint']==obs['fingerprint'] for e in events.values()):return
        related=[e for e in events.values() if e['day_key']==day_key]
        if candidate['time_basis']=='first_evidence' and related:return
        previous=[r.get('seen',now) for r in row.get('_reports',[])+[row.get('_last_hazard',{})]
                  if r.get('fingerprint')==obs['fingerprint']]
        first_evidence=min([now]+previous)
        start=candidate['event_at'] if candidate['time_basis']=='event_time' else first_evidence
        if related and start<=max(e['acute_event_at'] for e in related):return
        key=day_key+(':'+str(start) if candidate['time_basis']=='event_time' else ':first')
        identity=hashlib.sha256(key.encode()).hexdigest()[:24]
        if identity in events:return
        if len(events)>=256:
            self.error='突发事件去重账本达到安全容量，暂停新仓';return
        events[identity]=dict(acute_event_id=identity,acute_rule=candidate['rule'],acute_event_at=start,
                             time_basis=candidate['time_basis'],cooldown_until=start+COOLDOWN_SECONDS,
                             day_key=day_key,fingerprint=obs['fingerprint'],first_evidence_at=first_evidence,
                             source=obs['source'],url=obs['url'])

    def save(self):
        if self.path is None or (self.error and not self._retryable_write_error):return
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            content=json.dumps(dict(schema=2,symbols=self.symbols),ensure_ascii=False,allow_nan=False)
            if len(content.encode())>2_000_000:raise ValueError('oversize')
            fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as file:file.write(content);file.flush();os.fsync(file.fileno())
            os.replace(str(temporary),str(self.path))
            directory=os.open(str(self.path.parent),os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
            self._retryable_write_error=False
            self.error=''
        except Exception as exc:
            self._retryable_write_error=isinstance(exc,OSError)
            self.error='持续新闻风险账本无法可靠保存，暂停新开仓'
            try:temporary.unlink()
            except OSError:pass

    @staticmethod
    def independent(reports):
        # Shared sentences link publishers into one provenance cluster. Extra
        # sentences from that same publisher cannot manufacture corroboration.
        clusters=[]
        for report in reports:
            if report['origin']=='Unknown':continue
            origins={report['origin']};prints={report['fingerprint']}
            merged=[]
            for old_origins,old_prints in clusters:
                if origins & old_origins or prints & old_prints:
                    origins|=old_origins;prints|=old_prints
                else:merged.append((old_origins,old_prints))
            # Joining two older clusters can occur transitively via a report.
            changed=True
            while changed:
                changed=False;remaining=[]
                for old_origins,old_prints in merged:
                    if origins & old_origins or prints & old_prints:
                        origins|=old_origins;prints|=old_prints;changed=True
                    else:remaining.append((old_origins,old_prints))
                merged=remaining
            clusters=merged+[(origins,prints)]
        return len(clusters)>=2

    def update(self,symbol,observations,now):
        # Only retry an I/O write failure using the retained complete in-memory
        # ledger. Corrupt input and capacity/schema errors remain fail-closed.
        if self._retryable_write_error:self.save()
        if self.error:return self.snapshot(symbol,now)
        rows=self.symbols[symbol]
        for obs in observations:
            identity=hashlib.sha256((symbol+':'+obs['category']+':'+obs['subject']).encode()).hexdigest()[:24]
            row=rows.get(identity)
            if obs.get('background') is True:
                if row is not None:
                    mentions=row.setdefault('_mentions',[])
                    if not any(r['fingerprint']==obs['fingerprint'] and r['url']==obs['url'] for r in mentions):mentions.append(copy.deepcopy(obs))
                    row['_mentions']=sorted(mentions,key=lambda r:r['published'])[-24:]
                    row['last_seen']=now
                continue
            if row is None:
                if obs['action']=='resolved':continue
                if len(rows)>=100:self.error='持续新闻风险数量超过安全上限，暂停新开仓';break
                row=dict(id=identity,category=obs['category'],subject=obs['subject'],status='review_required',blocking=True,
                         reason='重大风险待独立核验；不会因新闻过期自动消失',source=obs['source'],url=obs['url'],
                         first_seen=now,last_seen=now,published=obs['published'],event_at=obs.get('event_at'),_reports=[],
                         last_hazard_at=max(obs['published'],obs.get('event_at') or 0),_last_hazard=copy.deepcopy(obs),_resolution_proof=[])
                rows[identity]=row
            # Re-reading an old report cannot reopen a resolved event.
            if row['status']=='resolved' and obs['published']<=row.get('resolved_at',0):continue
            row['last_seen']=now
            self.register_acute(row,obs,now)
            if obs['action']!='resolved' and max(obs['published'],obs.get('event_at') or 0)>=row['last_hazard_at']:
                row['last_hazard_at']=max(obs['published'],obs.get('event_at') or 0)
                row['_last_hazard']=copy.deepcopy(obs)
                row.pop('resolved_at',None);row['_resolution_proof']=[]
                row.update(status='review_required',blocking=True)
            same=lambda r:(r['url'],r['action'],r['fingerprint'])==(obs['url'],obs['action'],obs['fingerprint'])
            if not any(same(r) for r in row['_reports']):row['_reports'].append(copy.deepcopy(obs))
            row['_reports']=sorted(row['_reports'],key=lambda r:r['published'])[-24:]
        for row in rows.values():
            reports=row['_reports'];hazards=[r for r in reports if r['action']!='resolved']+[row['_last_hazard']]
            latest=row['last_hazard_at']
            candidates=[r for r in reports if r['action']=='resolved' and r['reliable'] and r.get('event_at') and
                        r['published']>latest and r['event_at']>latest and 0<=now-r['published']<=SIX_HOURS]
            # Confirmation must concern the same event day, not unrelated periods.
            groups={}
            for report in candidates:groups.setdefault(int(report['event_at']//86400),[]).append(report)
            resolved=next((v for v in groups.values() if self.independent(v)),None)
            newer=[r for r in hazards if r['published']>row.get('resolved_at',0)]
            if resolved and not any(r['published']>=max(x['event_at'] for x in resolved) for r in hazards):
                chosen=max(resolved,key=lambda r:r['published'])
                row.update(status='resolved',blocking=False,resolved_at=max(r['event_at'] for r in resolved),
                           _resolution_proof=copy.deepcopy(resolved),
                           reason='同事件较新正文由两独立来源确认解除；旧报道不会重新打开该事件')
            elif row['status']=='resolved' and not newer:
                continue
            else:
                chosen=row['_last_hazard']
                active=[r for r in newer if r['action']=='active' and r['reliable']]
                is_active=self.independent(active)
                row.update(status='active' if is_active else 'review_required',blocking=True,
                           reason='持续重大风险仍有效；必须取得同事件较新解除证据' if is_active else
                                  '风险或解除证据尚未完成独立核验；不因6小时、出RSS或重启自动清除')
            row.update(source=chosen['source'],url=chosen['url'],published=chosen['published'],event_at=chosen.get('event_at'))
        self.save()
        return self.snapshot(symbol,now)

    def snapshot(self,symbol,now=None):
        now=time.time() if now is None else now
        result=[]
        for raw in self.symbols[symbol].values():
            row=copy.deepcopy({k:v for k,v in raw.items() if not k.startswith('_')})
            row['related_mentions']=[{key:m[key] for key in ('source','url','published','seen')} for m in raw.get('_mentions',[])]
            row.update(gate_version=1,entry_gate='background',blocking=False,
                       gate_reason='持续风险作为背景保留；未发现新的突发冷静期，允许K线与常规风控判断')
            row.update({key:None for key in ACUTE_FIELDS})
            if raw['status']=='resolved':
                row.update(entry_gate='resolved',gate_reason='事实风险已有独立解除证据')
            else:
                latest=max(raw.get('_acute_events',{}).values(),key=lambda e:e['cooldown_until'],default=None)
                if latest:
                    row.update({key:latest[key] for key in ACUTE_FIELDS})
                    cooling=now<latest['cooldown_until']
                    row.update(entry_gate='cooldown' if cooling else 'background',blocking=cooling,
                               gate_reason=('新突发事件60分钟冷静期，暂停新仓' if cooling else
                                            '突发事件冷静期已结束；风险事实仍保留，允许K线与常规风控判断')+
                                           ('；计时基于首次新事件证据，并非精确发生时间' if latest['time_basis']=='first_evidence' else '；计时基于明确事件发生时间'))
            result.append(row)
        return result
