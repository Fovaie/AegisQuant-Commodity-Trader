'use strict';
// Inert DOM fixtures only: no model provider, trader service, storage or UI access.
// Run: node --test binance_trader/tests/test_ai_ui.js
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const now=1791100000;
class Clock extends Date{static now(){return now*1000;}}
function harness(){
 const nodes=new Map();
 const node=id=>{
  if(!nodes.has(id))nodes.set(id,{id,value:'',options:[],textContent:'',innerHTML:'',disabled:false,hidden:false,
   dataset:{},style:{},listeners:{},classList:{toggle(){},add(){}},
   addEventListener(event,handler){this.listeners[event]=handler;},append(){},setAttribute(){}});
  return nodes.get(id);
 };
 const storage=new Proxy({}, {get(){throw Error('Persistent credential storage must not be used');},set(){throw Error('Persistent credential storage must not be used');}});
 const context=vm.createContext({document:{getElementById:node,querySelector:()=>({content:'csrf-fixture'}),
  querySelectorAll:()=>[],createElement:()=>node('unused')},
  fetch(url,options){assert.equal(options,undefined);assert.ok(['/api/state','/api/analysis','/api/ai/status'].includes(url));return new Promise(()=>{});},
  setInterval(){},setTimeout(){},clearTimeout(){},Date:Clock,URL,Number,String,Math,JSON,Promise,Object,window:{},
  localStorage:storage,sessionStorage:storage});
 vm.runInContext(source,context);
 vm.runInContext('latest={mode:"paper",context_id:"fixture-context",state:{symbol:"XAUUSDT",enabled:false,risk:1.5,leverage:2}}',context);
 return {context,node,nodes};
}
function analysis(overrides={}){
 return {symbol:'XAUUSDT',status:'ok',trading_authority:false,summary:'金价受到利率预期影响。',direction:'bullish',
  analyzed_at:now,input_asof:now-10,model:'fixture-model',cached:false,
  events:[{article_id:'a1',source:'Fed',url:'https://www.federalreserve.gov/article',published:now-30,
   event_time:'today',event_time_verified:false,classification:'new_event',direction:'bullish',quote:'Public news quote.',impact:'模型认为可能影响黄金需求。'}],
  uncertainties:['仍需确认实际事件时间。'],...overrides};
}
function rendered(nodes){return [...nodes.values()].map(node=>node.textContent+' '+node.innerHTML+' '+node.value).join('\n');}

test('non-held standby is explained without a false analysis failure or pending queue',()=>{
 const {context,node}=harness();
 context.renderAIAnalysis({symbol:'XAUUSDT',status:'standby',trading_authority:false,model:'fixture'});
 assert.match(node('ai-badge').textContent,/轻量监测/);
 assert.match(node('ai-analysis-status').textContent,/暂停常规模型分析/);
 assert.doesNotMatch(node('ai-analysis-status').textContent,/失败|已排队/);
});

test('holding monitor shows event alerts as information and uses reported token counts',()=>{
 const {context,node}=harness();
 const status={enabled:true,usage:{analysis_calls:3,reported_calls:2,prompt_tokens:1200,completion_tokens:80},monitoring:{rows:[
  {symbol:'XAUUSDT',mode:'holding',focus:'XAUUSDT',reason:'持仓优先'},
  {symbol:'BZUSDT',mode:'standby',focus:'XAUUSDT',reason:'轻量监测',alert:{status:'ready',summary:'<script>fixture</script>',
    reason:'仅提示关注，不强制平仓或换仓',events:[{label:'供应事件',url:'javascript:alert(1)'}]}}
 ]}};
 context.renderAIMonitoring(status);
 assert.match(node('ai-monitor-summary').textContent,/优先监测 XAUUSDT/);
 assert.match(node('ai-opportunities').innerHTML,/仅提示关注，不强制平仓/);
 assert.doesNotMatch(node('ai-opportunities').innerHTML,/<script>|javascript:|<button/);
 assert.match(node('ai-token-usage').textContent,/2 次返回用量，输入 1200、输出 80/);
 assert.match(node('ai-token-usage').textContent,/未返回用量/);
 context.renderAIMonitoring({...status,usage:{analysis_calls:1,reported_calls:0,prompt_tokens:0,completion_tokens:0}});
 assert.match(node('ai-token-usage').textContent,/尚未返回可统计/);
 context.renderAIMonitoring({});
 assert.equal(node('ai-opportunities').innerHTML,'');
});

test('native form accepts the local HTTP gateway and preserves its configured address',()=>{
 const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
 const field=html.match(/<input id="ai-base-url"[^>]+>/)[0];
 const pattern=new RegExp('^(?:'+field.match(/pattern="([^"]+)"/)[1]+')$','v');
 assert.ok(pattern.test('http://127.0.0.1:8080/v1'));
 assert.ok(pattern.test('https://api.openai.com/v1'));
 assert.ok(!pattern.test('http://api.openai.com/v1'));
 const {context,node}=harness();
 context.renderAIConnection({configured:true,enabled:true,status:'ready',base_url:'http://127.0.0.1:8080/v1',model:'gpt-6.1-sol'});
 assert.equal(node('ai-base-url').value,'http://127.0.0.1:8080/v1');
 assert.equal(node('ai-api-key').value,'');
});

