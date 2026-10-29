'use strict';
// Run with: node --test binance_trader/tests/test_news_ui.js
// Evaluates source UI against an inert DOM; it never contacts a running trader.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const now = 1791100000;
class Clock extends Date { static now() { return now * 1000; } }
function harness(symbol = 'XAUUSDT') {
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: '', options: [], textContent: '', innerHTML: '', dataset: {}, style: {},
      classList: {toggle() {}, add() {}}, addEventListener() {}, append() {}, setAttribute() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: node, querySelector: () => ({content: 'test-only'}),
      querySelectorAll: () => [], createElement: () => node('unused')},
    fetch(url, options) {
      assert.equal(options, undefined, 'UI initialization must make no POST requests');
      assert.ok(['/api/state', '/api/analysis'].includes(url));
      return new Promise(() => {});
    },
    setInterval() {}, setTimeout() {}, clearTimeout() {}, Date: Clock, URL,
    Number, String, Math, JSON, Promise, Object, window: {},
  });
  vm.runInContext(source, context);
  context.fixture = {state: {symbol, enabled: false}, mode: 'paper'};
  vm.runInContext('latest = fixture', context);
  return {context, node};
}
function decision(overrides = {}) {
  return {policy_version: 2, entry_allowed: true, symbol: 'XAUUSDT',
    asof: now, expires: now + 60, status: 'bullish', direction: 'BUY',
    reason: '符合方向证据规则', confidence: '官方原始公告', risks: [], drivers: [{key: 'rates', label: '利率', status: 'verified', reason: '已核验'}],
    evidence: [{verified: true, direction: 'BUY', reason: '已有可核验证据'}], ...overrides};
}
function risk(status = 'active', overrides = {}) {
  return {id: 'risk-1', category: 'geopolitical', subject: '运输中断', status,
    blocking: status !== 'resolved', gate_version:1, entry_gate:status==='resolved'?'resolved':'cooldown',
    gate_reason:status==='resolved'?'事件已解除':'新事件暂缓1小时',
    acute_event_id:status==='resolved'?null:'acute-test',acute_rule:status==='resolved'?null:'new_attack',
    acute_event_at:status==='resolved'?null:now-60,time_basis:status==='resolved'?null:'event_time',
    cooldown_until:status==='resolved'?null:now+3540,reason: '尚未见可核验解除消息',
    source: 'Fed', url: 'https://www.federalreserve.gov/article',
    published: now - 60, first_seen: now - 10 * 86400,
    last_seen: now, ...overrides};
}

test('legacy neutral, absent flags and no evidence never become permission', () => {
  const {context} = harness();
  for (const data of [
    {symbol: 'XAUUSDT', status: 'neutral', expires: now + 60},
    decision({entry_allowed: undefined}), decision({entry_allowed: 'true'}),
    decision({policy_version: 1}), decision({status: 'insufficient', entry_allowed: false}),
    decision({status: 'neutral'}), decision({evidence: []}),
    decision({evidence: [{verified: false, direction: 'BUY'}]}),
    decision({evidence: [{verified: true, direction: 'SELL'}]}),
  ]) {
    const state = context.newsState({decision: data});
    assert.equal(state.ready, false);
    assert.match(state.label, /禁止新开仓/);
    assert.doesNotMatch(state.label, /条件通过/);
  }
});

test('fresh explicit permission still needs matching symbol, direction and time', () => {
  const {context} = harness();
  assert.equal(context.newsState({decision: decision()}).ready, true);
  for (const override of [
    {expires: now - 1}, {asof: now + 30}, {symbol: 'CLUSDT'},
    {asof: now - 100, expires: now + 30}, {expires: now + 100 * 365 * 86400},
    {asof: String(now)}, {expires: String(now + 30)},
    {direction: 'SELL'}, {status: 'unavailable'}, {status: 'blocked'},
    {entry_allowed: false},
  ]) assert.equal(context.newsState({decision: decision(override)}).ready, false);
});

