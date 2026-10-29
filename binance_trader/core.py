"""Closed-candle signals and shared, exchange-filter-aware risk sizing."""
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
import math
import time
from .risk import fresh_metadata, validate_brackets, estimate_liquidation, maintenance_margin
from .timeframes import timeframe

SYMBOLS = ("XAUUSDT", "CLUSDT", "BZUSDT", "XAGUSDT", "NATGASUSDT")
CAPITAL = 300.0
LIVE_CAPITAL_LIMIT = 100.0
FEE = 0.0005  # conservative configurable-model assumption, not account fee quote
SLIPPAGE = 0.0005


def number(value):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError("数值必须有限")
    return n


def floor_step(value, step):
    step = Decimal(str(step))
    if step <= 0:
        raise ValueError("无效交易精度")
    return float((Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN) * step)


def ceil_step(value, step):
    step = Decimal(str(step))
    if step <= 0:
        raise ValueError("无效交易精度")
    return float((Decimal(str(value)) / step).to_integral_value(rounding=ROUND_CEILING) * step)


def decimal_text(value):
    return format(Decimal(str(value)), "f")


def ema(values, period):
    result = values[0]
    for value in values[1:]:
        result += 2 / (period + 1) * (value - result)
    return result


def validate_candles(rows):
    if not isinstance(rows, list):raise ValueError('K线格式无效')
    for row in rows:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 7:raise ValueError()
            o,h,l,c,v = [number(row[i]) for i in range(1,6)]
            start,end = number(row[0]),number(row[6])
            if (min(o,h,l,c) <= 0 or v < 0 or h < max(o,l,c) or l > min(o,h,c)
                    or start < 0 or end < start or not start.is_integer() or not end.is_integer()):raise ValueError()
        except (ValueError, TypeError, IndexError, OverflowError):
            raise ValueError('K线OHLC、成交量或时间无效，禁止使用异常行情') from None
    return rows


def signal(rows, now_ms, interval='1h'):
    tf = timeframe(interval)
    closed = [r for r in rows if int(r[6]) < now_ms]
    validate_candles(closed)
    if len(closed) < 80:
        return {"side": None, "interval": interval, "reason": "等待至少80根已收盘的"+tf['label']+"K线"}
    if now_ms - int(closed[-1][6]) > tf['stale_ms']:
        raise ValueError("K线过期，禁止生成交易信号")
    if (any(int(b[0]) - int(a[0]) != tf['duration_ms'] for a, b in zip(closed, closed[1:])) or
        any(int(r[6])-int(r[0]) != tf['duration_ms']-1 for r in closed)):
        raise ValueError("K线不连续，禁止生成交易信号")
    closes = [number(r[4]) for r in closed]
    fast, slow = ema(closes, 20), ema(closes, 60)
    high = max(number(r[2]) for r in closed[-21:-1])
    low = min(number(r[3]) for r in closed[-21:-1])
    tr = [max(number(closed[i][2]) - number(closed[i][3]),
              abs(number(closed[i][2]) - closes[i-1]),
              abs(number(closed[i][3]) - closes[i-1])) for i in range(len(closed)-14, len(closed))]
    atr = sum(tr) / 14
    side = "BUY" if fast > slow and closes[-1] > high else (
        "SELL" if fast < slow and closes[-1] < low else None)
    return dict(side=side, interval=interval, candle=int(closed[-1][0]), reference=closes[-1], previous_close=closes[-2],
                atr=atr, fast=fast, slow=slow, upper=high, lower=low,
                reason="收盘突破且均线同向" if side else "等待收盘突破，当前不交易")


def leverage_cap(leverage, limits):
    leverage = number(leverage)
    if not leverage.is_integer() or leverage < 1:
        raise ValueError("杠杆必须是大于等于1的整数")
    caps = [number(b["cap"]) for b in limits.get("brackets", []) if leverage <= number(b["leverage"])]
    if not caps:
        raise ValueError("杠杆超过币安当前允许上限，或尚未读取杠杆档位")
    return max(caps)