test('live capital cap is distinct from funded budget and remains visible while disconnected',()=>{
 const {context,node}=harness();
 const data={mode:'live',state:{},capital_limit:100,capital_budget:100,initial_equity:1000,
  equity:1000,loss_limit:15,daily_loss_limit:2,live_connected:true,account_bound:true};
 context.renderAccountMetrics(data);
 assert.match(node('capital-budget').textContent,/策略资金预算 100.00 USDT · 资金上限 100.00 USDT/);
 assert.match(node('daily-limit-description').textContent,/每天亏损达 2.00 USDT/);
 assert.match(node('budget-description').textContent,/累计亏损达 15.00 USDT/);
 context.renderAccountMetrics({...data,capital_budget:.00147528,live_connected:false});
 assert.match(node('capital-budget').textContent,/0.00147528 USDT · 资金上限 100.00 USDT/);
 assert.equal(node('equity').textContent,'未取得');
 context.renderAccountMetrics({...data,account_bound:false,live_connected:false});
 assert.match(node('capital-budget').textContent,/待核验 · 资金上限 100.00 USDT/);
});

function factorNews(){return {symbol:'XAUUSDT',decision:{drivers:[
 {key:'usd_real_yield',label:'美元与实际利率',status:'not_covered',reason:'旧规则尚无结构化数据'},
 {key:'gold_demand',label:'央行与实物黄金需求',status:'not_covered',reason:'旧规则尚无结构化数据'},
 {key:'fed_policy',label:'联储政策',status:'review_required',reason:'规则未确认方向'}]}};}
function factorAnalysis(overrides={}){return analysis({drivers:[
 {key:'usd_real_yield',status:'analyzed',direction:'bullish',summary:'报道中的美元走弱可能支持黄金；未提供实际利率数值。',sources:[{article_id:'a1',url:'https://www.bbc.com/news/fixture',title:'Dollar outlook',source:'BBC',published:now-20}]},
 {key:'gold_demand',status:'no_evidence',direction:'uncertain',summary:'本批文章没有提供央行净购金或实物需求数据。',sources:[]},
 {key:'fed_policy',status:'analyzed',direction:'uncertain',summary:'政策报道仍处于预期阶段，无法确定方向。',sources:[{article_id:'a1',url:'https://www.bbc.com/news/fixture',title:'Rates outlook',source:'BBC',published:now-20}]},
 ],...overrides});}

test('factor cards display model interpretation and provenance instead of legacy coverage labels',()=>{
 const {context,node}=harness();context.news=factorNews();
 vm.runInContext('latestNews=news',context);
 const before=JSON.stringify(context.news);
 context.renderAIAnalysis(factorAnalysis());
 const html=node('news-drivers').innerHTML;
 assert.match(html,/AI 偏多/);assert.match(html,/实际利率数值/);assert.match(html,/Dollar outlook/);
 assert.match(html,/本批输入无相关证据/);assert.match(html,/AI 方向不确定/);
 assert.match(html,/人工核验可选/);assert.doesNotMatch(html,/class="driver-status">未覆盖/);
 assert.equal(JSON.stringify(context.news),before,'Display cannot modify the rule permission');
 node('news-drivers').innerHTML=html.replace('<details class="ai-review">','<details class="ai-review" open>');
 context.renderAIAnalysis(factorAnalysis());
 assert.match(node('news-drivers').innerHTML,/<details class="ai-review" open>/);
});

test('model comparison shows latency and evidence counts without treating them as accuracy',()=>{
 const {context,node}=harness();
 context.renderAIComparison({symbol:'XAUUSDT',status:'complete',rows:[{model:'gpt-6-luna',status:'ok',latency_ms:1234,
  matched_events:1,events:2,factors:4,unverified_factors:1,direction:'uncertain',summary:'<script>bad</script>'}]});
 const html=node('ai-comparison-results').innerHTML;
 assert.match(html,/1.23/);assert.match(html,/原文匹配 1\/2/);assert.match(html,/引用未通过 1/);
 assert.doesNotMatch(html,/<script>|准确率/);
 context.renderAIConnection({configured:true,enabled:true,base_url:'https://api.openai.com/v1',model:'gpt-6-luna'});
 assert.equal(node('ai-compare').disabled,false);
 vm.runInContext('latest.state.enabled=true',context);context.syncAIControls();
 assert.equal(node('ai-model-switch').disabled,true);assert.equal(node('ai-save').disabled,true);
});

test('bad citation is visible without hiding independent factors or rendering unsupported claims',()=>{
 const {context,node}=harness();context.news=factorNews();vm.runInContext('latestNews=news',context);
 const value=factorAnalysis({direction:'uncertain'});
 value.drivers[0]={...value.drivers[0],status:'unverified',direction:'uncertain',summary:'该因素引用未能与正文匹配，模型结论未采纳。'};
 value.events=[{...value.events[0],evidence_valid:false,quote_omitted:true,quote:'FABRICATED QUOTE',impact:'FABRICATED IMPACT',direction:'bullish'}];
 context.renderAIAnalysis(value);
 assert.match(node('news-drivers').innerHTML,/引用校验未通过/);
 assert.match(node('news-drivers').innerHTML,/政策报道仍处于预期阶段/);
 assert.match(node('ai-events').innerHTML,/该事件结论未采纳/);
 assert.doesNotMatch(node('ai-events').innerHTML,/FABRICATED|完整引文已与正文匹配|偏多/);
 assert.equal(node('ai-badge').textContent,'部分引用未通过');
});

