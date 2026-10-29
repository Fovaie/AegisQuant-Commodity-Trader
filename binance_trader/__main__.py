"""Run: python3 -m binance_trader [--mode paper|demo|live] [--setup-keys demo|readonly]."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
import signal
import threading
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .api import Binance, APIError
from .engine import Engine
from .analysis import Analysis
from .ai_news import AINewsError
from .runtime import Runtime
from .core import SYMBOLS
from .chatgpt_auth import ChatGPTAuth
from .chatgpt_chat import ChatGPTChat, build_context, context_summary
from .credential_store import CredentialStore, CredentialError, restore_connections, validate_binance_keys

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ["BINANCE_TRADER_DATA"]).expanduser() if os.getenv("BINANCE_TRADER_DATA") else ROOT.parent / "data" / "binance"


class LocalServer(ThreadingHTTPServer):
    # Retain ledger locks until every accepted request finishes, including a
    # slow exchange write. The stdlib default uses daemon request threads.
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args, **kwargs):
        self.stopping = threading.Event()
        super().__init__(*args, **kwargs)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


def credentials(kind):
    prefix = "BINANCE_DEMO" if kind == "demo" else "BINANCE_READONLY"
    key, secret = os.getenv(prefix+"_API_KEY", ""), os.getenv(prefix+"_API_SECRET", "")
    path = DATA / (kind+".credentials.json")
    if not key and path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError("密钥文件权限过宽，请执行 chmod 600 "+str(path))
        values = json.loads(path.read_text())
        key, secret = values["key"], values["secret"]
    return key, secret


def setup(kind):
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    print("密钥只保存在本机，不会回显。请使用HMAC密钥；实盘密钥只开启读取权限。")
    key = getpass.getpass("API Key（隐藏输入）: ").strip()
    secret = getpass.getpass("API Secret（隐藏输入）: ").strip()
    if not key or not secret:
        raise ValueError("密钥不能为空")
    path = DATA / (kind+".credentials.json")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict(key=key, secret=secret), f)
    print("密钥已保存。重启对应模式的交易器后生效。")


def server_for(engine, port, readonly=None, analysis=None, runtime=None, chat=None, credential_store=None):
    token = secrets.token_urlsafe(32)
    allowed = {"127.0.0.1:"+str(port), "localhost:"+str(port)}
    account_lock = threading.Lock()

    operation_lock = runtime.lock if runtime else engine.lock

    def selected():
        return runtime.engine if runtime else engine

    def account_client():
        current = selected()
        if current.mode in ("demo", "live"):
            if current.mode == "live" and not current.live_connected:
                return None
            return current.api
        return runtime.readonly if runtime else readonly

    def leverage_limits(symbol, force=False):
        with account_lock:
            client = account_client()
        if client is None:
            raise ValueError("请先连接当前环境API，以读取币安杠杆上限")
        return client.leverage_limits(symbol, force=force)

    def fee_rates(symbol, force=False):
        with account_lock:
            client = account_client()
        if client is None:
            raise ValueError("请先连接当前环境API，以读取账户手续费和保证金档位")
        return client.commission_rates(symbol, force=force)

    if runtime is None:
        engine.leverage_limits = leverage_limits
        engine.fee_rates = fee_rates
        if analysis is not None:
            engine.news_provider = analysis.decision
            engine.candidate_provider = analysis.candidates
    elif analysis is not None:
        runtime.set_analysis(analysis)

    def summary(a, current=None):
        if current is not None and current.mode == "live":
            wallet, available = current.live_balances(a)
            asset = next(v for v in a["assets"] if v.get("asset") == "USDT")
            return {"ok": True, "balance": wallet, "available": available,
                    "unrealized": asset.get("unrealizedProfit"),
                    "positions": len([p for p in a.get("positions", []) if float(p["positionAmt"]) != 0])}
        return {"ok": True, "balance": a["totalWalletBalance"],
                "available": a["availableBalance"], "unrealized": a["totalUnrealizedProfit"],
                "positions": len([p for p in a.get("positions", []) if float(p["positionAmt"]) != 0])}

    def confirm_action(current, body, action):
        if action=="start" and body.get("expected_interval","1h") != current.s.get("interval","1h"):
            raise ValueError("确认的K线周期已改变，请重新确认")
        if action=='start' and body.get('expected_selection_mode','manual') != current.s.get('selection_mode','manual'):
            raise ValueError('确认的选品模式已改变，请重新确认启动范围')
        if current.mode != "live":
            return
        if body.get("confirm_live") is not True:
            raise ValueError("请在实盘界面明确确认此项真实资金操作")
        if body.get("expected_symbol") != current.s["symbol"]:
            raise ValueError("确认的交易品种已改变，请重新确认")
        if action in ("start", "apply-leverage") and body.get("expected_leverage") != current.s["leverage"]:
            raise ValueError("确认的杠杆已改变，请重新确认")
        if action == "start" and body.get("expected_risk") != current.s["risk"]:
            raise ValueError("确认的单笔风险预算已改变，请重新确认")
        if action == "close" and body.get("expected_position_id") != (current.s["position"] or {}).get("id"):
            raise ValueError("确认的持仓已改变，请刷新后重新确认")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass

        def send(self, code, body, mime="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            if self.headers.get("Host") not in allowed:
                self.send(403, {"error": "只允许本机访问"})
                return False
            return True

        def do_GET(self):
            if not self.valid_host():
                return
            if self.path == "/api/chatgpt/status":
                self.send(200, chat.status() if chat else {'unavailable': True})
            elif self.path == "/api/chatgpt/context":
                try:
                    self.send(200, context_summary(build_context(analysis)) if analysis else {'unavailable': True})
                except ValueError:
                    self.send(400, {'error': '五品种公开资料暂不可用'})
            elif self.path == "/api/ai/status":
                self.send(200, analysis.ai.status() if analysis else dict(configured=False, enabled=False,
                    status='disabled', error='分析服务不可用', base_url='', model='', trading_authority=False))
            elif self.path == "/api/state":
                with operation_lock:
                    snapshot = runtime.snapshot() if runtime else engine.snapshot()
                    if credential_store is not None:
                        snapshot['credential_storage'] = credential_store.status()
                    if runtime is None:
                        with account_lock:
                            snapshot["readonly_connected"] = readonly is not None
                        snapshot["available_modes"] = [engine.mode]
                    self.send(200, snapshot)
            elif urlsplit(self.path).path == "/api/analysis":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                symbols = query.get('symbol', [])
                if set(query) - {'symbol'} or (symbols and (len(symbols) != 1 or symbols[0] not in SYMBOLS)):
                    self.send(400, {"error": "不支持的分析品种或参数"})
                    return
                generation = runtime.context_id if runtime else None
                mode = runtime.mode if runtime else engine.mode
                snapshot = (analysis.snapshot(symbols[0]) if symbols else analysis.snapshot()) if analysis else {"kline":{}, "news":{}}
                if runtime:
                    if generation != runtime.context_id:
                        snapshot = {"kline":{}, "news":{}}
                    snapshot.update(context_id=generation, context_mode=mode)
                self.send(200, snapshot)
            elif self.path in ("/", "/app.js", "/chatgpt.js", "/i18n.js", "/style.css"):
                name = "index.html" if self.path == "/" else self.path[1:]
                body = (ROOT/"static"/name).read_bytes().replace(b"__TOKEN__", token.encode())
                mime = {"index.html": "text/html", "app.js": "text/javascript", "chatgpt.js": "text/javascript", "i18n.js": "text/javascript", "style.css": "text/css"}[name]
                self.send(200, body, mime+"; charset=utf-8")
            else:
                self.send(404, {"error": "不存在"})

        def do_POST(self):
            nonlocal readonly
            if not self.valid_host():
                return
            if self.headers.get("Origin") != "http://"+self.headers.get("Host", "") or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), token):
                self.send(403, {"error": "请求来源校验失败"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= (16384 if self.path == '/api/chatgpt/send' else 4096):
                    raise ValueError("请求过大")
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效")
                if self.path.startswith('/api/chatgpt/'):
                    with operation_lock:
                        if self.server.stopping.is_set():
                            self.send(503, {'error': '服务正在退出'}); return
                        if runtime is not None: runtime.validate_context(body)
                    if chat is None: raise ValueError('此版本没有启用 ChatGPT 聊天')
                    # No account/order mutations, and no model network I/O under
                    # the trading engine's operation lock.
                    route = self.path.removeprefix('/api/chatgpt/')
                    if route == 'login': result = chat.login(body.get('account_id'))
                    elif route in ('logout','models'): result = chat.account_action(route)
                    elif route == 'welcome': chat.auth.acknowledge(); result = chat.status()
                    elif route == 'send': result = chat.send(body.get('message'),body.get('model'),
                        body.get('web_search',False),body.get('request_id'),body.get('session'))
                    elif route == 'cancel': result = chat.cancel()
                    elif route == 'clear': result = chat.clear()
                    else: self.send(404, {'error': '不存在'}); return
                    self.send(200, result); return
                with operation_lock:
                    if self.server.stopping.is_set():
                        self.send(503, {"error": "服务正在退出，未提交操作；请等待退出完成"})
                        return
                    if runtime is not None:
                        runtime.validate_context(body)
                    current = selected()
                    result = {"ok": True}
                    if self.path in ("/api/ai/config", "/api/ai/test", "/api/ai/disconnect", "/api/ai/compare", "/api/ai/model", "/api/ai/retry"):
                        if analysis is None:
                            raise ValueError("分析服务不可用")
                        # Configuration and test scheduling are local only.
                        # The AI worker performs requests without this lock.
                        if self.path == "/api/ai/config":
                            if current.s['enabled'] or current.s.get('position') or current.s.get('pending'):
                                raise ValueError('修改AI配置前须暂停策略，且无持仓或待核对订单')
                            result = analysis.ai.configure(body.get('base_url'), body.get('model'),
                                body.get('api_key'), enabled=body.get('enabled', True))
                        elif self.path == "/api/ai/test":
                            result = analysis.ai.request_test()
                        elif self.path == '/api/ai/retry':
                            result = analysis.ai.request_retry(body.get('symbol'))
                        elif self.path == "/api/ai/compare":
                            result = analysis.ai.request_comparison(current.s['symbol'])
                        elif self.path == "/api/ai/model":
                            if current.s['enabled'] or current.s.get('position') or current.s.get('pending'):
                                raise ValueError('切换模型前须暂停策略，且无持仓或待核对订单')
                            result = analysis.ai.select_model(body.get('model'))
                        else:
                            result = analysis.ai.disconnect()
                    elif self.path == "/api/mode":
                        if runtime is None:
                            raise ValueError("此服务不支持切换交易环境")
                        runtime.switch_mode(body.get("mode"))
                    elif self.path == "/api/leverage":
                        result = leverage_limits(body.get("symbol"))
                    elif self.path == "/api/config":
                        current.configure(body)
                    elif self.path == "/api/start":
                        confirm_action(current, body, "start")
                        current.start()
                    elif self.path == "/api/apply-leverage":
                        confirm_action(current, body, "apply-leverage")
                        result = current.apply_leverage()
                    elif self.path == "/api/pause":
                        current.pause()
                    elif self.path == "/api/pending/inspect":
                        result = current.inspect_pending()
                    elif self.path == "/api/pending/resolve":
                        if body.get('confirm_reconcile') is not True:
                            raise ValueError('请先核对原请求结果，再确认更新本地账本')
                        result = current.resolve_pending(body.get('expected_pending_id'), body.get('review_id'))
                    elif self.path == "/api/close":
                        confirm_action(current, body, "close")
                        current.pause()
                        current.close()
                    elif self.path == "/api/credentials/forget":
                        if credential_store is None: raise ValueError('此进程未启用钥匙串保存')
                        if current.s['enabled'] or current.s.get('position') or current.s.get('pending'):
                            raise ValueError('清除保存连接前须暂停且无持仓或待核对订单')
                        slot = body.get('slot')
                        credential_store.forget(slot)
                        result = dict(ok=True, credential_storage=credential_store.status())
                    elif self.path == "/api/credentials/bind-account":
                        result = current.bind_account_identity()
                        if runtime:runtime.rotate_context()
                    elif self.path in ("/api/connect", "/api/credentials/rotate"):
                        rotating = self.path == "/api/credentials/rotate"
                        if rotating and body.get("confirm_rotation") is not True:
                            raise ValueError("请确认只轮换同一账户密钥并保留原账本")
                        key, secret = validate_binance_keys(body.get('key'), body.get('secret'))
                        purpose = body.get("mode", "readonly")
                        if purpose == "live":
                            if runtime is None or current.mode != "live":
                                raise ValueError("请先切换到实盘环境再连接实盘交易密钥")
                            candidate = Binance(False, key.strip(), secret.strip(), live_trading=True)
                            result = current.rotate_live_key(candidate) if rotating else current.attach_live(candidate)
                        elif purpose == "readonly":
                            if rotating:raise ValueError("密钥轮换仅支持已绑定实盘账本")
                            if current.mode == "live":
                                raise ValueError("实盘环境需要独立连接交易密钥；只读密钥不会自动升级")
                            candidate = Binance(False, key.strip(), secret.strip())
                            a = candidate.account()
                            result = summary(a)
                            with account_lock:
                                if runtime:
                                    runtime.readonly = candidate
                                else:
                                    readonly = candidate
                        else:
                            raise ValueError("API连接环境无效")
                        if runtime:
                            runtime.rotate_context()
                        if credential_store is not None:
                            try:
                                credential_store.save(purpose, dict(key=key, secret=secret))
                                result['credential_saved'] = True
                            except CredentialError:
                                # The account validation already succeeded. Report that
                                # accurately instead of claiming the old client survived.
                                result['credential_saved'] = False
                            result['credential_storage'] = credential_store.status()
                    elif self.path == "/api/account":
                        with account_lock:
                            client = account_client()
                        if client is None:
                            raise ValueError("请先在当前环境填写密钥并连接账户")
                        account = current.check_account() if current.mode == "live" else client.account()
                        result = summary(account, current)
                        if current.mode == "live":
                            result.update(capital_budget=current.capital_budget(), capital_limit=current.s['capital_limit'],
                                          daily_loss_limit=current.daily_loss_limit(), loss_limit=current.loss_limit(),
                                          enabled=current.s['enabled'], halted=current.s['halted'])
                    else:
                        self.send(404, {"error": "不存在"})
                        return
                    self.send(200, result)
            except (ValueError, TypeError, KeyError, APIError, AINewsError) as exc:
                self.send(400, {"error": str(exc) if isinstance(exc, (ValueError, APIError, AINewsError)) else "请求格式无效"})
            except Exception:
                self.send(500, {"error": "操作未确认，请刷新状态；不要重复提交"})

    return LocalServer(("127.0.0.1", port), Handler)


def acquire_ledger_locks(directory, names):
    """Also honor legacy per-mode locks across different ports and entry points."""
    handles = []
    try:
        for name in sorted(names):
            handle = (Path(directory).resolve()/(name+".lock")).open("a")
            handles.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handles
    except BlockingIOError:
        for handle in handles:
            handle.close()
        raise SystemExit("此账本的交易器已经运行，请打开原窗口；不能重复启动执行进程") from None


def main():
    parser = argparse.ArgumentParser(description="币安自动策略交易器，默认本地模拟")
    parser.add_argument("--mode", choices=("paper", "demo", "live"), default=None)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--setup-keys", choices=("demo", "readonly"))
    parser.add_argument("--credential-migration", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.credential_migration:
        from .credential_migration import serve
        serve(DATA, args.port)
        return
    if args.setup_keys:
        setup(args.setup_keys)
        return
    lockfiles = acquire_ledger_locks(DATA, ["demo"] if args.mode == "demo" else ["runtime", "paper", "live"])
    key, secret = credentials("demo") if args.mode == "demo" else ("", "")
    if args.mode == "demo" and (not key or not secret):
        raise SystemExit("先运行 python3 -m binance_trader --setup-keys demo 配置Demo密钥")
    rk, rs = credentials("readonly")
    readonly = Binance(False, rk, rs) if rk and rs else None
    runtime = None
    if args.mode == "demo":
        engine = Engine(Binance(True, key, secret), DATA/"demo.sqlite", "demo")
        analysis = Analysis(engine, state_path=DATA/"demo-news-risk.json")
    else:
        runtime = Runtime(DATA, readonly, initial_mode=args.mode)
        engine = runtime.engine
        analysis = Analysis(runtime, state_path=DATA/"news-risk.json")
    chat = ChatGPTChat(ChatGPTAuth(DATA / 'chatgpt'), analysis)
    # Persistence is opt-in. Normal launches retain API keys in memory only.
    store = CredentialStore(DATA) if os.getenv('COMMODITY_SAVE_CREDENTIALS') == '1' else None
    analysis.ai.credential_store = store
    if store is not None:
        restore_connections(store, runtime, analysis.ai)
    server = server_for(engine, args.port, readonly, analysis, runtime=runtime, chat=chat, credential_store=store)
    analysis.start()
    done = threading.Event()

    def worker():
        while not done.is_set():
            (runtime or engine).tick()
            done.wait(5)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def shutdown(*_):
        server.stopping.set()
        chat.stop()
        analysis.stop()
        done.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("交易器：http://127.0.0.1:%s · %s · 自动开仓尚未启动" % (args.port, (runtime or engine).mode), flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        chat.stop()
        analysis.stop()
        done.set()
        server.server_close()
        thread.join(timeout=45)
        # Keep ledger locks for the worker's entire lifetime. Never permit a
        # second process to use a ledger while an old network operation runs.
        if thread.is_alive():
            thread.join()
        # Do not release process locks while a news worker may still persist
        # public risk state. Shutdown cannot erase or overwrite a newer process.
        analysis.join()
        if runtime:
            runtime.close_databases()
        else:
            engine.db.close()
        for handle in lockfiles:
            handle.close()
        print("服务已停止。本地模拟保护依赖进程运行；已确认的Demo/实盘保护单留在交易所。", flush=True)


if __name__ == "__main__":
    main()
