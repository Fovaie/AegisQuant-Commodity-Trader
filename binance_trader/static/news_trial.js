'use strict';
const el=id=>document.getElementById(id);
const labels={waiting_key:'等待填写模型密钥',ready:'已填写，准备试验',running:'试验进行中',complete:'本轮采样结束，请查看各项结果',error:'本轮未完成'};
const dirs={bullish:'偏多',bearish:'偏空',mixed:'混合',uncertain:'不确定'};
const states={ok:'完整通过',partial:'部分通过，失败项交Sol复核',error:'失败',skipped:'未调用',cached:'相同输入不重复调用'};
function cell(tr,text){const td=document.createElement('td');td.textContent=text;tr.appendChild(td);}
function arm(value){if(value.status==='cached')return `未重复请求；上次${states[value.prior_status]||'未完成'}（${((value.prior_latency_ms||0)/1000).toFixed(2)}s）`;if(value.status==='skipped')return '未调用';return `${states[value.status]||value.status} · ${(value.latency_ms/1000).toFixed(2)}s${value.direction?' · '+dirs[value.direction]:''}${value.error?' · '+value.error:''}${value.route==='original_text_fallback'?' · 初筛失败后原文复核':value.route==='original_text_routing_review'?' · 品种关联分歧，原文复核':''}${value.status==='ok'&&!value.input_fresh_at_completion?' · 快照已过期，仅供对照':''}`;}
let latestReport=null,busy=false,submitting=false;
function rows(report){el('results').replaceChildren();for(const r of report.rows||[]){const tr=document.createElement('tr');cell(tr,r.symbol);cell(tr,arm(r.direct));cell(tr,arm(r.cascade)+(report.luna?.latency_ms?`（共享 Luna ${(report.luna.latency_ms/1000).toFixed(2)}s）`:''));cell(tr,r.direction_agreement===null?'无法比较':r.direction_agreement?'一致':'不一致');cell(tr,r.review_reasons.join('；')||'无重要变化');el('results').appendChild(tr);}}
const issueNames={ROW_FIELDS:'字段不完整',UNKNOWN_ARTICLE_ID:'文章ID不匹配',DUPLICATE_ARTICLE_ID:'重复文章',MISSING_ARTICLE:'遗漏文章',INVALID_KIND:'事件分类无效',INVALID_SYMBOLS:'品种代码无效',SYMBOL_KIND_CONFLICT:'品种与分类矛盾',INVALID_FACT:'事实字段无效',UNKNOWN_QUOTE_ID:'原文片段ID不匹配',INVALID_QUOTE:'引文无效',QUOTE_BUDGET:'引文超出长度限制',QUOTE_NOT_IN_BODY:'引文与正文不匹配',EVENT_TIME_FORMAT:'事件时间格式无效',EVENT_TIME_FUTURE:'事件时间在未来',EVENT_AFTER_PUBLICATION:'事件时间晚于所给发布时间',ROW_LIMIT:'返回条数过多'};
function render(s){
 busy=s.status==='running';
 el('status').textContent=(labels[s.status]||s.status)+(s.error?'：'+s.error:'');
 el('diagnose').disabled=busy||submitting||!s.configured;el('connect').disabled=busy||submitting;el('run').disabled=busy||submitting||!s.configured;
 el('progress').textContent=s.progress?`${s.progress.symbol||''} ${s.progress.message||s.progress.stage}`:busy?'处理中':s.status==='complete'?(s.report?.kind==='triage'?'Luna初筛检查结束':'五品种采样结束'):'尚未开始';
 const live=busy&&(s.partial_luna||(s.partial_rows||[]).length);
 const shown=live?{luna:s.partial_luna||{status:'skipped',latency_ms:0},rows:s.partial_rows||[],note:'本轮进行中，显示已完成部分'}:s.report;
 if(busy&&s.report&&!live)el('progress').textContent+='（下表为上一轮结果）';
 if(shown){
  latestReport=shown;rows(shown);const luna=shown.luna||{};
  el('luna').textContent=`Luna：${luna.status==='skipped'?'本轮无新增，复用已有初筛；未重复请求':states[luna.status]||'等待结果'}${luna.status!=='skipped'&&Number.isFinite(luna.latency_ms)?' · '+(luna.latency_ms/1000).toFixed(2)+'s':''}；${shown.new_articles===undefined?'':`新增或修订 ${shown.new_articles} 条，去重后共 ${shown.unique_articles} 条。`}${luna.error||''}`;
  el('issues').replaceChildren();
  for(const issue of luna.validation_issues||[]){const li=document.createElement('li');li.textContent=`${issue.article_id||'整批'} · ${issue.field}：${issueNames[issue.code]||issue.code}${issue.words!==undefined?'（'+issue.words+'词 / '+issue.characters+'字符）':''}`;el('issues').appendChild(li);}
  el('note').textContent=shown.note||'';el('report').hidden=!s.report;
 }
}

async function post(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(data)});const s=await r.json();if(!r.ok)throw Error(s.error||'请求失败');render(s);return s;}
el('config').addEventListener('submit',async e=>{e.preventDefault();if(submitting||busy)return;submitting=true;el('connect').disabled=true;let key=el('key').value;el('key').value='';try{await post('/api/config',{api_key:key});key='';await post('/api/diagnose',{});}catch(err){el('status').textContent=err.message;}finally{key='';submitting=false;el('connect').disabled=busy;}});
el('run').addEventListener('click',async()=>{if(submitting||busy)return;submitting=true;el('run').disabled=true;try{await post('/api/start',{});}catch(err){el('status').textContent=err.message;}finally{submitting=false;}});
el('diagnose').addEventListener('click',async()=>{if(submitting||busy)return;submitting=true;el('diagnose').disabled=true;try{await post('/api/diagnose',{});}catch(err){el('status').textContent=err.message;}finally{submitting=false;}});
async function poll(){try{const r=await fetch('/api/status');if(r.ok)render(await r.json());}catch{el('status').textContent='试验服务暂不可用';}finally{setTimeout(poll,1500);}}
poll();
