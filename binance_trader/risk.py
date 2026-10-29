"""Isolated USDT linear-contract risk checks; estimates never replace venue state.

The maintenance equation follows Binance's isolated, one-way formula:
wallet + side * quantity * (mark - entry) = quantity * mark * MMR - cum.
The tier must be selected using the notional *at the resulting mark price*.
"""
import math
import time


def finite(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError("风控数据缺失或无效") from None
    if not math.isfinite(result):
        raise ValueError("风控数据必须有限")
    return result


def fresh_metadata(data, maximum_age, label, now=None):
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        raise ValueError(label + "缺失，禁止开仓")
    updated, expires = finite(data.get("updated")), finite(data.get("expires"))
    if not 0 <= now - updated <= maximum_age or expires <= now:
        raise ValueError(label + "已过期，禁止开仓")


def validate_brackets(limits, fresh=False, now=None):
    if not isinstance(limits, dict) or not limits.get("brackets"):
        raise ValueError("缺少维持保证金档位，禁止开仓")
    if fresh:
        fresh_metadata(limits, 65, "保证金档位", now)
    result = []
    try:
        for b in limits["brackets"]:
            row = {key: finite(b[key]) for key in
                   ("leverage", "floor", "cap", "maintMarginRatio", "cum")}
            if (not row["leverage"].is_integer() or row["leverage"] < 1
                    or not 0 <= row["floor"] < row["cap"]
                    or not 0 < row["maintMarginRatio"] < 1 or row["cum"] < 0):
                raise ValueError("维持保证金档位数值无效")
            result.append(row)
    except (KeyError, TypeError):
        raise ValueError("缺少维持保证金率或维持保证金速算额，禁止开仓") from None
    result.sort(key=lambda b: b["floor"])
    if result[0]["floor"] != 0 or result[0]["cum"] != 0:
        raise ValueError("维持保证金首档无效")
    for previous, current in zip(result, result[1:]):
        boundary = current["floor"]
        old_mm = boundary * previous["maintMarginRatio"] - previous["cum"]
        new_mm = boundary * current["maintMarginRatio"] - current["cum"]
        if (not math.isclose(previous["cap"], boundary, rel_tol=1e-10, abs_tol=1e-8)
                or current["maintMarginRatio"] < previous["maintMarginRatio"]
                or current["leverage"] > previous["leverage"]
                or not math.isclose(old_mm, new_mm, rel_tol=1e-6, abs_tol=1e-6)):
            raise ValueError("维持保证金档位不连续或不一致，禁止开仓")
    return result


def maintenance_margin(notional, limits):
    notional = finite(notional)
    if notional < 0:
        raise ValueError("仓位名义价值无效")
    for b in validate_brackets(limits):
        if b["floor"] <= notional < b["cap"]:
            return max(0, notional * b["maintMarginRatio"] - b["cum"])
    raise ValueError("仓位超过已知维持保证金档位")


def estimate_liquidation(side, quantity, entry, isolated_wallet, limits):
    """Solve every tier, retaining the solution in that tier (including crossing)."""
    if side not in ("BUY", "SELL"):
        raise ValueError("持仓方向无效")
    quantity, entry, wallet = map(finite, (quantity, entry, isolated_wallet))
    if quantity <= 0 or entry <= 0 or wallet <= 0:
        raise ValueError("逐仓保证金不足以覆盖费用预留")
    direction = 1 if side == "BUY" else -1
    brackets = validate_brackets(limits)
    if side == "BUY" and wallet >= quantity * entry:
        return dict(estimated_liquidation=0.0, maintMarginRatio=brackets[0]["maintMarginRatio"],
                    maintenance_cum=0.0, liquidation_tier=0)
    for index, b in enumerate(brackets):
        liquidation = (direction * quantity * entry - wallet - b["cum"]) / (
            quantity * (direction - b["maintMarginRatio"]))
        notional = quantity * liquidation
        if liquidation >= 0 and b["floor"] <= notional < b["cap"]:
            return dict(estimated_liquidation=liquidation,
                        maintMarginRatio=b["maintMarginRatio"], maintenance_cum=b["cum"],
                        liquidation_tier=index)
    raise ValueError("强平估算超出已知保证金档位，禁止开仓")


def validate_position_risk(order, row, mark=None):
    """Fail closed on mismatched venue state, or a stop inside the safety gap.

    V3 has no marginType; a positive isolatedWallet and isolatedMargin identify
    the isolated position. updateTime is its last change, not a quote timestamp.
    The caller must fetch this response immediately before validation.
    """
    if not isinstance(row, dict) or row.get("symbol") != order.get("symbol"):
        raise ValueError("成交后未确认本品种持仓")
    if row.get("positionSide") != "BOTH" or row.get("marginAsset") != "USDT":
        raise ValueError("只允许单向USDT逐仓持仓")
    wallet, margin = finite(row.get("isolatedWallet")), finite(row.get("isolatedMargin"))
    if wallet <= 0 or margin <= 0 or row.get("marginType", "isolated") != "isolated":
        raise ValueError("交易所未确认逐仓保证金")
    quantity, entry = finite(row.get("positionAmt")), finite(row.get("entryPrice"))
    direction = 1 if order["side"] == "BUY" else -1
    if entry <= 0 or quantity * direction <= 0 or not math.isclose(
            abs(quantity), finite(order["quantity"]), rel_tol=1e-8, abs_tol=1e-12):
        raise ValueError("交易所持仓方向或数量与订单不一致")
    liquidation = finite(row.get("liquidationPrice"))
    mark = finite(row.get("markPrice") if mark is None else mark)
    if liquidation < 0 or mark <= 0 or (liquidation == 0 and (
            direction != 1 or wallet < abs(quantity) * entry)):
        raise ValueError("交易所未返回有效强平价")
    gap = direction * (finite(order["stop"]) - liquidation)
    required = finite(order.get("required_liquidation_gap"))
    if required <= 0 or gap < required or direction * (mark - order["stop"]) <= 0:
        raise ValueError("交易所强平价与止损安全间距不足，必须减仓")
    audit = order.get("risk_audit", {})
    taker = finite(audit.get("taker_fee"))
    slippage = finite(audit.get("slippage_rate"))
    funding = finite(audit.get("funding_reserve_rate"))
    exit_price = order["stop"] * (1 - direction * slippage)
    loss = abs(quantity) * (direction * (entry - exit_price)
            + (entry + exit_price) * taker + max(entry, exit_price) * funding)
    budget = finite(order.get("risk_budget"))
    if loss > budget + 1e-8:
        raise ValueError("实际成交价使止损损失超过预算，必须减仓")
    return dict(actual_liquidation=liquidation, actual_liquidation_gap=gap,
                actual_isolated_wallet=wallet, actual_planned_loss=loss,
                venue_risk_checked=time.time())