test('factor cards do not retain current directions through errors, symbol changes or stale results',()=>{
 const {context,node}=harness();context.news=factorNews();vm.runInContext('latestNews=news',context);
 for(const status of ['pending','analyzing','error','stale','disabled','no_news','unchanged']){
  context.renderAIAnalysis(factorAnalysis());
  context.renderAIAnalysis(factorAnalysis({status}));
  assert.doesNotMatch(node('news-drivers').innerHTML,/AI 偏多|Dollar outlook/);
 }
 context.renderAIAnalysis(factorAnalysis({symbol:'CLUSDT'}));
 assert.doesNotMatch(node('news-drivers').innerHTML,/AI 偏多|Dollar outlook/);
 context.renderAIAnalysis(factorAnalysis({analyzed_at:now-901}));
 assert.match(node('news-drivers').innerHTML,/历史解读 · 非当前方向/);
 assert.doesNotMatch(node('news-drivers').innerHTML,/AI 偏多/);
 context.renderAIAnalysis(factorAnalysis({drivers:[]}));
 assert.match(node('news-drivers').innerHTML,/等待因素解读/);
});

test('factor summaries and source labels escape HTML, and invented links never become clickable',()=>{
 const {context,node}=harness();context.news=factorNews();vm.runInContext('latestNews=news',context);
 const value=factorAnalysis();value.drivers[0].summary='<img src=x onerror=alert(1)>';
 value.drivers[0].sources[0].title='<script>bad</script>';
 value.drivers[2].sources[0].url='javascript:alert(1)';
 context.renderAIAnalysis(value);const html=node('news-drivers').innerHTML;
 assert.doesNotMatch(html,/<img|<script>|href="javascript:/);assert.match(html,/&lt;img/);
});

test('AI disabled, pending, analyzing, stale and error states never retain a current direction',()=>{
 const {context,node}=harness();
 for(const status of ['disabled','pending','analyzing','error','stale','no_news','unchanged']){
  context.renderAIAnalysis(analysis());assert.equal(node('ai-direction').textContent,'偏多');
  context.renderAIAnalysis(analysis({status,reason:'RAW PROVIDER SECRET'}));
  assert.equal(node('ai-direction').textContent,'—');assert.equal(node('ai-events').innerHTML,'');
  assert.notEqual(node('ai-summary').textContent,'金价受到利率预期影响。');
  assert.doesNotMatch(node('ai-analysis-status').textContent,/RAW PROVIDER SECRET/);
 }
 context.renderAIAnalysis(analysis({symbol:'CLUSDT'}));assert.equal(node('ai-direction').textContent,'—');
 context.renderAIAnalysis(analysis({trading_authority:true}));assert.equal(node('ai-direction').textContent,'—');
 for(const analyzed_at of [undefined,null,NaN,Infinity,now+6]){context.renderAIAnalysis(analysis({analyzed_at}));assert.equal(node('ai-direction').textContent,'—');assert.equal(node('ai-events').innerHTML,'');assert.equal(node('ai-badge').textContent,'结果暂不可用');}
 for(const analyzed_at of [now-900,now-901,now-86400]){
  context.renderAIAnalysis(analysis({analyzed_at}));
  assert.equal(node('ai-direction').textContent,'历史解读 · 非当前方向');
  assert.equal(node('ai-badge').textContent,'历史解读');
  assert.equal(node('ai-summary').textContent,'金价受到利率预期影响。');
  assert.match(node('ai-events').innerHTML,/Public news quote/);
  assert.match(node('ai-analysis-status').textContent,/不因时间流逝重调模型/);
 }
});

test('AI quotes and uncertainty text escape markup, and source links require safe HTTPS',()=>{
 const {context,node}=harness();
 const attack='<img src=x onerror=alert(1)>';
 const events=['javascript:alert(1)','data:text/html,x','https://user:secret@example.org/story','https://www.bbc.com/news/fixture'].map(url=>({
  url,quote:attack,impact:attack,source:attack,published:now,event_time:attack,classification:'ongoing_background',direction:'mixed',
 }));
 context.renderAIAnalysis(analysis({events,uncertainties:[attack],summary:attack}));
 assert.equal(node('ai-summary').textContent,attack);
 const html=node('ai-events').innerHTML+node('ai-uncertainties').innerHTML;
 assert.doesNotMatch(html,/<img|href="(?:javascript|data):|user:secret/);
 assert.match(html,/&lt;img/);assert.match(html,/https:\/\/www.bbc.com\/news\/fixture/);
 assert.match(html,/模型提及的事件时间（未独立核验）/);
 // One-second polling must leave an optional review opened by the reader alone.
 node('ai-events').innerHTML=node('ai-events').innerHTML.replace('<details class="ai-review">','<details class="ai-review" open>');
 context.renderAIAnalysis(analysis({events,uncertainties:[attack],summary:attack}));
 assert.match(node('ai-events').innerHTML,/<details class="ai-review" open>/);
});

test('public status polling preserves dirty configuration and never fills an API key',()=>{
 const {context,node,nodes}=harness();
 context.renderAIConnection({configured:false,enabled:false,status:'disabled',base_url:'',model:''});
 assert.equal(node('ai-base-url').value,'http://127.0.0.1:8080/v1');assert.equal(node('ai-model').value,'gpt-6.1-sol');
 assert.equal(node('ai-api-key').value,'');assert.equal(node('ai-test').disabled,true);
 const configured={configured:true,enabled:true,status:'ready',base_url:'https://provider.example/v1',model:'saved-model',api_key:'must-never-render',request_count:1};
 context.renderAIConnection(configured);assert.equal(node('ai-base-url').value,configured.base_url);assert.equal(node('ai-model').value,'saved-model');assert.equal(node('ai-api-key').value,'');
 node('ai-base-url').value='https://edited.example/v1';node('ai-model').value='draft-model';
 node('ai-config').listeners.input();context.renderAIConnection({...configured,model:'older-poll-model'});
 assert.equal(node('ai-base-url').value,'https://edited.example/v1');assert.equal(node('ai-model').value,'draft-model');
 assert.match(node('ai-draft-status').textContent,/未保存/);assert.doesNotMatch(rendered(nodes),/must-never-render/);
 context.renderAIConnection({...configured,status:'error',error:'RAW HTML KEY <html>secret</html>'});
 assert.doesNotMatch(rendered(nodes),/RAW HTML KEY|<html>secret/);
});

test('model labels distinguish the Sol draft from saved models and preserve pending choices',()=>{
 const {context,node}=harness();
 const status={configured:false,enabled:false,status:'disabled',model:''};
 context.renderAIConnection(status);
 assert.equal(node('ai-model').value,'gpt-6.1-sol');
 assert.equal(node('ai-model-choice').value,'gpt-6.1-sol');
 assert.match(node('ai-model-context').textContent,/尚未连接/);
 assert.match(node('ai-draft-status').textContent,/gpt-6\.1-sol.*尚未连接/);
 for(const model of ['gpt-6-luna','gpt-6.1-sol']){
  context.renderAIConnection({...status,configured:true,enabled:true,status:'ready',model});
  assert.equal(node('ai-model').value,model);
  assert.equal(node('ai-model-choice').value,model);
  assert.ok(node('ai-model-context').textContent.includes(model));
  assert.ok(node('ai-draft-status').textContent.includes(model));
 }
 node('ai-model-choice').value='gpt-6-luna';
 context.renderAIConnection({...status,configured:true,enabled:true,status:'ready',model:'gpt-6.1-sol'});
 assert.equal(node('ai-model-choice').value,'gpt-6-luna');
 assert.doesNotMatch(node('ai-model-context').textContent,/luna/i);
 const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
 assert.match(html,/id="ai-model"[^>]*value="gpt-6\.1-sol"/);
 assert.doesNotMatch(html,/Luna 自动解读|已预选 Luna|已预填 Luna/);
});

test('omitted long quote retains interpretation and source without displaying a truncated quotation',()=>{
 const {context,node}=harness();
 const original=analysis();
 const omitted={...original.events[0],quote:'OVERLONG_QUOTE_MUST_NOT_RENDER',quote_omitted:true};
 context.renderAIAnalysis({...original,events:[omitted]});
 assert.equal(node('ai-badge').textContent,'解读已更新');
 assert.equal(node('ai-summary').textContent,original.summary);
 assert.equal(node('ai-direction').textContent,'偏多');
 const html=node('ai-events').innerHTML;
 assert.match(html,/因展示长度限制省略/);
 assert.match(html,/https:\/\/www.federalreserve.gov\/article/);
 assert.match(html,/模型认为可能影响黄金需求/);
 assert.doesNotMatch(html,/OVERLONG_QUOTE_MUST_NOT_RENDER|<blockquote>/);
});

test('saving AI configuration uses explicit context, clears the key and sends no trading data',async()=>{
 const {context,node,nodes}=harness();const calls=[];
 context.refresh=async()=>{};
 context.fetch=async(url,options)=>{calls.push({url,headers:options.headers,body:JSON.parse(options.body)});return {ok:true,json:async()=>({configured:true,enabled:true,status:'configured',base_url:'https://provider.example/v1',model:'fixture-model',api_key:'provider-echo-must-not-render'})};};
 node('ai-base-url').value='https://provider.example/v1';node('ai-model').value='fixture-model';node('ai-api-key').value='only-transient-test-key';
 const pending=node('ai-config').listeners.submit({preventDefault(){}});
 assert.equal(node('ai-api-key').value,'');await pending;
 assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/ai/config');
 assert.equal(calls[0].headers['X-CSRF-Token'],'csrf-fixture');
 assert.deepEqual(Object.keys(calls[0].body).sort(),['api_key','base_url','context_id','context_mode','enabled','model'].sort());
 assert.equal(calls[0].body.api_key,'only-transient-test-key');assert.equal(calls[0].body.context_id,'fixture-context');
 assert.equal(calls[0].body.enabled,true);assert.doesNotMatch(rendered(nodes),/only-transient-test-key|provider-echo-must-not-render/);
 assert.match(node('ai-action-result').textContent,/启停状态不变/);
});

test('AI operation failures do not echo raw provider errors or credentials',async()=>{
 const {context,node,nodes}=harness();context.refresh=async()=>{};
 context.fetch=async()=>({ok:false,json:async()=>({error:'<html>RAW_VENDOR_ERROR SECRET_SK_123</html>'})});
 node('ai-base-url').value='https://provider.example/v1';node('ai-model').value='fixture';node('ai-api-key').value='SECRET_SK_123';
 await node('ai-config').listeners.submit({preventDefault(){}});
 assert.equal(node('ai-api-key').value,'');assert.match(node('ai-action-result').textContent,/AI 请求未完成/);
 assert.doesNotMatch(rendered(nodes),/RAW_VENDOR_ERROR|SECRET_SK_123|<html>/);
});

test('AI results and connection states do not mutate rule-based trade permission or start controls',()=>{
 const {context,node}=harness();
 context.baseline={policy_version:2,entry_allowed:false,kline_allowed:false,status:'insufficient',symbol:'XAUUSDT'};
 vm.runInContext('latestNews={decision:baseline}',context);
 for(const disabled of [true,false]){
  node('start').disabled=disabled;node('start-status').textContent='existing Python gate';
  for(const status of ['ok','error','stale','pending','disabled']){
   context.renderAIAnalysis(analysis({status}));
   if(status==='ok'){
    assert.equal(node('ai-direction').textContent,'偏多');
    assert.match(node('ai-events').innerHTML,/<details class="ai-review"><summary>人工核验（可选）<\/summary>/);
    assert.match(node('ai-analysis-status').textContent,/不核验也会继续自动分析/);
   }
   context.renderAIConnection({configured:true,enabled:true,status:'ready',base_url:'https://provider.example/v1',model:'fixture'});
   assert.equal(node('start').disabled,disabled);assert.equal(node('start-status').textContent,'existing Python gate');
   assert.equal(vm.runInContext('latestNews.decision===baseline && latest.state.enabled===false',context),true);
  }
 }
});

test('test and disconnect submit only their asynchronous AI endpoints and clear displayed results',async()=>{
 const {context,node,nodes}=harness();const calls=[];context.refresh=async()=>{};
 context.fetch=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>url.endsWith('/test')?
  {configured:true,enabled:true,status:'configured',test_pending:true,base_url:'https://provider.example/v1',model:'fixture'}:
  {configured:false,enabled:false,status:'disabled',base_url:'',model:''}};};
 context.renderAIConnection({configured:true,enabled:true,status:'ready'});
 await node('ai-test').onclick();assert.equal(calls[0].url,'/api/ai/test');assert.equal(calls[0].body.context_id,'fixture-context');
 assert.equal(node('ai-test').disabled,true);assert.match(node('ai-connection-status').textContent,/请求已排队/);
 context.renderAIAnalysis(analysis());node('ai-api-key').value='unsubmitted-secret';
 await node('ai-disconnect').onclick();assert.equal(calls[1].url,'/api/ai/disconnect');
 assert.equal(node('ai-api-key').value,'');assert.equal(node('ai-direction').textContent,'—');assert.equal(node('ai-events').innerHTML,'');
 assert.doesNotMatch(rendered(nodes),/unsubmitted-secret/);assert.equal(calls.length,2);
 assert.equal(vm.runInContext('latest.state.enabled',context),false);
});

