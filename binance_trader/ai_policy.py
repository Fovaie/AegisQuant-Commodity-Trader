"""Bounded AI direction filter. The model cannot create an order or risk limit."""
import hashlib
import json
import math
from .news_risk import DRIVERS

MAX_AGE = 900


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def project_ai(value, symbol, now):
    base = dict(policy_version=1, symbol=symbol, status='fallback', direction=None,
                reason='AI暂无有效综合方向，使用新闻规则与健康K线备用路径',
                model='', analyzed_at=None, expires=None, result_id=None)
    if not isinstance(value, dict) or value.get('symbol') != symbol:
        return base
    base['model'] = value.get('model', '')
    state = value.get('status')
    reasons = {'disabled':'AI未启用', 'pending':'新增材料等待AI分析', 'analyzing':'AI正在分析新增材料',
               'historical':'AI解读已过期或输入版本改变', 'stale':'AI输入或结果已过期',
               'error':'AI分析失败', 'no_news':'AI无可分析输入', 'unchanged':'AI暂无可用结果',
               'standby':'未持有品种处于模型省用量监测'}
    if state != 'ok':
        base['reason'] = reasons.get(state, 'AI状态无效')+'；使用新闻规则与健康K线备用路径'
        return base
    stamp = value.get('analyzed_at')
    checked = value.get('retained_input_checked_at')
    retained = finite(checked) and 0 <= now-checked < 90

    if (value.get('trading_authority') is not False or not finite(stamp) or not (0 <= now-stamp and (retained or now-stamp < MAX_AGE))
            or not isinstance(base['model'], str) or not base['model'].strip()):
        return dict(base, reason='AI时效或结果标识无效；不采用模型方向')
    if value.get('direction') not in ('bullish', 'bearish'):
        return dict(base, reason='AI综合方向混合或不确定；使用新闻规则与健康K线备用路径')
    rows = value.get('drivers'); expected = {k for k, _ in DRIVERS[symbol]}
    if (not isinstance(rows, list) or len(rows) != len(expected)
            or any(not isinstance(r, dict) or r.get('key') not in expected for r in rows)
            or {r['key'] for r in rows} != expected
            or any(r.get('status') not in ('analyzed', 'no_evidence') for r in rows)
            or not any(r.get('status') == 'analyzed' and r.get('direction') == value['direction']
                       and isinstance(r.get('sources'), list) and r['sources'] for r in rows)):
        return dict(base, reason='AI因素或依据未完整通过校验；不采用模型方向')
    events = value.get('events', [])
    if not isinstance(events, list) or any(not isinstance(e, dict) or e.get('evidence_valid') is False for e in events):
        return dict(base, reason='AI含未通过引用校验的事件；不采用模型方向')
    direction = 'BUY' if value['direction'] == 'bullish' else 'SELL'
    identity = hashlib.sha256(json.dumps([symbol, base['model'], stamp, value.get('result_id'),
                                         value['direction'], rows], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return dict(base, status='active', direction=direction, analyzed_at=stamp, retained_input_checked_at=checked if retained else None, expires=checked+90 if retained else stamp+MAX_AGE,
                result_id=identity, reason='AI综合'+('偏多' if direction == 'BUY' else '偏空')+'；仅接受同向已收盘突破，不能单独下单')


def active_direction(value, symbol, now):
    """Recheck the host projection at every candidate and pre-order check."""
    if not isinstance(value, dict):return None
    if (type(value.get('policy_version')) is not int or value['policy_version'] != 1
            or value.get('symbol') != symbol or value.get('status') != 'active'
            or value.get('direction') not in ('BUY', 'SELL')):return None
    stamp, expires = value.get('analyzed_at'), value.get('expires')
    checked = value.get('retained_input_checked_at')
    if checked is not None:
        if not finite(stamp) or not finite(checked) or not finite(expires) or not stamp <= checked <= now < expires or expires != checked+90:return None
    elif not finite(stamp) or not finite(expires) or expires != stamp+MAX_AGE or not 0 <= now-stamp < MAX_AGE:return None
    identity = value.get('result_id')
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):return None
    if not isinstance(value.get('model'), str) or not value['model'].strip():return None
    return value['direction']