test('unified news panel uses overall AI direction, expires it and preserves rule gates',()=>{
 const {context}=harness();
 const ai={policy_version:1,symbol:'XAUUSDT',status:'active',direction:'SELL',model:'gpt-6-luna',
  analyzed_at:now-10,expires:now+890,result_id:'a'.repeat(64)};
 const fallback=decision({status:'insufficient',direction:null,entry_allowed:false,kline_allowed:true,evidence:[],event_id:null,ai});
 const state=context.newsState({decision:fallback});
 assert.equal(state.ready,true);assert.equal(state.path,'ai+kline');assert.match(state.label,/AI综合偏空/);
 assert.equal(context.newsState({decision:decision({ai})}).ready,false);
 assert.equal(context.newsState({decision:{...fallback,risks:[risk()]}}).ready,false);
 for(const change of [{analyzed_at:now-900,expires:now},{symbol:'CLUSDT'},{policy_version:true},{result_id:'fake'},{status:'fallback',direction:null}]){
  const value=context.newsState({decision:{...fallback,ai:{...ai,...change}}});
  assert.equal(value.path,'kline');assert.doesNotMatch(value.label,/AI综合偏空/);
 }
});

test('active cooldown, unknown or malformed risks cannot be cancelled by direction', () => {
  const {context} = harness();
  for (const value of [risk(), risk('review_required'), risk('unknown'),
    risk('resolved', {blocking: true}), risk('active', {blocking: false}),
    risk('active', {blocking: undefined}),risk('resolved', {id: ''}),
    risk('resolved', {published: now + 100}),risk('resolved', {last_seen: now - 20 * 86400}),
    risk('resolved', {source: 'Unknown'}),risk('resolved', {published: String(now - 1)})]) {
    assert.equal(context.newsState({decision: decision({risks: [value]})}).ready, false);
  }
  assert.equal(context.newsState({decision: decision({risks: undefined})}).ready, false);
  assert.equal(context.newsState({decision: decision({risks: [risk('resolved')]})}).ready, true);
});

test('source availability is not called adequate evidence or market coverage', () => {
  const {context} = harness();
  const text = context.renderCoverage({healthy: true, successful: ['A', 'B'],
    scope: '有限新闻来源', uncovered: ['未接入库存预期数据'], failed: ['C']});
  assert.match(text, /来源读取正常/);
  assert.match(text, /不代表覆盖完整或允许交易/);
  assert.match(text, /未接入库存预期数据/);
  assert.match(text, /暂不可用：C/);
  assert.doesNotMatch(text, /覆盖达标/);
});

test('five instruments retain their own driver labels and unknown states', () => {
  const profiles = {
    XAUUSDT: ['利率与美元', '避险需求'], XAGUSDT: ['工业与光伏需求', '贵金属联动'],
    CLUSDT: ['美国库存与产量', '炼厂需求'], BZUSDT: ['全球供应与OPEC', '航运与地缘风险'],
    NATGASUSDT: ['美国天然气库存', '天气与LNG需求'],
  };
  for (const [symbol, labels] of Object.entries(profiles)) {
    const {context, node} = harness(symbol);
    context.renderNewsDrivers({symbol, decision: decision({symbol, drivers: [
      {key: 'first', label: labels[0], status: 'no_recent_evidence', reason: '未取得近期证据'},
      {key: 'second', label: labels[1], status: 'not_covered', reason: '没有专门数据源'},
    ]})});
    for (const label of labels) assert.ok(node('news-drivers').innerHTML.includes(label));
    assert.match(node('news-drivers').innerHTML, /规则暂无方向证据/);
    assert.match(node('news-drivers').innerHTML, /AI 未启用/);
    assert.doesNotMatch(node('news-drivers').innerHTML, /class="driver-status">未覆盖/);
    assert.doesNotMatch(node('news-drivers').innerHTML, /条件通过/);
  }
});

