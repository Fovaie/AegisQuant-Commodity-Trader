"""Local model-work admission; never authorizes a trade or a rotation."""
import hashlib
import json
import math
import re

from .core import SYMBOLS
from .news import safe_url

EVENT_TTL = 3600


def recent(stamp, now, age=EVENT_TTL):
    return type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now-stamp < age


def work_scope(state):
    position, pending = state.get('position'), state.get('pending')
    focus = (position or pending or {}).get('symbol', state.get('symbol'))
    if focus not in SYMBOLS:
        return dict(mode='unavailable', focus=None, auto=False)
    return dict(mode='pending' if pending else 'holding' if position else 'flat',
                focus=focus, auto=state.get('selection_mode') == 'auto')


def major_events(data, now):
    """Wake on recent, sourced risk events or verified directional facts.

    This intentionally bounded rule detector is not a market-wide event feed.
    Full public text must be in the model input; a headline alone cannot wake it.
    Event/article revisions are deduped independently from unrelated news.
    """
    decision = data.get('decision', {})
    if data.get('coverage', {}).get('healthy') is not True:
        return []
    if not recent(decision.get('asof'), now, 75):
        return []
    candidates = []
    for risk in decision.get('risks', []):
        if (risk.get('entry_gate') == 'cooldown' and risk.get('blocking') is True
                and recent(risk.get('acute_event_at'), now)):
            candidates.append(dict(id=risk.get('acute_event_id'), url=risk.get('url'),
                event_at=risk['acute_event_at'], label='新突发风险：'+str(risk.get('subject', ''))[:100]))
    if decision.get('entry_allowed') is True and decision.get('event_id'):
        for event in decision.get('evidence', []):
            if event.get('verified') is True and recent(event.get('event_at'), now):
                candidates.append(dict(id=decision['event_id'], url=event.get('url'),
                    event_at=event['event_at'], label='新方向事实：'+str(event.get('title', ''))[:100]))
    articles = {a.get('url'): a for a in data.get('_ai_articles', []) if isinstance(a, dict)}
    result = {}
    for candidate in candidates:
        article = articles.get(candidate['url'])
        identity = candidate['id']
        if (not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{24}', identity)
                or not safe_url(candidate['url']) or not article or not article.get('body')
                or not recent(article.get('published'), now)):
            continue
        version = [identity, article.get('origin'), article.get('id'),
                   re.sub(r'\s+', ' ', article.get('title', '')).strip(),
                   re.sub(r'\s+', ' ', article['body'][:5000]).strip()]
        token = hashlib.sha256(json.dumps(version, ensure_ascii=False).encode()).hexdigest()
        result[token] = dict(token=token, label=candidate['label'], url=candidate['url'],
                             source=str(article.get('source', ''))[:100], event_at=candidate['event_at'],
                             expires=candidate['event_at']+EVENT_TTL)
    return list(result.values())[:8]


def admission(scope, symbol, events, attempted, now):
    if scope is None:  # Independent service users retain the legacy all-symbol behavior.
        return dict(mode='flat', focus=None, allowed=True, reason='空仓：按新增资料分析候选', events=[])
    focus, mode = scope.get('focus'), scope.get('mode')
    base = dict(focus=focus, mode=mode, allowed=False, events=[])
    if mode == 'unavailable':
        return dict(base, reason='持仓状态尚未取得，暂不发起自动模型请求')
    if symbol == focus:
        return dict(base, allowed=True, reason='持仓品种优先更新；无新增资料不调用模型' if mode == 'holding'
                    else '待核对订单品种持续监测' if mode == 'pending' else '按新增资料分析候选')
    if mode == 'flat' and scope.get('auto'):
        return dict(base, allowed=True, reason='空仓：五个品种按新增资料比较')
    new_events = [e for e in events if e['token'] not in attempted and recent(e['event_at'], now)]
    if mode == 'holding' and scope.get('auto') and new_events:
        return dict(base, mode='event', allowed=True, events=new_events,
                    reason='未持有品种出现新重大事件，复核后仅提示机会')
    return dict(base, mode='standby', reason='未持有：继续采集和本地规则监测，暂停常规模型调用；重大事件例外'
                if mode == 'holding' and scope.get('auto') else '仅监测当前持仓或固定品种，其他品种不发起模型请求')
