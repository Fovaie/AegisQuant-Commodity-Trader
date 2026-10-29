"""Single-position execution loop with durable intents and loss latches."""
import json
import math
import sqlite3
import threading
import time
import uuid
import copy
import hashlib
from datetime import datetime, timezone
from .api import APIError
from .core import CAPITAL, LIVE_CAPITAL_LIMIT, FEE, SLIPPAGE, SYMBOLS, decimal_text, number, plan, signal, leverage_cap
from .strategy import decide, news_state
from .risk import estimate_liquidation, validate_position_risk
from .selection import scan, fresh_candidate, entry_signal, depth_cost
from .timeframes import timeframe, candles_for


def day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Engine:
    def __init__(self, api, path, mode="paper"):
        if (mode not in ("paper", "demo", "live") or (mode == "demo" and not api.demo)
                or (mode == "live" and (api.demo or getattr(api, "live_trading", False) is not True))):
            raise ValueError("执行模式与API通道不匹配；实盘必须显式开启交易能力")
        self.api, self.path, self.mode = api, path, mode
        self.venue = "实盘" if mode == "live" else "Demo"
        self.live_connected = False
        self.leverage_limits = api.leverage_limits
        self.fee_rates = api.commission_rates
        self.news_provider = lambda symbol: None
        self.candidate_provider = lambda: {}
        self.selection = dict(updated=0,selected=None,rows=[],reason='等待五品种扫描')
        self.candidate_values = {}
        self.decision = {"action": "WAIT", "reason": "等待行情与新闻核验", "source": "kline"}
        self.risk_preview = {"ok": False, "reason": "等待保证金、费率和行情数据", "updated": 0}
        self.last_preview = 0
        self.funding_reconcile_due = 0
        self.lock = threading.RLock()
        self.quote = None
        self.rows = []
        self.sig = {"side": None, "reason": "正在连接行情"}
        self.error = ""
        self.pending_review = None
        self.emergency_attempts = set()
        self.updated = 0
        self.last_candles = 0
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        saved = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.s = json.loads(saved[0]) if saved else dict(mode=mode, wallet=0 if mode == "live" else CAPITAL, position=None,
            enabled=False, symbol=SYMBOLS[0], risk=1.5, leverage=2, events=[], last_candle=0,
            halted="", pending=None, date=day(), day_start=CAPITAL, remote_base=None, key_id=None,
            total_latch=False, daily_latch=False, curve=[])
        if self.s["mode"] != mode:
            self.db.close()
            raise ValueError("账本环境不匹配")
        old_symbols = [self.s["symbol"]] + [v.get("symbol") for v in
            (self.s["position"], self.s["pending"]) if v and v.get("symbol")]
        if any(s not in SYMBOLS for s in old_symbols):
            if self.s["position"] or self.s["pending"]:
                self.db.close()
                raise ValueError("旧账本有非商品持仓或未确认订单；账本已保留，请先核对处理，不能直接迁移")
            self.s.update(symbol=SYMBOLS[0], last_candle=0, enabled=False)
            self.event("交易范围已限定为五种商品，默认选择黄金；历史资金和风控记录保留")
        if mode == "demo" and self.s["key_id"]:
            import hashlib
            if hashlib.sha256(api.key.encode()).hexdigest() != self.s["key_id"]:
                raise ValueError("Demo账本绑定了其他API密钥；拒绝读取或执行")
        self.s["enabled"] = False  # restart never silently re-arms entries
        self.s.setdefault('selection_mode','manual')
        if self.s['selection_mode'] not in ('manual','auto'):
            self.s['selection_mode']='manual'
        self.s.setdefault('last_candles_by_symbol',{self.s['symbol']:self.s['last_candle']})
        self.s.setdefault('last_auto_candle',0)
        self.s.setdefault('interval','1h')
        timeframe(self.s['interval'])
        self.s.setdefault('candle_history',{})
        previous_candle=max([self.s['last_auto_candle']]+list(self.s['last_candles_by_symbol'].values()))
        self.s.setdefault('last_entry_close',previous_candle+timeframe(self.s['interval'])['duration_ms'] if previous_candle else 0)
        if mode == "live":
            self.s.setdefault("capital_budget", 0)
            self.s.setdefault("available_balance", 0)
            self.s.setdefault("last_flat_wallet", None)
            self.s.setdefault("loss_limit", 0)
            self.s.setdefault("daily_loss_limit", 0)
            self.s.setdefault("has_traded", bool(self.s["position"] or self.s["pending"]))
            frozen = self.s["has_traded"] or self.s["position"] or self.s["pending"]
            # Existing traded ledgers keep their risk baseline. An untouched ledger
            # can adopt the smaller trial budget without resetting incurred losses.
            self.s.setdefault("capital_limit", self.s["capital_budget"] if frozen else LIVE_CAPITAL_LIMIT)
            if not frozen and self.s["capital_budget"] > self.s["capital_limit"]:
                reduction = self.s["capital_budget"] - self.s["capital_limit"]
                self.s.update(capital_budget=self.s["capital_limit"],
                              loss_limit=self.s["capital_limit"]*.15,
                              daily_loss_limit=self.s["capital_limit"]*.02,
                              day_start=self.s["day_start"]-reduction)
        if self.s["pending"]:
            self.s["halted"] = "上次交易未完成核对；请到币安"+self.venue+"检查订单和持仓，禁止重复开仓"
        self.save()

    def save(self):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (1,?)", (json.dumps(self.s),))
        self.db.commit()

    def event(self, message, *, trade=None, execution=None):
        event = {"time": int(time.time()*1000), "message": message}
        self.s["events"].insert(0, event)
        self.s["events"] = self.s["events"][:150]
        # Full history survives the 150-row UI window. No credentials/account
        # identity are recorded; estimated fee reserves are not actual commissions.
        record = dict(event, schema_version=1, mode=self.mode,
                      symbol=self.s["symbol"], leverage=self.s["leverage"],
                      risk_budget=self.s["risk"], capital_budget=self.capital_budget(),
                      wallet_observed=self.s["wallet"],
                      selection_mode=self.s.get("selection_mode", "manual"),
                      trade=trade if trade is not None else self.s.get("position"),
                      execution=execution,
                      cost_note="risk_audit中的费用为开仓预估；funding_paid仅截至最近核验。实际成交手续费及最终资金费需与交易所账单核对。")
        self.db.execute("INSERT INTO journal(body) VALUES (?)", (json.dumps(record, ensure_ascii=False),))

    def equity(self):
        p = self.s["position"]
        unreal = 0
        if p and self.quote and not p.get("remote_flat_confirmed"):
            unreal = (self.quote["mark"]-p["entry"])*p["quantity"]*(1 if p["side"] == "BUY" else -1)
        return self.s["wallet"] + unreal

    def loss_check(self):
        if self.mode == "live" and (self.s["remote_base"] is None or
                (not self.s.get("has_traded") and self.capital_budget() <= 0)):
            return False
        equity = self.strategy_equity()
        if self.s["date"] != day():
            self.s.update(date=day(), day_start=equity, daily_latch=False)
        if equity <= self.capital_budget()-self.loss_limit():
            self.s["total_latch"] = True
        if equity <= self.s["day_start"]-self.daily_loss_limit():
            self.s["daily_latch"] = True
        if self.s["total_latch"] or self.s["daily_latch"]:
            self.s["enabled"] = False
            return True
        return False

    def capital_budget(self):
        return self.s.get("capital_budget", 0) if self.mode == "live" else CAPITAL

    def loss_limit(self):
        return self.s.get("loss_limit", 0) if self.mode == "live" else 45

    def daily_loss_limit(self):
        return self.s.get("daily_loss_limit", 0) if self.mode == "live" else 6

    def strategy_equity(self):
        if self.mode != "live":
            return self.equity()
        baseline = self.s["remote_base"]
        return 0 if baseline is None else self.capital_budget()+self.equity()-baseline

    def sizing_balance(self):
        if self.mode != "live":
            return self.s["wallet"]
        return max(0, min(self.s.get("available_balance", 0), self.capital_budget(), self.strategy_equity()))

    def require_live_connection(self):
        if self.mode == "live" and (not self.live_connected or not self.api.key or not self.api.secret):
            raise ValueError("实盘尚未连接；请在App重新输入密钥，连接后再操作")

    def snapshot(self):
        with self.lock:
            return dict(version="2026.10.09-account-rotation", pending_recovery_supported=True, credential_rotation_supported=True,
                account_identity_bound=bool(self.s.get("account_uid_hash")), mode=self.mode, state=json.loads(json.dumps(self.s)), quote=self.quote,
                signal=self.sig, error=self.error, updated=self.updated, equity=self.equity(),
                decision=self.decision, risk_preview=self.risk_preview, selection=self.selection,
                live_connected=self.live_connected, account_bound=bool(self.s["key_id"]),
                initial_equity=self.s["remote_base"] if self.mode == "live" else CAPITAL,
                capital_budget=self.capital_budget(), strategy_equity=self.strategy_equity(),
                capital_limit=self.s.get("capital_limit", CAPITAL),
                loss_limit=self.loss_limit(), daily_loss_limit=self.daily_loss_limit(),
                candles=[{"time": r[0], "close": number(r[4])} for r in self.rows
                         if self.quote and int(r[6]) < self.quote["server"]][-80:],
                credentials=bool(self.api.key and self.api.secret))

    def evaluate(self, sig, news, symbol, now_ms, position=None):
        return decide(sig, news, symbol, now_ms, position, interval=self.s['interval'])

    def read_candles(self, symbol):
        return candles_for(self.api, symbol, self.s['interval'])

    def configure(self, body):
        with self.lock:
            if self.s["enabled"] or self.s["position"] or self.s["pending"]:
                raise ValueError("请先暂停策略并平仓，再修改配置")
            symbol = body.get("symbol")
            interval=body.get('interval',self.s['interval'])
            timeframe(interval)
            selection_mode = body.get('selection_mode',self.s['selection_mode'])
            if selection_mode not in ('manual','auto'):
                raise ValueError('选品模式必须为手动固定或五品种自动选择')
            risk, leverage = number(body.get("risk")), number(body.get("leverage"))
            if symbol not in SYMBOLS or not 0 < risk <= 3 or not leverage.is_integer() or leverage < 1:
                raise ValueError("交易对、风险或杠杆参数无效")
            leverage_cap(leverage, self.leverage_limits(symbol, force=True))
            if interval != self.s['interval']:
                self.s['candle_history'][self.s['interval']] = dict(consumed=dict(self.s['last_candles_by_symbol']), auto=self.s['last_auto_candle'])
                previous=self.s['candle_history'].get(interval,dict(consumed={},auto=0))
                self.s.update(interval=interval,last_candles_by_symbol=dict(previous['consumed']),last_auto_candle=previous['auto'])
                self.s['last_candle']=self.s['last_candles_by_symbol'].get(symbol,0)
                self.quote,self.rows,self.last_candles=None,[],0
                self.sig={'side':None,'interval':interval,'reason':'周期已修改，等待新的已收盘K线'}
            if symbol != self.s["symbol"]:
                self.quote, self.rows, self.last_candles = None, [], 0
                self.sig = {"side": None, "reason": "正在更新交易对行情"}
                self.s["last_candle"] = self.s['last_candles_by_symbol'].get(symbol,0)
            self.s.update(symbol=symbol, risk=risk, leverage=int(leverage),selection_mode=selection_mode)
            self.last_preview = 0
            self.risk_preview = {"ok": False, "reason": "配置已变更，等待重新计算", "updated": 0}
            self.save()

    def start(self):
        with self.lock:
            self.require_live_connection()
            if self.s['position'] or self.s['pending']:
                raise ValueError('已有持仓或待核对订单；继续保护，暂不能重新启动新开仓')
            if self.mode == "live":
                self.bind_account()
            if self.s['selection_mode']=='auto':
                if self.s['position'] or self.s['pending'] or self.s['halted'] or self.loss_check():
                    raise ValueError('持仓、订单或账户风控尚未核对，不能启动自动选品')
                values=self.candidate_provider()
                ready=0
                for symbol in SYMBOLS:
                    try:fresh_candidate(values.get(symbol),symbol,time.time(),self.s['interval']);ready+=1
                    except (ValueError,KeyError,TypeError):pass
                if not ready:raise ValueError('五品种行情尚未就绪，等待扫描完成')
                if self.sizing_balance() <= 0:raise ValueError('当前可用策略资金不足')
                # Verify account metadata without requiring any entry signal to exist.
                self.fee_rates(self.s['symbol'],force=True)
                if self.mode=='demo':self.bind_account()
                self.s['enabled']=True
                self.event('五品种自动选品已启动；仅在候选和风控均合格时开一笔仓，无合格候选保持空仓')
                self.save();return
            if self.error or not self.quote or time.time()-self.updated > 20:
                raise ValueError("行情尚未就绪或已过期，暂不能启动")
            if self.s["halted"] or self.s["pending"] or self.loss_check():
                raise ValueError("风控或订单核对锁定，禁止启动")
            leverage_cap(self.s["leverage"], self.leverage_limits(self.s["symbol"], force=True))
            fees = self.fee_rates(self.s["symbol"], force=True)
            if self.mode == "live":
                minimum = number(self.api.filters(self.s["symbol"])["minNotional"])
                minimum_cash = minimum/self.s["leverage"]+minimum*(2*fees["taker"]+.0013)
                if self.sizing_balance()*.2 < minimum_cash:
                    raise ValueError("实盘可用余额不足以满足最小订单及费用预留；请先补充资金再启动")
            n = news_state(self.news_provider(self.s["symbol"]), self.s["symbol"])
            if n.get("entry_allowed") is not True and n.get("kline_allowed") is not True:
                raise ValueError(n["reason"])
            if self.mode == "demo":
                self.bind_account()
            self.s["enabled"] = True
            self.event("自动策略已启动；新闻无明确方向时采用收盘K线突破，新闻风险与仓位风控仍生效")
            self.save()

    def pause(self):
        with self.lock:
            self.s["enabled"] = False
            self.event("已暂停新开仓，持仓保护继续运行")
            self.save()

    @staticmethod
    def live_balances(account):
        assets = account.get("assets")
        if not isinstance(assets, list):
            raise ValueError("实盘账户缺少USDT资产明细，不能使用合计余额替代")
        rows = [a for a in assets if a.get("asset") == "USDT"]
        if len(rows) != 1:
            raise ValueError("实盘账户USDT资产明细不唯一")
        wallet, available = number(rows[0]["walletBalance"]), number(rows[0]["availableBalance"])
        if wallet < 0 or available < 0:
            raise ValueError("USDT余额或可用保证金无效")
        return wallet, available

    def validate_remote_account(self, client, account):
        config = client.request("GET", "/fapi/v1/accountConfig", signed=True) if self.mode == "live" else account
        issues, checks = [], []

        def flag(payload, field, label):
            # Missing or malformed booleans do not establish an unsafe mode,
            # nor do they establish permission to trade. Never echo the payload.
            if not isinstance(payload, dict):
                issues.append(label+"响应格式异常（"+field+"），无法核验，请重新读取")
                return None
            if field not in payload:
                issues.append(label+"字段缺失（"+field+"），无法核验，请重新读取")
                return None
            value = payload[field]
            if type(value) is not bool:
                issues.append(label+"字段格式异常（"+field+"），无法核验，请重新读取")
                return None
            return value

        multi = flag(config, "multiAssetsMargin", "保证金模式")
        trade = flag(config, "canTrade", "账户可交易状态")
        checks.append("保证金模式："+({True:"联合保证金（多资产）",False:"单币保证金（单资产）"}.get(multi, "未确认")))
        checks.append("账户可交易："+({True:"是",False:"否"}.get(trade, "未确认")))
        if multi is True:
            issues.append("币安确认当前为联合保证金模式；本程序的逐仓风控仅支持单币保证金模式，请在币安核对后重连")
        if trade is False:
            issues.append("币安返回 canTrade=false，当前账户不允许交易，请核对合约账户状态；不能仅凭此字段认定是API密钥权限问题")
        try:
            dual = flag(client.request("GET", "/fapi/v1/positionSide/dual", signed=True), "dualSidePosition", "持仓模式")
        except APIError as exc:
            if not issues:
                raise
            issues.append("持仓模式读取失败："+str(exc))
            dual = None
        conflict = (isinstance(config, dict) and type(config.get("dualSidePosition")) is bool
                    and type(dual) is bool and config["dualSidePosition"] != dual)
        checks.append("持仓模式："+("未确认（接口不一致）" if conflict else {True:"双向",False:"单向"}.get(dual, "未确认")))
        if conflict:
            issues.append("账户配置与持仓模式接口返回不一致，请稍后重新读取；不能据此判断应修改哪项设置")
        elif dual is True:
            issues.append("币安确认当前为双向持仓；本程序仅支持单向持仓，请在币安核对后重连")
        if issues:
            raise ValueError("账户检查未通过。"+"；".join(checks)+"。"+"；".join(issues))
        positions = [p for p in account.get("positions", []) if number(p["positionAmt"]) != 0]
        p, pending = self.s["position"], self.s["pending"]
        confirmed_flat = False
        if p:
            if not positions:
                remote = client.position_risk(p["symbol"])
                if any(number(row.get("positionAmt", 0)) != 0 for row in remote):
                    raise ValueError("账户与持仓风险接口不一致，暂不能核验重连")
                confirmed_flat = True
            elif any(x.get("symbol") != p["symbol"] for x in positions) or len(positions) != 1:
                raise ValueError("账户持仓与本机账本不一致，连接不会平仓或改写账本")
            expected = p["quantity"]*(1 if p["side"] == "BUY" else -1)
            if positions and not math.isclose(number(positions[0]["positionAmt"]), expected, rel_tol=1e-8, abs_tol=1e-12):
                raise ValueError("账户持仓数量与本机账本不一致")
        elif positions and not (pending and len(positions) == 1 and positions[0].get("symbol") == pending.get("symbol")):
            raise ValueError("账户有其他持仓；必须使用专用空账户")
        ordinary = client.request("GET", "/fapi/v1/openOrders", signed=True)
        if not isinstance(ordinary, list) or any(not pending or o.get("clientOrderId") != pending.get("id") for o in ordinary):
            raise ValueError("账户有非本程序未完成普通订单")
        algos = client.request("GET", "/fapi/v1/openAlgoOrders", signed=True)
        if isinstance(algos, dict):
            algos = algos.get("orders", [])
        known = {str(i) for i in p.get("algos", [])} if p else set()
        if not isinstance(algos, list) or any(str(o.get("algoId")) not in known for o in algos):
            raise ValueError("账户有非本程序未完成条件订单")
        return confirmed_flat

    def refresh_live_wallet(self, account, check_flat=True, allow_pretrade_rebase=False):
        wallet, available = self.live_balances(account)
        previous = self.s.get("last_flat_wallet")
        flat = not self.s["position"] and not self.s["pending"]
        if allow_pretrade_rebase and flat and not self.s.get("has_traded"):
            budget = min(self.s["capital_limit"], wallet)
            self.s.update(remote_base=wallet, capital_budget=budget, loss_limit=budget*.15,
                          daily_loss_limit=budget*.02, day_start=budget, date=day(),
                          total_latch=False, daily_latch=False)
            if self.s["halted"].startswith("空仓期间账户余额发生变化"):
                self.s["halted"] = ""
            previous = wallet
        if check_flat and flat and previous is not None and not math.isclose(wallet, previous, rel_tol=1e-10, abs_tol=1e-7):
            self.s.update(enabled=False, halted="空仓期间账户余额发生变化，可能有转入转出或外部交易；原始预算不重置，请核对账户")
        self.s.update(wallet=wallet, available_balance=available)
        if flat:
            self.s["last_flat_wallet"] = wallet

    def _credential_change_ready(self):
        if self.mode != 'live' or self.s['enabled'] or self.s['position'] or self.s['pending']:
            raise ValueError('账户身份绑定或轮换须实盘策略暂停、空仓且无待核对订单')
        if not self.s['key_id']:raise ValueError('请先使用原密钥连接已绑定账户')

    def bind_account_identity(self):
        with self.lock:
            self._credential_change_ready()
            self.require_live_connection()
            if hashlib.sha256(self.api.key.encode()).hexdigest() != self.s['key_id']:
                raise ValueError('当前连接与原密钥绑定不一致')
            identity = self.api.account_identity()
            old = self.s.get('account_uid_hash')
            if old and old != identity:raise ValueError('原账户身份核验不一致，禁止改写绑定')
            self.validate_remote_account(self.api,self.api.account())
            before = copy.deepcopy(self.s)
            try:
                self.s['account_uid_hash'] = identity
                self.event('原密钥账户身份已只读核验并记录；未启动交易')
                self.save()
            except Exception:
                self.db.rollback();self.s = before;raise
            return dict(ok=True,account_identity_bound=True)

    def rotate_live_key(self, client):
        with self.lock:
            self._credential_change_ready()
            identity = self.s.get('account_uid_hash')
            if not identity:
                raise ValueError('尚无原账户身份记录；请在原密钥有效时先绑定，无法证明时不能轮换或清空账本')
            if client.demo or getattr(client,'live_trading',False) is not True or not client.key or not client.secret:
                raise ValueError('请使用显式实盘客户端进行密钥轮换')
            if client.account_identity() != identity:
                raise ValueError('新密钥属于不同账户，禁止更换；原账本及连接保留')
            account = client.account()
            self.live_balances(account)
            self.validate_remote_account(client,account)
            before = copy.deepcopy(self.s)
            try:
                self.s['key_id'] = hashlib.sha256(client.key.encode()).hexdigest()
                # Rotation must never rebase the budget, loss latches or original equity.
                self.refresh_live_wallet(account,allow_pretrade_rebase=False)
                self.event('同一账户密钥轮换已核验；保留原账本和亏损基准，新开仓仍暂停')
                self.save()
            except Exception:
                self.db.rollback();self.s = before;raise
            self.api = client
            self.leverage_limits,self.fee_rates = client.leverage_limits,client.commission_rates
            self.live_connected = True
            return dict(ok=True,connected=True,rotated=True,balance=self.s['wallet'],
                available=self.s['available_balance'],positions=0,capital_budget=self.capital_budget())

    def attach_live(self, client):
        """Explicit user connection: validate with GETs only; never arm or mutate venue."""
        with self.lock:
            if self.mode != "live" or client.demo or getattr(client, "live_trading", False) is not True:
                raise ValueError("只有显式实盘客户端可以连接实盘账本")
            if not client.key or not client.secret:
                raise ValueError("请填写实盘API密钥")
            import hashlib
            identity = hashlib.sha256(client.key.encode()).hexdigest()
            if self.s["key_id"] and self.s["key_id"] != identity:
                raise ValueError("实盘账本已绑定其他API密钥；禁止更换账户或重置亏损基准")
            account = client.account()
            self.live_balances(account)
            confirmed_flat = self.validate_remote_account(client, account)
            if self.s["position"] and not self.s["pending"] and not confirmed_flat:
                # Verify existing venue protection without the emergency-close path.
                self.verify_demo_protection(self.s["position"], api=client)
                rows = [r for r in client.position_risk(self.s["position"]["symbol"]) if number(r.get("positionAmt", 0)) != 0]
                if len(rows) != 1:
                    raise ValueError("重连未能核验唯一持仓")
                validate_position_risk(self.s["position"], rows[0])
            self.api = client
            self.leverage_limits, self.fee_rates = client.leverage_limits, client.commission_rates
            self.s.update(key_id=identity, enabled=False)
            if self.s["position"]:
                self.s["position"]["remote_flat_confirmed"] = confirmed_flat
            self.refresh_live_wallet(account, allow_pretrade_rebase=True)
            self.live_connected = True
            self.loss_check()
            self.event("实盘账户已连接并完成只读核验；新开仓仍暂停")
            self.save()
            usdt = next(row for row in account["assets"] if row.get("asset") == "USDT")
            return dict(ok=True, connected=True, balance=self.s["wallet"], wallet=self.s["wallet"], available=self.s["available_balance"],
                        unrealized=number(usdt.get("unrealizedProfit", 0)),
                        initial_equity=self.s["remote_base"], capital_budget=self.capital_budget(),
                        positions=sum(number(p["positionAmt"]) != 0 for p in account.get("positions", [])), halted=self.s["halted"])

    def bind_account(self):
        import hashlib
        self.require_live_connection()
        identity = hashlib.sha256(self.api.key.encode()).hexdigest()
        if self.s["key_id"] and self.s["key_id"] != identity:
            raise ValueError("API密钥与此账户账本不一致，请使用独立账本")
        account = self.api.account()
        confirmed_flat = self.validate_remote_account(self.api, account)
        if self.s["position"]:
            self.s["position"]["remote_flat_confirmed"] = confirmed_flat
        if self.mode == "live":
            self.refresh_live_wallet(account, allow_pretrade_rebase=not self.s["enabled"])
        else:
            if self.s["remote_base"] is None:
                self.s["remote_base"] = number(account["totalWalletBalance"])
            self.s["wallet"] = CAPITAL+number(account["totalWalletBalance"])-self.s["remote_base"]
        self.s["key_id"] = identity
        self.save()
        return account

    def check_account(self):
        """Explicit account refresh: GETs only, never enable entries or close."""
        with self.lock:
            if self.mode != "live":
                raise ValueError("此核验仅用于实盘账户")
            before = self.capital_budget()
            account = self.bind_account()
            self.loss_check()
            self.last_preview = 0
            self.risk_preview = dict(ok=False, reason="账户已更新，等待重新计算风险预检", updated=time.time())
            if self.capital_budget() != before:
                self.event("首次交易前资金已重新核验：策略预算 %.2f USDT，上限 %.2f USDT；新开仓仍暂停" %
                           (self.capital_budget(), self.s["capital_limit"]))
            self.save()
            return account

    def assert_flat(self, account):
        if any(number(p["positionAmt"]) != 0 for p in account.get("positions", [])):
            raise ValueError("账户有其他持仓，不能启动；请使用专用空账户")
        if self.api.request("GET", "/fapi/v1/openOrders", signed=True):
            raise ValueError("账户有未完成普通订单")
        algos = self.api.request("GET", "/fapi/v1/openAlgoOrders", signed=True)
        if isinstance(algos, dict): algos = algos.get("orders", [])
        if algos: raise ValueError("账户有未完成条件订单")

    def apply_leverage(self, symbol=None, leverage=None):
        """One explicit exchange change, with durable intent and no blind retry."""
        with self.lock:
            self.require_live_connection()
            if self.mode not in ("demo", "live"):
                raise ValueError("模拟模式只能保存本地杠杆设置")
            if self.s["enabled"] or self.s["position"] or self.s["pending"] or self.s["halted"]:
                raise ValueError("请先暂停、平仓并完成订单核对，再修改交易所杠杆")
            symbol = self.s["symbol"] if symbol is None else symbol
            leverage = self.s["leverage"] if leverage is None else number(leverage)
            if symbol != self.s["symbol"] or leverage != self.s["leverage"]:
                raise ValueError("交易所杠杆必须与已保存的当前品种和倍数一致")
            self.bind_account()
            if self.s["halted"] or self.loss_check():
                raise ValueError("账户风控已锁定，不能修改交易所杠杆")
            leverage_cap(leverage, self.leverage_limits(symbol, force=True))
            self.s["pending"] = dict(kind="leverage", symbol=symbol, leverage=int(leverage), id="bl"+uuid.uuid4().hex[:24])
            self.save()
            try:
                result = self.api.request("POST", "/fapi/v1/leverage", dict(symbol=symbol, leverage=int(leverage)), True)
                if (result.get("symbol") != symbol or number(result.get("leverage", 0)) != leverage
                        or number(result.get("maxNotionalValue", 0)) <= 0):
                    raise APIError("交易所未确认杠杆修改结果")
                self.s["pending"] = None
                self.event(self.venue+"杠杆已核验为 %s 倍；策略仍暂停" % int(leverage))
                self.save()
                return dict(symbol=symbol, leverage=int(leverage), maxNotionalValue=number(result["maxNotionalValue"]))
            except Exception:
                self.s.update(enabled=False, halted="杠杆修改结果待核对；禁止重试或开仓，请在交易所检查")
                self.save()
                raise

    def inspect_pending(self):
        """Read venue evidence only. Never retry, cancel or infer a missing order."""
        with self.lock:
            self.pending_review = None
            self.require_live_connection()
            pending, position = self.s['pending'], self.s['position']
            if self.mode not in ('live', 'demo') or not pending:
                raise ValueError('当前没有待核对交易请求')
            kind, symbol, identity = (pending.get(k) for k in ('kind', 'symbol', 'id'))
            if kind not in ('leverage', 'entry', 'close') or symbol not in SYMBOLS or not isinstance(identity, str):
                raise ValueError('待核对记录不完整，保留锁定，请核对原始账本')
            if not identity.startswith({'leverage':'bl', 'entry':'bt', 'close':'bc'}[kind]):
                raise ValueError('原请求标识不符合本程序规则，保留锁定')
            account = self.api.account()
            self.live_balances(account) if self.mode == 'live' else number(account['totalWalletBalance'])
            if not isinstance(account.get('positions'), list):
                raise ValueError('账户持仓响应不完整，保留锁定')
            self.validate_remote_account(self.api, account)
            positions = [r for r in account['positions'] if number(r['positionAmt']) != 0]
            remote = self.api.position_risk(symbol)
            if not isinstance(remote, list) or any(r.get('symbol') != symbol for r in remote):
                raise ValueError('持仓风险响应品种不一致，保留锁定')
            own = [r for r in remote if number(r['positionAmt']) != 0]
            ordinary = self.api.request('GET', '/fapi/v1/openOrders', signed=True)
            algos = self.api.request('GET', '/fapi/v1/openAlgoOrders', signed=True)
            if isinstance(algos, dict):algos = algos.get('orders')
            if not isinstance(ordinary, list) or not isinstance(algos, list):
                raise ValueError('未完成订单响应不完整，保留锁定')
            flat = not positions and not own
            result = dict(kind=kind, symbol=symbol, pending_id=identity, can_resolve=False,
                          outcome='', reason='', checked_at=time.time())
            evidence = dict(pending=pending, position=position, halted=self.s['halted'],
                            positions=positions, remote=remote, orders=ordinary, algos=algos,
                            wallet=account['totalWalletBalance'])
            if kind == 'leverage':
                config = self.api.request('GET', '/fapi/v1/symbolConfig', dict(symbol=symbol), True)
                evidence['config'] = config
                if (not isinstance(config, list) or len(config) != 1 or config[0].get('symbol') != symbol
                        or isinstance(config[0].get('leverage'), bool)):
                    raise ValueError('杠杆配置响应不完整，保留锁定')
                actual = number(config[0]['leverage'])
                result['reason'] = '交易所杠杆为 %s 倍；原请求为 %s 倍' % (actual, pending.get('leverage'))
                if flat and not position and not ordinary and not algos and actual == pending.get('leverage'):
                    result.update(can_resolve=True, outcome='flat', reason=result['reason']+'；已交叉核验空仓且无未完成订单')
                else:result['reason'] += '；条件未全部一致，保留锁定'
            else:
                def terminal_order(order_id, order_kind):
                    row = self.api.request('GET', '/fapi/v1/order', dict(symbol=symbol, origClientOrderId=order_id), True)
                    if (not isinstance(row, dict) or row.get('symbol') != symbol or row.get('clientOrderId') != order_id
                            or row.get('type') != 'MARKET' or row.get('positionSide') != 'BOTH'
                            or row.get('side') not in ('BUY', 'SELL') or row.get('reduceOnly') is not (order_kind == 'close')):
                        raise ValueError('原订单身份或交易语义不一致，保留锁定')
                    qty, original = number(row['executedQty']), number(row['origQty'])
                    if qty < 0 or original <= 0 or qty > original or (qty and number(row['avgPrice']) <= 0):
                        raise ValueError('原订单成交数据无效，保留锁定')
                    if row.get('status') == 'FILLED' and not math.isclose(qty, original, rel_tol=1e-10):
                        raise ValueError('原订单成交终态与数量不一致，保留锁定')
                    if position and row['side'] != (position['side'] if order_kind == 'entry' else ('SELL' if position['side'] == 'BUY' else 'BUY')):
                        raise ValueError('原订单方向与账本不一致，保留锁定')
                    return row
                # New intents persist this phase before any margin/leverage I/O,
                # and persist 'submitting' before the entry order can be sent.
                # Legacy intents without a phase never get this inference.
                if kind == 'entry' and pending.get('phase') == 'preparing':
                    row = dict(status='NOT_SUBMITTED', executedQty=0)
                else:row = terminal_order(identity, kind)
                evidence['order'] = row
                result['order_status'] = row.get('status')
                terminal = row.get('status') in ('NOT_SUBMITTED', 'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED')
                if position and position.get('entry_unresolved') and kind == 'close':
                    entry = terminal_order(position['id'], 'entry')
                    evidence['entry_order'] = entry
                    terminal = terminal and entry.get('status') in ('FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED')
                if not terminal:
                    result['reason'] = '原订单尚非终态，保留锁定；请在交易所核对剩余委托'
                elif ordinary:
                    result['reason'] = '仍有未完成普通订单，保留锁定'
                elif flat and not algos:
                    result.update(can_resolve=True, outcome='flat', reason='原订单已终结；账户与持仓接口均为空仓，且无未完成订单')
                elif position and len(positions) == len(own) == 1 and positions[0]['symbol'] == symbol:
                    expected = position['quantity']*(1 if position['side'] == 'BUY' else -1)
                    matching = all(math.isclose(number(r['positionAmt']), expected, rel_tol=1e-8, abs_tol=1e-12) for r in (positions[0], own[0]))
                    known = {str(i) for i in position['algos']}
                    protected = {str(r.get('algoId')) for r in algos} == known
                    entry_matches = kind != 'entry' or (math.isclose(number(row['executedQty']), position['quantity'], rel_tol=1e-8)
                        and math.isclose(number(row.get('avgPrice', 0)), position['entry'], rel_tol=1e-8))
                    close_unfilled = kind != 'close' or number(row['executedQty']) == 0
                    if matching and protected and entry_matches and close_unfilled:
                        self.verify_demo_protection(position)
                        validate_position_risk(position, own[0])
                        result.update(can_resolve=True, outcome='protected', reason='原订单已终结；账本持仓数量、止损止盈与强平距离核验通过，继续监控但不开新仓')
                    else:result['reason'] = '持仓、成交数量或保护单与账本不一致，保留锁定；请在交易所处理后重新核对'
                else:
                    result['reason'] = '仍有持仓或条件订单，无法安全恢复；请在交易所处理后重新核对'
            # Exclude timestamps and volatile mark-price fields from the review
            # fingerprint; confirmation still revalidates them with fresh GETs.
            evidence = copy.deepcopy(evidence)
            for r in evidence['remote']:
                for field in ('markPrice', 'unRealizedProfit', 'isolatedMargin', 'updateTime'):
                    r.pop(field, None)
            signature = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
            result['review_id'] = uuid.uuid4().hex
            self.pending_review = dict(result, fingerprint=signature, account=copy.deepcopy(account))
            return result

    def resolve_pending(self, identity, review_id):
        """Explicit local reconciliation after a fresh, matching read-only review."""
        with self.lock:
            review = self.pending_review
            if (not review or not review['can_resolve'] or review['pending_id'] != identity
                    or review['review_id'] != review_id or not 0 <= time.time()-review['checked_at'] <= 60):
                raise ValueError('核对结果已失效，请重新查询原请求结果')
            fresh = self.inspect_pending()
            if not fresh['can_resolve'] or self.pending_review['fingerprint'] != review['fingerprint']:
                raise ValueError('交易所或本地状态已改变，请重新核对并确认；未解除锁定')
            before = copy.deepcopy(self.s)
            try:
                p = self.s['position']
                if fresh['outcome'] == 'flat':self.s['position'] = None
                elif p:p['entry_unresolved'] = False
                self.s.update(pending=None, enabled=False)
                if fresh['kind'] in ('entry', 'close') and self.mode == 'live':self.s['has_traded'] = True
                prefixes = ('上次交易未完成核对', '杠杆修改结果待核对', self.venue+'交易或保护单未完整确认', '平仓结果未确认')
                if self.s['halted'].startswith(prefixes):self.s['halted'] = ''
                # Sync wallet without ever rebasing capital or loss budgets.
                account = self.pending_review['account']
                if self.mode == 'live':self.refresh_live_wallet(account, check_flat=False)
                else:self.s['wallet'] = CAPITAL+number(account['totalWalletBalance'])-self.s['remote_base']
                self.loss_check()
                self.event('待核对请求已完成只读核验并更新本地账本；新开仓仍暂停', trade=p,
                           execution=dict(client_order_id=identity, kind=fresh['kind'], outcome=fresh['outcome'], order_status=fresh.get('order_status')))
                self.save()
            except Exception:
                self.db.rollback();self.s = before;self.pending_review = None
                raise
            self.pending_review = None
            return dict(ok=True, reason=fresh['reason'], enabled=False)

    def order_inputs(self, force=False, symbol=None):
        self.require_live_connection()
        symbol = self.s["symbol"] if symbol is None else symbol
        filters = dict(self.api.filters(symbol))
        filters["leverageLimits"] = self.leverage_limits(symbol, force=force)
        return filters, self.fee_rates(symbol, force=force)

    def remaining_risk(self):
        return min(self.s['risk'],self.strategy_equity()-(self.capital_budget()-self.loss_limit()),
                   self.strategy_equity()-(self.s['day_start']-self.daily_loss_limit()))

    def entry_lock_reason(self):
        if self.s['pending']:return '订单结果待核对，禁止新开仓'
        if self.s['position']:return '已有持仓，继续保护；禁止切换和新开仓'
        if self.s['halted']:return self.s['halted']
        if self.s['total_latch']:return '累计亏损阈值已触发，禁止新开仓'
        if self.s['daily_latch']:return '当日亏损阈值已触发；UTC换日后仍需手动启动'
        if self.mode=='live' and not self.live_connected:return '请先连接实盘账户并核验预算'
        return ''

    def refresh_selection(self):
        values=self.candidate_provider()
        def inputs(symbol,sig,quote):
            try:
                filters,fees=self.order_inputs(symbol=symbol)
                return plan(sig,quote['price'],filters,self.remaining_risk(),self.s['leverage'],
                            self.sizing_balance(),quote=quote,fees=fees)
            except APIError as exc:raise ValueError(str(exc)) from None
        lock_reason=self.entry_lock_reason()
        self.selection=scan(values,self.news_provider,inputs,time.time(),
                            self.s['last_candles_by_symbol'],self.s['last_auto_candle'],
                            locked=bool(lock_reason),lock_reason=lock_reason,interval=self.s['interval'],last_entry_close=self.s['last_entry_close'])
        self.candidate_values=values
        if self.s['selection_mode']=='auto' and not self.s['position'] and not self.s['pending']:
            try:
                sig,quote=fresh_candidate(values.get(self.s['symbol']),self.s['symbol'],time.time(),self.s['interval'])
                self.sig,self.quote=sig,quote
                self.rows=values[self.s['symbol']]['rows']
                self.last_candles=values[self.s['symbol']]['kline_checked']
            except (ValueError,KeyError,TypeError):pass

    def select_candidate(self):
        if not self.s['enabled'] or self.s['position'] or self.s['pending'] or self.s['halted']:
            return False
        symbol=self.selection.get('selected')
        if symbol not in SYMBOLS:return False
        value=self.candidate_values.get(symbol)
        try:
            sig,quote=fresh_candidate(value,symbol,time.time(),self.s['interval'])
            entry_signal(sig,self.news_provider(symbol),symbol,quote['server'],self.s['interval'])
        except (ValueError,KeyError,TypeError):
            self.selection.update(selected=None,reason='候选数据或信号已变化，保持空仓并等待下一轮扫描')
            return False
        previous=self.s['symbol']
        self.s.update(symbol=symbol,last_candle=self.s['last_candles_by_symbol'].get(symbol,0))
        self.quote,self.rows,self.sig=quote,value['rows'],sig
        self.last_candles=value['kline_checked'];self.last_preview=0
        if previous!=symbol:self.event('自动选品选择 '+symbol+'；'+self.selection['reason'])
        self.save()
        return True

    def check_auto_entry(self, side, candle):
        if self.s['selection_mode']!='auto':return
        if self.s['position'] or (self.s['pending'] and self.s['pending'].get('kind')!='entry'):
            raise ValueError('已有持仓或待核对订单，禁止自动开仓')
        symbol=self.s['symbol']
        self.rows=self.read_candles(symbol)
        self.quote=self.api.market(symbol)
        self.sig=signal(self.rows,self.quote['server'],self.s['interval']);self.last_candles=time.time()
        decision=entry_signal(self.sig,self.news_provider(symbol),symbol,self.quote['server'],self.s['interval'])
        if decision['action']!=side or self.sig['candle']!=candle:
            raise ValueError('自动选品信号已变化，本轮取消开仓')

    def check_entry_depth(self, order):
        """Shared execution guard for manual/auto, paper/demo/live entries."""
        return depth_cost(self.api.order_book(self.s['symbol']), order['side'],
                          order['quantity'], self.quote['price'], self.quote['server'])

    def refresh_risk_preview(self):
        reason = self.entry_lock_reason()
        if reason:
            self.last_preview = 0
            self.risk_preview = dict(ok=False, symbol=self.s['symbol'], leverage=self.s['leverage'],
                                    updated=time.time(), reason=reason, long=None, short=None)
            return
        if time.time()-self.last_preview < 20:
            return
        self.last_preview = time.time()
        remaining = self.remaining_risk()
        preview = dict(ok=False, symbol=self.s["symbol"], leverage=self.s["leverage"], risk_budget=remaining,
                       updated=time.time(), reason="", long=None, short=None)
        try:
            if remaining <= 0:
                raise ValueError("当日或累计剩余亏损预算不足，禁止新开仓")
            filters, fees = self.order_inputs()
            if not self.quote or not self.sig.get("atr"):
                raise ValueError("等待已收盘K线的波动数据")
            for side, key in (("BUY", "long"), ("SELL", "short")):
                try:
                    # Preview uses current price for a hypothetical signal; it never creates one.
                    hypothetical = dict(self.sig, side=side, reference=self.quote["price"])
                    preview[key] = plan(hypothetical, self.quote["price"], filters,
                                        remaining, self.s["leverage"], self.sizing_balance(),
                                        quote=self.quote, fees=fees)
                except ValueError as exc:
                    preview[key+"_error"] = str(exc)
            preview["ok"] = bool(preview["long"] or preview["short"])
            preview["reason"] = "当前配置已完成开仓前风险估算，实际订单还需重新校验" if preview["ok"] else (
                preview.get("long_error") or preview.get("short_error") or "当前配置无法通过风控")
        except (ValueError, APIError) as exc:
            preview["reason"] = str(exc)
        self.risk_preview = preview

    def settle_paper_funding(self, p, now_ms, force=False):
        direction = 1 if p["side"] == "BUY" else -1
        # Retrieve final exchange funding events, not the predicted next rate.
        # Re-read a bounded overlap so a late publication is not silently lost.
        if force or time.time() >= p.get("funding_check_due", 0):
            start = max(p["opened"], p.get("funding_cursor", p["opened"])-3_600_000)
            expected = p.get("next_funding_time", 0)
            if expected and expected <= now_ms:
                start = min(start, max(p["opened"], expected-1))
            rows = self.api.funding_history(p["symbol"], start, now_ms)
            known = set(p.get("funding_ids", []))
            for row in rows:
                stamp = row["fundingTime"]
                if stamp <= p["opened"] or stamp in known:
                    continue
                charge = direction*p["quantity"]*row["markPrice"]*row["fundingRate"]
                self.s["wallet"] -= charge
                p["funding_paid"] += charge
                known.add(stamp)
                self.event("模拟资金费结算 %+.6f USDT（正数为支出）" % charge)
            pending = expected and expected <= now_ms and expected not in known
            next_time = self.quote.get("nextFundingTime", 0)
            if not pending and next_time > now_ms:
                p["next_funding_time"] = next_time
            p.update(funding_ids=sorted(known)[-1000:], funding_cursor=expected-1 if pending else now_ms,
                     funding_check_due=time.time()+30)
            self.save()
            if pending and (force or now_ms-expected > 90000):
                raise ValueError("已到资金费结算时点但交易所尚未公布结果，费用待核对")

    def paper_protection(self):
        p = self.s["position"]
        if not p:
            return
        direction = 1 if p["side"] == "BUY" else -1
        audit = p.get("risk_audit")
        if not audit:
            self.close("旧模拟持仓缺少保证金核验数据，退出后使用新风控")
            return
        now_ms = self.quote["server"]
        self.settle_paper_funding(p, now_ms)
        limits = self.leverage_limits(p["symbol"])
        entry_fee = p["quantity"]*p["entry"]*audit["taker_fee"]
        collateral = p["initial_margin"]-entry_fee-p["funding_paid"]
        if collateral <= 0:
            self.paper_liquidate(p, collateral)
            return
        result = estimate_liquidation(p["side"], p["quantity"], p["entry"], collateral, limits)
        liq = result["estimated_liquidation"]
        p["simulated_liquidation"] = liq
        p["simulated_isolated_wallet"] = collateral
        mark = self.quote["mark"]
        if direction*(mark-liq) <= 0:
            self.paper_liquidate(p, collateral)
        elif direction*(mark-p["stop"]) <= 0:
            self.close("触发模拟止损")
        elif direction*(mark-p["target"]) >= 0:
            self.close("触发模拟止盈")
        elif direction*(p["stop"]-liq) < p["required_liquidation_gap"]:
            self.close("模拟保证金变化导致强平安全间距不足")
        elif p["funding_paid"] > audit["funding_reserve"]:
            self.close("累计资金费超过开仓预留，退出持仓")
        elif now_ms-p["opened"] >= 24*3600*1000:
            self.close("持仓达到24小时上限，退出并重新评估")

    def paper_liquidate(self, p, collateral):
        # Isolated liquidation consumes the collateral remaining in this model.
        # Price paths between polls and venue liquidation execution are not reproduced.
        self.s["wallet"] -= max(0, collateral)
        self.s.update(position=None, enabled=False, halted="模拟持仓触发强平模型，策略已锁定，请复核杠杆与行情跳空")
        self.event("模拟强平：按剩余逐仓保证金记损，停止自动开仓", trade=p)
        self.loss_check()
        self.save()

    def reconcile_paper_funding(self):
        pending = self.s.get("funding_unresolved", [])
        if not pending or time.time() < self.funding_reconcile_due:
            return
        self.funding_reconcile_due = time.time()+30
        remaining = []
        for item in pending:
            try:
                rows = self.api.funding_history(item["symbol"], item["opened"], item["closed"])
                expected = item.get("expected", 0)
                if expected and expected <= item["closed"] and not any(r["fundingTime"] == expected for r in rows):
                    remaining.append(item)
                    continue
                total = sum((1 if item["side"] == "BUY" else -1)*item["quantity"]*r["markPrice"]*r["fundingRate"]
                            for r in rows if item["opened"] < r["fundingTime"] <= item["closed"])
                self.s["wallet"] += item["reserve"]-(total-item["paid"])
                self.event("已核对平仓后的延迟资金费，最终资金费 %+.6f USDT" % total)
            except (ValueError, APIError):
                remaining.append(item)
        self.s["funding_unresolved"] = remaining
        if not remaining and self.s["halted"] == "模拟平仓资金费待核对，已预扣费用预留并锁定新开仓":
            self.s["halted"] = ""
        self.loss_check()
        self.save()

    def tick(self):
        with self.lock:
            try:
                symbol = self.s["symbol"]
                auto=self.s['selection_mode']=='auto'
                try:self.quote = self.api.market(symbol)
                except (APIError,ValueError):
                    if not auto or self.s['position'] or self.s['pending']:raise
                    self.quote=None  # One failed display symbol cannot starve healthy candidates.
                if self.mode == "live" and not self.live_connected:
                    if not auto and self.quote and time.time()-self.last_candles > 1:
                        self.rows = self.read_candles(symbol)
                        self.sig = signal(self.rows, self.quote["server"], self.s["interval"])
                        self.last_candles = time.time()
                    self.decision = dict(action="WAIT", source="kline", reason="实盘未连接；已有交易所保护单保留，请重新连接账户")
                    self.risk_preview = dict(ok=False, reason="连接实盘账户后计算实际余额与保证金", updated=time.time())
                    self.s["enabled"] = False
                    self.refresh_selection()
                    self.updated, self.error = time.time(), ""
                    self.save()
                    return
                if self.mode == "paper":
                    self.reconcile_paper_funding()
                if self.mode in ("demo", "live") and self.s["key_id"]:
                    self.sync_demo()
                p = self.s["position"]
                if p and self.mode == "paper":
                    try:
                        self.paper_protection()
                    except (ValueError, APIError):
                        # Fresh mark is already available. Close known local exposure
                        # if funding or margin cannot be revalidated; don't stop protecting it.
                        self.close("无法重新核验模拟持仓保证金或资金费，退出持仓", quote=self.quote)
                        raise
                if self.loss_check() and self.s["position"] and not self.s["pending"]:
                    self.close("账户亏损阈值触发")
                # Protection is evaluated before optional signal/news/metadata work can fail.
                if (not auto or self.s['position'] or self.s['pending']) and self.quote and time.time()-self.last_candles > 1:
                    self.rows = self.read_candles(symbol)
                    self.sig = signal(self.rows, self.quote["server"], self.s["interval"])
                    self.last_candles = time.time()
                news = self.news_provider(symbol)
                if self.quote:self.decision = self.evaluate(self.sig, news, symbol, self.quote["server"], self.s["position"])
                if self.s["position"] and not self.s["pending"] and self.decision["action"] == "EXIT":
                    self.close(self.decision["reason"])
                    # A reversal closes exposure but must not reopen on this same candle.
                    self.s["last_candle"] = max(self.s["last_candle"], self.sig.get("candle", 0))
                    self.s['last_candles_by_symbol'][symbol]=self.s['last_candle']
                    if auto:self.s['last_auto_candle']=max(self.s['last_auto_candle'],self.s['last_candle'])
                    self.s['last_entry_close']=max(self.s['last_entry_close'],self.s['last_candle']+timeframe(self.s['interval'])['duration_ms'])
                selected=False
                self.refresh_selection()
                if auto:
                    if not self.s['position'] and not self.s['pending']:
                        selected=self.select_candidate()
                        if not selected:self.decision=dict(action='WAIT',source='selection',reason=self.selection['reason']+('；策略已暂停' if not self.s['enabled'] else ''))
                self.refresh_risk_preview()
                if self.s["enabled"] and not self.s["position"] and not self.s["pending"] and not self.s["halted"]:
                    if not auto or selected:self.maybe_enter()
                self.error = ""
                self.updated = time.time()
                curve = self.s["curve"]
                if not curve or time.time()*1000-curve[-1]["time"] >= 60000:
                    curve.append({"time": int(time.time()*1000), "value": self.equity()})
                    self.s["curve"] = curve[-1440:]
                self.save()
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, (ValueError, APIError)) else "内部异常，已暂停新开仓，请检查终端日志"
                if not isinstance(exc, (ValueError, APIError)):
                    import traceback
                    traceback.print_exc()
                self.s["enabled"] = False
                try:
                    self.save()
                except Exception:
                    # A second storage failure must not kill the worker. Keep
                    # entries paused in memory; the next tick retries persistence
                    # and reconciles existing exposure before any optional work.
                    self.error = "交易账本保存失败，已暂停新开仓；请检查磁盘空间与写入权限，恢复后仍需手动启动"
                    self.decision = dict(action='WAIT', source='storage', reason=self.error)
                    self.risk_preview = dict(ok=False, reason=self.error, updated=time.time())

    def maybe_enter(self):
        self.require_live_connection()
        if self.s['position'] or self.s['pending']:
            raise ValueError('已有持仓或待核对订单，禁止重复开仓')
        if self.s['selection_mode']=='auto' and not self.s['enabled']:
            raise ValueError('自动选品尚未启动')
        if self.mode == "live" and not self.s["enabled"]:
            raise ValueError("实盘策略未由用户启动，禁止开仓")
        if self.s["symbol"] not in SYMBOLS:
            raise ValueError("交易对不在五种商品允许列表中")
        sig = dict(self.sig)
        self.decision = self.evaluate(sig, self.news_provider(self.s["symbol"]),
                               self.s["symbol"], self.quote["server"])
        if (not sig.get("candle") or sig["candle"] <= self.s["last_candle"] or
            sig["candle"]+timeframe(self.s["interval"])["duration_ms"] <= self.s["last_entry_close"]):
            return
        if self.decision["action"] not in ("BUY", "SELL"):
            # Read-only evidence may recover during the two-minute window; no order was sent.
            return
        sig["side"] = self.decision["action"]
        self.s["last_candle"] = sig["candle"]
        self.s["last_entry_close"] = max(self.s["last_entry_close"], sig["candle"]+timeframe(self.s["interval"])["duration_ms"])
        self.s['last_candles_by_symbol'][self.s['symbol']]=sig['candle']
        if self.s['selection_mode']=='auto':self.s['last_auto_candle']=max(self.s['last_auto_candle'],sig['candle'])
        self.save()  # consume candle before any write, including restart after lost response
        if not sig["side"]:
            return
        if self.quote["server"] - (sig["candle"]+timeframe(self.s["interval"])["duration_ms"]) > timeframe(self.s["interval"])["entry_window_ms"]:
            self.event("跳过旧信号：已超过当前周期的收盘入场窗口")
            return
        try:
            self.check_auto_entry(sig['side'],sig['candle'])
            if self.s['selection_mode']=='auto':sig=dict(self.sig,side=sig['side'])
            remaining = min(self.s["risk"], self.strategy_equity()-(self.capital_budget()-self.loss_limit()),
                            self.strategy_equity()-(self.s["day_start"]-self.daily_loss_limit()))
            filters, fees = self.order_inputs(force=True)
            self.quote = self.api.market(self.s["symbol"])
            fresh = self.evaluate(self.sig, self.news_provider(self.s["symbol"]), self.s["symbol"], self.quote["server"])
            if fresh["action"] != sig["side"]:
                raise ValueError("下单前信号或新闻已失效："+fresh["reason"])
            self.decision = fresh
            order = plan(sig, self.quote["price"], filters,
                         remaining, self.s["leverage"], self.sizing_balance(), quote=self.quote, fees=fees)
            self.check_entry_depth(order)
        except ValueError as exc:
            self.event("风控跳过："+str(exc))
            self.decision.update(action="WAIT", reason="风控跳过："+str(exc))
            return
        order.update(symbol=self.s["symbol"], interval=self.s["interval"], opened=int(time.time()*1000), id="bt"+uuid.uuid4().hex[:24],
                     algos=[], decision=self.decision, funding_paid=0, funding_ids=[],
                     next_funding_time=self.quote.get("nextFundingTime", 0))
        if self.mode == "paper":
            order["entry"] = order["risk_audit"]["expected_entry"]
            self.s["wallet"] -= order["entry"]*order["quantity"]*fees["taker"]
            self.s["position"] = order
            self.event("模拟开%s %s，仓位约 %.2f USDT" % ("多" if order["side"] == "BUY" else "空", order["symbol"], order["notional"]))
        else:
            self.enter_demo(order)
        self.save()

    def enter_demo(self, order):
        self.require_live_connection()
        if self.mode == "live" and (not self.s["enabled"] or self.s["halted"] or self.s["pending"]):
            raise ValueError("实盘策略未启动或已锁定，禁止开仓")
        if order.get("symbol") not in SYMBOLS:
            raise ValueError("交易对不在五种商品允许列表中")
        cap = leverage_cap(order["leverage"], self.leverage_limits(order["symbol"], force=True))
        if order["notional"] > cap:
            raise ValueError("仓位超过币安当前杠杆档位上限")
        account = self.bind_account()
        if self.s["halted"] or self.loss_check() or (self.mode == "live" and not self.s["enabled"]):
            raise ValueError("账户已锁定、暂停或达到亏损阈值，取消开仓")
        filters, fees = self.order_inputs(force=True)
        self.quote = self.api.market(order["symbol"])
        fresh = self.evaluate(self.sig, self.news_provider(order["symbol"]), order["symbol"], self.quote["server"])
        if fresh["action"] != order["side"]:
            raise ValueError(self.venue+"提交前新闻或收盘信号失效")
        available = self.sizing_balance() if self.mode == "live" else min(self.s["wallet"], number(account["availableBalance"]))
        checked = plan(dict(self.sig, side=order["side"]), self.quote["price"], filters,
                       min(order["risk_budget"], self.remaining_risk()), order["leverage"],
                       available, quote=self.quote, fees=fees)
        order.update(checked)
        if available < order["cash_required"]:
            raise ValueError(self.venue+"可用余额不足")
        self.check_entry_depth(order)
        self.s["pending"] = {"kind": "entry", "id": order["id"], "symbol": order["symbol"], "phase": "preparing"}
        if self.mode == "live":
            self.s["has_traded"] = True  # freeze baseline before any entry-related exchange mutation
        self.save()
        try:
            try:
                self.api.request("POST", "/fapi/v1/marginType", {"symbol": order["symbol"], "marginType": "ISOLATED"}, True)
            except APIError as exc:
                if exc.code != -4046:  # already isolated
                    raise
            applied = self.api.request("POST", "/fapi/v1/leverage", {"symbol": order["symbol"], "leverage": order["leverage"]}, True)
            if (applied.get("symbol") != order["symbol"] or applied.get("leverage") != order["leverage"]
                    or number(applied.get("maxNotionalValue", 0)) < order["notional"]):
                raise APIError("交易所未确认所选杠杆与仓位限额")
            self.check_auto_entry(order['side'],self.sig['candle'])
            # Both selection modes must revalidate after slow margin/leverage calls.
            if self.s['selection_mode']!='auto':self.quote=self.api.market(order['symbol'])
            fresh=self.evaluate(self.sig,self.news_provider(order['symbol']),order['symbol'],self.quote['server'])
            if (fresh['action']!=order['side'] or self.s['halted'] or self.loss_check()
                    or (self.mode=='live' and not self.s['enabled'])):
                raise ValueError('提交前信号、新闻或账户风控已变化，本轮取消并核对')
            checked=plan(dict(self.sig,side=order['side']),self.quote['price'],filters,
                         min(order['risk_budget'],self.remaining_risk()),order['leverage'],available,
                         quote=self.quote,fees=fees)
            if not math.isclose(checked['quantity'],order['quantity'],rel_tol=1e-10):
                raise ValueError('提交前风险仓位变化，本轮取消并核对')
            order.update(checked)
            self.check_entry_depth(order)
            self.s['pending']['phase'] = 'submitting'
            self.save()  # durable boundary: recovery must never guess a sent order did not exist
            result = self.api.request("POST", "/fapi/v1/order", dict(symbol=order["symbol"], side=order["side"],
                type="MARKET", quantity=decimal_text(order["quantity"]), newClientOrderId=order["id"], newOrderRespType="RESULT"), True)
            if result.get("status") != "FILLED":
                if number(result.get("executedQty", 0)) > 0:
                    order.update(entry=number(result["avgPrice"]), quantity=number(result["executedQty"]),
                                 entry_unresolved=True)
                    self.s["position"] = order
                    self.save()
                    # Cancel residual once; the subsequent query must confirm a terminal
                    # order before assuming no additional fills can occur.
                    self.api.request("DELETE", "/fapi/v1/order", dict(symbol=order["symbol"],
                        origClientOrderId=order["id"]), True)
                    terminal = self.api.request("GET", "/fapi/v1/order", dict(symbol=order["symbol"],
                        origClientOrderId=order["id"]), True)
                    if terminal.get("status") in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"):
                        quantity = number(terminal.get("executedQty", 0))
                        if quantity >= order["quantity"]:
                            order.update(quantity=quantity, entry=number(terminal["avgPrice"]), entry_unresolved=False)
                            self.save()
                raise APIError("开仓未完整成交；已记录已知成交，停止并核对剩余订单")
            original = order["entry"]
            requested = order["quantity"]
            executed = number(result["executedQty"])
            if executed <= 0 or number(result["avgPrice"]) <= 0:
                raise APIError("成交响应数量或价格无效，保留待核对状态")
            order.update(entry=number(result["avgPrice"]), quantity=executed)
            self.s["position"] = order
            self.save()
            if not math.isclose(requested, executed, rel_tol=1e-8, abs_tol=1e-12):
                raise APIError("成交数量偏离请求，尝试减仓并锁定")
            if abs(order["entry"]/original-1) > .005:
                raise APIError("成交偏离预期，尝试紧急减仓")
            for kind, price in (("STOP_MARKET", order["stop"]), ("TAKE_PROFIT_MARKET", order["target"])):
                algo = self.api.request("POST", "/fapi/v1/algoOrder", dict(symbol=order["symbol"],
                    algoType="CONDITIONAL", side="SELL" if order["side"] == "BUY" else "BUY", type=kind,
                    closePosition="true", triggerPrice=decimal_text(price), workingType="MARK_PRICE",
                    clientAlgoId=order["id"]+("s" if kind == "STOP_MARKET" else "t")), True)
                order["algos"].append(algo["algoId"])
                self.save()
            self.verify_demo_protection(order)
            self.verify_demo_risk(order)
            self.s["pending"] = None
            self.event(self.venue+"开仓成交，保护单内容和交易所实际强平价已核验", trade=order,
                       execution={k: result[k] for k in ("orderId", "clientOrderId", "status", "avgPrice", "executedQty", "updateTime") if k in result})
        except Exception as failure:
            self.s["halted"] = self.venue+"交易或保护单未完整确认；请立即检查币安订单、持仓与保护单"
            self.s["enabled"] = False
            storage_failed = isinstance(failure, (sqlite3.Error, OSError))
            try:self.save()
            except (sqlite3.Error, OSError):storage_failed = True
            if self.s["position"]:
                # Confirmed exposure: one reduce-only emergency close, never blind retry.
                try:
                    if storage_failed:self.storage_emergency_close()
                    else:
                        try:self.submit_demo_close("保护单异常，紧急平仓")
                        except (sqlite3.Error, OSError):
                            if self.s['position']:self.storage_emergency_close()
                except Exception:
                    self.error = "紧急平仓未确认，请立即在币安"+self.venue+"处理持仓"
                    try:
                        self.event(self.error);self.save()
                    except (sqlite3.Error, OSError):pass
            raise

    def storage_emergency_close(self):
        """Reduce confirmed exposure once even when SQLite cannot accept writes.

        The durable entry ID determines the close ID. Any ambiguous query or
        write result remains locked; this path never loops or retries a POST.
        """
        self.require_live_connection()
        p = self.s['position']
        close_id = 'bc'+hashlib.sha256(p['id'].encode()).hexdigest()[:24]
        if close_id in self.emergency_attempts:
            raise ValueError('应急平仓已尝试，禁止重复提交；请核对原请求')
        remote = self.api.position_risk(p['symbol'])
        account = self.api.account()
        own = [r for r in remote if number(r['positionAmt']) != 0]
        observed = [r for r in account['positions'] if r['symbol'] == p['symbol'] and number(r['positionAmt']) != 0]
        expected = p['quantity']*(1 if p['side'] == 'BUY' else -1)
        if own or observed:
            if (len(own) != 1 or len(observed) != 1 or any(not math.isclose(number(r['positionAmt']), expected,
                    rel_tol=1e-8, abs_tol=1e-12) for r in own+observed)):
                raise ValueError('应急平仓前持仓不一致，保留保护并锁定')
            try:
                existing = self.api.request('GET', '/fapi/v1/order', dict(symbol=p['symbol'],origClientOrderId=close_id), True)
            except APIError as exc:
                if exc.code != -2013:raise
                existing = None
            if existing is not None:
                raise ValueError('原应急平仓请求已有结果，禁止重复提交；请核对交易所持仓')
            self.emergency_attempts.add(close_id)
            self.s['pending'] = dict(kind='close',id=close_id,symbol=p['symbol'])
            result = self.api.request('POST', '/fapi/v1/order', dict(symbol=p['symbol'],
                side='SELL' if p['side']=='BUY' else 'BUY',type='MARKET',reduceOnly='true',
                quantity=decimal_text(p['quantity']),newClientOrderId=close_id,newOrderRespType='RESULT'),True)
            if result.get('status') != 'FILLED' or not math.isclose(number(result.get('executedQty',0)),p['quantity'],rel_tol=1e-8):
                raise APIError('存储故障应急平仓未确认，禁止重试')
            remote = self.api.position_risk(p['symbol']);account = self.api.account()
            if (any(number(r['positionAmt']) != 0 for r in remote) or any(r['symbol']==p['symbol'] and number(r['positionAmt'])!=0 for r in account['positions'])):
                raise APIError('应急平仓后尚未交叉确认空仓，保留保护及核对状态')
        # Only confirmed flat exposure permits protection cleanup.
        if not p.get('entry_unresolved'):self.cleanup_algos(p)
        self.s['position'] = None
        self.s['pending'] = dict(kind='entry' if p.get('entry_unresolved') else 'close',
                                id=p['id'] if p.get('entry_unresolved') else close_id,symbol=p['symbol'])
        self.error = '存储故障期间已交叉确认空仓；账本及原订单仍待核对，新开仓已锁定'
        try:
            self.event(self.error,trade=p,execution=dict(client_order_id=close_id,status='remote_flat_confirmed'))
            self.save()
        except (sqlite3.Error, OSError):
            self.db.rollback()

    def verify_demo_risk(self, p):
        rows = [r for r in self.api.position_risk(p["symbol"]) if number(r.get("positionAmt", 0)) != 0]
        if len(rows) != 1:
            raise ValueError("交易所未确认唯一持仓及强平价")
        p.update(validate_position_risk(p, rows[0]))
        if time.time() >= p.get("income_check_due", 0):
            income = self.api.funding_income(p["symbol"], p["opened"], int(time.time()*1000))
            p["funding_paid"] = -sum(r["income"] for r in income)
            p["funding_income_checked"] = time.time()
            p["income_check_due"] = time.time()+30
        if p["funding_paid"] > p["risk_audit"]["funding_reserve"]:
            raise ValueError("实际资金费超过开仓预留，退出并重新评估")
        if time.time()*1000-p["opened"] >= 24*3600*1000:
            raise ValueError("持仓超过24小时，退出并重新评估")

    def verify_demo_protection(self, p, api=None):
        api = self.api if api is None else api
        if len(p["algos"]) != 2:
            raise ValueError("缺少止损或止盈保护单")
        for algo_id, kind, price in zip(p["algos"], ("STOP_MARKET", "TAKE_PROFIT_MARKET"), (p["stop"], p["target"])):
            row = api.request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}, True)
            if (row.get("algoStatus") != "NEW" or row.get("symbol") != p["symbol"]
                    or row.get("orderType") != kind or row.get("positionSide") != "BOTH"
                    or row.get("side") != ("SELL" if p["side"] == "BUY" else "BUY")
                    or row.get("workingType") != "MARK_PRICE" or row.get("closePosition") is not True
                    or abs(number(row.get("triggerPrice", 0))-price) > max(1e-10, price*1e-10)):
                raise ValueError("保护单内容或状态与预期不一致")

    def sync_demo(self):
        self.require_live_connection()
        account = self.api.account()
        if self.mode == "live":
            self.refresh_live_wallet(account)
        else:
            self.s["wallet"] = CAPITAL+number(account["totalWalletBalance"])-self.s["remote_base"]
        p = self.s["position"]
        positions = [x for x in account.get("positions", []) if number(x["positionAmt"]) != 0]
        if self.s["pending"]:
            self.s["enabled"] = False
            return  # no inference from an eventually consistent empty position response
        if not p:
            if positions:
                self.s["halted"] = "检测到非本程序持仓；已锁定，请在币安"+self.venue+"核对"
                self.s["enabled"] = False
            return
        own = [x for x in positions if x["symbol"] == p["symbol"]]
        if len(positions) != len(own):
            self.s["halted"] = "检测到其他持仓；暂停自动开仓"
            self.s["enabled"] = False
        if not own:
            remote = self.api.position_risk(p["symbol"])
            if any(number(r.get("positionAmt", 0)) != 0 for r in remote):
                self.s["enabled"] = False
                self.s["halted"] = "账户与持仓风险接口状态不一致，保留保护单并暂停"
                self.verify_demo_protection(p)
                return
            self.cleanup_algos(p)
            self.s["position"] = None
            if self.mode == "live":
                self.refresh_live_wallet(account, check_flat=False)
            self.event(self.venue+"已平仓，账户余额已同步（包含实际费用与资金费）", trade=p,
                       execution=dict(status="remote_flat_confirmed", fill_details="待交易所成交账单核验"))
            return
        amount = number(own[0]["positionAmt"])
        expected = p["quantity"]*(1 if p["side"] == "BUY" else -1)
        if abs(amount-expected) > 1e-9:
            self.s["halted"] = "持仓与账本不一致，禁止继续自动交易"
            self.s["enabled"] = False
            return
        try:
            self.verify_demo_protection(p)
            self.verify_demo_risk(p)
        except (ValueError, APIError) as exc:
            self.s["enabled"] = False
            self.s["halted"] = "持仓风控核验失败；尝试平仓并锁定："+str(exc)
            self.submit_demo_close("保护单或强平距离异常")

    def cleanup_algos(self, p):
        for algo_id in p["algos"]:
            try:
                self.api.request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id, "symbol": p["symbol"]}, True)
            except APIError as exc:
                if exc.code not in (-2011, -2013):
                    raise

    def submit_demo_close(self, reason):
        self.require_live_connection()
        p = self.s["position"]
        close_id = "bc"+uuid.uuid4().hex[:24]
        self.s["pending"] = {"kind": "close", "id": close_id, "symbol": p["symbol"]}
        self.save()
        result = self.api.request("POST", "/fapi/v1/order", dict(symbol=p["symbol"],
            side="SELL" if p["side"] == "BUY" else "BUY", type="MARKET", reduceOnly="true",
            quantity=decimal_text(p["quantity"]), newClientOrderId=close_id, newOrderRespType="RESULT"), True)
        if result.get("status") != "FILLED":
            raise APIError("平仓尚未完全确认，请在币安"+self.venue+"核对")
        if not math.isclose(number(result.get("executedQty", 0)), p["quantity"], rel_tol=1e-8, abs_tol=1e-12):
            raise APIError("平仓成交数量与持仓不一致，保留核对状态")
        remote = self.api.position_risk(p["symbol"])
        account = self.api.account()
        if (any(number(r.get("positionAmt", 0)) != 0 for r in remote)
                or any(r.get("symbol") == p["symbol"] and number(r.get("positionAmt", 0)) != 0
                       for r in account.get("positions", []))):
            raise APIError("平仓后远端仍有持仓或状态尚未一致，保留保护与核对状态")
        self.cleanup_algos(p)
        residual = {"kind": "entry", "id": p["id"], "symbol": p["symbol"]} if p.get("entry_unresolved") else None
        self.s.update(position=None, pending=residual)
        if self.mode == "live":
            self.refresh_live_wallet(account, check_flat=False)
        self.event(reason+"："+self.venue+"平仓已成交", trade=p,
                   execution=dict(client_order_id=close_id, **{k: result[k] for k in
                                  ("orderId", "clientOrderId", "status", "avgPrice", "executedQty", "updateTime") if k in result}))
        self.save()

    def close(self, reason="手动停止并平仓", quote=None):
        with self.lock:
            self.require_live_connection()
            p = self.s["position"]
            if not p:
                return
            if self.s["pending"]:
                raise ValueError("有未确认订单，请直接在币安"+self.venue+"核对，不能重复提交")
            if self.mode in ("demo", "live"):
                try:
                    self.submit_demo_close(reason)
                except Exception:
                    self.s["enabled"] = False
                    self.s["halted"] = "平仓结果未确认，请在币安"+self.venue+"检查；不会重复提交"
                    self.save()
                    raise
            else:
                q = quote if quote and 0 <= time.time()*1000-quote["time"] <= 15000 else self.api.market(p["symbol"])
                self.quote = q
                if p.get("risk_audit"):
                    try:
                        self.settle_paper_funding(p, q["server"], force=True)
                    except (APIError, ValueError):
                        # Protection must still close. Retain a durable audit and reserve
                        # rather than treating an unavailable funding result as zero cost.
                        reserve = p["risk_audit"]["funding_reserve"]
                        self.s["wallet"] -= reserve
                        self.s.setdefault("funding_unresolved", []).append(dict(
                            symbol=p["symbol"], opened=p["opened"], closed=q["server"],
                            quantity=p["quantity"], side=p["side"], paid=p["funding_paid"], reserve=reserve,
                            expected=p.get("next_funding_time", 0)))
                        self.s.update(enabled=False, halted="模拟平仓资金费待核对，已预扣费用预留并锁定新开仓")
                        self.event(self.s["halted"])
                direction = 1 if p["side"] == "BUY" else -1
                exit_price = q["price"]*(1-SLIPPAGE*direction)
                fee = p.get("risk_audit", {}).get("taker_fee", FEE)
                pnl = (exit_price-p["entry"])*direction*p["quantity"]-exit_price*p["quantity"]*fee
                self.s["wallet"] += pnl
                self.s["position"] = None
                self.event("%s：退出价 %.6f，本次平仓净额 %+.3f USDT（入场费另计）" % (reason, exit_price, pnl),
                           trade=p, execution=dict(status="simulated", avgPrice=exit_price,
                           executedQty=p["quantity"], simulated_exit_fee=exit_price*p["quantity"]*fee))
            self.loss_check()
            self.save()
