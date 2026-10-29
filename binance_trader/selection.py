"""Deterministic, read-only cross-commodity qualification and ranking."""
import math
from .core import SYMBOLS, SLIPPAGE, number
from .strategy import decide
from .timeframes import timeframe


def fresh_candidate(value, symbol, now, interval=None):
    if not isinstance(value, dict) or value.get('symbol') != symbol or value.get('error'):
        raise ValueError('本品种行情未就绪或读取失败')
    for field, ttl in (('checked', 20), ('kline_checked', 10)):
        stamp = value.get(field)
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= now-stamp <= ttl:
            raise ValueError('本品种行情或K线过期')
    q = value['quote']
    if abs(now*1000-number(q['server'])) > 20000 or not 0 <= number(q['server'])-number(q['time']) <= 15000:
        raise ValueError('本品种标记价格过期')
    if interval is not None and value['signal'].get('interval','1h') != interval:
        raise ValueError('候选K线周期与当前策略不一致')
    return value['signal'], q


def entry_signal(sig, news, symbol, server, interval=None):
    decision = decide(sig, news, symbol, server, interval=interval)
    if decision['action'] not in ('BUY', 'SELL'):
        raise ValueError(decision['reason'])
    if sig.get('side') != decision['action']:
        raise ValueError('自动选品等待同向已收盘突破')
    tf = timeframe(sig.get('interval','1h'))
    age = server-(number(sig['candle'])+tf['duration_ms'])
    if not 0 <= age <= tf['entry_window_ms']:
        raise ValueError('已超过当前周期的收盘入场窗口')
    return decision


def depth_cost(book, side, quantity, price, now_ms):
    if not isinstance(book, dict) or abs(now_ms-number(book.get('T', 0))) > 15000:
        raise ValueError('盘口缺失或过期')
    bids, asks = book.get('bids'), book.get('asks')
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ValueError('盘口深度不足')
    def levels(rows, descending):
        result = [(number(row[0]), number(row[1])) for row in rows]
        if any(p <= 0 or q <= 0 for p, q in result):
            raise ValueError('盘口数量或价格无效')
        if any((a[0] <= b[0] if descending else a[0] >= b[0]) for a,b in zip(result,result[1:])):
            raise ValueError('盘口价格顺序异常')
        return result
    bids, asks = levels(bids, True), levels(asks, False)
    bid, ask = bids[0][0], asks[0][0]
    if ask <= bid or (ask-bid)/((ask+bid)/2) > .001:
        raise ValueError('买卖价差异常或超过0.10%')
    remaining, cash = quantity, 0
    for p, q in asks if side == 'BUY' else bids:
        take = min(remaining, q); cash += take*p; remaining -= take
        if remaining <= 1e-12: break
    if remaining > 1e-12:
        raise ValueError('可见盘口不足以覆盖计划数量')
    average = cash/quantity
    adverse = (average/price-1)*(1 if side == 'BUY' else -1)
    if adverse > SLIPPAGE or abs(average/price-1) > .003:
        raise ValueError('盘口预计成交偏离超过滑点预算')
    return dict(spread_pct=(ask-bid)/((ask+bid)/2)*100,
                estimated_fill=average, estimated_slippage_pct=max(0,adverse)*100)


def ranking(order, sig, depth):
    audit = order['risk_audit']; direction = 1 if order['side'] == 'BUY' else -1
    exit_price = order['target']*(1-direction*SLIPPAGE)
    entry = audit['expected_entry']; qty = order['quantity']
    net = direction*(exit_price-entry)*qty-(entry+exit_price)*qty*audit['taker_fee']-audit['funding_reserve']
    ratio = net/order['planned_loss']
    if not math.isfinite(ratio) or ratio < 1:
        raise ValueError('扣除费用及资金费预留后的计划盈亏比低于1')
    atr = number(sig['atr'])
    if atr <= 0: raise ValueError('波动指标无效')
    trend = abs(number(sig['fast'])-number(sig['slow']))/atr
    breakout = (number(sig['reference'])-number(sig['upper'] if direction == 1 else sig['lower']))*direction/atr
    return dict(net_reward_risk=round(ratio,6), trend_atr=round(trend,6), breakout_atr=round(breakout,6),
                planned_loss=order['planned_loss'], notional=order['notional'], **depth)


def scan(values, news_provider, inputs, now, consumed=None, global_candle=0, locked=False, lock_reason='', interval='1h', last_entry_close=0):
    """No writes. inputs returns a risk-checked plan; failures exclude one symbol."""
    result = dict(updated=now, selected=None, rows=[], locked=locked,
                  reason=(lock_reason or '持仓或订单核对中，保持原品种') if locked else '全部候选未达标，保持空仓')
    eligible = []
    for symbol in SYMBOLS:
        row = dict(symbol=symbol,status='excluded',side=None,reason='',rank=None)
        try:
            if locked:raise ValueError(result['reason'])
            sig,q = fresh_candidate(values.get(symbol),symbol,now,interval)
            row.update(price=q['price'],candle=sig.get('candle'))
            decision = entry_signal(sig,news_provider(symbol),symbol,q['server'],interval)
            row.update(side=decision['action'],source=decision['source'])
            if (sig['candle'] <= max((consumed or {}).get(symbol,0),global_candle) or
                sig['candle']+timeframe(interval)['duration_ms'] <= last_entry_close):
                raise ValueError('该收盘信号已处理，等待下一根K线')
            order = inputs(symbol,dict(sig,side=decision['action']),q)
            depth = depth_cost(values[symbol].get('book'),decision['action'],order['quantity'],q['price'],q['server'])
            row.update(ranking(order,sig,depth),status='eligible',reason=decision['reason'])
            eligible.append(row)
        except (ValueError, KeyError, TypeError, IndexError, ZeroDivisionError) as exc:
            row['reason'] = str(exc) if isinstance(exc,ValueError) else '候选数据不完整，暂不参与排序'
        result['rows'].append(row)
    eligible.sort(key=lambda r:(-r['net_reward_risk'],-r['trend_atr'],-r['breakout_atr'],SYMBOLS.index(r['symbol'])))
    for rank,row in enumerate(eligible,1): row['rank']=rank
    if eligible:
        winner = eligible[0]
        result.update(selected=winner['symbol'],reason='选择 '+winner['symbol']+'：合格候选中计划净盈亏比优先，同分比较趋势及突破强度')
    return result
