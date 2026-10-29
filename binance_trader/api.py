"""Fixed-host Binance transport with explicit, narrowly scoped live-write opt-in."""
import hashlib
import hmac
import json
import re
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from .core import SYMBOLS, number
from .risk import validate_brackets

MAIN = "https://fapi.binance.com"
DEMO = "https://demo-fapi.binance.com"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Signed headers must never be forwarded to a redirected host.
        raise HTTPError(req.full_url, code, "Redirects are disabled", headers, fp)


def urlopen(request, timeout=10):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


class APIError(RuntimeError):
    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class Binance:
    def __init__(self, demo=False, key="", secret="", live_trading=False):
        if demo and live_trading:
            raise ValueError("Demo与实盘交易权限不能同时开启")
        self.demo, self.key, self.secret = demo, key, secret
        self.live_trading = live_trading is True
        self.base = DEMO if demo else MAIN
        self.offset = 0
        self.synced = 0
        self._clock_anchor = None
        self._clock_lock = threading.RLock()
        self._time_nonce = 0
        self.cooldown = 0
        self._filters = {}
        self._leverage = {}
        self._commission = {}

    def request(self, method, path, params=None, signed=False):
        if self.base != (DEMO if self.demo else MAIN) or not path.startswith("/fapi/") or "?" in path or "#" in path:
            raise APIError("API请求必须使用固定币安环境与路径")
        p = dict(params or {})
        if method != "GET" and not self.demo:
            if not self.live_trading:
                raise APIError("此API连接仅允许账户读取，未开启实盘交易通道")
            self.validate_live_write(method, path, p, signed)
            if method == "DELETE" and path == "/fapi/v1/algoOrder":
                # Binance's cancellation endpoint has no symbol parameter.
                # Check ownership first, then omit our local symbol guard on wire.
                current = self.request("GET", path, {"algoId": p["algoId"]}, signed=True)
                if (current.get("symbol") != p["symbol"]
                        or not str(current.get("clientAlgoId", "")).startswith("bt")):
                    raise APIError("保护单所属品种不一致，拒绝撤单")
                p.pop("symbol")
        if time.time() < self.cooldown:
            raise APIError("交易所限流，正在退避")
        if "symbol" in p and p["symbol"] not in SYMBOLS:
            raise ValueError("仅支持黄金、WTI原油、布伦特原油、白银和天然气")
        if method == "DELETE" and path == "/fapi/v1/algoOrder":
            p.pop("symbol", None)
        headers = {"User-Agent": "LocalStrategyTerminal/1.0"}
        if signed:
            if not self.key or not self.secret:
                raise APIError("尚未配置本机API密钥")
            if {"timestamp", "recvWindow", "signature"} & p.keys():
                raise ValueError("时间戳、接收窗口和签名由交易器统一生成")
            headers["X-MBX-APIKEY"] = self.key
        if path == "/fapi/v1/time" and not signed:
            # Binance accepts this unsigned cache-busting value on the public
            # time endpoint. Never let an intermediary reuse a fixed-URL sample.
            with self._clock_lock:
                self._time_nonce = max(time.time_ns()//1_000_000, self._time_nonce+1)
                p["timestamp"] = self._time_nonce
            headers.update({"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})
        for attempt in range(2 if signed and method == "GET" else 1):
            # Rebuild from original parameters: never sign an earlier signature.
            wire = dict(p)
            if signed:
                wire.update(timestamp=self.signed_timestamp(), recvWindow=5000)
                wire["signature"] = hmac.new(self.secret.encode(), urlencode(wire).encode(), hashlib.sha256).hexdigest()
            try:
                return self._send(method, path, wire, headers)
            except APIError as exc:
                if signed and exc.code == -1021:
                    with self._clock_lock:
                        self._clock_anchor = None
                    # Only retry an explicitly rejected read, once. No order,
                    # leverage change or cancellation is automatically repeated.
                    if method == "GET" and attempt == 0 and exc.http_status == 400:
                        self.sync_time(force=True)
                        continue
                    suffix = "已停止自动重试，请检查网络延迟后重新连接" if method == "GET" else "写请求未自动重发，请核对交易所状态"
                    raise APIError("币安时间戳校验失败（-1021）；"+suffix, -1021, exc.http_status) from None
                raise

    def account_identity(self):
        """Read Spot account UID on its fixed host; never expose balances or UID."""
        if self.demo or self.base != MAIN or not self.key or not self.secret:
            raise APIError('账户身份核验仅支持已配置的正式账户')
        if time.time() < self.cooldown:raise APIError('交易所限流，正在退避')
        wire = dict(timestamp=self.signed_timestamp(), recvWindow=5000, omitZeroBalances='true')
        wire['signature'] = hmac.new(self.secret.encode(),urlencode(wire).encode(),hashlib.sha256).hexdigest()
        data = self._send('GET','/api/v3/account',wire,{'X-MBX-APIKEY':self.key,
            'User-Agent':'LocalStrategyTerminal/1.0'},identity_read=True)
        uid = data.get('uid') if isinstance(data,dict) else None
        if type(uid) is not int or not 0 < uid < 2**63:
            raise APIError('币安未返回有效账户 UID；不能证明账户一致，未更换密钥')
        return hashlib.sha256(('binance-account:'+str(uid)).encode()).hexdigest()

    def sync_time(self, force=False):
        """Anchor exchange time to receipt, with bounded public-only resampling."""
        with self._clock_lock:
            anchor = self._clock_anchor
            if anchor and not force:
                elapsed = time.monotonic()-anchor[1]
                wall_elapsed = time.time()-anchor[2]
                if 0 <= elapsed < 30 and abs(wall_elapsed-elapsed) <= .5:
                    return
            self._clock_anchor = None
            for attempt in range(3):
                started_mono, started_wall = time.monotonic(), time.time()
                try:
                    result = self.request("GET", "/fapi/v1/time")
                except APIError as exc:
                    # Malformed/stale public samples can be resampled. Preserve
                    # rate-limit and other transport failures without a retry loop.
                    if exc.code == "STALE_TIME" and attempt < 2:
                        continue
                    raise
                received_mono, received_wall = time.monotonic(), time.time()
                duration = received_mono-started_mono
                if not 0 <= duration <= 1.5 or abs((received_wall-started_wall)-duration) > .5:
                    continue
                server = result["serverTime"]
                self._clock_anchor = (server, received_mono, received_wall)
                self.synced = received_wall  # diagnostics only; not used for signing
                self.offset = server-received_wall*1000
                return
            raise APIError("币安校时未完成：网络延迟或系统时间变化过大，未发送签名请求，请稍后重新连接")

    def signed_timestamp(self):
        with self._clock_lock:
            self.sync_time()
            server, received_mono, _ = self._clock_anchor
            # Do not add RTT/2: asymmetric outward latency can otherwise put
            # timestamps over Binance's one-second future-time allowance.
            return int(server+(time.monotonic()-received_mono)*1000-150)

    @staticmethod
    def validate_time_sample(result, headers):
        server = result.get("serverTime") if isinstance(result, dict) else None
        try:
            if isinstance(server, bool) or not isinstance(server, int) or server <= 0:
                raise ValueError()
            age = float(headers.get("Age", 0))
            if not 0 <= age < 1:
                raise ValueError()
            date = headers.get("Date")
            if date and abs(server-parsedate_to_datetime(date).timestamp()*1000) > 1500:
                raise ValueError()
        except (ValueError, TypeError, OverflowError, AttributeError):
            raise APIError("币安校时响应过期或无效；未使用该时间签名，请检查网络缓存", "STALE_TIME") from None

    def _send(self, method, path, p, headers, *, identity_read=False):
        query = urlencode(p)
        if identity_read and (self.demo or method != "GET" or path != "/api/v3/account"):
            raise APIError("账户身份接口仅允许正式环境固定只读查询")
        url = ("https://api.binance.com" if identity_read else self.base) + path
        data = None
        if method == "GET":
            if query:
                url += "?" + query
        else:
            data = query.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=10) as response:
                result = json.load(response)
                if path == "/fapi/v1/time":
                    self.validate_time_sample(result, getattr(response, "headers", {}))
                return result
        except HTTPError as exc:
            if exc.code in (418, 429):
                try: delay = max(60, float(exc.headers.get("Retry-After", 60)))
                except ValueError: delay = 60
                self.cooldown = time.time() + delay
            try: code = json.loads(exc.read()).get("code")
            except (ValueError, AttributeError): code = None
            # Never expose request URLs, headers, or remote text that might echo credentials.
            raise APIError("币安API拒绝请求（HTTP %s，代码 %s）" % (exc.code, code), code, exc.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError("网络超时或无效响应；写请求状态可能未知，请核对订单") from None

    @staticmethod
    def validate_live_write(method, path, params, signed):
        """Only the execution engine's five-symbol isolated market-order dialect."""
        allowed = {
            ("POST", "/fapi/v1/marginType"): {"symbol", "marginType"},
            ("POST", "/fapi/v1/leverage"): {"symbol", "leverage"},
            ("POST", "/fapi/v1/order"): {"symbol", "side", "type", "quantity", "reduceOnly", "newClientOrderId", "newOrderRespType"},
            ("POST", "/fapi/v1/algoOrder"): {"symbol", "algoType", "side", "type", "closePosition", "triggerPrice", "workingType", "clientAlgoId"},
            ("DELETE", "/fapi/v1/order"): {"symbol", "origClientOrderId"},
            ("DELETE", "/fapi/v1/algoOrder"): {"symbol", "algoId"},
        }
        fields = allowed.get((method, path))
        if signed is not True or fields is None or params.get("symbol") not in SYMBOLS or set(params)-fields:
            raise APIError("实盘写请求不在五种商品交易白名单内")
        def identifier(value):
            return isinstance(value, str) and bool(re.fullmatch(r"(?:bt|bc)[A-Za-z0-9._:/-]{1,34}", value))
        try:
            if path.endswith("marginType"):
                valid = params.get("marginType") == "ISOLATED"
            elif path.endswith("leverage"):
                lev = number(params.get("leverage"))
                valid = not isinstance(params.get("leverage"), bool) and lev.is_integer() and 1 <= lev <= 125
            elif method == "DELETE":
                valid = (identifier(params.get("origClientOrderId")) if path.endswith("/order") else
                         str(params.get("algoId", "")).isdigit() and int(params["algoId"]) > 0)
            elif path.endswith("/order"):
                valid = (params.get("side") in ("BUY", "SELL") and params.get("type") == "MARKET"
                    and 0 < number(params.get("quantity")) <= 1e12
                    and not isinstance(params.get("quantity"), bool)
                    and params.get("reduceOnly") in (None, "true")
                    and identifier(params.get("newClientOrderId")) and params.get("newOrderRespType") == "RESULT"
                    and ((params["newClientOrderId"].startswith("bc") and params.get("reduceOnly") == "true")
                         or (params["newClientOrderId"].startswith("bt") and "reduceOnly" not in params)))
            else:
                valid = (params.get("side") in ("BUY", "SELL")
                    and params.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
                    and params.get("algoType") == "CONDITIONAL" and params.get("closePosition") == "true"
                    and params.get("workingType") == "MARK_PRICE" and number(params.get("triggerPrice")) > 0
                    and not isinstance(params.get("triggerPrice"), bool)
                    and identifier(params.get("clientAlgoId")) and params["clientAlgoId"].startswith("bt"))
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise APIError("实盘交易参数不符合逐仓、市价或全仓保护单约束")

    def market(self, symbol):
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        ticker = self.request("GET", "/fapi/v1/ticker/24hr", {"symbol": symbol})
        mark = self.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        server = self.request("GET", "/fapi/v1/time")["serverTime"]
        if not 0 <= server - int(mark["time"]) <= 15000:
            raise APIError("标记价格已过期，停止交易")
        return dict(price=number(ticker["lastPrice"]), mark=number(mark["markPrice"]),
                    change=number(ticker["priceChangePercent"]), high=number(ticker["highPrice"]),
                    low=number(ticker["lowPrice"]), funding=number(mark["lastFundingRate"]),
                    time=int(mark["time"]), server=server,
                    nextFundingTime=int(mark["nextFundingTime"]))

    def candles(self, symbol, interval='1h'):
        from .timeframes import timeframe
        from .core import validate_candles
        timeframe(interval)
        return validate_candles(self.request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": 200}))

    def order_book(self, symbol):
        return self.request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": 50})

    def filters(self, symbol):
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        if self._filters.get(symbol, {}).get("expires", 0) < time.time():
            data = self.request("GET", "/fapi/v1/exchangeInfo")
            for item in data["symbols"]:
                if (item["symbol"] not in SYMBOLS or item["status"] != "TRADING"
                        or item["contractType"] != "TRADIFI_PERPETUAL"
                        or item.get("underlyingType") != "COMMODITY"):
                    continue
                fs = {f["filterType"]: f for f in item["filters"]}
                lot = fs["MARKET_LOT_SIZE"]
                self._filters[item["symbol"]] = dict(tick=number(fs["PRICE_FILTER"]["tickSize"]),
                    step=number(lot["stepSize"]), minQty=number(lot["minQty"]), maxQty=number(lot["maxQty"]),
                    minNotional=number(fs["MIN_NOTIONAL"]["notional"]),
                    liquidationFee=number(item["liquidationFee"]), expires=time.time()+300)
        if symbol not in self._filters or self._filters[symbol]["expires"] < time.time():
            raise APIError("交易对不可交易")
        return self._filters[symbol]

    def leverage_limits(self, symbol, force=False):
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        cached = self._leverage.get(symbol)
        if not force and cached and cached["expires"] > time.time():
            return cached
        data = self.request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True)
        items = data if isinstance(data, list) else [data]
        item = next((v for v in items if isinstance(v, dict) and v.get("symbol") == symbol), None)
        if not item or not item.get("brackets"):
            raise APIError("币安未返回此品种的杠杆档位")
        try:
            brackets = validate_brackets({"brackets": [dict(leverage=b["initialLeverage"],
                cap=b["notionalCap"], floor=b["notionalFloor"],
                maintMarginRatio=b["maintMarginRatio"], cum=b["cum"]) for b in item["brackets"]]})
        except (ValueError, KeyError, TypeError):
            raise APIError("币安杠杆档位缺少有效维持保证金率或速算额，禁止开仓") from None
        # Endpoint returns this user's bracket values; do not multiply notionalCoef twice.
        result = dict(symbol=symbol, maximum=max(b["leverage"] for b in brackets),
                      brackets=brackets, updated=time.time(), expires=time.time()+60,
                      source="Binance Demo" if self.demo else "Binance USDT-M")
        self._leverage[symbol] = result
        return result

    def commission_rates(self, symbol, force=False):
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        cached = self._commission.get(symbol)
        if not force and cached and cached["expires"] > time.time():
            return cached
        data = self.request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True)
        try:
            maker, taker = number(data["makerCommissionRate"]), number(data["takerCommissionRate"])
            if data["symbol"] != symbol or not -.02 <= maker <= .02 or not 0 <= taker <= .02:
                raise ValueError("invalid fee")
        except (ValueError, KeyError, TypeError):
            raise APIError("币安账户手续费数据无效，禁止开仓") from None
        result = dict(symbol=symbol, maker=maker, taker=taker, updated=time.time(),
                      expires=time.time()+300, source="Binance Demo account" if self.demo else "Binance USDT-M account")
        self._commission[symbol] = result
        return result

    def position_risk(self, symbol):
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        data = self.request("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True)
        if not isinstance(data, list) or any(not isinstance(row, dict) or row.get("symbol") != symbol for row in data):
            raise APIError("币安持仓风控数据与查询品种不一致")
        return data

    def funding_history(self, symbol, start_time, end_time):
        """Return the complete bounded range, including settled funding mark price.

        Pagination is bounded; failure or truncation is never treated as zero fees.
        """
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        start_time, end_time = int(start_time), int(end_time)
        if not 0 <= start_time <= end_time:
            raise ValueError("资金费查询时间范围无效")
        rows, cursor = [], start_time
        for _ in range(10):
            data = self.request("GET", "/fapi/v1/fundingRate", dict(symbol=symbol,
                                startTime=cursor, endTime=end_time, limit=1000))
            if not isinstance(data, list):
                raise APIError("币安资金费历史无效")
            batch = []
            try:
                for row in data:
                    timestamp = int(row["fundingTime"])
                    rate, mark = number(row["fundingRate"]), number(row["markPrice"])
                    if (row["symbol"] != symbol or not cursor <= timestamp <= end_time
                            or abs(rate) > .1 or mark <= 0):
                        raise ValueError("invalid funding")
                    batch.append(dict(fundingTime=timestamp, fundingRate=rate, markPrice=mark))
                batch.sort(key=lambda row: row["fundingTime"])
                if any(a["fundingTime"] >= b["fundingTime"] for a, b in zip(batch, batch[1:])):
                    raise ValueError("duplicate funding")
            except (ValueError, KeyError, TypeError):
                raise APIError("币安资金费历史不完整或数值无效") from None
            rows.extend(batch)
            if len(data) < 1000 or batch[-1]["fundingTime"] >= end_time:
                return rows
            cursor = batch[-1]["fundingTime"]+1
        raise APIError("资金费历史超过查询上限；需人工核对，不能跳过扣费")

    def account(self):
        return self.request("GET", "/fapi/v3/account", signed=True)

    def funding_income(self, symbol, start_time, end_time):
        """Read actual account funding cash flows, never infer them from a rate.

        The fixed inclusive window and transaction IDs make overlapping pages
        safe. A malformed or truncated result is not reported as zero funding.
        """
        if symbol not in SYMBOLS:
            raise ValueError("不支持的交易对")
        start_time, end_time = int(start_time), int(end_time)
        if not 0 <= start_time <= end_time:
            raise ValueError("账户资金费查询时间范围无效")
        records = {}
        for page in range(1, 11):
            data = self.request("GET", "/fapi/v1/income", dict(symbol=symbol,
                                incomeType="FUNDING_FEE", startTime=start_time,
                                endTime=end_time, limit=1000, page=page), signed=True)
            if not isinstance(data, list):
                raise APIError("币安账户资金费流水无效")
            previous_count = len(records)
            try:
                for row in data:
                    timestamp = int(row["time"])
                    income = number(row["income"])
                    identity = str(row["tranId"])
                    if (row["symbol"] != symbol or row["incomeType"] != "FUNDING_FEE"
                            or row["asset"] != "USDT" or not start_time <= timestamp <= end_time
                            or not identity.isascii() or not identity.isdecimal() or int(identity) <= 0):
                        raise ValueError("invalid funding income")
                    record = dict(time=timestamp, income=income, tranId=identity)
                    if identity in records and records[identity] != record:
                        raise ValueError("inconsistent funding income")
                    records[identity] = record
            except (ValueError, KeyError, TypeError, OverflowError):
                raise APIError("币安账户资金费流水品种、资产、时间或金额不一致") from None
            if len(data) < 1000:
                return sorted(records.values(), key=lambda row: (row["time"], int(row["tranId"])))
            if len(records) == previous_count:
                raise APIError("币安账户资金费分页未前进，不能确认完整流水")
        raise APIError("账户资金费流水超过查询上限，需人工核对")