test('background older than six hours persists with explicit state and source dates', () => {
  const {context, node} = harness();
  context.renderPersistentRisks({symbol: 'XAUUSDT', decision: decision({
    expires: now - 1, risks: [backgroundRisk('active',{published:now-10*86400,last_seen:now-7*3600}), backgroundRisk('review_required'), risk('resolved')],
  })});
  const html = node('news-risks').innerHTML;
  assert.match(html, /持续背景 · 不单独阻止开仓/);
  assert.match(html, /事实状态：尚待独立核验/);
  assert.match(html, /已确认解除/);
  assert.match(html, /首次记录/); assert.match(html, /最近抓取/);
  assert.match(html, /来源发布时间/); assert.match(html, /Fed/);
  assert.match(html, /https:\/\/www.federalreserve.gov\/article/);
});

test('news, drivers and persistent risk text cannot inject HTML or non-HTTPS links', () => {
  const {context, node} = harness();
  const attack = '<img src=x onerror=alert(1)>';
  context.renderNews({symbol: 'XAUUSDT', checked: now, updated: now,
    coverage: {healthy: true, successful: [attack]},
    decision: decision({risks: [risk('active', {subject: attack, reason: attack,
      source: attack, url: 'javascript:alert(1)'})],
      drivers: [{key: 'x', label: attack, status: 'not_covered', reason: attack}],
      evidence: [{verified: true, direction: 'BUY', title: attack, source: attack,
        excerpt: attack, url: 'data:text/html,x'}]}),
    data: [{title: attack, url: 'javascript:alert(1)', source: attack, analysis: attack}],
  });
  for (const id of ['news-risks', 'news-drivers', 'news-evidence', 'news-analysis']) {
    assert.doesNotMatch(node(id).innerHTML, /<img|href="(?:javascript|data):/);
    assert.match(node(id).innerHTML, /&lt;img/);
  }
  assert.match(node('news-decision').textContent, /禁止新开仓/);
});

test('one-second repeated rendering preserves disclosure content', () => {
  const {context, node} = harness();
  const news = {symbol: 'XAUUSDT', checked: now, updated: now, decision: decision(), data: []};
  context.renderNews(news);
  node('news-evidence').innerHTML = 'user disclosure remains open';
  context.renderNews(news);
  assert.equal(node('news-evidence').innerHTML, 'user disclosure remains open');
});


test('drivers require complete valid metadata and review_required blocks other verified drivers', () => {
  const {context} = harness();
  const good = {key: 'rates', label: '利率', status: 'verified', reason: '可核验'};
  for (const drivers of [undefined, [], [null], [{...good, key: ''}], [{...good, reason: ''}],
    [{...good, status: 'unexpected'}], [good, {...good, key: 'dollar', status: 'review_required'}]]) {
    assert.equal(context.newsState({decision: decision({drivers})}).ready, false);
  }
  for (const status of ['no_recent_evidence', 'not_covered', 'background']) {
    assert.equal(context.newsState({decision: decision({drivers: [good, {...good, key: 'demand', status}]})}).ready, true);
  }
});

test('start control responds to both snapshot news and analysis news without bypassing existing locks', () => {
  const {context, node} = harness();
  const good = decision();
  context.snapshot = {mode: 'paper',updated: now, error: '', decision: {news: good},
    state: {symbol:'XAUUSDT',risk:1.5,leverage:2,enabled:false,position:null,pending:null}};
  vm.runInContext('latest=snapshot;stateAvailable=true;analysisAvailable=true;leverageInfo={symbol:"XAUUSDT",maximum:20,expires:Date.now()/1000+60}',context);
  node('symbol').value='XAUUSDT';node('risk').value='1.5';node('leverage').value='2';
  context.syncStartControls();assert.equal(node('start').disabled,false);assert.match(node('start-status').textContent,/已核验新闻许可/);
  context.bad=decision({entry_allowed:false,status:'insufficient',direction:null,asof:now+1});
  vm.runInContext('latestNews={decision:bad}',context);context.syncStartControls();assert.equal(node('start').disabled,true);assert.match(node('start-status').textContent,/禁止新开仓/);
  context.newGood=decision({asof:now+2});vm.runInContext('latestNews={decision:newGood}',context);context.syncStartControls();assert.equal(node('start').disabled,false);
  context.bad.asof=now+3;vm.runInContext('latest.decision.news=bad',context);context.syncStartControls();assert.equal(node('start').disabled,true);
  context.newGood.asof=now+4;vm.runInContext('latestNews={decision:newGood};stateAvailable=false',context);context.syncStartControls();assert.equal(node('start').disabled,true);
  vm.runInContext('stateAvailable=true;analysisAvailable=false',context);context.syncStartControls();assert.equal(node('start').disabled,true);
  vm.runInContext('analysisAvailable=true;latest.state.pending={id:"fixture"}',context);context.syncStartControls();assert.equal(node('start').disabled,true);
});


test('risk subjects use readable Chinese while retaining facility names and unknown text', () => {
 const {context}=harness();
 assert.equal(context.riskSubject({subject:'iran_israel_us_conflict'}),'伊朗、以色列、美国相关冲突');
 assert.equal(context.riskSubject({subject:'middle_east_conflict'}),'中东地区冲突');
 assert.equal(context.riskSubject({subject:'strait_of_hormuz'}),'霍尔木兹海峡');
 assert.equal(context.riskSubject({subject:'suez_canal'}),'苏伊士运河');
 assert.equal(context.riskSubject({subject:'us:freeport_lng',category:'us_lng_exports'}),'美国 · Freeport LNG');
 assert.equal(context.riskSubject({subject:'unknown <text>'}),'unknown <text>');
});


test('driver reasons translate risk keys without changing source text, state or HTML escaping', () => {
 const {context,node}=harness();
 const reason='持续背景待解除；Guardian：middle_east_conflict、iran_us_conflict、strait_of_hormuz；EIA：us:freeport_lng。<img src=x onerror=alert(1)> 原文保留';
 const driver={key:'shipping',label:'航运与海峡',status:'review_required',reason};
 context.renderNewsDrivers({symbol:'XAUUSDT',decision:decision({drivers:[driver],risks:[{subject:'us:freeport_lng',category:'us_lng_exports'}]})});
 const html=node('news-drivers').innerHTML;
 assert.match(html,/Guardian：中东地区冲突、伊朗、美国相关冲突、霍尔木兹海峡/);
 assert.match(html,/EIA：美国 · Freeport LNG/);
 assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt; 原文保留/);
 assert.doesNotMatch(html,/<img|middle_east_conflict|iran_us_conflict|strait_of_hormuz/);
 assert.equal(driver.reason,reason);assert.equal(driver.status,'review_required');
});


