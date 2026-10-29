"""Local research UI. No exchange credentials, runtime, or execution endpoints."""
import copy
import hashlib
import importlib
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .ai_news import validate_config, _json
from . import news_trial
from .news_trial import NewsTrial, collect_inputs
from .news import NewsService

ROOT = Path(__file__).resolve().parent


class TrialSession:
    def __init__(self, report_dir, collector=collect_inputs, runner_factory=NewsTrial):
        self.lock = threading.Lock()
        self.report_dir, self.collector, self.factory = Path(report_dir), collector, runner_factory
        self.reload_enabled = runner_factory is NewsTrial and collector is collect_inputs
        self.runner, self.busy, self.closed = None, False, False
        self.code_digest = hashlib.sha256(Path(news_trial.__file__).read_bytes()).hexdigest()
        self.news = NewsService()
        self.state = dict(status='waiting_key',configured=False,progress=None,report=None,
                          error='',report_path='',execution_connected=False,
                          version=news_trial.TRIAL_VERSION,partial_rows=[],partial_luna=None)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def configure(self, key):
        config = validate_config('http://127.0.0.1:8080/v1','gpt-6.1-sol',key)
        with self.lock:
            if self.closed or self.busy: raise ValueError('试验进行中，暂不能更换配置')
            self.runner = self.factory(config)
            self.state.update(status='ready',configured=True,error='')
        return self.snapshot()

    def progress(self, value):
        with self.lock:
            if not self.closed:
                self.state['progress'] = copy.deepcopy(value)
                if value.get('stage')=='result': self.state['partial_rows'].append(copy.deepcopy(value['row']))
                if value.get('stage')=='luna_result': self.state['partial_luna']=copy.deepcopy(value['luna'])

    def start(self, mode='compare'):
        with self.lock:
            if mode not in ('compare','triage'): raise ValueError('试验类型无效')
            if self.closed or self.runner is None: raise ValueError('请先在本页填写模型密钥')
            if self.busy: return copy.deepcopy(self.state)
            # Research-only fixed-module reload, on an explicit Start action.
            # No dynamic path/code from HTTP, no credential export or disk save.
            # Keeps the in-memory key across local fixes while invalidating all
            # old protocol caches. Never reload during active model requests.
            current = hashlib.sha256(Path(news_trial.__file__).read_bytes()).hexdigest()
            if current != self.code_digest and self.reload_enabled:
                module = importlib.reload(news_trial)
                self.factory = module.NewsTrial
                self.runner = self.factory(self.runner.config)
                self.collector = module.collect_inputs
                self.code_digest = current
                self.state['version'] = module.TRIAL_VERSION
            self.busy = True
            runner = self.runner
            self.state.update(status='running',mode=mode,error='',partial_rows=[],partial_luna=None,
                progress={'stage':'collect','message':'准备读取五品种公开资料'})
        def work():
            try:
                begin = time.monotonic()
                inputs,asof = self.collector(progress=self.progress,news=self.news)
                collection_ms = round((time.monotonic()-begin)*1000)
                result = runner.run(inputs,asof,progress=self.progress,mode=mode)
                result['collection_ms'] = collection_ms
                result['experiment_total_ms'] = round((time.monotonic()-begin)*1000)
                self.report_dir.mkdir(parents=True,exist_ok=True)
                path = self.report_dir/('news-pipeline-trial-'+uuid.uuid4().hex[:12]+'.json')
                # Reports contain selected public results/measurements, no config,
                # credentials, headers, article bodies or raw provider responses.
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as handle:
                    json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
                with self.lock:
                    self.state.update(status='complete',report=result,report_path=str(path),progress=None)
            except Exception:
                with self.lock:
                    self.state.update(status='error',error='试验未完成；请检查本机模型网关、交易App公开数据或网络后重试。',progress=None)
            finally:
                with self.lock: self.busy = False
        threading.Thread(target=work,daemon=True).start()
        return self.snapshot()

    def close(self):
        with self.lock:
            self.closed = True
            self.runner = None


def make_server(session, port=8767):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass

        def send(self, status, value, mime='application/json; charset=utf-8'):
            raw = value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            for key,val in [('Content-Type',mime),('Content-Length',str(len(raw))),('Cache-Control','no-store'),
                ('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),
                ('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")]:
                self.send_header(key,val)
            self.end_headers()
            self.wfile.write(raw)

        def valid_host(self):
            return self.headers.get('Host') == '127.0.0.1:'+str(self.server.server_port)

        def do_GET(self):
            if not self.valid_host(): return self.send(403,{'error':'仅允许本机访问'})
            if self.path == '/api/status': return self.send(200,session.snapshot())
            if self.path == '/api/report':
                return self.send(200,session.snapshot().get('report'))
            files = {'/':('news_trial.html','text/html; charset=utf-8'),
                     '/i18n.js':('i18n.js','text/javascript; charset=utf-8'),
                     '/trial.js':('news_trial.js','text/javascript; charset=utf-8'),
                     '/style.css':('style.css','text/css; charset=utf-8')}
            if self.path not in files: return self.send(404,{'error':'不存在'})
            name,mime = files[self.path]
            return self.send(200,(ROOT/'static'/name).read_bytes().replace(b'__TOKEN__',token.encode()),mime)

        def do_POST(self):
            if (not self.valid_host() or self.headers.get('Origin') != 'http://'+self.headers.get('Host','') or
                not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),token)):
                return self.send(403,{'error':'请求来源无效'})
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 1 <= length <= 2048 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = _json(self.rfile.read(length))
                if not isinstance(data,dict): raise ValueError()
                if self.path == '/api/config' and set(data) == {'api_key'}:
                    result = session.configure(data['api_key'])
                elif self.path in ('/api/start','/api/diagnose') and not data:
                    result = session.start('triage' if self.path=='/api/diagnose' else 'compare')
                else: return self.send(404,{'error':'不支持此操作'})
                return self.send(200,result)
            except Exception:
                return self.send(400,{'error':'参数无效、尚未连接或已有试验正在进行'})
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads = True
    return server


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Luna/Sol只读新闻试验')
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--report-dir',default=str(ROOT.parent/'reports'))
    args = parser.parse_args()
    session = TrialSession(args.report_dir)
    server = make_server(session,args.port)
    print('试验页 http://127.0.0.1:%d/ （模型密钥仅存本进程内存）'%server.server_port,flush=True)
    try: server.serve_forever()
    finally: session.close();server.server_close()


if __name__ == '__main__': main()