test('underlying data stays visible without AI and distinguishes missing, stale and partial coverage',()=>{
 const {context,node}=harness();context.news=factorNews();
 const metrics=[{id:'real_yield_10y',driver:'usd_real_yield',label:'10年期TIPS实际收益率',value:2.92,unit:'%',change:4,change_unit:'基点',previous_value:2.88,previous_date:'2026-10-01',observation_date:'2026-10-02',frequency:'daily',status:'ok',url:'https://www.federalreserve.gov/releases/h15/',source:'Fed H.15',fetched_at:now,last_checked:now},
 {id:'broad_usd',driver:'usd_real_yield',label:'广义美元指数',status:'unavailable',frequency:'daily'}];
 context.fixture={symbol:'XAUUSDT',metrics};vm.runInContext('latestFundamentals=fixture;latestNews=news',context);
 context.renderAIAnalysis(analysis({status:'disabled'}));let html=node('news-drivers').innerHTML;
 assert.match(html,/结构化数据：部分可用（1\/2）/);assert.match(html,/2\.9200 %/);assert.match(html,/\+4\.0000 基点/);
 assert.match(html,/统计期 2026-10-02/);assert.match(html,/广义美元指数：暂无有效数值/);
 assert.match(html,/结构化数据：未接入/);assert.match(html,/未接入实时 ICE DXY/);assert.match(html,/AI 未启用/);
 metrics[0].status='stale';context.renderNewsDrivers(context.news);html=node('news-drivers').innerHTML;
 assert.match(html,/旧值，仅供历史参考/);assert.match(html,/结构化数据：暂不可用（0\/2）/);
 context.fixture.symbol='XAGUSDT';context.renderNewsDrivers(context.news);
 assert.doesNotMatch(node('news-drivers').innerHTML,/2\.9200/);
});