function klineFallback(overrides = {}) {
 return decision({status:'insufficient',entry_allowed:false,kline_allowed:true,direction:null,
   evidence:[],event_id:null,reason:'新闻方向不足，已完成持续风险检查',...overrides});
}

test('only explicit clean insufficient permission allows waiting for closed-kline breakout', () => {
 const {context}=harness();
 const state=context.newsState({decision:klineFallback()});
 assert.equal(state.ready,true);assert.equal(state.path,'kline');assert.equal(state.tone,'');
 assert.equal(state.label,'新闻证据不足 · 按K线判断');
 assert.match(state.reason,/已收盘突破/);assert.match(state.reason,/不会立即买卖/);
 for(const override of [
  {kline_allowed:undefined},{kline_allowed:false},{kline_allowed:'true'},{kline_allowed:1},{kline_allowed:null},
  {entry_allowed:true},{direction:'BUY'},{status:'blocked'},{status:'unavailable'},
  {status:'neutral'},{status:'bullish',direction:'BUY',entry_allowed:true},
  {status:'bearish',direction:'SELL',entry_allowed:true},
  {evidence:[{verified:false,direction:'BUY'}]},{evidence:undefined},{event_id:'unverified-event'},
 ]) assert.equal(context.newsState({decision:klineFallback(override)}).ready,false);
 assert.equal(context.newsState({decision:decision()}).ready,true,'verified-news path remains available when kline flag is absent');
 assert.equal(context.newsState({decision:decision({kline_allowed:false})}).ready,true);
 assert.equal(context.newsState({decision:decision({kline_allowed:true})}).ready,false);
});

