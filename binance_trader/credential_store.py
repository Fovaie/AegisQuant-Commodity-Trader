"""macOS Keychain-backed connections, scoped to this app's data directory.

No plaintext fallback. Status never contains credentials. Restoring connections
does not enable trading, alter venue leverage, or submit exchange orders.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading

SLOTS = ('live', 'readonly', 'model')


class CredentialError(ValueError):
    pass


class CredentialStore:
    def __init__(self, directory, helper=None, runner=subprocess.run):
        self.directory = Path(directory).resolve()
        self.helper = Path(helper) if helper else self.directory / 'secure-store'
        self.service = 'local.frank.commoditytrader.credentials.' + hashlib.sha256(str(self.directory).encode()).hexdigest()[:24]
        self.runner = runner
        self.lock = threading.RLock()
        self.saved = {s: False for s in SLOTS}
        self.errors = {}
        self.restored = {s: False for s in SLOTS}

    def _install(self):
        if self.helper.exists():
            info = self.helper.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise CredentialError('钥匙串组件权限异常，未读取密钥')
            return
        bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.parent)) / 'secure-store'
        if not bundled.is_file():
            raise CredentialError('钥匙串组件尚未安装，请使用新版独立 App')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not replace an existing helper's identity during app upgrades.
        with self.helper.open('xb') as out, bundled.open('rb') as src:
            shutil.copyfileobj(src, out)
        self.helper.chmod(0o700)

    def _call(self, action, slot, value=None):
        if slot not in SLOTS:
            raise CredentialError('无效的连接类型')
        with self.lock:
            try:
                self._install()
                body = dict(action=action, slot=slot, service=self.service)
                if value is not None: body['value'] = value
                result = self.runner([str(self.helper)], input=json.dumps(body).encode(),
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
                response = json.loads(result.stdout)
                if result.returncode or response.get('ok') is not True:
                    raise CredentialError('钥匙串访问失败（代码 %s）；请解锁本机登录钥匙串后重试' % response.get('code', 'unknown'))
                self.errors.pop(slot, None)
                return response.get('value')
            except CredentialError as exc:
                self.errors[slot] = str(exc)
                raise
            except Exception:
                self.errors[slot] = '钥匙串操作未完成，连接密钥未确认保存'
                raise CredentialError(self.errors[slot]) from None

    def load(self, slot):
        value = self._call('get', slot)
        self.saved[slot] = value is not None
        if value is not None and not isinstance(value, dict):
            raise CredentialError('钥匙串连接记录格式异常')
        return value

    def save(self, slot, value):
        self._call('set', slot, value)
        # Verify a successful write can be recovered by a separate helper process.
        if self.load(slot) != value:
            self.errors[slot] = '钥匙串写入后校验失败'
            raise CredentialError(self.errors[slot])
        self.restored[slot] = False

    def forget(self, slot):
        self._call('delete', slot)
        self.saved[slot] = self.restored[slot] = False

    def status(self):
        with self.lock:
            return dict(backend='macos_keychain', saved=dict(self.saved), restored=dict(self.restored), errors=dict(self.errors))


def restore_connections(store, runtime, ai, client_factory=None):
    """Startup-only: use the same read-only account validation as manual connect."""
    from .api import Binance
    from .ai_news import validate_config
    factory = client_factory or Binance
    for slot in SLOTS:
        try:
            value = store.load(slot)
            if not value: continue
            if slot == 'model':
                config = validate_config(value.get('base_url'), value.get('model'), value.get('api_key'))
                ai.configure(config['base_url'], config['model'], config['api_key'], enabled=value.get('enabled', True), persist=False)
            elif runtime is not None:
                key, secret = validate_binance_keys(value.get('key'), value.get('secret'))
                if slot == 'live':
                    runtime.engines['live'].attach_live(factory(False, key, secret, live_trading=True))
                else:
                    candidate = factory(False, key, secret)
                    candidate.account()
                    runtime.readonly = candidate
            else: continue
            store.restored[slot] = True
        except Exception:
            # Keep saved credentials for retry, and never expose provider error bodies.
            store.errors[slot] = store.errors.get(slot) or '已保存连接未能恢复，请检查网络、密钥权限或账户模式；自动开仓保持暂停'


def validate_binance_keys(key, secret):
    if (not isinstance(key, str) or not isinstance(secret, str)
            or not 16 <= len(key.strip()) <= 256 or not 16 <= len(secret.strip()) <= 256
            or not key.strip().isascii() or not secret.strip().isascii()
            or any(c.isspace() for c in key.strip()+secret.strip())):
        raise ValueError('请输入有效的币安 HMAC API Key 和 Secret')
    return key.strip(), secret.strip()
