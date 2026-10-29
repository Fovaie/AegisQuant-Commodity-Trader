"""Closed-candle entries with verified news, or explicit healthy-news fallback."""
import time
from urllib.parse import urlparse
from .core import SYMBOLS, number
from . import news as news_policy
from .ai_policy import active_direction
from .timeframes import timeframe


_DIRECTION = {'bullish': 'BUY', 'bearish': 'SELL'}
_ORIGINS = {'Fed', 'EIA', 'BBC', 'Guardian', 'Reuters', 'AP', 'AFP', 'PA'}
_RISK_STATES = {'active', 'review_required', 'resolved'}
_DRIVER_STATES = {'verified', 'review_required', 'no_recent_evidence', 'not_covered', 'background'}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('新闻时间必须是数值')
    return number(value)


def _source_url(source, url):
    return (isinstance(url, str) and news_policy.safe_url(url) is not None
            and news_policy.DOMAINS.get(urlparse(url).hostname) == source)


def _valid_risks(risks, asof):
    if not isinstance(risks, list):
        return False
    for risk in risks:
        if not isinstance(risk, dict) or any(not _text(risk.get(k)) for k in
                ('id', 'category', 'subject', 'reason', 'source', 'url')):
            return False
        status = risk.get('status')
        gate = risk.get('entry_gate')
        if (status not in _RISK_STATES or type(risk.get('blocking')) is not bool
                or type(risk.get('gate_version')) is not int or risk['gate_version'] != 1
                or gate not in ('background', 'cooldown', 'resolved')
                or not _text(risk.get('gate_reason'))
                or risk['blocking'] != (gate == 'cooldown')
                or (status == 'resolved') != (gate == 'resolved')
                or not _source_url(risk['source'], risk['url'])):
            return False
        acute_fields = ('acute_event_id', 'acute_rule', 'acute_event_at', 'time_basis', 'cooldown_until')
        if any(key not in risk for key in acute_fields):
            return False
        no_acute = all(risk[key] is None for key in acute_fields)
        if gate == 'resolved' and not no_acute:
            return False
        if gate == 'cooldown' and no_acute:
            return False
        try:
            first, last, published = (_numeric(risk[k]) for k in ('first_seen', 'last_seen', 'published'))
            if not 0 < first <= last <= asof+5 or not 0 < published <= asof+5:
                return False
            if risk.get('event_at') is not None and not 0 < _numeric(risk['event_at']) <= asof+5:
                return False
            if not no_acute:
                if (not _text(risk['acute_event_id']) or not _text(risk['acute_rule'])
                        or risk['time_basis'] not in ('event_time', 'first_evidence')):
                    return False
                event_at, until = _numeric(risk['acute_event_at']), _numeric(risk['cooldown_until'])
                if not 0 < event_at <= asof+5 or until != event_at+3600:
                    return False
                if gate == 'cooldown' and not event_at <= asof < until:
                    return False
                if gate == 'background' and not until <= asof:
                    return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def _valid_drivers(drivers):
    return (isinstance(drivers, list) and bool(drivers)
            and all(isinstance(d, dict) and d.get('status') in _DRIVER_STATES
                    and all(_text(d.get(k)) for k in ('key', 'label', 'reason')) for d in drivers))


def _verified_evidence(items, direction, symbol, now):
    if not isinstance(items, list) or not items:
        return False
    groups = {}
    for item in items:
        if (not isinstance(item, dict) or item.get('verified') is not True
                or type(item.get('primary')) is not bool or item.get('direction') != direction
                or any(not _text(item.get(k)) for k in
                       ('rule', 'event_key', 'fingerprint', 'source', 'origin', 'url', 'title', 'reason', 'excerpt'))
                or item['origin'] not in _ORIGINS or not _source_url(item['source'], item['url'])):
            return False
        try:
            event_at, published = _numeric(item['event_at']), _numeric(item['published'])
            if not 0 <= now-event_at <= news_policy.EVENT_AGE or not 0 < published <= now+5:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        if item['primary'] and not (item['source'] == item['origin'] == 'Fed'
                and symbol in ('XAUUSDT', 'XAGUSDT') and item['rule'].startswith('fed_')):
            return False
        groups.setdefault(item['event_key'], []).append(item)
    # Recheck corroboration so a truncated/single-source/malformed snapshot cannot
    # acquire authority just by carrying a direction and entry_allowed=True.
    return all(any(e['primary'] for e in group) or news_policy.RiskLedger.independent(group)
        for group in groups.values())


