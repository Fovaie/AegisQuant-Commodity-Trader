"""One-time local credential migration. No trading engine or order routes."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import signal
import threading

from .api import Binance
from .ai_news import ModelClient, validate_config
from .credential_store import CredentialStore, validate_binance_keys


def migration_server(directory, port, store=None, client_factory=Binance, model_factory=ModelClient):
    store = store or CredentialStore(directory)
    token = secrets.token_urlsafe(32)
    root = Path(__file__).parent/'static'
    allowed = {'127.0.0.1:'+str(port), 'localhost:'+str(port)}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(15)

        def log_message(self, *_): pass

        def send(self, status, body, mime='application/json'):
            if not isinstance(body, bytes): body=json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            for k,v in {'Content-Type':mime+'; charset=utf-8','Content-Length':str(len(body)),
                        'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
                        'X-Content-Type-Options':'nosniff',
                        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(k,v)
            self.end_headers(); self.wfile.write(body)

        def host(self):
            if self.headers.get('Host') not in allowed:
                self.send(403, {'error':'只允许本机访问'}); return False
            return True

        def do_GET(self):
            if not self.host(): return
            if self.path == '/api/status': self.send(200, store.status()); return
            names={'/i18n.js':('i18n.js','text/javascript'),'/':('credential_migration.html','text/html'),'/migration.js':('credential_migration.js','text/javascript'),'/style.css':('style.css','text/css')}
            if self.path not in names: self.send(404, {'error':'不存在'}); return
            name,mime=names[self.path]
            self.send(200,(root/name).read_bytes().replace(b'__TOKEN__',token.encode()),mime)

        def do_POST(self):
            if not self.host():return
            if (self.headers.get('Origin') != 'http://'+self.headers.get('Host','') or
                    not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),token)):
                self.send(403, {'error':'请求来源校验失败'});return
            if self.path != '/api/save':self.send(404,{'error':'不存在'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 4096:raise ValueError()
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise ValueError()
                with lock:
                    slot=body.get('slot')
                    if slot in ('live','readonly'):
                        key,secret=validate_binance_keys(body.get('key'),body.get('secret'))
                        # GET only: migration never installs a live-capable client.
                        client=client_factory(False,key,secret)
                        client.account()
                        store.save(slot,dict(key=key,secret=secret))
                    elif slot=='model':
                        config=validate_config(body.get('base_url'),body.get('model'),body.get('api_key'))
                        model_factory(config).test_connection()
                        store.save(slot,dict(base_url=config['base_url'],model=config['model'],api_key=config['api_key'],enabled=True))
                    else:raise ValueError()
                self.send(200,dict(ok=True,storage=store.status()))
            except Exception:
                self.send(400,dict(error='检查或保存未完成。请核对连接配置、网络与钥匙串状态；旧 App 的连接仍保留。',storage=store.status()))

    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def serve(directory,port):
    server=migration_server(directory,port)
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print('连接保存页：http://127.0.0.1:%s · 只读检查 · 不启动交易' % port,flush=True)
    try:server.serve_forever()
    finally:server.server_close()