test('kline fallback cannot hide stale metadata or active cooldowns', () => {
 const {context}=harness();
 for(const override of [
  {asof:now-100,expires:now+20},{expires:now+100*365*86400},{policy_version:1},
  {risks:[risk()]},{risks:[risk('review_required')]},{risks:[risk('resolved',{blocking:true})]},
  {risks:undefined},{drivers:[]},
 ]) assert.equal(context.newsState({decision:klineFallback(override)}).ready,false);
 for(const status of ['no_recent_evidence','not_covered','review_required']){
  assert.equal(context.newsState({decision:klineFallback({drivers:[{key:'demand',label:'需求',status,reason:'无可用方向证据'}]})}).ready,true);
 }
});

test('unconfirmed driver is displayed as reference on an allowed kline path', () => {
 const {context,node}=harness();
 const news={symbol:'XAUUSDT',decision:klineFallback({drivers:[
  {key:'rates',label:'联储政策',status:'review_required',reason:'规则未确认方向，仅作参考'}]}),data:[]};
 const state=context.newsState(news);
 assert.equal(state.ready,true);assert.equal(state.path,'kline');
 assert.doesNotMatch(state.label,/禁止/);
 context.renderNews(news);
 assert.match(node('news-drivers').innerHTML,/规则未确认方向/);
 assert.match(node('news-decision').textContent,/按K线判断/);
 assert.doesNotMatch(node('news-decision').textContent,/禁止新开仓/);
});

test('fallback updates start permission and source without claiming immediate orders', () => {
 const {context,node}=harness();
 context.snapshot={mode:'paper',updated:now,error:'',decision:{source:'kline',action:'WAIT',reason:'等待收盘突破',news:klineFallback()},
  state:{symbol:'XAUUSDT',risk:1.5,leverage:2,enabled:false,position:null,pending:null}};
 vm.runInContext('latest=snapshot;stateAvailable=true;analysisAvailable=true;leverageInfo={symbol:"XAUUSDT",maximum:20,expires:Date.now()/1000+60}',context);
 node('symbol').value='XAUUSDT';node('risk').value='1.5';node('leverage').value='2';
 context.syncStartControls();assert.equal(node('start').disabled,false);
 assert.match(node('start-status').textContent,/已允许按K线判断/);assert.match(node('start-status').textContent,/不会立即下单/);
 context.renderDecision(context.snapshot);assert.equal(node('decision-source').textContent,'已收盘突破 + 风控检查');
 const news={symbol:'XAUUSDT',updated:now,checked:now,decision:klineFallback(),data:[]};
 context.renderNews(news);assert.match(node('news-analysis').innerHTML,/等待已收盘突破与风控条件/);
 news.decision=klineFallback({kline_allowed:false});context.renderNews(news);
 assert.match(node('news-analysis').innerHTML,/当前未获准开仓/);
 context.newBlocked=klineFallback({asof:now+1,risks:[risk()]});
 vm.runInContext('latestNews={decision:newBlocked}',context);context.syncStartControls();assert.equal(node('start').disabled,true);
 assert.match(node('start-status').textContent,/突发事件冷静期/);
});

test('same-time blocked snapshot takes precedence over a raw fallback permission', () => {
 const {context,node}=harness();
 context.allow=klineFallback();context.block=klineFallback({kline_allowed:false,status:'blocked'});
 vm.runInContext('latestNews={decision:allow};latest.decision={news:block}',context);
 assert.equal(context.newsState(context.currentNews()).ready,false);
});


function backgroundRisk(status='active',overrides={}){
 return risk(status,{entry_gate:'background',blocking:false,gate_reason:'持续背景，不单独限制开仓',
  acute_event_id:null,acute_rule:null,acute_event_at:null,time_basis:null,cooldown_until:null,...overrides});
}