def news_state(value, symbol, now=None):
    now = time.time() if now is None else now
    unavailable = dict(symbol=symbol, policy_version=2, entry_allowed=False, kline_allowed=False,
                       status='unavailable', direction=None,
                       reason='新闻证据缺失、过期或格式未通过核验，暂停新开仓',
                       evidence=[], risks=[], drivers=[])
    if symbol not in SYMBOLS or not isinstance(value, dict) or value.get('symbol') != symbol:
        return unavailable
    if type(value.get('policy_version')) is not int or value['policy_version'] != 2:
        return dict(unavailable, reason='新闻策略版本未通过核验，暂停新开仓')
    kline_allowed = value.get('kline_allowed', False)
    if type(value.get('entry_allowed')) is not bool or type(kline_allowed) is not bool:
        return unavailable
    try:
        now, asof, expires = _numeric(now), _numeric(value['asof']), _numeric(value['expires'])
        if not -5 <= now-asof <= 90 or not now < expires <= asof+90:
            return unavailable
    except (ValueError, TypeError, KeyError):
        return unavailable
    status = value.get('status')
    if status not in ('bullish', 'bearish', 'insufficient', 'blocked', 'unavailable') or not _text(value.get('reason')):
        return unavailable
    if not _valid_risks(value.get('risks'), asof) or not _valid_drivers(value.get('drivers')):
        return dict(unavailable, reason='新闻风险或因素覆盖字段不完整，暂停新开仓')
    if not isinstance(value.get('evidence'), list):
        return unavailable
    expected = _DIRECTION.get(status)
    if value.get('direction') != expected or value['entry_allowed'] != bool(expected):
        return unavailable
    if kline_allowed and (status != 'insufficient' or value['evidence']
                          or value.get('event_id') not in (None, '')):
        return unavailable
    if any(r['blocking'] for r in value['risks']):
        # Facts may persist as background after a fixed acute cooldown. Granting
        # permission still requires a fresh producer snapshot, not a local timer.
        return dict(value, status='blocked', direction=None, entry_allowed=False, kline_allowed=False,
                    reason='新闻急性事件仍在冷静期，暂停新开仓')
    if not kline_allowed and any(d['status'] == 'review_required' for d in value['drivers']):
        return dict(value, status='blocked', direction=None, entry_allowed=False, kline_allowed=False,
                    reason='近期相关新闻驱动尚待核验，暂停新开仓')
    if expected and (not _text(value.get('event_id')) or not _verified_evidence(value['evidence'], expected, symbol, now)):
        return dict(unavailable, reason='新闻方向缺少完整、有效的独立核验证据，暂停新开仓')
    return dict(value, kline_allowed=kline_allowed)


def trend_confirmation(sig):
    """News requires both the closed EMA trend and last closed price move."""
    try:
        fast, slow = number(sig['fast']), number(sig['slow'])
        current, previous = number(sig['reference']), number(sig['previous_close'])
        if min(fast, slow, current, previous) <= 0:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return 'BUY' if fast > slow and current > previous else (
        'SELL' if fast < slow and current < previous else None)