def plan(sig, price, filters, risk=1.5, leverage=2, available=300, quote=None, fees=None):
    price, risk, available = number(price), number(risk), number(available)
    if sig.get("side") not in ("BUY", "SELL"):
        raise ValueError("当前没有开仓信号")
    if not 0 < risk <= 3:
        raise ValueError("单笔风险上限3 USDT")
    if price <= 0 or available <= 0:
        raise ValueError("价格或余额无效")
    limits = filters.get("leverageLimits", {})
    notional_cap = leverage_cap(leverage, limits)
    validate_brackets(limits, fresh=True)
    fresh_metadata(fees, 305, "账户手续费")
    try:
        taker = number(fees["taker"])
        liquidation_fee = number(filters["liquidationFee"])
        mark, funding = number(quote["mark"]), number(quote["funding"])
        quote_time, server_time = number(quote["time"]), number(quote["server"])
    except (KeyError, TypeError):
        raise ValueError("缺少标记价格、账户手续费或强平清算费，禁止开仓") from None
    if not 0 <= taker <= .02 or not 0 <= liquidation_fee <= .2 or mark <= 0 or abs(funding) > .1:
        raise ValueError("费用或标记价格数据无效")
    if not 0 <= server_time - quote_time <= 15000 or abs(time.time()*1000-server_time) > 20000:
        raise ValueError("标记价格过期，禁止开仓")
    if abs(mark / price - 1) > .003:
        raise ValueError("成交价与标记价格偏离超过0.3%，禁止开仓")
    atr, reference = number(sig["atr"]), number(sig["reference"])
    if atr < 0 or reference <= 0:
        raise ValueError("K线波动或信号价格无效")
    distance = max(2 * atr, price * .01)
    if distance / price > .08:
        raise ValueError("波动过大，止损距离超过8%，跳过交易")
    if abs(price / reference - 1) > .005:
        raise ValueError("价格已偏离信号收盘价0.5%，不追单")
    direction = 1 if sig["side"] == "BUY" else -1
    stop = (ceil_step if direction == 1 else floor_step)(price - direction * distance, filters["tick"])
    target = (floor_step if direction == 1 else ceil_step)(price + direction * 2 * distance, filters["tick"])
    if stop <= 0 or target <= 0:
        raise ValueError("无效保护价格")
    if direction * (mark - stop) <= 0 or direction * (target - mark) <= 0:
        raise ValueError("标记价格已越过保护价，禁止开仓")
    # No credit is assumed for receiving funding. Three events is a reserve,
    # not a bound on holding-period charges; the engine accounts for each event.
    funding_rate = max(abs(funding), .0001) * 3
    expected_entry = price * (1 + direction * SLIPPAGE)
    stressed_exit = stop * (1 - direction * SLIPPAGE)
    unit_funding = max(expected_entry, stressed_exit) * funding_rate
    unit_fee = (expected_entry + stressed_exit) * taker
    unit_risk = direction * (expected_entry - stressed_exit) + unit_fee + unit_funding
    unit_margin = expected_entry / leverage
    unit_reserve = unit_fee + unit_funding + price * SLIPPAGE * 2
    quantity = floor_step(min(risk / unit_risk, min(150, notional_cap) / max(price, expected_entry, mark),
                              available * .2 / (unit_margin + unit_reserve),
                              filters["maxQty"]), filters["step"])
    notional = quantity * price
    if quantity < filters["minQty"] or quantity*min(price, expected_entry) < filters["minNotional"]:
        raise ValueError("风控仓位低于交易所最小下单量，本次跳过")
    initial_margin = quantity * unit_margin
    reserve = quantity * unit_reserve
    # Haircut isolated collateral by entry/exit fees and reserved funding. This
    # intentionally places the estimate closer to the stop than a zero-fee model.
    conservative_wallet = initial_margin - quantity * (unit_fee + unit_funding)
    liquidation = estimate_liquidation(sig["side"], quantity, expected_entry,
                                      conservative_wallet, limits)
    gap = direction * (stop - liquidation["estimated_liquidation"])
    required_gap = max(price * .005, abs(expected_entry-stop) * .25,
                       number(filters["tick"]) * 2)
    if gap < required_gap:
        raise ValueError("所选杠杆的止损与强平安全间距不足；请降低杠杆（当前间距 %.6g，要求 %.6g）" %
                         (gap, required_gap))
    audit = dict(**liquidation, model="isolated-one-way-conservative-v1",
                 mark_price=mark, quote_time=quote_time, expected_entry=expected_entry,
                 taker_fee=taker, fee_source=fees.get("source", "account"),
                 fee_updated=fees["updated"], slippage_rate=SLIPPAGE,
                 funding_reserve_rate=funding_rate, funding_reserve_events=3,
                 funding_reserve=quantity*unit_funding, fee_reserve=quantity*unit_fee,
                 liquidation_fee=liquidation_fee,
                 maintenance_at_entry=maintenance_margin(quantity*mark, limits),
                 conservative_isolated_wallet=conservative_wallet,
                 margin_reserve=reserve, initial_margin=initial_margin,
                 liquidation_gap=gap, required_liquidation_gap=required_gap,
                 bracket_updated=limits["updated"], limits=limits)
    return dict(side=sig["side"], quantity=quantity, entry=price, stop=stop,
                target=target, notional=notional, margin=initial_margin,
                initial_margin=initial_margin, margin_reserve=reserve,
                cash_required=initial_margin+reserve, planned_loss=quantity*unit_risk,
                risk_budget=risk, leverage=leverage, risk_audit=audit,
                estimated_liquidation=liquidation["estimated_liquidation"],
                liquidation_gap=gap, required_liquidation_gap=required_gap)
