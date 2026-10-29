"""Public-news model service. Host policy may consume direction; no direct order authority."""
import copy
import hashlib
import json
import re
import threading
import time

from .ai_news import AINewsError, ModelClient, validate_config
from .core import SYMBOLS
from .fundamentals import validate_metrics, metric_version
from .ai_schedule import admission, recent

REFRESH_SECONDS = 1
ERROR_BACKOFF_SECONDS = 30
MAX_CONCURRENT = 2
RESULT_TTL = 15 * 60
INPUT_TTL = 120
PUBLIC_FIELDS = ('id', 'url', 'title', 'source', 'origin', 'published', 'body')
COMPARE_MODELS = ('gpt-6-luna', 'gpt-6.1-sol')


class AIService:
    def __init__(self, client_factory=ModelClient, clock=time.time, credential_store=None):
        self.credential_store = credential_store
        self.lock = threading.RLock()
        self.factory = client_factory
        self.clock = clock
        self.client = None
        self.config = None
        self.enabled = False
        self.generation = 0
        self.closed = False
        self.inputs = {}
        self.results = {}
        self.failures = {}
        self.attempted = {}
        self.active = None
        self.inflight = {}
        self.last_analysis_latency_ms = None
        self.last_test_latency_ms = None
        self.test_pending = False
        self.last_success = None
        self.last_error = ''
        self.request_count = 0
        self.next_allowed_at = 0
        self.last_latency_ms = None
        self.comparison = None
        self.scope_provider = lambda: None
        self.major_attempted = set()
        self.event_alerts = {}
        self.usage = dict(analysis_calls=0, reported_calls=0, prompt_tokens=0, completion_tokens=0)

    def configure(self, base_url, model, api_key, enabled=True, persist=True):
        if type(enabled) is not bool:
            raise ValueError('AI启用设置无效')
        config = validate_config(base_url, model, api_key)
        client = self.factory(config)
        with self.lock:
            if self.closed:
                raise ValueError('服务正在退出')
            if persist and self.credential_store is not None:
                self.credential_store.save('model', dict(base_url=config['base_url'], model=config['model'],
                                                       api_key=config['api_key'], enabled=enabled))
            self.generation += 1
            self.config, self.client, self.enabled = config, client, enabled
            self.results.clear()
            self.failures.clear()
            self.attempted.clear()
            self.major_attempted.clear()
            self.event_alerts.clear()
            self.usage = dict(analysis_calls=0, reported_calls=0, prompt_tokens=0, completion_tokens=0)
            self.last_success = None
            self.last_error = ''
            self.test_pending = False
            self.comparison = None
            self.last_latency_ms = None
            self.last_analysis_latency_ms = self.last_test_latency_ms = None
            self.next_allowed_at = self.clock()
            # Collection runs before credentials are entered. Queue latency
            # starts when this configuration becomes eligible to analyze it.
            for value in self.inputs.values():
                value['queued_at'] = self.next_allowed_at
        return self.status()

    def disconnect(self, forget=True):
        with self.lock:
            if forget and self.credential_store is not None:
                self.credential_store.forget('model')
            self.generation += 1
            self.client = self.config = None
            self.enabled = self.test_pending = False
            self.results.clear()
            self.failures.clear()
            self.attempted.clear()
            self.major_attempted.clear()
            self.event_alerts.clear()
            self.usage = dict(analysis_calls=0, reported_calls=0, prompt_tokens=0, completion_tokens=0)
            self.last_success = None
            self.last_error = ''
            self.comparison = None
            self.last_latency_ms = None
            self.last_analysis_latency_ms = self.last_test_latency_ms = None
        return self.status()

    def stop(self):
        with self.lock:
            self.closed = True
            self.disconnect(forget=False)

    def status(self):
        with self.lock:
            config = self.config or {}
            testing = bool(self.active and self.active['kind'] == 'test' and
                           self.active['generation'] == self.generation)
            health = {}
            for symbol in SYMBOLS:
                state = self.snapshot(symbol)['status']
                health[state] = health.get(state,0)+1
            return dict(configured=self.client is not None, enabled=self.enabled,
                        base_url=config.get('base_url', ''), model=config.get('model', ''),
                        status='disabled' if not self.enabled else 'testing' if testing else
                               'error' if self.last_error else 'ready' if self.last_success else 'configured',
                        error=self.last_error, analysis_health=health, retry_supported=True, last_success=self.last_success,
                        test_pending=self.test_pending, request_count=self.request_count,
                        last_latency_ms=self.last_latency_ms, last_analysis_latency_ms=self.last_analysis_latency_ms,
                        last_test_latency_ms=self.last_test_latency_ms, active_count=len(self.inflight)+bool(self.active),
                        max_concurrent=MAX_CONCURRENT, comparison=self.comparison_status(),
                        next_allowed_at=self.next_allowed_at, monitoring=self.monitoring_status(),
                        usage=dict(self.usage), trading_authority=False)

    def _admission(self, symbol, value, now):
        try:scope = self.scope_provider()
        except Exception:scope = dict(mode='unavailable', focus=None)
        policy = admission(scope, symbol, (value or {}).get('major_events', []), self.major_attempted, now)
        if value is not None:
            if policy['allowed'] and value.get('_eligible') is False:
                value['queued_at'] = now  # Exclude deliberate standby from model queue latency.
            value['_eligible'] = policy['allowed']
        return policy

    def monitoring_status(self):
        with self.lock:
            now = self.clock()
            rows = []
            for symbol in SYMBOLS:
                value = self.inputs.get(symbol)
                policy = self._admission(symbol, value, now)
                alert = self.event_alerts.get(symbol)
                if alert and not recent(alert['started_at'], now):alert = None
                alert = copy.deepcopy(alert)
                if alert and alert['status'] == 'ready':
                    result = self.results.get(symbol)
                    if (not result or not self._input_current(value, now) or
                            result['fingerprint'] != alert['result_id'] or
                            not {e['token'] for e in alert['events']}.issubset({e['token'] for e in value.get('major_events', [])}) or
                            not recent(result['completed_at'], now, RESULT_TTL) or
                            not value['versions'].issubset(result['versions'])):
                        alert.update(status='historical', direction='uncertain',
                                     reason='输入已改变、过期或来源不可用；仅保留历史事件提示，不能据此开仓')
                rows.append(dict(symbol=symbol, **policy, inflight=symbol in self.inflight,
                                 has_new_input=bool(value and self._new_versions(symbol, value)),
                                 alert=alert))
            return dict(rows=rows, rule='position_first', automatic_rotation=False)

    def comparison_status(self):
        with self.lock:
            c=self.comparison
            if c is None:return None
            return {k:copy.deepcopy(c[k]) for k in ('symbol','status','started_at','rows','models','input_id')}

    def request_comparison(self, symbol):
        with self.lock:
            value=self.inputs.get(symbol)
            if not self.enabled or not self.config or self.config['provider']!='api.openai.com':
                raise ValueError('模型对比需要已启用的官方OpenAI连接')
            if not self._input_current(value,self.clock()) or not (value['articles'] or value['metrics']):
                raise ValueError('等待当前品种的有效公开输入后再对比')
            identity=hashlib.sha256(json.dumps(sorted(value['versions'])).encode()).hexdigest()
            if self.comparison and self.comparison['symbol']==symbol and self.comparison['input_id']==identity:
                return self.status()
            if self.comparison and self.comparison['status'] in ('pending','running'):
                raise ValueError('上一组模型对比尚未完成')
            self.comparison=dict(symbol=symbol,status='pending',started_at=self.clock(),rows=[],models=list(COMPARE_MODELS),
                input_id=identity,articles=copy.deepcopy(value['articles']),metrics=copy.deepcopy(value['metrics']))
        return self.status()

    def select_model(self, model):
        with self.lock:
            if not self.enabled or not self.config or self.config['provider']!='api.openai.com' or model not in COMPARE_MODELS:
                raise ValueError('请选择官方连接支持的Luna或GPT-6.1 Sol')
            config=dict(self.config)
            comparison=copy.deepcopy(self.comparison)
            if model==config['model']:return self.status()
            self.configure(config['base_url'],model,config['api_key'])
            if comparison and comparison['status']=='complete':self.comparison=comparison
        return self.status()

    def run_comparison_once(self):
        """Two bounded public-input calls; never publish to trading results."""
        with self.lock:
            c=self.comparison
            if (self.closed or not self.enabled or self.active or self.inflight or c is None or c['status'] not in ('pending','running')):return False
            if self.clock()-c['started_at']>300:
                c['status']='expired';return False
            model=c['models'][len(c['rows'])]
            config=dict(self.config,model=model)
            client=self.factory(config)
            generation=self.generation; c['status']='running'
            self.active=dict(generation=generation,symbol=c['symbol'],kind='comparison')
            self.request_count+=1
            articles=copy.deepcopy(c['articles']);metrics=copy.deepcopy(c['metrics'])
        start=time.monotonic();error='';result=None
        try:result=client.analyze(c['symbol'],articles,now=c['started_at'],fundamentals=metrics)
        except AINewsError as exc:error=str(exc)
        except Exception:error='模型对比请求或结果校验失败'
        elapsed=round((time.monotonic()-start)*1000)
        events=(result or {}).get('events',[]);drivers=(result or {}).get('drivers',[])
        row=dict(model=model,latency_ms=elapsed,status='error' if error else 'ok',error=error,
                 direction=(result or {}).get('direction'),summary=(result or {}).get('summary',''),
                 events=len(events),matched_events=sum(e.get('evidence_valid') is not False for e in events),
                 factors=len(drivers),unverified_factors=sum(d.get('status')=='unverified' for d in drivers),
                 usage=copy.deepcopy(getattr(client,'last_usage',{})))
        with self.lock:
            self.active=None
            if generation!=self.generation or self.comparison is not c or self.closed:return True
            c['rows'].append(row)
            c['status']='complete' if len(c['rows'])==len(c['models']) else 'pending'
        return True

    def request_retry(self,symbol):
        with self.lock:
            if symbol not in SYMBOLS or symbol not in self.failures:
                raise ValueError('当前品种没有可重试的失败结果')
            if not self.enabled or symbol in self.inflight:
                raise ValueError('AI未启用或当前品种仍在处理中')
            self.failures.pop(symbol,None)
            self.attempted.pop(symbol,None)
            if symbol in self.inputs:self.inputs[symbol]['queued_at']=self.clock()
        return self.status()

    def request_test(self):
        with self.lock:
            if self.closed or not self.client or not self.enabled:
                raise ValueError('请先填写模型接口、模型名和密钥并启用AI分析')
            self.test_pending = True
        return self.status()

    def submit(self, symbol, articles, asof, healthy=True, fundamentals=None, major_events=None):
        if symbol not in SYMBOLS:
            raise ValueError('不支持的AI新闻品种')
        # Deliberately rebuild a public allowlist: never pass an account,
        # portfolio, execution decision or credentials object to the model.
        public = []
        identities = set()
        if isinstance(articles, list):
            for item in articles[:64]:
                if not isinstance(item, dict):
                    continue
                row = {key: copy.deepcopy(item.get(key)) for key in PUBLIC_FIELDS}
                if not all(isinstance(row[k], str) for k in PUBLIC_FIELDS if k != 'published'):
                    continue
                if type(row['published']) not in (int, float):
                    continue
                row.update(body=row['body'][:5000], title=row['title'][:500],
                           url=row['url'][:2000], id=row['id'][:80], source=row['source'][:100],
                           origin=row['origin'][:100])
                identity = (row['origin'], row['id'])
                if identity in identities:
                    continue
                identities.add(identity)
                public.append(row)
                if len(public) == 8:
                    break
        public.sort(key=lambda row: (row['origin'], row['id']))
        metrics=validate_metrics(fundamentals or [],symbol,self.clock())
        metrics_version=metric_version(metrics)
        fingerprint = hashlib.sha256(json.dumps([public,metrics], sort_keys=True, ensure_ascii=False,
                                                 allow_nan=False).encode()).hexdigest()
        # Refresh timestamps, tracking URLs and ordering do not create news.
        # Keep all attempted versions, including failures and items that have
        # left the feed, so polling cannot re-bill the same interpretation.
        versions = frozenset(hashlib.sha256(json.dumps(
            [row['origin'], row['id'], re.sub(r'\s+', ' ', row['title']).strip(),
             re.sub(r'\s+', ' ', row['body']).strip()], ensure_ascii=False
        ).encode()).hexdigest() for row in public)
        versions=versions | frozenset('metric:'+metric_version([row]) for row in metrics)
        with self.lock:
            if not self.closed:
                old=self.inputs.get(symbol,{})
                queued_at=old.get('queued_at',self.clock()) if old.get('versions')==versions else self.clock()
                self.inputs[symbol] = dict(queued_at=queued_at, articles=public, metrics=metrics, metrics_version=metrics_version, fingerprint=fingerprint, versions=versions,
                                          _eligible=old.get('_eligible'),
                                          major_events=copy.deepcopy(major_events or []),
                                          asof=asof, healthy=healthy is True)
                if healthy is True and self.last_error == 'AI新闻输入处理异常；规则分析继续独立运行':
                    self.last_error = ''

    def _new_versions(self, symbol, value):
        return value['versions'] - self.attempted.get(symbol, set())

    def _input_current(self, value, now):
        return (value and value['healthy'] and type(value['asof']) in (int, float)
                and 0 <= now - value['asof'] <= INPUT_TTL)

    def input_error(self, symbol):
        with self.lock:
            if symbol in self.inputs:
                self.inputs[symbol]['healthy'] = False
            self.last_error = 'AI新闻输入处理异常；规则分析继续独立运行'

    def snapshot(self, symbol, current_metrics=None):
        now = self.clock()
        with self.lock:
            base = dict(symbol=symbol, status='disabled', reason='AI未启用；当前使用Python规则分析',
                        summary='', direction='uncertain', events=[], drivers=[], uncertainties=[],
                        model=(self.config or {}).get('model', ''), analyzed_at=None,
                        input_asof=None, trading_authority=False, cached=False)
            value = self.inputs.get(symbol)
            policy = self._admission(symbol, value, now)
            base['monitoring'] = policy
            if not self.enabled or self.closed:
                return base
            if not value:
                return dict(base, status='error' if self.last_error else 'pending',
                            reason=self.last_error or '等待当前品种的公开新闻或统计数据')
            if not self._input_current(value, now):
                return dict(base, status='stale', reason='新闻输入过期或来源异常；AI解读不可用')
            if not value['articles'] and not value['metrics']:
                return dict(base, status='no_news', reason='暂无可分析的新闻正文或有效统计数据；不调用模型')
            active = self.inflight.get(symbol) or self.active
            if active and active['kind'] != 'comparison' and active['generation'] == self.generation and active['symbol'] == symbol:
                return dict(base, status='analyzing', reason='模型正在后台分析；K线判断与持仓保护不等待模型',
                    processing_ms=max(0,round((now-active['started_at'])*1000)),queue_wait_ms=active.get('queue_wait_ms',0))
            if self._new_versions(symbol, value):
                if not policy['allowed']:
                    return dict(base, status='standby', reason=policy['reason'])
                return dict(base, status='pending', reason='发现新增或修订的新闻或统计数据，等待模型分析',queue_wait_ms=max(0,round((now-value.get('queued_at',now))*1000)))
            failure = self.failures.get(symbol)
            if failure and failure['versions'] & value['versions']:
                return dict(base, status='error', reason=failure['reason'],
                            latency_ms=failure.get('latency_ms'), queue_wait_ms=failure.get('queue_wait_ms'))
            result = self.results.get(symbol)
            if not result:
                return dict(base, status='unchanged', reason='没有新增或修订的新闻或统计数据，不重复调用模型；暂无可展示结果')
            if now < result['completed_at']:
                return dict(base, status='stale', reason='模型结果时间异常，暂不展示')
            metrics_changed=current_metrics is not None and result['metrics_version']!=metric_version(current_metrics)
            historical = (metrics_changed or result['metrics_version']!=value['metrics_version'] or
                          not value['versions'].issubset(result['versions']))
            display = copy.deepcopy(result['value'])
            return dict(base, provider=display.get('provider', ''),
                        status='historical' if historical else 'ok',
                        reason='无新增新闻或统计数据，保留原分析时间，不重复调用模型；人工核验可选',
                        summary=display.get('summary', ''), direction='uncertain' if historical else display.get('direction', 'uncertain'),
                        events=display.get('events', []), drivers=display.get('drivers', []), uncertainties=display.get('uncertainties', []),
                        model=(self.config or {}).get('model', ''), analyzed_at=result['completed_at'],
                        latency_ms=result.get('latency_ms'), queue_wait_ms=result.get('queue_wait_ms'), result_id=result['fingerprint'],
                        input_asof=result['input_asof'], retained_input_checked_at=now if not historical else None, cached=now-result['completed_at'] >= REFRESH_SECONDS,
                        trading_authority=False)

    def run_once(self, symbol):
        """Called only by the model worker. Never hold locks across network I/O."""
        now = self.clock()
        with self.lock:
            if (self.closed or not self.enabled or self.active or symbol in self.inflight or
                len(self.inflight)>=MAX_CONCURRENT or now < self.next_allowed_at):
                return False
            kind = 'test' if self.test_pending else 'analysis'
            if kind=='test' and self.inflight:return False
            value = self.inputs.get(symbol)
            policy = self._admission(symbol, value, now)
            if kind == 'analysis':
                if not policy['allowed']:return False
                if not self._input_current(value, now) or not (value['articles'] or value['metrics']):
                    return False
                if not self._new_versions(symbol, value):
                    return False
            # Construct before reserving a slot: a client initialization
            # failure must not leave an analysis permanently in flight.
            client = self.factory(dict(self.config))
            job = dict(generation=self.generation, symbol=symbol, kind=kind, started_at=now,
                       policy=policy,
                       queue_wait_ms=max(0,round((now-(value or {}).get('queued_at',now))*1000)),
                       metrics_version=value['metrics_version'] if value else None,
                       fingerprint=value['fingerprint'] if value else None,
                       versions=value['versions'] if value else frozenset())
            if kind=='test':self.active = job
            else:self.inflight[symbol] = job
            self.test_pending = False
            self.request_count += 1
            self.next_allowed_at = now + REFRESH_SECONDS
            articles = copy.deepcopy(value['articles']) if kind == 'analysis' else []
            metrics = copy.deepcopy(value['metrics']) if kind == 'analysis' else []
            input_asof = value['asof'] if value else None
            if kind == 'analysis':
                self.attempted.setdefault(symbol, set()).update(job['versions'])
                self.major_attempted.update(e['token'] for e in value.get('major_events', []))
                self.usage['analysis_calls'] += 1
                if policy['mode'] == 'event':
                    self.event_alerts[symbol] = dict(symbol=symbol, status='analyzing', started_at=now,
                        result_id=job['fingerprint'],
                        events=copy.deepcopy(policy['events']), direction='uncertain', summary='',
                        automatic_rotation=False)
        result = None
        error = ''
        backoff = False
        started=time.monotonic()
        try:
            result = (client.test_connection() if kind == 'test' else
                      client.analyze(symbol, articles, now=now, **({'fundamentals':metrics} if metrics else {})))
        except AINewsError as exc:
            error = str(exc)
            backoff = exc.status==429
        except Exception:
            error = 'AI请求或结果核验失败；请检查接口设置后重试'
        completed = self.clock()
        latency=round((time.monotonic()-started)*1000)
        with self.lock:
            if kind=='test' and self.active is job:self.active = None
            if self.inflight.get(symbol) is job:self.inflight.pop(symbol)
            if self.closed or job['generation'] != self.generation:
                return True
            if kind == 'analysis':
                usage = getattr(client, 'last_usage', {})
                if isinstance(usage, dict) and all(type(usage.get(k)) is int and 0 <= usage[k] < 10_000_000
                                                for k in ('prompt_tokens', 'completion_tokens')):
                    self.usage['reported_calls'] += 1
                    for key in ('prompt_tokens', 'completion_tokens'):self.usage[key] += usage[key]
                if policy['mode'] == 'event':
                    alert = self.event_alerts.get(symbol)
                    if alert and alert['started_at'] == job['started_at']:
                        alert.update(status='error' if error else 'ready', completed_at=completed,
                            direction=(result or {}).get('direction', 'uncertain'),
                            summary=str((result or {}).get('summary', ''))[:500],
                            reason=error or '仅提示关注；现有持仓按原退出规则处理，空仓后重新核验')
            # Retain completed work even if the input becomes unavailable or
            # changes in flight. snapshot hides it during outages/new work and
            # labels results for a different article set as historical.
            if kind == 'test':self.last_error = error
            self.last_latency_ms = latency
            if kind=='test':self.last_test_latency_ms=latency
            else:self.last_analysis_latency_ms=latency
            if error:
                if kind == 'analysis':
                    self.failures[symbol] = dict(versions=job['versions'], reason=error,
                                                latency_ms=latency, queue_wait_ms=job['queue_wait_ms'])
                if backoff:self.next_allowed_at = max(self.next_allowed_at, completed + ERROR_BACKOFF_SECONDS)
            else:
                self.last_success = completed
                if kind == 'analysis':
                    self.failures.pop(symbol, None)
                    self.results[symbol] = dict(fingerprint=job['fingerprint'], versions=job['versions'], metrics_version=job['metrics_version'], value=copy.deepcopy(result),
                                                completed_at=completed, input_asof=input_asof, latency_ms=latency, queue_wait_ms=job['queue_wait_ms'])
        return True