def decide(sig, news, symbol, now_ms, position=None, interval=None):
    n = news_state(news, symbol, now_ms/1000)
    result = dict(action='WAIT', source='news+kline', reason='等待新闻与已收盘K线共同确认', news=n)
    try:
        tf = timeframe(sig.get('interval','1h'))
        if interval is not None and tf['interval'] != interval: raise ValueError()
    except ValueError:
        return dict(result, reason='K线周期与当前策略不一致，等待新周期数据')
    window_reason = ('等待下一根'+tf['label']+'K线结束；仅在收盘后'+str(tf['entry_window_ms']//1000)+'秒内评估开仓')
    ai_direction = active_direction(n.get('ai'), symbol, now_ms/1000)
    if ai_direction:result['source'] = 'ai+kline'
    candle = sig.get('candle')
    if isinstance(candle, bool) or not isinstance(candle, (int, float)) or not 0 <= now_ms-(candle+tf['duration_ms']) <= tf['stale_ms']:
        result['reason'] = '已收盘K线缺失或过期'
        return result
    confirmed = trend_confirmation(sig)
    direction = n.get('direction') if n.get('entry_allowed') is True else None
    # AI is a directional filter, never a replacement for healthy source/risk
    # gates. A factor bias or model prose is not an executable direction.
    if ai_direction and (n.get('entry_allowed') is True or n.get('kline_allowed') is True):
        if direction and direction != ai_direction:
            result['reason'] = 'AI综合方向与已核验新闻冲突，等待一致'
            return result
        side = sig.get('side')
        if position:
            if (isinstance(position, dict) and position.get('side') in ('BUY', 'SELL')
                    and ai_direction != position['side'] and side == ai_direction and confirmed == ai_direction
                    and now_ms-(candle+tf['duration_ms']) <= tf['entry_window_ms']):
                result.update(action='EXIT', reason='AI综合方向与已收盘突破同向反转，退出原持仓')
            else:result['reason'] = '继续持仓保护；AI反向退出仍需新的同向收盘突破'
            return result
        if now_ms-(candle+tf['duration_ms']) > tf['entry_window_ms']:
            result['reason'] = 'AI综合方向已纳入；'+window_reason
        elif side not in (None, 'BUY', 'SELL'):
            result['reason'] = 'K线方向格式无效，禁止开仓'
        elif side and side != ai_direction:
            result['reason'] = 'AI综合方向与已收盘突破冲突，禁止开仓'
        elif side == ai_direction and confirmed == ai_direction:
            result.update(action=side, reason='AI综合方向与已收盘突破、趋势同向；仍须通过仓位风控')
        else:result['reason'] = 'AI综合方向已纳入，等待同向已收盘突破'
        return result
    if position:
        if (isinstance(position, dict) and position.get('side') in ('BUY', 'SELL')
                and direction and direction != position['side'] and confirmed == direction):
            result.update(action='EXIT', reason='核验新闻与已收盘趋势同向反转，退出原持仓')
        else:
            result['reason'] = '继续持仓保护；新闻不足以确认反转'
        return result
    if n.get('entry_allowed') is not True and n.get('kline_allowed') is not True:
        result['reason'] = n['reason']
        return result
    if n.get('kline_allowed') is True:
        result['source'] = 'kline'
    if now_ms-(candle+tf['duration_ms']) > tf['entry_window_ms']:
        result['reason'] = window_reason
        return result
    side = sig.get('side')
    if side not in (None, 'BUY', 'SELL'):
        result['reason'] = 'K线方向格式无效，禁止开仓'
        return result
    if n.get('kline_allowed') is True:
        result.update(source='kline', reason='新闻暂无可执行方向，等待已收盘K线突破')
        if side in ('BUY', 'SELL'):
            result.update(action=side, reason='新闻暂无可执行方向，采用已收盘K线突破信号')
        return result
    if side and direction != side:
        result['reason'] = '新闻与突破方向冲突，禁止开仓'
        return result
    if side in ('BUY', 'SELL'):
        result.update(action=side, reason='收盘突破与核验新闻方向一致')
    else:
        result['reason'] = '新闻已核验，等待同向已收盘突破；均线与动量不能单独触发新仓'
    return result
