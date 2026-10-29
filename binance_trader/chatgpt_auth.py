"""Official ChatGPT OAuth for a local personal app; credentials never reach JS."""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from .chatgpt_http import ChatGPTError, request_json

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
TOKEN_ENDPOINT = ISSUER + '/api/accounts/oauth/token'


def protected_write(path, value):
    """Replace credentials and rotating refresh tokens as one owner-only record."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ChatGPTError('ChatGPT 凭证路径不能是符号链接。')
    temp = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists(): temp.unlink()


def validate_identity(token, client_id, nonce, fetch=request_json):
    try:
        import jwt
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
            raise ValueError()
        config = fetch(ISSUER + '/.well-known/openid-configuration')
        if config.get('issuer') != ISSUER or config.get('jwks_uri') != ISSUER + '/.well-known/jwks.json':
            raise ValueError()
        keys = fetch(config['jwks_uri']).get('keys', [])
        candidates = [k for k in keys if k.get('kid') == header['kid'] and k.get('kty') == 'RSA'
                      and k.get('use', 'sig') == 'sig' and k.get('alg', 'RS256') == 'RS256']
        if len(candidates) != 1: raise ValueError()
        key = jwt.PyJWK.from_dict(candidates[0], algorithm='RS256').key
        claims = jwt.decode(token, key, algorithms=['RS256'], audience=client_id, issuer=ISSUER,
                            leeway=30, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']})
        if not isinstance(claims.get('sub'), str) or not claims['sub'] or not hmac.compare_digest(str(claims.get('nonce')), nonce):
            raise ValueError()
        if isinstance(claims.get('aud'), list) and len(claims['aud']) > 1 and claims.get('azp') != client_id:
            raise ValueError()
        return dict(subject=claims['sub'], email=str(claims.get('email', ''))[:254], issuer=ISSUER)
    except ImportError:
        raise ChatGPTError('登录验证组件缺失，请重新安装本次 App 版本。') from None
    except Exception:
        raise ChatGPTError('ChatGPT 登录身份校验失败，未保存凭证。', 'IDENTITY') from None


class ChatGPTAuth:
    def __init__(self, directory, fetch=request_json, verifier=validate_identity, browser=webbrowser.open, clock=time.time):
        self.directory, self.fetch, self.verifier, self.browser, self.clock = Path(directory), fetch, verifier, browser, clock
        self.lock, self.io_lock = threading.RLock(), threading.Lock()
        self.path = self.directory / 'connections.json'
        self.pending, self.listener, self.error = None, None, ''
        self.generation, self.processing, self.closed = 0, False, False
        self.models = []
        self.data = dict(version=1, host_id='urn:uuid:' + str(uuid.uuid4()), active_id=None, profiles=[])
        if self.path.exists():
            try:
                mode = self.path.lstat().st_mode
                if not stat.S_ISREG(mode) or mode & 0o077 or self.path.stat().st_size > 262144:
                    raise ValueError()
                d = json.loads(self.path.read_text())
                if (d.get('version') != 1 or not re.fullmatch(r'urn:uuid:[0-9a-f-]{36}', d.get('host_id', ''))
                        or not isinstance(d.get('profiles'), list) or len(d['profiles']) > 10):
                    raise ValueError()
                ids = set()
                for p in d['profiles']:
                    if (not isinstance(p, dict) or not isinstance(p.get('id'), str) or p['id'] in ids
                            or not re.fullmatch(r'[A-Za-z0-9_-]{3,200}', p.get('client_id',''))
                            or p['client_id'] == 'dynamic_agent_client' or not isinstance(p.get('subject'),str)
                            or not isinstance(p.get('scopes',[]),list)):
                        raise ValueError()
                    ids.add(p['id'])
                if d.get('active_id') is not None and d['active_id'] not in ids: raise ValueError()
                if d.get('retry_client_id') and not re.fullmatch(r'[A-Za-z0-9_-]{3,200}',d['retry_client_id']):
                    raise ValueError()
                self.data = d
            except Exception:
                self.error = '本机 ChatGPT 凭证文件无效或权限过宽，未加载；请检查连接文件。'
                self.closed = True  # Never overwrite an unreadable credential store.

    def _save(self):
        protected_write(self.path, self.data)

    def _profile(self, identity=None):
        identity = self.data.get('active_id') if identity is None else identity
        return next((p for p in self.data['profiles'] if p.get('id') == identity), None)

    def status(self):
        with self.lock:
            p = self._profile() or {}
            connected = bool(p.get('access_token'))
            allowed = connected and 'chatgpt.tokens.use.direct' in p.get('scopes', [])
            pending = self.pending if self.pending and self.pending['expires'] > self.clock() else None
            return dict(connected=connected, plan_enabled=allowed, account_id=p.get('id'),
                        email=p.get('email', ''), accounts=[dict(id=x['id'], label=x.get('label', 'ChatGPT'),
                        connected=bool(x.get('access_token'))) for x in self.data['profiles']],
                        models=copy.deepcopy(self.models), login_pending=bool(pending) or self.processing,
                        login_url=pending.get('url') if pending else None, error=self.error,
                        welcome=allowed and not p.get('welcome_seen', False), trading_authority=False)

    def begin(self, profile_id=None):
        with self.lock:
            if self.closed: raise ChatGPTError(self.error or '应用正在退出。')
            if self.processing: raise ChatGPTError('上一项登录操作仍在处理。')
            if profile_id is not None and not self._profile(profile_id):
                raise ChatGPTError('所选 ChatGPT 账户不存在。')
            if profile_id is None and len(self.data['profiles']) >= 10:
                raise ChatGPTError('已达到本机账户数量上限，请选择已有账户。')
            self._end_listener()
            p = self._profile(profile_id) if profile_id is not None else None
            auth = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_GET(self):
                    if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                        self.send_error(403); return
                    if urlsplit(self.path).path != '/auth/callback' or len(self.path) > 16384:
                        self.send_error(404); return
                    ok, message = True, '登录完成，可以回到商品策略交易器。'
                    try:
                        auth.complete(parse_qs(urlsplit(self.path).query, keep_blank_values=True))
                    except ChatGPTError as exc:
                        ok, message = False, str(exc)
                    except Exception:
                        ok, message = False, '登录未完成，请回到交易器重新登录。'
                    import html
                    body = ('<!doctype html><meta charset="utf-8"><title>商品策略交易器</title><h2>'
                            + html.escape(message) + '</h2>').encode()
                    self.send_response(200 if ok else 400)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body)

            listener = ThreadingHTTPServer(('127.0.0.1', 0), Callback)
            listener.daemon_threads = True
            self.listener = listener
            threading.Thread(target=listener.serve_forever, daemon=True).start()
            state, nonce, verifier = (secrets.token_urlsafe(40) for _ in range(3))
            redirect = 'http://127.0.0.1:%s/auth/callback' % listener.server_port
            client_id = p['client_id'] if p else self.data.get('retry_client_id', 'dynamic_agent_client')
            query = dict(client_id=client_id, ext_agent_host_id=self.data['host_id'], response_type='code',
                         redirect_uri=redirect, scope=SCOPES, resource=RESOURCE, state=state, nonce=nonce,
                         code_challenge_method='S256', code_challenge=base64.urlsafe_b64encode(
                             hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='))
            if client_id == 'dynamic_agent_client': query['agent_name_hint'] = '商品策略交易器'
            elif p and p.get('email'): query['login_hint'] = p['email']
            # ID tokens are intentionally not put in URLs, UI state or process arguments.
            url = ISSUER + '/api/accounts/authorize?' + urlencode(query)
            self.pending = dict(state=state, nonce=nonce, verifier=verifier, redirect=redirect,
                                client_id=client_id, profile_id=profile_id, expires=self.clock()+600,
                                generation=self.generation, url=url)
            self.error = ''
            self._save()  # Persist the host ID before opening authorization.
            def expire():
                with self.lock:
                    if self.pending and self.pending['state'] == state:
                        self.pending = None; self.error = 'ChatGPT 登录已超时，请重新登录。'; self._end_listener()
            timer = threading.Timer(600, expire); timer.daemon = True; timer.start()
        try: self.browser(url)
        except Exception: pass  # The UI also offers an explicit link.
        return self.status()

    def _end_listener(self):
        listener, self.listener = self.listener, None
        if listener:
            def stop():
                listener.shutdown(); listener.server_close()
            threading.Thread(target=stop, daemon=True).start()

    def complete(self, query):
        with self.lock:
            p = self.pending
            if (not p or self.clock() >= p['expires'] or query.get('state') != [p['state']]
                    or any(len(v) != 1 for v in query.values())):
                raise ChatGPTError('登录回调已过期或校验不匹配，请重新登录。')
            self.pending, self.processing = None, True
        try:
            if query.get('error'):
                raise ChatGPTError('未完成 ChatGPT 授权，请按需重新登录。')
            code = query.get('code', [''])[0]
            supplied = query.get('client_id', [None])[0]
            client_id = supplied if p['client_id'] == 'dynamic_agent_client' else p['client_id']
            if (not code or len(code) > 4096 or not isinstance(client_id, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{3,200}', client_id) or client_id == 'dynamic_agent_client'
                    or (p['client_id'] != 'dynamic_agent_client' and supplied is not None and supplied != client_id)):
                raise ChatGPTError('ChatGPT 未返回匹配的应用注册信息。')
            if not p['profile_id']:
                # A failed code exchange must retry the issued registration, not
                # create another dynamic registration. This is not an identity.
                with self.lock:
                    if self.closed or p['generation'] != self.generation: raise ChatGPTError('登录已取消。')
                    self.data['retry_client_id'] = client_id; self._save()
            with self.io_lock:
                value = self._form(TOKEN_ENDPOINT, dict(grant_type='authorization_code', client_id=client_id,
                    code=code, code_verifier=p['verifier'], redirect_uri=p['redirect'], resource=RESOURCE))
                token = value.get('id_token')
                if not isinstance(token, str) or len(token) > 32768:
                    raise ChatGPTError('ChatGPT 未返回有效身份凭证。')
                identity = self.verifier(token, client_id, p['nonce'], self.fetch)
                credentials = self._tokens(value)
                with self.lock:
                    if self.closed or p['generation'] != self.generation:
                        raise ChatGPTError('登录已取消，未启用连接。')
                    current = self._profile(p['profile_id']) if p['profile_id'] else None
                    if current and identity['subject'] != current.get('subject'):
                        raise ChatGPTError('登录账户与所选注册不匹配，请使用添加账户。')
                    if current is None:
                        current = dict(id=str(uuid.uuid4()), client_id=client_id, welcome_seen=False)
                        self.data['profiles'].append(current)
                        self.data.pop('retry_client_id', None)
                    current.update(identity, **credentials)
                    current['label'] = (identity.get('email') or 'ChatGPT 账户') + ' · ' + current['id'][:6]
                    self.data['active_id'] = current['id']
                    self.generation += 1
                    self.models, self.error = [], ''
                    self._save()
            if 'chatgpt.tokens.use.direct' in credentials['scopes']:
                try: self.load_models()
                except ChatGPTError as exc:
                    with self.lock: self.error = str(exc)
            else:
                with self.lock: self.error = '已登录，但没有授予 ChatGPT 套餐使用权限；请重新登录并授权。'
        except ChatGPTError as exc:
            with self.lock: self.error = str(exc)
            raise
        finally:
            with self.lock:
                self.processing = False
                self._end_listener()

    def _form(self, url, body):
        return self.fetch(url, 'POST', urlencode(body).encode(), {'Content-Type': 'application/x-www-form-urlencoded'})

    def _tokens(self, value, old=None):
        old = old or {}
        access = value.get('access_token')
        if not isinstance(access, str) or not 1 <= len(access) <= 32768 or any(c.isspace() for c in access):
            raise ChatGPTError('ChatGPT 返回了无效访问凭证。')
        if not isinstance(value.get('token_type'),str) or value['token_type'].lower() != 'bearer':
            raise ChatGPTError('ChatGPT 返回了不支持的凭证类型。')
        duration = value.get('expires_in')
        if type(duration) not in (int, float) or not 0 < duration <= 604800:
            raise ChatGPTError('ChatGPT 凭证有效期无效。')
        scope = value.get('scope')
        if scope is not None and not isinstance(scope, str):
            raise ChatGPTError('ChatGPT 返回了无效授权范围。')
        result = dict(access_token=access, expires_at=self.clock()+duration,
                      scopes=scope.split() if scope is not None else old.get('scopes', []))
        for field in ('refresh_token', 'id_token'):
            token = value.get(field, old.get(field, ''))
            if not isinstance(token, str) or len(token) > 32768:
                raise ChatGPTError('ChatGPT 返回了无效凭证。')
            result[field] = token
        return result

    def access(self):
        with self.io_lock:
            with self.lock:
                p = copy.deepcopy(self._profile())
                generation = self.generation
            if not p or not p.get('access_token') or 'chatgpt.tokens.use.direct' not in p.get('scopes', []):
                raise ChatGPTError('请先登录 ChatGPT 并授权使用套餐。')
            if self.clock() + 90 >= p.get('expires_at', 0):
                if not p.get('refresh_token'):
                    raise ChatGPTError('ChatGPT 登录已过期，请重新登录。')
                try:
                    response = self._form(TOKEN_ENDPOINT, dict(grant_type='refresh_token', client_id=p['client_id'],
                                          refresh_token=p['refresh_token'], resource=RESOURCE))
                    updated = self._tokens(response, p)
                    # Only the original verified identity is retained. Refresh
                    # does not change the account or promote a new ID token.
                    updated['id_token'] = p.get('id_token','')
                except ChatGPTError as exc:
                    with self.lock:
                        self.error = str(exc)
                        if generation == self.generation and (exc.code == 'invalid_grant' or exc.status in (401, 403)):
                            self._clear_tokens(self._profile()); self.models = []; self.generation += 1; self._save()
                    raise
                with self.lock:
                    if generation != self.generation or self.closed:
                        raise ChatGPTError('账户已改变，本次请求未发送。')
                    self._profile().update(updated); self._save(); p.update(updated)
            if 'chatgpt.tokens.use.direct' not in p['scopes']:
                raise ChatGPTError('当前账户没有 ChatGPT 套餐使用权限。')
            return p['access_token'], generation

    def load_models(self):
        token, generation = self.access()
        result = self.fetch(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token})
        rows = result.get('models')
        if not isinstance(rows, list): raise ChatGPTError('无法读取此账户的模型列表。')
        models = []
        for r in rows[:100]:
            if (isinstance(r, dict) and r.get('visibility') == 'list' and
                    isinstance(r.get('slug'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', r['slug'])):
                models.append(dict(slug=r['slug'], display_name=str(r.get('display_name') or r['slug'])[:160]))
        with self.lock:
            if generation != self.generation: raise ChatGPTError('账户已改变，请重新读取模型。')
            self.models = models
            self.error = '' if models else '当前账户没有可用模型，请检查工作区与套餐权限。'
        return self.status()

    def acknowledge(self):
        with self.lock:
            if self._profile(): self._profile()['welcome_seen'] = True; self._save()
        return self.status()

    @staticmethod
    def _clear_tokens(p):
        if p:
            for k in ('access_token', 'refresh_token', 'id_token', 'expires_at', 'scopes'): p.pop(k, None)

    def sign_out(self):
        with self.lock:
            self.generation += 1
            self.pending = None
            self._end_listener()
            p = copy.deepcopy(self._profile())
            self._clear_tokens(self._profile())
            self.models = []
            self.error = ''
            self._save()
        confirmed = not p or not p.get('refresh_token')
        if not confirmed:
            try:
                with self.io_lock:
                    config = self.fetch(ISSUER + '/.well-known/openid-configuration')
                    endpoint = config.get('revocation_endpoint', '')
                    if urlsplit(endpoint).netloc != 'auth.openai.com': raise ChatGPTError('注销地址异常。')
                    self._form(endpoint, dict(token=p['refresh_token'], token_type_hint='refresh_token', client_id=p['client_id']))
                    confirmed = True
            except Exception: pass
        with self.lock:
            self.error = '' if confirmed else '已清除本机登录；远程撤销未确认，可到 ChatGPT 设置断开本应用。'
        return self.status()

    def stop(self):
        with self.lock:
            self.closed = True; self.generation += 1; self.pending = None; self._end_listener()