test('background fact states do not independently block either execution path', () => {
 const {context,node}=harness();
 for(const status of ['active','review_required']){
  const row=backgroundRisk(status,{last_seen:now});
  assert.equal(context.newsState({decision:decision({risks:[row]})}).ready,true);
  const value=klineFallback({risks:[row],drivers:[{key:'geopolitics',label:'地缘背景',status:'background',reason:'长期背景不独立禁仓'}]});
  assert.equal(context.newsState({decision:value}).ready,true);
  context.renderPersistentRisks({symbol:'XAUUSDT',decision:value});
  const html=node('news-risks').innerHTML;
  assert.match(html,/持续背景 · 不单独阻止开仓/);
  assert.doesNotMatch(html,/事实状态：已确认解除|突发事件冷静期/);
 }
});

test('gate metadata and the one-hour acute group are required and consistent', () => {
 const {context}=harness();
 const invalid=[
  {gate_version:undefined},{gate_version:true},{entry_gate:'unknown'},{gate_reason:''},
  {blocking:false},{entry_gate:'background'},{acute_event_id:null},{acute_rule:''},
  {acute_event_at:true},{acute_event_at:NaN},{cooldown_until:Infinity},{time_basis:'fetched_at'},
  {cooldown_until:now+3600},{acute_event_at:now+1,cooldown_until:now+3601},
  {entry_gate:'resolved',status:'resolved',blocking:false},
 ];
 for(const override of invalid)assert.equal(context.newsState({decision:klineFallback({risks:[risk('active',override)]})}).ready,false);
 assert.equal(context.newsState({decision:klineFallback({risks:[backgroundRisk('active',{acute_event_id:undefined})]})}).ready,false);
 assert.equal(context.newsState({decision:klineFallback({risks:[backgroundRisk('resolved')]})}).ready,false);
 assert.equal(context.newsState({decision:klineFallback({risks:[risk('resolved')]})}).ready,true);
 const ended=backgroundRisk('active',{acute_event_id:'past',acute_rule:'attack',acute_event_at:now-4000,time_basis:'first_evidence',cooldown_until:now-400});
 assert.equal(context.newsState({decision:klineFallback({risks:[ended]})}).ready,true);
});

test('cooldown displays exact expiry and distinguishes event time from first evidence', () => {
 const {context,node}=harness();
 for(const basis of ['event_time','first_evidence']){
  const row=risk('active',{time_basis:basis});
  const value=klineFallback({risks:[row]});
  assert.equal(context.newsState({decision:value}).ready,false);
  context.renderPersistentRisks({symbol:'XAUUSDT',decision:value});
  const html=node('news-risks').innerHTML;
  assert.match(html,/突发事件冷静期 · 暂缓至/);
  assert.ok(html.includes(context.newsDate(row.cooldown_until)));
  assert.ok(html.includes(basis==='event_time'?'事件时间':'首次新事件证据'));
  assert.match(html,/最近抓取/);assert.doesNotMatch(html,/最近观察/);
 }
});

test('local clock expiry never releases an old cooldown snapshot or marks the fact resolved', () => {
 const {context,node}=harness();
 const row=risk('review_required',{acute_event_at:now-3610,cooldown_until:now-10,last_seen:now-20});
 const old=klineFallback({asof:now-20,expires:now+40,risks:[row]});
 assert.equal(context.validRiskGate(row,old.asof),true);
 assert.equal(context.newsState({decision:old}).ready,false);
 context.renderPersistentRisks({symbol:'XAUUSDT',decision:old});
 assert.match(node('news-risks').innerHTML,/冷静期截止已到 · 等待下一轮核验/);
 assert.match(node('news-risks').innerHTML,/页面不会自行允许开仓/);
 assert.doesNotMatch(node('news-risks').innerHTML,/事实状态：已确认解除/);
 const updated=klineFallback({risks:[{...row,entry_gate:'background',blocking:false,gate_reason:'冷静期届满，背景仍保留'}]});
 assert.equal(context.newsState({decision:updated}).ready,true);
 context.renderPersistentRisks({symbol:'XAUUSDT',decision:updated});
 assert.match(node('news-risks').innerHTML,/持续背景 · 不单独阻止开仓/);
 assert.match(node('news-risks').innerHTML,/事实状态：尚待独立核验/);
});