test('statistics and model metric provenance allow only trusted HTTPS and escape labels',()=>{
 const {context}=harness();
 const html=context.renderFactorData({metrics:[{driver:'gold_demand',status:'ok',label:'<script>bad</script>',value:288.9,unit:'吨',observation_date:'2026-06-30',frequency:'quarterly',url:'javascript:alert(1)',source:'<img>'}]},'gold_demand');
 assert.doesNotMatch(html,/<script>|<img>|href="javascript:/);assert.match(html,/&lt;script/);assert.match(html,/季度/);assert.match(html,/统计期 2026-06-30/);
 for(const url of ['https://www.gold.org/goldhub/research/gold-demand-trends','https://www.safe.gov.cn/safe/gfcbzc/'])assert.ok(context.dataSourceURL(url));
 for(const url of ['https://user:pass@www.gold.org/x','https://www.gold.org:8443/x','https://evil.invalid/x'])assert.equal(context.dataSourceURL(url),null);
});

test('numeric factor cards render before the first news cycle when the catalog is available',()=>{
 const {context,node}=harness();context.fixture={symbol:'XAUUSDT',factors:[{key:'usd_real_yield',label:'美元与实际利率'}],metrics:[{driver:'usd_real_yield',label:'实际收益率',value:2.92,unit:'%',status:'ok',frequency:'daily',observation_date:'2026-10-02'}]};
 vm.runInContext('latestFundamentals=fixture',context);context.renderNewsDrivers({symbol:'XAUUSDT',decision:{drivers:[]}});
 assert.match(node('news-drivers').innerHTML,/2\.9200 %/);assert.match(node('news-drivers').innerHTML,/结构化数据：已接入（1\/1）/);
});

test('oil, gas and silver data cards show units, periods and remaining data gaps',()=>{
 const {context}=harness();
 const cases=[['inventory','美国商业原油库存',427.32,'百万桶','weekly','2026-09-25','市场一致预期尚未接入'],['oil_supply','美国原油产量',13955,'千桶/日','weekly','2026-09-25','未接入OPEC'],['gas_weather','人口加权供暖度日',17,'°F·日','weekly','2026-10-04','天气预报及预测修订尚未接入'],['gas_inventory','工作气库存',3415,'Bcf','weekly','2026-09-25','市场一致预期尚未接入'],['us_gas_supply','干气日均产量',113.7,'Bcf/日','monthly','2026-07-31','发布滞后'],['us_lng','LNG出口',16.7,'Bcf/日','monthly','2026-07-31','不是实时'],['global_lng','TTF周均价格',24.18,'USD/MMBtu','weekly','2026-09-30','不是Henry Hub'],['silver_industry','工业用银',657.4,'百万盎司','annual','2025-12-31','F预测列'],['silver_supply','矿产白银',846.6,'百万盎司','annual','2025-12-31','年度']];
 for(const [driver,label,value,unit,frequency,date,gap] of cases){
  const html=context.renderFactorData({metrics:[{driver,label,value,unit,frequency,observation_date:date,status:'ok',previous_value:value-1,change:1,change_unit:unit}]},driver);
  assert.match(html,/结构化数据：已接入（1\/1）/);assert.ok(html.includes(label));assert.ok(html.includes(date));assert.ok(html.includes(unit));assert.ok(html.includes(gap),driver);
 }
});

test('geopolitical event records never imply verified facts or live numeric shipping coverage',()=>{
 const {context}=harness();const event={key:'shipping',status:'connected',total:1,records:[{subject:'strait_of_hormuz',category:'shipping_disruption',status:'review_required',source:'BBC',url:'https://www.bbc.com/news/test',published:now}]};
 let html=context.renderFactorEvents({events:[event]},'shipping');assert.match(html,/事件数据：已接入/);assert.match(html,/事实尚待独立核验/);assert.match(html,/霍尔木兹海峡/);
 assert.match(context.renderFactorData({metrics:[]},'shipping'),/尚无实时船舶\/AIS/);
 event.status='unavailable';html=context.renderFactorEvents({events:[event]},'shipping');assert.match(html,/来源暂不可用/);assert.match(html,/历史记录/);
 event.records[0].url='javascript:alert(1)';assert.doesNotMatch(context.renderFactorEvents({events:[event]},'shipping'),/href="javascript:/);
});

test('all five analysis snapshots stay isolated when symbol changes',()=>{
 const {context,node}=harness();
 for(const symbol of ['XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','NATGASUSDT']){
  context.fixture={symbol,metrics:[{driver:'fixture',label:symbol+' unique value',value:42,unit:'unit',status:'ok'}],factors:[{key:'fixture',label:symbol}]};
  vm.runInContext('latestFundamentals=fixture',context);context.renderNewsDrivers({symbol,decision:{drivers:[]}});
  assert.ok(node('news-drivers').innerHTML.includes(symbol+' unique value'));
  context.renderNewsDrivers({symbol:symbol==='BZUSDT'?'CLUSDT':'BZUSDT',decision:{drivers:[{key:'fixture',label:'another'}]}});
  assert.ok(!node('news-drivers').innerHTML.includes(symbol+' unique value'));
 }
});

test('price curves render finite SVG coordinates at all five commodity price scales',()=>{
 const {context,node}=harness();
 for(const base of [4145,61,89,100,3.08]){
  const rows=Array.from({length:80},(_,i)=>({time:i*3600000,close:base*(1+Math.sin(i)/100)}));
  context.chart(rows);const svg=node('chart').innerHTML;
  assert.match(svg,/<svg/);assert.match(svg,/<polyline points=/);assert.doesNotMatch(svg,/NaN|Infinity|undefined/);
  const points=svg.match(/<polyline points="([^"]+)"/)[1].split(' ');assert.equal(points.length,80);
  for(const point of points){const [x,y]=point.split(',').map(Number);assert.ok(x>=24&&x<=676);assert.ok(y>=24&&y<=211);}
 }
});

test('selection table exposes rejection reasons and invalidates old rankings',()=>{
 const {context,node}=harness();
 const data={state:{selection_mode:'auto',enabled:false},selection:{updated:now,selected:'BZUSDT',rows:[
  {symbol:'BZUSDT',side:'SELL',status:'eligible',rank:1,net_reward_risk:1.6,reason:'费用及风控通过'},
  {symbol:'XAUUSDT',status:'excluded',reason:'<script>盘口过期</script>'}]}};
 context.renderSelection(data);
 assert.match(node('selection-status').textContent,/已暂停.*BZUSDT/);
 assert.match(node('selection-rows').innerHTML,/做空.*第 1 名.*1.60/);
 assert.match(node('selection-rows').innerHTML,/盘口过期/);
 assert.doesNotMatch(node('selection-rows').innerHTML,/<script>/);
 data.selection.updated=now-21;context.renderSelection(data);
 assert.doesNotMatch(node('selection-rows').innerHTML,/第 1 名|1.60/);
 data.selection.updated=now;data.selection.selected=null;data.selection.locked=true;context.renderSelection(data);
 assert.match(node('selection-status').textContent,/账户风控锁定/);
 data.selection.reason='当日亏损阈值已触发';context.renderSelection(data);
 assert.match(node('selection-status').textContent,/当日亏损阈值已触发/);
 assert.doesNotMatch(node('selection-status').textContent,/持仓或订单核对中/);
 data.state.selection_mode='manual';context.renderSelection(data);
 assert.match(node('selection-status').textContent,/仅观察/);
});

test('account lock is visible in both risk directions without stale green plans',()=>{
 const {context,node}=harness();
 context.renderRiskPreview({risk_preview:{ok:false,updated:now,reason:'当日亏损阈值已触发',long:null,short:null}});
 assert.equal(node('risk-badge').textContent,'未通过');
 assert.equal((node('risk-preview').innerHTML.match(/当日亏损阈值已触发/g)||[]).length,2);
});

test('paused held position cannot enable a new-entry start in either mode',()=>{
 const {context,node}=harness();
 vm.runInContext('stateAvailable=true;latest.state.position={id:"held"};latest.state.enabled=false',context);
 for(const mode of ['auto','manual']){
  vm.runInContext(`latest.state.selection_mode=${JSON.stringify(mode)}`,context);
  context.syncStartControls();assert.equal(node('start').disabled,true);
  assert.match(node('start-status').textContent,/已有持仓，继续保护/);
 }
});

test('auto mode starts observation with no candidate while manual still needs symbol news permission',()=>{
 const {context,node}=harness();
 node('symbol').value='XAUUSDT';node('risk').value='1.5';node('leverage').value='2';node('selection-mode').value='auto';
 vm.runInContext(`latest={mode:'paper',updated:${now},state:{symbol:'XAUUSDT',selection_mode:'auto',enabled:false,risk:1.5,leverage:2},selection:{updated:${now},selected:null}};stateAvailable=true;analysisAvailable=true;leverageInfo={symbol:'XAUUSDT',expires:${now+30},maximum:3};latestNews=null`,context);
 context.syncStartControls();assert.equal(node('start').disabled,false);
 assert.match(node('start-status').textContent,/无合格候选保持空仓/);
 vm.runInContext("latest.state.selection_mode='manual'",context);node('selection-mode').value='manual';
 context.syncStartControls();assert.equal(node('start').disabled,true);
 node('selection-mode').value='auto';assert.equal(context.settingsDirty(),true);
});

test('leverage metadata never silently reduces the saved choice and explicit lower choices remain usable',async()=>{
 const {context,node}=harness();node('symbol').value='XAUUSDT';node('leverage').value='15';
 vm.runInContext('latest.state.leverage=15',context);
 context.refresh=async()=>{};
 context.fetch=async(url,options)=>{
  assert.equal(url,'/api/leverage');assert.equal(options.method,'POST');
  return {ok:true,json:async()=>({symbol:'XAUUSDT',maximum:10,source:'Test',updated:now,expires:now+60})};
 };
 await context.loadLeverage();
 assert.equal(Number(node('leverage').value),15);assert.equal(Number(node('leverage').max),15);
 assert.equal(node('save').disabled,true);assert.equal(node('apply-leverage').disabled,true);
 assert.match(node('leverage-status').textContent,/不自动降低/);
 node('leverage').value='5';node('leverage').listeners.input();
 assert.equal(Number(node('leverage').value),5);assert.equal(Number(node('leverage').max),10);
 assert.equal(node('save').disabled,false);assert.match(node('leverage-status').textContent,/选择的 5×/);
 assert.doesNotMatch(node('leverage-status').textContent,/超过本品种/);
});


test('period changes are unsaved settings and occupied accounts lock the selector',()=>{
 const {context,node}=harness();
 node('symbol').value='XAUUSDT';node('risk').value=1.5;node('leverage').value=2;node('selection-mode').value='manual';node('interval').value='1h';
 assert.equal(context.settingsDirty(),false);
 node('interval').value='1m';assert.equal(context.settingsDirty(),true);
 vm.runInContext('latest.state.position={id:"fixture"}',context);context.syncLeverageControls();assert.equal(node('interval').disabled,true);
});
test('connection test time is separate from complete analysis and queue wait',()=>{
 const {context,node}=harness();
 context.renderAIConnection({configured:true,enabled:true,status:'ready',model:'gpt-6.1-sol',base_url:'http://127.0.0.1:8080/v1',last_test_latency_ms:100,last_analysis_latency_ms:32000,active_count:2});
 assert.match(node('ai-connection-status').textContent,/连接测试 0.10秒/);assert.match(node('ai-connection-status').textContent,/完整分析 32.00秒/);
 context.renderAIAnalysis(analysis({latency_ms:32000,queue_wait_ms:1200}));
 assert.match(node('ai-analysis-time').textContent,/完整分析 32.00秒.*排队 1.20秒/);
});
test('minute chart badge preserves m instead of mislabeling it as monthly M',async()=>{
 const {context,node}=harness();vm.runInContext('analysisBusy=false',context);
 context.fetch=async()=>({ok:true,json:async()=>({kline:{symbol:'XAUUSDT',interval:'1m',updated:now},news:{symbol:'XAUUSDT',checked:now,updated:now,coverage:{fetch_duration_ms:1250,newest_publication_age_seconds:120}},ai:{status:'disabled'}})});
 await context.refreshAnalysis();assert.equal(node('chart-interval').textContent,'1m');assert.match(node('chart-period').textContent,/1m/);
 assert.match(node('news-status').textContent,/抓取 1.25秒/);assert.match(node('news-status').textContent,/非事件延迟/);
});

test('symbol failures remain visible beside a healthy connection and retry requires new backend',()=>{
 const {context,node}=harness();context.renderAIAnalysis(analysis({status:'error',reason:'429 provider private text'}));
 assert.doesNotMatch(node('ai-analysis-status').textContent,/provider private text/);
 context.renderAIConnection({configured:true,enabled:true,status:'ready',analysis_health:{ok:1,error:4}});
 assert.match(node('ai-connection-status').textContent,/成功 1、失败 4/);assert.equal(node('ai-retry').disabled,true);
 context.renderAIConnection({configured:true,enabled:true,status:'ready',retry_supported:true,analysis_health:{ok:1,error:4}});
 assert.equal(node('ai-retry').disabled,false);
});

test('rotation controls require new backend and a recorded original identity',()=>{
 const {context,node}=harness();const data={mode:'live',live_connected:true,state:{enabled:false,position:null,pending:null}};
 context.renderEnvironment(data);assert.equal(node('rotate-key').disabled,true);assert.equal(node('bind-account-identity').disabled,true);
 context.renderEnvironment({...data,credential_rotation_supported:true});assert.equal(node('bind-account-identity').disabled,false);assert.equal(node('rotate-key').disabled,true);
 context.renderEnvironment({...data,credential_rotation_supported:true,account_identity_bound:true});assert.equal(node('rotate-key').disabled,false);
 context.renderEnvironment({...data,credential_rotation_supported:true,account_identity_bound:true,state:{...data.state,pending:{id:'unknown'}}});assert.equal(node('rotate-key').disabled,true);
});
test('rotation form requires confirmation and sends only the dedicated route',async()=>{
 const {context,node}=harness();vm.runInContext('latest.mode="live";latest.credential_rotation_supported=true;latest.account_identity_bound=true',context);
 node('rotate-key').checked=true;node('api-key').value='fixture-new-key';node('api-secret').value='fixture-new-secret';
 const calls=[];context.post=async(path,body)=>{calls.push({path,body:{...body}});return {rotated:true,balance:300,available:300,positions:0};};context.loadLeverage=()=>{};
 context.askConfirmation=async()=>false;await node('api-credentials').listeners.submit({preventDefault(){}});assert.equal(calls.length,0);
 context.askConfirmation=async()=>true;await node('api-credentials').listeners.submit({preventDefault(){}});
 assert.equal(calls.length,1);assert.equal(calls[0].path,'credentials/rotate');assert.equal(calls[0].body.confirm_rotation,true);assert.equal(calls[0].body.mode,'live');
 assert.equal(node('api-key').value,'');assert.equal(node('api-secret').value,'');assert.match(node('account-result').textContent,/原账本保留/);
});

test('unchanged analysis retains direction only with fresh input verification',()=>{
 const {context,node}=harness();
 context.renderAIAnalysis(analysis({analyzed_at:now-86400,retained_input_checked_at:now}));
 assert.equal(node('ai-direction').textContent,'偏多');
 context.renderAIAnalysis(analysis({analyzed_at:now-86400,retained_input_checked_at:now-91}));
 assert.equal(node('ai-direction').textContent,'历史解读 · 非当前方向');
});
