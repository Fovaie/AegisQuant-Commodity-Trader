const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="csrf"]').content;
let leverageInfo=null, leverageSequence=0, leverageConnection=null, uiGeneration=0, modeSwitching=false;
let stateAvailable=false, analysisAvailable=false;
let pendingReview=null, pendingReviewContext=null;
let initialized = false, busy = false, toastTimer, latest, latestNews=null, stateRequest=null, lastError='', pauseRequest=null, startSettled=null;
const fmt = (n, d = 2) => Number(n).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(text) { $('toast').textContent=text; $('toast').style.display='block'; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').style.display='none',6000); }
function requestContext(){return {context_mode:latest?.mode,context_id:latest?.context_id};}
function contextMatches(context){return !!latest&&context.context_mode===latest.mode&&context.context_id===latest.context_id;}
async function post(path, body={}) {
  if(path==='pause')return requestPause(body);
  if(pauseRequest){toast('正在等待暂停确认，请稍后操作');return null;}
  if (busy) {toast('上一项操作尚未完成，请稍后重试；暂停仍可提交');return null;}
  const request={...requestContext(),...body};
  if(!contextMatches(request)){lastError='交易环境或账户连接已变化，请重新查看后操作';toast(lastError);return null;}
  busy=true;lastError='';
  let finishStart;
  if(path==='start')startSettled=new Promise(resolve=>{finishStart=resolve;});
  try {
    const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(request)});
    const data=await r.json(); if(!r.ok) throw Error(data.error||'操作失败');
    if(finishStart){finishStart();startSettled=null;}
    if(path==='mode'){resetEnvironment();latest=null;}
    await refresh(true); return data;
  } catch(e){lastError=path.startsWith('ai/')?'AI 请求未完成，请检查模型地址、模型名称与密钥后重试。':e.message;toast(lastError); return null;} finally{if(finishStart){finishStart();startSettled=null;}busy=false;}
}
function syncPauseControl(){
 $('pause').disabled=!!pauseRequest||!latest?.state.enabled;
 $('pause').textContent=pauseRequest?'正在暂停…':'暂停新开仓';
}
function requestPause(body={}){
 if(pauseRequest)return pauseRequest;
 const request={...requestContext(),...body};
 if(!contextMatches(request)){lastError='交易环境或账户连接已变化，请重新查看后操作';toast(lastError);return Promise.resolve(null);}
 const pendingStart=startSettled;
 toast('正在请求暂停，等待服务确认；已有持仓保护继续运行');
 pauseRequest=(async()=>{
  try{
   // A previously submitted start must finish first, or it could arrive at
   // the server after this pause and turn new entries back on.
   if(pendingStart)await pendingStart;
   const r=await fetch('/api/pause',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(request)});
   const data=await r.json();if(!r.ok)throw Error(data.error||'暂停失败');
   if(contextMatches(request)){
    // Invalidate state reads begun before the pause acknowledgement.
    uiGeneration++;latest.state.enabled=false;$('running').textContent='已暂停';
   }
   // A slow state read must not withhold the server's pause acknowledgement.
   refresh(true).catch(()=>{});return data;
  }catch(e){lastError='暂停尚未确认：'+e.message+'。请核对状态后重试';toast(lastError);return null;}
  finally{pauseRequest=null;syncPauseControl();syncStartControls();}
 })();
 syncPauseControl();syncStartControls();return pauseRequest;
}
function resetEnvironment(){
 pendingReview=null;pendingReviewContext=null;
 uiGeneration++;leverageSequence++;initialized=false;stateAvailable=false;analysisAvailable=false;leverageInfo=null;leverageConnection=null;latestNews=null;
 $('api-key').value='';$('api-secret').value='';$('ai-api-key').value='';$('account-result').textContent='';$('leverage-applied').textContent='';
 $('chart').textContent='正在载入当前环境K线';$('news-analysis').dataset.version='';$('news-evidence').dataset.version='';
 if($('confirm-dialog').open)$('confirm-dialog').close('cancel');
}
function chart(rows) {
  if(rows.length<2) return;
  const w=700,h=235,pad=24,values=rows.map(r=>r.close),min=Math.min(...values),max=Math.max(...values),spread=max-min||max*.01;
  const x=i=>pad+i*(w-2*pad)/(rows.length-1),y=v=>h-pad-(v-min)*(h-2*pad)/spread;
  const points=values.map((v,i)=>`${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
  const lines=[0,.5,1].map(t=>{const yy=pad+t*(h-2*pad);return `<line x1="${pad}" y1="${yy}" x2="${w-pad}" y2="${yy}" stroke="#e9eee4" stroke-dasharray="3 5"/><text x="${w-pad}" y="${yy+13}" text-anchor="end" fill="#879581" font-size="10">${fmt(max-t*spread,6)}</text>`;}).join('');
  $('chart').innerHTML=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="最近80根K线收盘价曲线（含当前未收盘K线）"><defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#adc8a1" stop-opacity=".35"/><stop offset="100%" stop-color="#adc8a1" stop-opacity="0"/></linearGradient></defs>${lines}<polygon points="${pad},${h-pad} ${points} ${w-pad},${h-pad}" fill="url(#fill)"/><polyline points="${points}" fill="none" stroke="#517d54" stroke-width="2"/><circle cx="${x(values.length-1)}" cy="${y(values.at(-1))}" r="4" fill="#345e3d"/></svg>`;
}
function refresh(force=false){
 if(stateRequest)return force?stateRequest.then(()=>refresh()):stateRequest;
 stateRequest=refreshState().finally(()=>{stateRequest=null;});return stateRequest;
}
async function refreshState(){
 const generation=uiGeneration;
 try{
  const r=await fetch('/api/state'); if(!r.ok)throw Error('服务响应异常'); const d=await r.json();
  if(generation!==uiGeneration)return;
  if(latest&&(d.mode!==latest.mode||d.context_id!==latest.context_id))resetEnvironment();
  const previousSymbol=latest?.state.symbol,wasDirty=initialized&&settingsDirty();
  latest=d;stateAvailable=true; const s=d.state,q=d.quote;
  renderPendingRecovery();
  renderEnvironment(d);
  renderAccountMetrics(d);
  $('running').textContent=s.halted||s.pending?'核对锁定':s.total_latch||s.daily_latch?'风控暂停':s.enabled?'自动运行':'已暂停';
  const stale=!d.updated||Date.now()/1000-d.updated>20;
  $('freshness').textContent=d.updated?`更新 ${new Date(d.updated*1000).toLocaleTimeString()}${stale?' · 已过期':''}`:'等待首次行情';
  const warning=s.halted||d.error||(stale?'行情未就绪，禁止开仓':s.total_latch?'累计亏损阈值已触发':s.daily_latch?'今日亏损阈值已触发，暂停至UTC次日':'');
  $('notice').textContent=warning||(d.mode==='paper'?'你正在使用模拟资金。自动交易只在点击启动后运行，当前策略未经盈利验证。':d.mode==='live'?'当前为真实资金环境。连接账户后仍保持暂停；确认启动后，策略会自动发送实盘开仓、保护及平仓订单。':'你正在使用币安Demo。请勿在同一测试账户手动交易或划转，避免干扰账本。');
  $('notice').classList.toggle('error',!!warning);
  $('signal').textContent=d.signal.reason;
  $('signal-badge').textContent=d.signal.side==='BUY'?'做多信号':d.signal.side==='SELL'?'做空信号':'等待机会';
  if(!initialized){$('interval').value=s.interval||'1h';$('selection-mode').value=s.selection_mode||'manual';$('symbol').value=s.symbol;$('risk').value=s.risk;$('leverage').max=Math.max(2,s.leverage);$('leverage').value=s.leverage;initialized=true;}
  else if(previousSymbol!==s.symbol&&(s.enabled||!wasDirty)){$('symbol').value=s.symbol;leverageInfo=null;loadLeverage();}
  const locked=s.enabled||!!s.position||!!s.pending;
  ['symbol','risk','selection-mode'].forEach(id=>$(id).disabled=locked);
  syncStartControls();
  syncPauseControl(); $('close').disabled=!s.position||!!s.pending;
  $('pair-label').textContent=s.symbol; $('chart-title').textContent=s.symbol.replace('USDT',' / USDT');
  const connectionKey=JSON.stringify([d.mode,d.context_id,d.readonly_connected,d.live_connected,d.credentials]);
  if(leverageConnection!==connectionKey){leverageConnection=connectionKey;leverageInfo=null;loadLeverage();}
  syncLeverageControls();
  updateDraft();
  renderDecision(d);
  renderSelection(d);
  renderRiskPreview(d);
  if(!q){$('price').textContent='—';$('change').textContent='等待当前品种行情';$('range').textContent='—';$('funding').textContent='—';}
  if(q){$('price').textContent=fmt(q.price,q.price<10?3:2);$('change').textContent=`24h ${q.change>0?'+':''}${fmt(q.change)}%`;$('change').className=q.change>=0?'up':'down';$('range').textContent=`24h ${fmt(q.low,6)} — ${fmt(q.high,6)}`;$('funding').textContent=`最近资金费率 ${fmt(q.funding*100,4)}%`;}
  if(s.position){renderPosition(s.position);}
  else{$('position').className='empty';$('position').textContent='暂无持仓。等待符合规则的机会。';}
  $('events').className=s.events.length?'':'empty';
  $('events').innerHTML=s.events.length?s.events.slice(0,30).map(e=>`<div class="event"><time>${new Date(e.time).toLocaleString()}</time><span>${esc(e.message)}</span></div>`).join(''):'尚无交易记录';
  // The independent one-second analysis feed owns the chart.
  if($('products-dialog').open)updateProducts();
 }catch(e){if(generation!==uiGeneration)return;stateAvailable=false;syncStartControls();$('execution-mode').disabled=true;$('apply-leverage').disabled=true;$('notice').textContent='本机服务未连接。请启动交易器；不要将旧报价当作实时行情。';$('notice').classList.add('error');$('start').disabled=true;$('decision-reason').textContent='本机服务断开，当前决策不可用';setBadge('decision-badge','服务断开','blocked');$('risk-reason').textContent='服务断开，预检结果不可用';$('risk-preview').textContent='';setBadge('risk-badge','不可用','blocked');}
}
$('settings').addEventListener('submit',async e=>{e.preventDefault();const d=await post('config',{interval:$('interval').value||'1h',symbol:$('symbol').value,risk:$('risk').value,leverage:$('leverage').value,selection_mode:$('selection-mode').value||'manual'});if(d)toast('设置已保存；启动状态不变');});
$('start').onclick=async()=>{
 const context=requestContext(),s=latest?.state;if(!s)return;
 const request={...context,expected_interval:s.interval||'1h',expected_symbol:s.symbol,expected_leverage:s.leverage,expected_risk:s.risk,expected_selection_mode:s.selection_mode||'manual'};
 if(context.context_mode==='live'){
  if(!await askConfirmation('确认启动真实资金自动交易？',{details:`${s.selection_mode==='auto'?'五品种自动选择：黄金、白银、WTI、布伦特、天然气；最多一笔持仓，无合格候选保持空仓':s.symbol} · ${s.interval||'1h'} 已收盘K线 · ${s.leverage}× · 单笔风险预算 ${fmt(s.risk)} USDT。启动后，策略将根据K线、新闻与风控规则自动开仓、设置保护单和平仓，使用币安正式账户资金。`,label:'确认启动实盘'}))return;
  request.confirm_live=true;
 }else if(context.context_mode==='demo'&&!await askConfirmation('将在币安Demo测试账户按策略自动下单，确认启动？'))return;
 if(await post('start',request))toast(context.context_mode==='live'?'实盘策略已启动，请持续关注持仓与风控记录':'已启动，等待通过新闻与风控检查的交易信号');
};
$('pause').onclick=async()=>{if(await post('pause'))toast('已暂停新开仓，已有持仓保护继续运行');};
function renderPendingRecovery(){
 const p=latest?.state.pending;
 $('pending-panel').hidden=!p;
 if(!p){pendingReview=null;pendingReviewContext=null;return;}
 const supported=latest.pending_recovery_supported===true;
 $('pending-description').textContent=supported?`${({leverage:'杠杆修改',entry:'开仓',close:'平仓'})[p.kind]||'未知操作'} · ${p.symbol||'未知品种'} · 原请求 ${p.id||'缺失'}。未核验前保持锁定。`:'此后台版本尚未加载恢复功能；新版已安装时需重启后台后使用，重启可能需要重新输入API密钥。';
 $('inspect-pending').disabled=!supported;
 if(!pendingReview||!contextMatches(pendingReviewContext||{})||pendingReview.pending_id!==p.id||Date.now()/1000-pendingReview.checked_at>60){
  pendingReview=null;pendingReviewContext=null;$('resolve-pending').disabled=true;
  $('pending-result').textContent='请查询原请求结果；核对结果60秒后失效。不会重发、撤销订单或自动启动。';
 }
}
$('inspect-pending').onclick=async()=>{
 const context=requestContext(),identity=latest?.state.pending?.id;if(!identity)return;
 pendingReview=null;pendingReviewContext=null;$('resolve-pending').disabled=true;
 $('inspect-pending').disabled=true;$('pending-result').textContent='正在只读查询原请求、持仓与未完成订单…';
 try{
  const data=await post('pending/inspect',context);
  if(!contextMatches(context)||latest?.state.pending?.id!==identity)return;
  if(!data){$('pending-result').textContent=lastError||'查询未完成，待核对状态保留';return;}
  pendingReview=data;pendingReviewContext=context;
  $('pending-result').textContent=data.reason;
  $('resolve-pending').disabled=!data.can_resolve;
 }finally{if(contextMatches(context))$('inspect-pending').disabled=false;}
};
$('resolve-pending').onclick=async()=>{
 const review=pendingReview,context=pendingReviewContext;
 if(!review?.can_resolve||!contextMatches(context||{})||latest?.state.pending?.id!==review.pending_id)return;
 if(!await askConfirmation('确认采用核对结果更新本地账本？',{details:review.reason+'。确认前会再次只读核验；保留资金预算、亏损基准与防重复记录，新开仓仍暂停。不会发送或撤销交易所订单。',label:'确认更新本地账本'}))return;
 $('resolve-pending').disabled=true;
 const data=await post('pending/resolve',{...context,expected_pending_id:review.pending_id,review_id:review.review_id,confirm_reconcile:true});
 pendingReview=null;pendingReviewContext=null;
 $('pending-result').textContent=data?'核对完成，新开仓仍暂停。':lastError||'更新未确认，请重新查询；不要重复操作';
 if(data)toast('待核对请求已恢复，新开仓仍暂停');
};
$('close').onclick=async()=>{
 const context=requestContext(),position=latest?.state.position;if(!position)return;
 const live=context.context_mode==='live';
 if(!await askConfirmation(live?'确认停止策略并平掉实盘持仓？':'停止自动开仓并平掉本程序持仓？',{details:live?`${position.symbol} · ${position.side==='BUY'?'Long position':'Short position'} · 数量 ${fmt(position.quantity,5)}。将向币安正式账户发送只减仓平仓请求，成交会产生真实盈亏和费用。`:'',label:live?'确认实盘平仓':'确认平仓'}))return;
 if(await post('close',{...context,confirm_live:live,expected_symbol:position.symbol,expected_position_id:position.id}))toast('平仓请求已处理，请核对持仓状态');
};
$('apply-leverage').onclick=async()=>{
 const context=requestContext(),s=latest?.state;if(context.context_mode!=='live'||!s)return;
 if(!await askConfirmation('确认修改币安实盘杠杆？',{details:`将币安正式账户中 ${s.symbol} 的杠杆调整为已保存的 ${s.leverage}×。此操作修改交易所设置，不会开仓。`,label:'确认应用实盘杠杆'}))return;
 const d=await post('apply-leverage',{...context,confirm_live:true,expected_symbol:s.symbol,expected_leverage:s.leverage});
 $('leverage-applied').textContent=d?`${s.symbol} 的 ${s.leverage}× 杠杆已提交币安确认。`:lastError||'未完成杠杆修改';
};
$('execution-mode').addEventListener('change',async()=>{
 const context=requestContext(),mode=$('execution-mode').value;
 if(mode===context.context_mode)return;
 if(busy||pauseRequest||modeSwitching){$('execution-mode').value=context.context_mode;return;}
 modeSwitching=true;$('execution-mode').disabled=true;$('start').disabled=true;
 try{if(await post('mode',{...context,mode}))toast(mode==='live'?'已切换实盘环境；连接账户后仍需手动确认启动':'已切换本地模拟环境');}
 finally{modeSwitching=false;if(latest){renderEnvironment(latest);await loadLeverage();await refresh();}}
});
$('account').onclick=async()=>{
 const context=requestContext(),d=await post('account',context);
 if(!d){$('account-result').textContent=lastError||'查询未完成';return;}
 $('account-result').textContent=`${context.context_mode==='live'?'实盘账户查询成功':'只读连接成功'}：余额 ${fmt(d.balance,8)} USDT，可用 ${fmt(d.available,8)}，持仓 ${d.positions} 笔。`;
 if(context.context_mode==='live')$('account-result').textContent+=`策略预算 ${fmt(d.capital_budget)} USDT（上限 ${fmt(d.capital_limit)}）；每日亏损限额 ${fmt(d.daily_loss_limit)}，累计 ${fmt(d.loss_limit)} USDT。${d.halted|| (d.enabled?'策略运行中':'新开仓仍暂停')}。`;
};
refresh();setInterval(refresh,5000);

$('bind-account-identity').onclick=async()=>{
 if(!latest?.credential_rotation_supported)return;
 const context=requestContext();
 const d=await post('credentials/bind-account',context);
 if(d)toast('原账户身份已核验；可在原密钥撤销后轮换同一账户的新密钥');
};
$('api-credentials').addEventListener('submit',async e=>{
  e.preventDefault(); if(busy||pauseRequest||!latest)return;
  const context=requestContext(),live=context.context_mode==='live',recovering=live&&!!(latest.state.position||latest.state.pending);
  const rotating=live&&$('rotate-key').checked;
  if(rotating){
    if(!latest.credential_rotation_supported||!latest.account_identity_bound){toast('先使用新版后台及原密钥建立账户身份记录');return;}
    if(!await askConfirmation('确认轮换同一账户的 API 密钥？',{details:'将只读核验新密钥的账户身份及空仓状态；一致时替换连接，保留原资金预算、亏损锁定和全部账本，新开仓仍暂停。',label:'核验并轮换密钥'}))return;
    if(!contextMatches(context))return;
  }
  const body={...context,key:$('api-key').value,secret:$('api-secret').value,mode:live?'live':'readonly',...(rotating?{confirm_rotation:true}:{})};
  $('api-key').value='';$('api-secret').value='';
  $('connect-api').disabled=true;
  $('account-result').textContent=recovering?'正在核验原账户并恢复已有仓位监控；不会恢复新开仓…':live?'正在通过只读请求核验实盘账户；连接不会启动策略…':'正在验证币安只读账户…';
  try {
    const d=await post(rotating?'credentials/rotate':'connect',body);
    if(d){leverageInfo=null;loadLeverage();if(rotating)$('rotate-key').checked=false;}
    $('account-result').textContent=d?`${rotating?'同一账户密钥已轮换，原账本保留、策略仍暂停':recovering?'已恢复实盘账户连接与仓位监控，新开仓仍暂停':live?'实盘交易连接已配置，策略仍暂停':'实盘只读连接成功'}：余额 ${fmt(d.balance,8)} USDT，可用 ${fmt(d.available,8)}，持仓 ${d.positions} 笔。${recovering?'请核对持仓、保护单与待核对订单状态。':live?'启动策略或应用杠杆时会分别请求确认。':'当前策略使用模拟或Demo资金。'}`:`连接未成功：${lastError||'请稍后重试'}。原有连接未被替换。`;
    if(d?.credential_saved===false)$('account-result').textContent+=' 本次账户连接成功，但钥匙串保存失败；重启前请检查下方保存状态并重新提交。';
  } finally {body.key='';body.secret='';$('connect-api').disabled=false;}
});

function updateProducts(){
  const locked=!latest||modeSwitching||latest.state.enabled||!!latest.state.position||!!latest.state.pending;
  $('products-note').textContent=locked?'请先暂停策略并平仓，再切换交易品种。':`当前策略品种：${latest.state.symbol}`;
  document.querySelectorAll('#products-list button').forEach(b=>{b.disabled=locked;b.setAttribute('aria-pressed',String(b.dataset.symbol===latest?.state.symbol));});
}
for(const option of $('symbol').options){
  const button=document.createElement('button');button.type='button';button.className='product-option';button.dataset.symbol=option.value;button.textContent=option.textContent;
  button.onclick=()=>{
    if($('symbol').disabled){updateProducts();return;}
    $('symbol').value=option.value;updateDraft();loadLeverage();$('products-dialog').close();
    $('settings').scrollIntoView({behavior:'smooth',block:'center'});$('symbol').focus({preventScroll:true});
    toast(`已选择${option.textContent}，点击“保存设置”后生效`);
  };
  $('products-list').append(button);
}
$('nav-products').onclick=()=>{updateProducts();$('products-dialog').showModal();};
$('products-close').onclick=()=>$('products-dialog').close();
$('nav-workspace').onclick=()=>{window.scrollTo({top:0,behavior:'smooth'});};
$('nav-ai').onclick=()=>$('ai-panel').scrollIntoView({behavior:'smooth',block:'start'});

let analysisBusy=false;
const stamp=t=>t?new Date(t*1000).toLocaleTimeString():'尚未成功更新';
async function refreshAnalysis(){
 if(analysisBusy)return;analysisBusy=true;const generation=uiGeneration;
 try{
  const r=await fetch('/api/analysis');if(!r.ok)throw Error('分析服务不可用');
  const a=await r.json();if(generation!==uiGeneration)return;const k=a.kline,n=a.news;latestNews=n;analysisAvailable=true;
  const interval=k.interval||k.data?.interval||latest?.state?.interval||'1h';
  $('chart-interval').textContent=interval;$('chart-period').textContent=interval+' 价格曲线 · 包含当前未收盘K线';$('kline-period').textContent='目标每1秒更新 · '+interval+' K线';
  const ks=!k.updated||Date.now()/1000-k.updated>5;
  $('kline-status').textContent=`${k.symbol||''} · 更新 ${stamp(k.updated)}${ks?' · 数据延迟':''}${k.error?' · '+k.error:''}`;
  if(k.data){const d=k.data;
   $('kline-analysis').textContent=`盘中趋势${d.trend}；最新价 ${fmt(d.price,3)}。EMA20 ${fmt(d.fast,3)} / EMA60 ${fmt(d.slow,3)}；前20根收盘K线区间 ${fmt(d.lower,3)}～${fmt(d.upper,3)}，ATR14 ${fmt(d.atr,3)}。收盘信号：${d.confirmed}。`;
   chart(d.rows);$('chart-title').textContent=k.symbol.replace('USDT',' / USDT');
  }else{$('kline-analysis').textContent='等待当前品种K线';$('chart').textContent='正在载入当前品种K线';}
  latestFundamentals=a.fundamentals||null;
  renderAIAnalysis(a.ai);
  renderNews(n);
  if(latest)renderDecision(latest);
  syncStartControls();
 }catch(e){if(generation!==uiGeneration)return;analysisAvailable=false;latestFundamentals=null;syncStartControls();renderAIAnalysis({status:'error',trading_authority:false});$('kline-status').textContent='分析服务断开，显示内容可能已过期';$('news-status').textContent='分析服务断开，等待恢复';$('news-decision').textContent='新闻服务断开；新闻条件未通过，新开仓需等待恢复';$('news-decision').className='news-decision blocked';$('decision-news').textContent='新闻服务断开';}
 finally{analysisBusy=false;}
}
refreshAnalysis();setInterval(refreshAnalysis,1000);

function settingsDirty(){
 if(!latest)return true;
 const s=latest.state;return ($('interval').value||'1h')!==(s.interval||'1h')||($('selection-mode').value||'manual')!==(s.selection_mode||'manual')||$('symbol').value!==s.symbol||Number($('risk').value)!==s.risk||Number($('leverage').value)!==s.leverage;
}
function updateDraft(){
 if(!latest)return;
 const s=latest.state,dirty=settingsDirty();
 $('settings-status').dataset.interval=s.interval||'1h';
 $('settings-status').textContent=dirty?`尚未保存；当前模式为 ${s.selection_mode==='auto'?'五品种自动选择':'固定品种'}，当前品种 ${s.symbol}`:`已保存 · ${s.selection_mode==='auto'?'五品种自动选择 · 当前展示':'固定品种'} ${s.symbol} · ${s.interval||'1h'}${latest.mode==='live'?' · 保存仅更新本地设置':''}`;
 const preview=latest.risk_preview,usable=preview?.symbol===s.symbol&&preview.leverage===s.leverage&&finite(preview.risk_budget)&&Date.now()/1000-preview.updated<=30;
 $('risk-context').textContent=`${s.symbol} · 已保存 ${s.leverage}× · 单笔设置上限 ${fmt(s.risk)} USDT${usable?` · 本轮可用 ${fmt(Math.max(0,preview.risk_budget))} USDT`:''}${dirty?'。待保存设置尚未参与预检':''}`;
 if(dirty)$('apply-leverage').disabled=true;
}
$('settings').addEventListener('input',()=>{updateDraft();syncLeverageControls();});
function askConfirmation(message,options={}){
 return new Promise(resolve=>{
  const dialog=$('confirm-dialog');$('confirm-message').textContent=message;$('confirm-details').textContent=options.details||'';$('confirm-submit').textContent=options.label||'确认执行';dialog.returnValue='cancel';
  dialog.addEventListener('close',()=>resolve(dialog.returnValue==='ok'),{once:true});dialog.showModal();
 });
}

function syncLeverageControls(){
 const s=latest?.state,locked=!s||modeSwitching||s.enabled||!!s.position||!!s.pending;
 const valid=leverageInfo&&leverageInfo.symbol===$('symbol').value&&leverageInfo.expires>Date.now()/1000;
 const selected=Number($('leverage').value),withinLimit=valid&&Number.isInteger(selected)&&selected>=1&&selected<=leverageInfo.maximum;
 $('interval').disabled=locked;$('leverage').disabled=locked||!valid;$('save').disabled=locked||!withinLimit;
 $('leverage-value').textContent=$('leverage').value+'×';
 syncStartControls();
 $('apply-leverage').disabled=locked||!withinLimit||latest?.mode!=='live'||!latest.live_connected||!latest.account_bound||settingsDirty();
 if(valid&&!withinLimit)$('leverage-status').textContent=`当前选择 ${selected}× 超过本品种币安上限 ${leverageInfo.maximum}×；已保留你的选择，不自动降低。请手动调整或选择其他品种。`;
 else if(withinLimit)$('leverage-status').textContent=`${leverageInfo.source||'币安'} · ${$('symbol').value} 最高 ${leverageInfo.maximum}× · ${stamp(leverageInfo.updated)} 更新。使用你选择的 ${selected}×，不会自动降低；保存仅修改本程序设置。`;
 if(!valid&&leverageInfo)$('leverage-status').textContent='杠杆档位已过期，请刷新后再保存';
}
async function loadLeverage(){
 if(!latest||modeSwitching)return;
 const sequence=++leverageSequence,symbol=$('symbol').value,context=requestContext();
 leverageInfo=null;syncLeverageControls();$('leverage-max').textContent='查询中';
 $('leverage-status').textContent='正在读取 '+symbol+' 的币安账户杠杆档位…';
 try{
  const response=await fetch('/api/leverage',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({...context,symbol})});
  const data=await response.json();if(!response.ok)throw Error(data.error||'查询失败');
  if(sequence!==leverageSequence||symbol!==$('symbol').value||!contextMatches(context))return;
  if(data.symbol!==symbol||!Number.isInteger(data.maximum)||data.maximum<1)throw Error('杠杆上限数据无效');
  const previous=Number($('leverage').value);leverageInfo=data;$('leverage').max=Math.max(previous,data.maximum);
  $('leverage').value=previous;$('leverage-max').textContent=data.maximum+'×（币安上限）';
  $('leverage-status').textContent=`${data.source} · ${symbol} 最高 ${data.maximum}× · ${stamp(data.updated)} 更新。实际仓位受分档限制；保存仅修改本程序设置。倍数由你选择，系统不自动降低。`;
  syncLeverageControls();updateDraft();await refresh();
 }catch(e){
  if(sequence!==leverageSequence||!contextMatches(context))return;
  $('leverage-max').textContent='上限未获取';$('leverage-status').textContent=e.message;syncLeverageControls();
 }
}
$('symbol').addEventListener('change',loadLeverage);
$('leverage').addEventListener('input',()=>{if(leverageInfo&&Number($('leverage').value)<=leverageInfo.maximum)$('leverage').max=leverageInfo.maximum;syncLeverageControls();updateDraft();});
$('leverage-retry').onclick=loadLeverage;
setInterval(loadLeverage,60000);

function setBadge(id,text,tone=''){
 const element=$(id);element.textContent=text;element.className='tag'+(tone?' '+tone:'');
}
function finite(value){return value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value));}
function metric(value,digits=4,suffix=''){return finite(value)?fmt(value,digits)+suffix:'未取得';}
function safeNewsURL(value){
 try{const url=new URL(value);return url.protocol==='https:'?url.href:null;}catch{return null;}
}
function activeAIDirection(ai,symbol){
 const now=Date.now()/1000;
 if(!ai||ai.policy_version!==1||ai.symbol!==symbol||ai.status!=='active'||!['BUY','SELL'].includes(ai.direction))return null;
 if(typeof ai.analyzed_at!=='number'||!Number.isFinite(ai.analyzed_at)||typeof ai.expires!=='number'||!Number.isFinite(ai.expires)||now<ai.analyzed_at||now>=ai.expires)return null;
 if(ai.retained_input_checked_at!=null){const checked=ai.retained_input_checked_at;if(typeof checked!=='number'||!Number.isFinite(checked)||checked<ai.analyzed_at||checked>now||ai.expires!==checked+90)return null;}else if(ai.expires!==ai.analyzed_at+900)return null;
 if(typeof ai.model!=='string'||!ai.model.trim()||typeof ai.result_id!=='string'||!/^[a-f0-9]{64}$/.test(ai.result_id))return null;
 return ai.direction;
}
function newsState(news){
 const base=ruleNewsState(news),decision=news?.decision;
 if(!base.ready)return base;
 const ai=decision?.ai,direction=activeAIDirection(ai,decision?.symbol);
 if(!direction)return {...base,reason:base.reason+(ai?.status==='active'?'；AI综合方向已过期或格式无效，不采用模型方向':ai?.reason?'；'+ai.reason:'')};
 if(decision.entry_allowed===true&&decision.direction!==direction)return {label:'AI与核验新闻冲突 · 等待',tone:'blocked',reason:'综合方向不一致，暂不新开仓或按方向反转退出；已有持仓保护继续。',ready:false,path:'ai+kline'};
 return {label:'AI综合'+(direction==='BUY'?'偏多':'偏空')+' · 等待同向收盘突破',tone:'accepted',ready:true,path:'ai+kline',reason:`${ai.model} 综合方向已参与决策；与收盘突破冲突则等待，单因素或低频统计不能单独触发买卖。AI方向有效至 ${newsDate(ai.expires)}。`};
}
function ruleNewsState(news){
 const decision=news?.decision;
 const blocked=(label,reason)=>({label:label+' · 禁止新开仓',tone:'blocked',reason,ready:false});
 if(!decision)return blocked('待核验','尚未取得本品种的可执行新闻证据');
 if(latest&&decision.symbol!==latest.state.symbol)return blocked('品种待更新','正在取得当前品种的新闻证据');
 if(decision.policy_version!==2||typeof decision.entry_allowed!=='boolean')return blocked('待核验','新闻决策缺少当前规则的明确许可；不能把无信号或旧版中性状态当作通过');
 if(decision.kline_allowed!==undefined&&typeof decision.kline_allowed!=='boolean')return blocked('K线许可待核验','K线备用路径的许可格式无效');
 const klineAllowed=decision.kline_allowed===true;
 if(klineAllowed&&(decision.status!=='insufficient'||decision.entry_allowed!==false||decision.direction!==null))return blocked('K线许可待核验','K线备用路径只适用于没有新闻方向的证据不足状态');
 if(decision.status==='unavailable')return blocked('来源或证据不可用',decision.reason||'新闻证据不可用');
 const now=Date.now()/1000;
 if(typeof decision.asof!=='number'||!Number.isFinite(decision.asof)||typeof decision.expires!=='number'||!Number.isFinite(decision.expires)||now-decision.asof < -5||now-decision.asof>90||!(now<decision.expires&&decision.expires<=decision.asof+90))return blocked('短线证据已过期或时间无效','新闻核验与有效期须在90秒范围内；持续风险背景仍保留');
 const text=value=>typeof value==='string'&&value.trim().length>0;
 if(!text(decision.reason))return blocked('核验说明缺失','缺少新闻执行条件说明');
 const drivers=decision.drivers;
 if(!Array.isArray(drivers)||!drivers.length||drivers.some(driver=>!driver||!['verified','review_required','no_recent_evidence','not_covered','background'].includes(driver.status)||!['key','label','reason'].every(key=>text(driver[key]))))return blocked('品种驱动待核验','驱动清单缺失或格式无效，不能视为条件通过');
 if(!klineAllowed&&drivers.some(driver=>driver.status==='review_required'))return blocked('新闻方向尚未确认','当前尚未取得K线备用路径许可，请查看数据时效与风险限制');
 const risks=decision.risks;
 if(!Array.isArray(risks)||risks.some(risk=>!validNewsRisk(risk,decision.asof)))return blocked('持续风险状态待核验','事实或开仓限制记录缺失，不能确认是否处于突发事件冷静期');
 if(risks.some(risk=>risk.blocking))return blocked('突发事件冷静期',decision.reason||'新事件仍处于暂缓窗口；截止后等待后台重新核验，不立即开仓');
 if(!Array.isArray(decision.evidence))return blocked('证据结构待核验','新闻证据清单缺失或格式无效');
 if(klineAllowed&&(decision.evidence.length!==0||![undefined,null,''].includes(decision.event_id)))return blocked('K线许可待核验','新闻证据不足状态仍携带方向证据或事件标识，不能切换到K线判断');
 if(klineAllowed)return {label:'新闻证据不足 · 按K线判断',tone:'',reason:decision.reason+'；只有已收盘突破与仓位风控同时允许才可开仓，不会立即买卖',ready:true,path:'kline'};
 const labels={bullish:'短线利多',bearish:'短线利空',insufficient:'证据不足',blocked:'条件冲突或事件暂缓'};
 const directionValid=(decision.status==='bullish'&&decision.direction==='BUY')||(decision.status==='bearish'&&decision.direction==='SELL');
 if(decision.entry_allowed!==true||!directionValid)return blocked(labels[decision.status]||'待核验',decision.reason||'没有通过当前新闻执行条件');
 if(!Array.isArray(decision.evidence)||!decision.evidence.length||decision.evidence.some(item=>item.verified!==true||item.direction!==decision.direction))return blocked('方向证据待核验','缺少与执行方向一致的已核验证据');
 return {label:labels[decision.status]+' · 新闻条件通过',tone:'accepted',reason:decision.reason||'仍须通过已收盘K线及仓位风控检查',ready:true,path:'news'};
}
function validNewsRisk(risk,asof){
 if(!risk||!['id','category','subject','reason','source','url'].every(key=>typeof risk[key]==='string'&&risk[key].trim()))return false;
 if(!validRiskGate(risk,asof))return false;
 const sources={'www.federalreserve.gov':'Fed','federalreserve.gov':'Fed','www.eia.gov':'EIA','eia.gov':'EIA','www.bbc.com':'BBC','www.bbc.co.uk':'BBC','feeds.bbci.co.uk':'BBC','www.theguardian.com':'Guardian'};
 const url=safeNewsURL(risk.url);if(!url||sources[new URL(url).hostname]!==risk.source)return false;
 if(!['first_seen','last_seen','published'].every(key=>typeof risk[key]==='number'&&Number.isFinite(risk[key])))return false;
 if(!(0<risk.first_seen&&risk.first_seen<=risk.last_seen&&risk.last_seen<=asof+5&&0<risk.published&&risk.published<=asof+5))return false;
 return risk.event_at===undefined||risk.event_at===null||(typeof risk.event_at==='number'&&Number.isFinite(risk.event_at)&&risk.event_at>0&&risk.event_at<=asof+5);
}
function validRiskGate(risk,asof){
 if(!risk||risk.gate_version!==1||typeof risk.gate_reason!=='string'||!risk.gate_reason.trim()||typeof risk.blocking!=='boolean'||!['active','review_required','resolved'].includes(risk.status)||!['background','cooldown','resolved'].includes(risk.entry_gate))return false;
 if(typeof asof!=='number'||!Number.isFinite(asof)||risk.blocking!==(risk.entry_gate==='cooldown')||(risk.status==='resolved')!==(risk.entry_gate==='resolved'))return false;
 const fields=['acute_event_id','acute_rule','acute_event_at','time_basis','cooldown_until'];
 if(fields.some(key=>!Object.hasOwn(risk,key)))return false;
 const empty=fields.every(key=>risk[key]===null);
 if(empty)return risk.entry_gate!=='cooldown';
 if(risk.entry_gate==='resolved'||typeof risk.acute_event_id!=='string'||!risk.acute_event_id.trim()||typeof risk.acute_rule!=='string'||!risk.acute_rule.trim()||!['event_time','first_evidence'].includes(risk.time_basis))return false;
 if(typeof risk.acute_event_at!=='number'||!Number.isFinite(risk.acute_event_at)||typeof risk.cooldown_until!=='number'||!Number.isFinite(risk.cooldown_until)||risk.acute_event_at<=0||risk.acute_event_at>asof+5||risk.cooldown_until!==risk.acute_event_at+3600)return false;
 return risk.entry_gate==='cooldown'?risk.acute_event_at<=asof&&asof<risk.cooldown_until:risk.cooldown_until<=asof;
}
function currentNews(){
 const candidates=[latestNews,latest?.decision?.news?{decision:latest.decision.news}:null].filter(item=>item?.decision?.symbol===latest?.state.symbol);
 candidates.sort((a,b)=>(Number(b.decision.asof)||0)-(Number(a.decision.asof)||0)||Number(a.decision.entry_allowed===true||a.decision.kline_allowed===true)-Number(b.decision.entry_allowed===true||b.decision.kline_allowed===true));
 return candidates[0]||latestNews;
}
function syncStartControls(){
 const s=latest?.state,news=newsState(currentNews());
 const permission=news.path==='kline'?'新闻证据不足，已允许按K线判断':'已核验新闻许可';
 const leverageValid=leverageInfo&&leverageInfo.symbol===s?.symbol&&leverageInfo.symbol===$('symbol').value&&leverageInfo.expires>Date.now()/1000&&s.leverage<=leverageInfo.maximum;
 let reason='';
 if(!latest||!stateAvailable)reason='本机状态未连接，等待核验';
 else if(pauseRequest)reason='正在等待暂停确认';
 else if(modeSwitching)reason='交易环境切换中';
 else if(s.enabled)reason='策略已运行；'+news.label;
 else if(s.position)reason='已有持仓，继续保护；平仓后才能重新启动新开仓';
 else if(s.halted||s.pending||s.total_latch||s.daily_latch)reason=s.halted||'账户风控或订单核对锁定';
 else if(latest.error||!latest.updated||Date.now()/1000-latest.updated>20)reason=latest.error||'行情已过期，等待更新';
 else if(latest.mode==='live'&&(!latest.live_connected||!latest.account_bound))reason='实盘账户未连接或未绑定';
 else if(!analysisAvailable)reason='新闻服务尚未连接，等待风险与交易路径许可核验';
 else if(s.selection_mode==='auto'&&(!latest.selection?.updated||Date.now()/1000-latest.selection.updated>20))reason='等待五品种扫描；所有候选都不达标时仍可启动观察';
 else if(s.selection_mode!=='auto'&&!news.ready)reason=news.label+'：'+news.reason;
 else if(!leverageValid)reason=permission+'；等待有效杠杆档位';
 else if(settingsDirty())reason=permission+'；请先保存当前设置';
 $('start').disabled=!!reason;
 $('start-status').textContent=reason||(s.selection_mode==='auto'?'启动后扫描五个品种，合格候选中选一个；无合格候选保持空仓':news.path==='kline'?permission+'；启动后等待已收盘突破和风控条件，不会立即下单':permission+'；启动后仍逐笔检查收盘信号与仓位风控');
}
function renderSelection(data){
 const value=data.selection,s=data.state||{},stale=!value?.updated||Date.now()/1000-value.updated>20;
 $('selection-status').textContent=stale?'等待有效五品种扫描':`${s.selection_mode==='auto'?'五品种自动选择':'固定品种模式 · 下表仅观察'} · ${s.enabled?'运行中':'已暂停'} · ${stamp(value.updated)} · ${value.locked?value.reason||'账户风控锁定':value.selected?'本轮候选 '+value.selected:'无合格候选，保持空仓'}`;
 const rows=Array.isArray(value?.rows)?value.rows:[];
 $('selection-rows').innerHTML=rows.map(row=>`<tr><td>${esc(row.symbol)}</td><td>${row.side==='BUY'?'做多':row.side==='SELL'?'做空':'—'}</td><td>${!stale&&row.status==='eligible'?'第 '+esc(row.rank)+' 名':'未入选'}</td><td>${!stale&&typeof row.net_reward_risk==='number'?esc(fmt(row.net_reward_risk,2)):'—'}</td><td>${esc(stale?'扫描已过期，等待刷新':row.reason||'等待数据')}</td></tr>`).join('');
}
function renderDecision(data){
 const decision=data.decision,s=data.state;
 const context=`${s.symbol} · ${s.enabled?'策略运行中':'策略已暂停'}${decision?.updated||decision?.asof?' · '+stamp(decision.updated||decision.asof):''}`;
 $('decision-context').textContent=context;
 const labels={WAIT:'等待',BUY:'候选做多',SELL:'候选做空',EXIT:'平仓条件'};
 setBadge('decision-badge',labels[decision?.action]||'等待',decision?.action==='EXIT'?'blocked':decision?.action==='BUY'||decision?.action==='SELL'?'accepted':'');
 $('decision-reason').textContent=decision?.reason||'等待执行引擎完成K线、新闻与风控核验';
 $('decision-source').textContent=decision?.source==='ai+kline'?'AI综合方向 + 同向收盘突破':decision?.source==='news+kline'?'正文证据 + 收盘趋势':decision?.source==='kline'?'已收盘突破 + 风控检查':'尚无候选信号';
 const state=newsState(currentNews());
 $('decision-news').textContent=state.label;
 $('decision-news').title=state.reason;
 const preview=data.risk_preview;
 $('decision-risk').textContent=preview?(!finite(preview.updated)||Date.now()/1000-preview.updated>30?'预检已过期':preview.ok?'候选仓位通过预检':preview.reason||'预检未通过'):'等待保证金与费用数据';
}
function riskRows(order){
 if(!order)return '';
 return [
  ['名义价值',metric(order.notional,2,' USDT')],
  ['预计逐仓保证金',metric(order.margin,2,' USDT')],
  ['止损价',metric(order.stop,6)],
  ['预计强平价',metric(order.estimated_liquidation,6)],
  ['止损至强平间距',metric(order.liquidation_gap,6)],
  ['最低所需间距',metric(order.required_liquidation_gap,6)],
  ['所需可用资金',metric(order.cash_required,3,' USDT')],
  ['计划风险（含预留）',metric(order.planned_loss,3,' USDT')],
  ['资金费预留',metric(order.risk_audit?.funding_reserve,4,' USDT')]
 ].map(([label,value])=>`<div><dt>${label}</dt><dd>${value}</dd></div>`).join('');
}
function renderRiskPreview(data){
 const preview=data.risk_preview;
 if(!preview){setBadge('risk-badge','待核验','blocked');$('risk-reason').textContent='尚未取得保证金档位与资金费数据；不能据此确认杠杆可用';$('risk-preview').textContent='';return;}
 const stale=!finite(preview.updated)||Date.now()/1000-preview.updated>30;
 setBadge('risk-badge',stale?'已过期':preview.ok?'通过预检':'未通过',stale||!preview.ok?'blocked':'accepted');
 $('risk-reason').textContent=stale?'风控预检已过期，开仓前必须重新计算':preview.reason||'已按当前行情、逐仓档位与费用预算计算';
 const sides=[['long','做多'],['short','做空']];
 $('risk-preview').innerHTML=sides.map(([key,label])=>{
  const order=preview[key];
  const reason=preview[key+'_error']||preview.reason||'当前条件下无法生成符合风控的仓位';
  return `<section class="risk-side"><h3>${label}<span>${order?(stale?'过期估算':'候选仓位'):'不可开仓'}</span></h3>${order?`<dl>${riskRows(order)}</dl>`:`<p class="muted">${esc(reason)}</p>`}</section>`;
 }).join('')+`<p class="muted">计算时间 ${stamp(preview.updated)}${stale?' · 不可据此开仓':''}</p>`;
}
function renderPosition(position){
 const rows=[['方向',`${esc(position.symbol)} · ${position.side==='BUY'?'做多':'做空'}`],['数量',metric(position.quantity,5)],['成交价',metric(position.entry,6)],['止损',metric(position.stop,6)],['止盈',metric(position.target,6)],['计划风险',metric(position.planned_loss,3,' USDT')],['预计强平价',metric(position.simulated_liquidation??position.estimated_liquidation,6)],['止损至强平间距',metric(position.actual_liquidation_gap??position.liquidation_gap,6)],['已计资金费',metric(position.funding_paid,4,' USDT')]];
 if(latest?.mode==='demo'||latest?.mode==='live')rows.push(['交易所强平价',metric(position.actual_liquidation,6)],['实际保证金',metric(position.actual_isolated_wallet,3,' USDT')],['实际成交后计划风险',metric(position.actual_planned_loss,3,' USDT')],['交易所风控核对',position.venue_risk_checked?esc(stamp(position.venue_risk_checked)):'待确认']);
 $('position').className='position-grid';$('position').innerHTML=rows.map(([label,value])=>`<div><small>${label}</small>${value}</div>`).join('');
}
function renderCoverage(coverage){
 if(!coverage)return '来源读取状态未知；覆盖范围尚未核验。未知信息不能视为中性或无风险。';
 const successful=Array.isArray(coverage.successful)?coverage.successful:[],failed=Array.isArray(coverage.failed)?coverage.failed:[];
 const parts=[coverage.healthy?'来源读取正常（不代表覆盖完整或允许交易）':'来源读取不完整'];
 if(successful.length)parts.push('已读取：'+successful.join('、'));
 if(failed.length)parts.push('暂不可用：'+failed.join('、'));
 parts.push('有限覆盖范围：'+(coverage.scope||'当前已配置的来源；尚未确认全部市场事件'));
 const uncovered=coverage.uncovered||coverage.missing;
 parts.push('未覆盖或未核验：'+(Array.isArray(uncovered)?uncovered.join('、'):uncovered||'未接入来源、未取得正文的报道，以及尚未被报道的事件'));
 if(coverage.overflow)parts.push('报道超出本轮处理容量，不能视为已完整检查');
 return parts.join('\n');
}
function newsDate(value){return finite(value)&&Number(value)>0?new Date(Number(value)*1000).toLocaleString():'未知';}
let latestAI=null,latestFundamentals=null;
function dataSourceURL(value){
 try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&(!u.port||u.port==='443')&&['www.federalreserve.gov','www.gold.org','www.safe.gov.cn','www.eia.gov','ir.eia.gov','ftp.cpc.ncep.noaa.gov','silverinstitute.org'].includes(u.hostname)?u.href:null;}catch{return null;}
}
function renderFactorData(data,key){
 const metrics=Array.isArray(data?.metrics)?data.metrics.filter(row=>row.driver===key):[];
 if(!metrics.length&&['geopolitics','shipping'].includes(key))return '<section class="factor-data"><strong>数值指标：未接入</strong><p>此项使用下方事件记录；尚无实时船舶/AIS、海峡流量或统一地缘风险数值序列。</p></section>';
 if(!metrics.length)return '<section class="factor-data"><strong>结构化数据：未接入</strong><p>本因素尚无专门数值数据源；新闻解读单独展示，不代替数据接入。</p></section>';
 const count=metrics.filter(row=>row.status==='ok'&&typeof row.value==='number'&&Number.isFinite(row.value)).length;
 const label=count===metrics.length?'已接入':count?'部分可用':'暂不可用';
 const limits={usd_real_yield:'日度观测，按官方发布更新；未接入实时 ICE DXY 或盘中实际利率。',fed_policy:'已接入有效联邦基金利率；政策目标区间与期货隐含降息概率尚未接入。',gold_demand:'全球需求按季度、中国储备按月更新；未接入各国央行实时交易及日内实物成交。',oil_supply:'美国周度产量、进出口与炼厂投入；未接入OPEC及全球实时供给。对布伦特仅作跨市场参考。',inventory:'实际库存已接入；市场一致预期尚未接入，前周差不能当作超预期。',gas_inventory:'本土48州工作气库存；市场一致预期尚未接入，库存变化不自动产生买卖方向。',us_gas_supply:'美国月度干气产量，换算为当月日均；月度统计存在发布滞后。',us_lng:'美国月度LNG出口，按月长换算日均；不是实时管道进气或装船流量。',gas_weather:'全美人口加权实测度日，比较近7日和前7日；天气预报及预测修订尚未接入。',global_lng:'EIA发布的欧洲TTF、亚洲JKM周均价格；不是Henry Hub或实时套利价差。',silver_industry:'全球年度工业及光伏用银历史估计；不采用报告中的F预测列，也不是实时工厂订单。',silver_supply:'全球年度矿产与回收供给估计；矿山即时停产与复产另看事件报道。'};
 const rows=metrics.map(row=>{
  const has=typeof row.value==='number'&&Number.isFinite(row.value),ok=row.status==='ok'&&has;
  const status=ok?'':row.status==='stale'?' · 旧值，仅供历史参考':' · 待读取';
  const frequency={daily:'日度',monthly:'月度',quarterly:'季度',weekly:'周度',annual:'年度'}[row.frequency]||'未知频率';
  const delta=typeof row.change==='number'&&Number.isFinite(row.change)?`${row.change>0?'+':''}${fmt(row.change,4)} ${row.change_unit}`:'未知';
  const percent=row.unit!=='%'&&typeof row.change_percent==='number'&&Number.isFinite(row.change_percent)?`（${row.change_percent>0?'+':''}${fmt(row.change_percent,2)}%）`:'';
  const url=dataSourceURL(row.url),source=url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(row.source||'原始数据')}</a>`:esc(row.source||'来源未知');
  return `<li><strong>${esc(row.label)}：${has?esc(fmt(row.value,4)+' '+row.unit):'暂无有效数值'}${esc(status)}</strong><small>${esc(frequency)} · 统计期 ${esc(row.observation_date||'未知')}</small>${has?`<small>前值 ${esc(row.previous_date||'未知')}：${esc(fmt(row.previous_value,4))}；变化 ${esc(delta+percent)}</small>`:''}<details><summary>来源、时效与计算说明</summary><p>${source}<br>发布日期：${esc(row.published_date||'来源未标注')}<br>读取：${esc(newsDate(row.fetched_at))}；最近检查：${esc(newsDate(row.last_checked))}</p><p>${esc(row.error||'')}${row.error?'。':''}${esc(row.note||'')}</p>${typeof row.year_ago_value==='number'?`<p>去年同期：${esc(fmt(row.year_ago_value,4))} ${esc(row.unit)}</p>`:''}</details></li>`;
 }).join('');
 return `<section class="factor-data"><strong>结构化数据：${label}（${count}/${metrics.length}）</strong><p>${esc(limits[key]||'按来源发布频率更新，前后值差可能受已公布数值的四舍五入影响。')}</p><ul>${rows}</ul></section>`;
}
function renderFactorEvents(data,key){
 const state=Array.isArray(data?.events)?data.events.find(row=>row.key===key):null;
 if(!state)return '';
 const valid=state.status==='connected';
 const records=Array.isArray(state.records)?state.records:[];
 const entries=records.map(row=>{
  const url=aiSourceURL(row.url);if(!url)return '';
  const fact={active:'记录为持续影响',review_required:'事实尚待独立核验',resolved:'记录为已解除'}[row.status]||'事实状态未确认';
  return `<li><strong>${esc(riskSubject(row))}</strong><small>${esc(fact)}${valid?'':' · 历史记录'} · 报道 ${esc(newsDate(row.published))}</small><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(row.source||'来源')} · 事件原文</a></li>`;
 }).filter(Boolean).join('');
 return `<section class="factor-data factor-events"><strong>事件数据：${valid?'已接入':'来源暂不可用'} · ${Number.isInteger(state.total)?state.total:records.length} 条记录</strong><p>公开报道与持续事件台账已关联；已记录不代表已独立核实。${records.length?'展示最近记录。':'本轮未识别相关记录，不代表风险不存在。'}</p>${entries?`<details><summary>查看事件与来源</summary><ul>${entries}</ul></details>`:''}</section>`;
}
function renderNewsDrivers(news){
 const data=latestFundamentals?.symbol===news.symbol?latestFundamentals:null;
 const drivers=Array.isArray(news.decision?.drivers)&&news.decision.drivers.length?news.decision.drivers:(data?.factors||[]);
 const names={XAUUSDT:'黄金',XAGUSDT:'白银',CLUSDT:'WTI原油',BZUSDT:'布伦特原油',NATGASUSDT:'天然气'};
 const ai=latestAI?.symbol===news.symbol&&latestAI.trading_authority===false?latestAI:null;
 let aiStatus=ai?.status||'disabled';
 if(['ok','historical'].includes(aiStatus)){
  if(typeof ai.analyzed_at!=='number'||!Number.isFinite(ai.analyzed_at)||ai.analyzed_at>Date.now()/1000+5)aiStatus='stale';
  else if(!(typeof ai.retained_input_checked_at==='number'&&Date.now()/1000-ai.retained_input_checked_at>=0&&Date.now()/1000-ai.retained_input_checked_at<90)&&Date.now()/1000-ai.analyzed_at>=900)aiStatus='historical';
 }
 $('news-driver-context').textContent=`${names[news.symbol]||news.symbol||'当前品种'} · 展示已接入的原始统计、变化计算及模型解读。日、周、月、季、年频率分别标注；缺失仍明确列出。新闻方向未确认不单独禁仓。`;
 const signature=JSON.stringify([news.symbol,drivers,ai,aiStatus,data]);
 if($('news-drivers').dataset.version===signature)return;
 $('news-drivers').dataset.version=signature;
 if(!drivers.length){$('news-drivers').innerHTML='<p class="muted">尚未取得本品种驱动清单与证据，状态未知，禁止新开仓。</p>';return;}
 const states={standby:['AI 轻量监测','未持有品种暂停常规模型调用；新重大事件可触发复核，原始数据和规则监测继续。'],disabled:['AI 未启用','启用模型后逐项解读；当前可查看规则记录。'],pending:['等待 AI 分析','新增新闻或数据修订已排队，尚未取得本因素的模型结果。'],analyzing:['AI 分析中','模型正在阅读本批新闻与有效统计数据，完成后展示各因素的解释与来源。'],error:['AI 分析失败',aiSafeFailure()],stale:['分析暂不可用','新闻来源或结果时间异常，暂不展示旧方向。'],no_news:['暂无可分析输入','本轮没有可分析的新闻正文或有效统计值，不代表市场没有相关因素。'],unchanged:['无新增新闻','没有新增新闻或数据修订，不重复调用模型；暂无可展示结果。']};
 const labels={verified:'规则已提取方向',background:'持续背景',review_required:'规则未确认方向',no_recent_evidence:'规则暂无方向证据',not_covered:'固定新闻规则未覆盖'};
 $('news-drivers').innerHTML=drivers.map(driver=>{
  const rows=Array.isArray(ai?.drivers)?ai.drivers:[];
  const matches=rows.filter(row=>row&&row.key===driver.key);
  const row=matches.length===1?matches[0]:null;
  const valid=row&&['analyzed','no_evidence','unverified'].includes(row.status)&&['bullish','bearish','mixed','uncertain'].includes(row.direction)&&typeof row.summary==='string'&&row.summary.trim()&&Array.isArray(row.sources)&&(row.status==='analyzed'?row.sources.length>0:row.direction==='uncertain'&&(row.status==='unverified'||row.sources.length===0));
  let [label,reason]=states[aiStatus]||['等待因素解读','尚未取得完整的逐因素分析结果。'];
  let details='';
  if(['ok','historical'].includes(aiStatus)&&valid){
   label=row.status==='unverified'?'引用校验未通过':aiStatus==='historical'?'历史解读 · 非当前方向':row.status==='no_evidence'?'本批输入无相关证据':'AI '+({bullish:'偏多',bearish:'偏空',mixed:'影响混合',uncertain:'方向不确定'}[row.direction]);
   reason=row.summary;
   const links=row.sources.map(source=>{const url=aiSourceURL(source?.url)||dataSourceURL(source?.url);return url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(source.source||'原文')} · ${esc(source.title||'查看来源')}</a>`:'';}).filter(Boolean);
   details=`<small>${esc(ai.model||'模型')} · ${esc(newsDate(ai.analyzed_at))}${ai.cached?' · 缓存解读':''}</small>${links.length?`<details class="ai-review"><summary>查看依据（人工核验可选）</summary>${links.map(link=>'<p>'+link+'</p>').join('')}</details>`:''}`;
  }
  const ruleReason=driver.status==='not_covered'?'固定新闻规则没有覆盖本因素。实际数据接入状态见上方数值区；模型结合可用新闻与统计进行解读。':readableRiskReason(driver.reason||'尚未取得规则记录',news.decision?.risks);
  return `<article class="news-driver"><div><h4>${esc(driver.label||driver.key||'未命名驱动')}</h4><span class="driver-status">${esc(label)}</span></div>${renderFactorData(data,driver.key)}${renderFactorEvents(data,driver.key)}<p>${esc(reason)}</p>${details}<details><summary>${esc(labels[driver.status]||'规则记录')} · 查看规则说明</summary><p>${esc(ruleReason)}</p></details></article>`;
 }).join('');
}
function readableRiskReason(reason,risks){
 const known=new Map((Array.isArray(risks)?risks:[]).filter(risk=>risk&&typeof risk.subject==='string').map(risk=>[risk.subject,risk]));
 return String(reason).replace(/\b[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?\b/g,subject=>riskSubject(known.get(subject)||{subject}));
}
function riskSubject(risk){
 const value=String(risk.subject||risk.category||'未命名风险');
 const names={middle_east_conflict:'中东地区冲突',red_sea_conflict:'红海地区冲突',strait_of_hormuz:'霍尔木兹海峡',red_sea_route:'红海与曼德海峡航线',suez_canal:'苏伊士运河',multiple_routes:'多条国际航运通道',us_lng_unspecified:'美国LNG出口（设施未明确）',qatar_lng:'卡塔尔LNG供应',global_lng_unspecified:'全球LNG传导风险（地区未明确）'};
 if(names[value])return names[value];
 const places={iran:'伊朗',israel:'以色列',us:'美国',russia:'俄罗斯',ukraine:'乌克兰',yemen:'也门',qatar:'卡塔尔',iraq:'伊拉克',libya:'利比亚',unknown_region:'地区未明确'};
 if(value.endsWith('_conflict')){const parts=value.slice(0,-9).split('_');if(parts.every(part=>places[part]))return parts.map(part=>places[part]).join('、')+'相关冲突';}
 if(['facility_supply','silver_supply','us_lng_exports','global_lng_transmission'].includes(risk.category)&&value.includes(':')){
  const [country,...facility]=value.split(':');const site=facility.join(':').replace(/_/g,' ').replace(/\b[a-z]/g,letter=>letter.toUpperCase()).replace(/\bLng\b/g,'LNG');
  return (places[country]||country.replace(/_/g,' '))+' · '+site;
 }
 return value;
}
function renderPersistentRisks(news){
 const risks=Array.isArray(news.decision?.risks)?news.decision.risks:[],asof=news.decision?.asof;
 const restricted=risks.filter(risk=>!validNewsRisk(risk,asof)||!['background','resolved'].includes(risk.entry_gate)).length;
 const summary=$('news-risk-summary');
 if(summary)summary.textContent=risks.length?`共 ${risks.length} 条背景记录 · ${restricted?`${restricted} 条执行限制需关注，请展开查看`:'这些背景记录不单独阻止开仓'}`:'暂无背景记录；不代表没有风险';
 const ended=risks.map(risk=>risk?.entry_gate==='cooldown'&&finite(risk.cooldown_until)&&Date.now()/1000>=risk.cooldown_until);
 const signature=JSON.stringify([news.symbol,asof,risks,ended]);
 if($('news-risks').dataset.version===signature)return;
 $('news-risks').dataset.version=signature;
 if(!risks.length){$('news-risks').innerHTML='<p class="muted">当前有限来源中尚无可展示的持续背景记录；这不代表风险不存在或已经解除。</p>';return;}
 const facts={active:'事实状态：持续影响',review_required:'事实状态：尚待独立核验',resolved:'事实状态：已确认解除'};
 $('news-risks').innerHTML=risks.map((risk,index)=>{
  const url=safeNewsURL(risk.url),valid=validNewsRisk(risk,asof);
  const gate=!valid?'执行限制待核验 · 暂缓新开仓':risk.entry_gate==='background'?'持续背景 · 不单独阻止开仓':risk.entry_gate==='resolved'?'事件已解除 · 不单独阻止开仓':ended[index]?'冷静期截止已到 · 等待下一轮核验':`突发事件冷静期 · 暂缓至 ${newsDate(risk.cooldown_until)}`;
  const mentions=Array.isArray(risk.related_mentions)?risk.related_mentions:[];
  const related=mentions.map(m=>{const u=safeNewsURL(m.url);return u?`<li>${esc(m.source||'来源')} · ${esc(newsDate(m.published))} · <a href="${esc(u)}" target="_blank" rel="noopener noreferrer">相关报道</a></li>`:'';}).join('');
  const acute=valid&&risk.acute_event_at!==null;
  const acuteTime=acute?`<div><dt>${risk.time_basis==='event_time'?'事件时间':'首次新事件证据'}</dt><dd>${esc(newsDate(risk.acute_event_at))}</dd></div><div><dt>冷静期截至</dt><dd>${esc(newsDate(risk.cooldown_until))}</dd></div>`:'';
  return `<article class="persistent-risk"><div class="risk-record-head"><h4>${esc(riskSubject(risk))}</h4><span class="risk-record-status">${esc(gate)}</span></div><p class="risk-fact-status">${esc(facts[risk.status]||'事实状态：未知')}。${risk.status==='resolved'?'解除状态来自后续事实核验。':'冷静期结束不表示事件已经解除。'}</p><p>${esc(risk.reason||'尚未取得事实状态说明')}</p><p class="risk-gate-reason">${esc(valid?risk.gate_reason:'执行限制元数据不完整，等待后台重新核验')}${valid&&risk.entry_gate==='cooldown'&&ended[index]?'；仍保留暂缓，页面不会自行允许开仓。':''}</p><dl class="risk-record-times">${acuteTime}<div><dt>首次记录</dt><dd>${esc(newsDate(risk.first_seen))}</dd></div><div><dt>最近抓取</dt><dd>${esc(newsDate(risk.last_seen))}</dd></div><div><dt>来源发布时间</dt><dd>${esc(newsDate(risk.published))}</dd></div></dl><p class="risk-record-source">来源：${esc(risk.source||'未知')}${url?` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看来源证据</a>`:' · 无可核验来源链接'}</p>${related?`<details><summary>后续背景报道（不更新事件发生时间）</summary><ul>${related}</ul></details>`:''}</article>`;
 }).join('');
}
function evidenceHTML(evidence){
 const items=Array.isArray(evidence)?evidence:[];
 if(!items.length)return '';
 return `<details class="decision-evidence"><summary>查看参与决策的 ${items.length} 条正文证据</summary>${items.map(item=>{
  const link=safeNewsURL(item.url),title=esc(item.title||item.source||'正文证据');
  const direction=item.direction==='BUY'?'短线利多':item.direction==='SELL'?'短线利空':'方向待核验';
  return `<div class="evidence-item"><strong>${direction} · ${esc(item.source||'来源待确认')}</strong>${item.reason?`<p>${esc(item.reason)}</p>`:''}${item.excerpt?`<blockquote>${esc(item.excerpt)}</blockquote>`:''}<small>${finite(item.event_at)?'事件时间 '+esc(new Date(item.event_at*1000).toLocaleString()):'事件时间待确认'}</small>${link?`<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${title}</a>`:''}</div>`;
 }).join('')}</details>`;
}
function renderNews(news){
 const state=newsState(news),decision=news.decision;
 const stale=!news.updated||Date.now()/1000-news.updated>65;
 $('news-status').textContent=`${news.symbol||''} · 检查 ${stamp(news.checked)} · 来源更新 ${stamp(news.updated)}${stale?' · 数据延迟':''}${news.error?' · '+news.error:''}${finite(news.coverage?.fetch_duration_ms)?' · 本轮抓取 '+fmt(news.coverage.fetch_duration_ms/1000,2)+'秒':''}${finite(news.coverage?.newest_publication_age_seconds)?' · 最新报道距今 '+fmt(news.coverage.newest_publication_age_seconds/60,1)+'分钟（非事件延迟）':''}`;
 const confidence=finite(decision?.confidence)?` · 规则置信评分 ${fmt(decision.confidence<=1?decision.confidence*100:decision.confidence,0)}%` : decision?.confidence?` · ${decision.confidence}`:'';
 $('news-decision').className='news-decision'+(state.tone?' '+state.tone:'');
 $('news-decision').textContent=`${state.label}${confidence}。${state.reason}${decision?.expires?` · 短线有效至 ${stamp(decision.expires)}`:''}`;
 $('news-coverage').textContent=renderCoverage(news.coverage);
 renderNewsDrivers(news);renderPersistentRisks(news);
 const evidenceSignature=JSON.stringify([news.symbol,decision?.evidence]);
 if($('news-evidence').dataset.version!==evidenceSignature){$('news-evidence').dataset.version=evidenceSignature;$('news-evidence').innerHTML=evidenceHTML(decision?.evidence);}
 const signature=JSON.stringify([news.symbol,news.data,state.label]);
 if($('news-analysis').dataset.version===signature)return;
 $('news-analysis').dataset.version=signature;
 $('news-analysis').innerHTML=news.data?.length?news.data.map(item=>{
  const href=safeNewsURL(item.url),title=esc(item.title||'未命名报道');
  const read=item.fulltext===true||item.evidence_level==='正文已读取';
  const badge=read?'正文已读取':'仅标题/摘要';
  const explanation=item.reason||item.analysis||'等待正文核验';
  const evidence=Array.isArray(item.events)?item.events:[];
  const details=evidence.map(value=>[value.reason,value.excerpt].filter(Boolean).join('：')).filter(Boolean);
  const time=finite(item.article_published)?`正文发布时间 ${new Date(item.article_published*1000).toLocaleString()}`:'正文发布时间待核实';
  return `<article class="news-item">${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${title}</a>`:`<span class="news-title">${title}</span>`}<small>${esc(item.source||'来源待核验')} · ${finite(item.published)?esc(new Date(item.published*1000).toLocaleString()):'发布时间待确认'}</small><div class="news-evidence-meta"><span class="tag">${badge}</span><span>${esc(time)}</span></div><p>${esc(explanation)}${item.uncertain?' · 内容仍有不确定性':''}</p>${details.length?`<details><summary>查看核验依据</summary><ul>${details.map(text=>`<li>${esc(text)}</li>`).join('')}</ul></details>`:''}</article>`;
 }).join(''):(news.updated?(state.path==='kline'?'暂无符合条件的短线新闻方向；已允许按K线判断，等待已收盘突破与风控条件，不会立即下单。':state.ready?'暂无可展示的报道列表；已核验的方向依据请查看上方正文证据。':'暂无符合条件的短线新闻证据，当前未获准开仓；持续背景与K线许可以上方核验结果为准。'):'等待当前品种新闻');
}

function renderEnvironment(data){
 const s=data.state,live=data.mode==='live',labels={paper:'本地模拟 · 模拟资金',live:'币安实盘 · 真实资金',demo:'币安 Demo · 测试资金'};
 const modes=(Array.isArray(data.available_modes)?data.available_modes:[data.mode]).filter(mode=>Object.hasOwn(labels,mode));
 const signature=JSON.stringify(modes);
 if($('execution-mode').dataset.modes!==signature){$('execution-mode').dataset.modes=signature;$('execution-mode').innerHTML=modes.map(mode=>`<option value="${mode}">${labels[mode]}</option>`).join('');}
 $('execution-mode').value=data.mode;
 const exposed=s.enabled||!!s.position||!!s.pending;
 $('execution-mode').disabled=modeSwitching||exposed||modes.length<2;
 $('mode').textContent=labels[data.mode]||'环境未确认';$('mode').classList.toggle('live',live);
 $('execution-note').textContent=live?'实盘资金 · 可自动下单':data.mode==='demo'?'Demo测试资金':'模拟资金 · 实盘只读';
 $('mode-description').textContent=live?'真实账户与模拟账本分开保存。切换和连接不启动策略；有持仓、待核对订单或策略运行时不能切换环境。':data.mode==='demo'?'当前为独立Demo测试环境，仅使用测试账户资金。':'使用本地模拟资金。实盘API连接在本环境中只用于读取账户与风控资料。';
 $('start').textContent=live?'确认并启动实盘自动交易':data.mode==='demo'?'启动 Demo 自动交易':'启动自动模拟';
 $('start').classList.toggle('live-action',live);
 $('close').textContent=live?'停止策略并平掉实盘持仓':'停止策略并平仓';
 $('apply-leverage').hidden=!live;$('leverage-applied').hidden=!live;
 $('connection').textContent=live?(data.live_connected?'实盘交易连接已配置':'实盘交易连接未配置'):data.mode==='demo'?(data.credentials?'Demo API密钥已配置':'Demo API密钥未配置'):'正式市场公共行情 · 无需密钥';
 $('api-description').textContent=live&&!s.enabled&&(s.position||s.pending)?'已有实盘持仓或待核对订单。请用原账户的相同HMAC密钥重新连接，恢复已有仓位的核验与监控；重新连接不会恢复新开仓。策略运行时不能替换连接。':live?'币安正式账户 · HMAC 密钥。请启用所需的合约交易权限，不需要提现权限。连接时仅查询并核验账户；启动策略后会发送真实订单，应用杠杆会修改交易所设置。':'币安正式账户 · HMAC 密钥。当前连接只查询账户与风控资料，请使用仅开启读取权限的密钥，不需要交易或提现权限。';
 $('api-secret-note').textContent=(live?'实盘支持条件：单币保证金（单资产）＋单向持仓。它们是两项账户模式；逐仓／全仓属于另一项保证金方式设置。连接仅检查，不会自动切换币安账户模式。\n':'')+'密钥仅保存在本次后台内存，重启后需重新输入；新开仓保持暂停。';
 const identityReady=live&&data.credential_rotation_supported===true&&!s.enabled&&!s.position&&!s.pending;
 $('bind-account-identity').hidden=!live;$('rotate-key-label').hidden=!live;
 $('bind-account-identity').disabled=busy||!identityReady||!data.live_connected;
 $('rotate-key').disabled=busy||!identityReady||!data.account_identity_bound;
 if($('rotate-key').disabled)$('rotate-key').checked=false;
 $('account-identity-status').textContent=!live?'':!data.credential_rotation_supported?'密钥轮换需新版后台重启生效。':data.account_identity_bound?'原账户身份已记录；轮换仍须暂停、空仓且无待核对订单。':'请在原密钥有效时先核验账户身份；原密钥失效且没有身份记录时无法安全轮换。';
 const storage=data.credential_storage;
 if(storage)$('api-secret-note').textContent='已启用 macOS 钥匙串保存；连接成功后保存，重启只读核验恢复，新开仓仍暂停。支持单币保证金及单向持仓。';
 if($('credential-storage-status'))$('credential-storage-status').textContent=storage?Object.entries({live:'实盘密钥',readonly:'只读密钥',model:'模型密钥'}).map(([k,n])=>n+'：'+(storage.saved?.[k]?'已保存':'未保存')+(storage.restored?.[k]?' · 已恢复':'')+(storage.errors?.[k]?' · '+storage.errors[k]:'')).join('；'):'此后台尚未启用钥匙串保存';
 if($('forget-credentials'))$('forget-credentials').disabled=!storage||!!s.enabled||!!s.position||!!s.pending;

 $('connect-api').textContent=live&&!s.enabled&&(s.position||s.pending)?'重新连接并恢复持仓监控':live?'连接实盘交易账户（保持暂停）':'连接实盘账户（只读）';
 $('api-status').textContent=live?(data.live_connected?`实盘密钥已配置 · ${data.account_bound?'账户已绑定':'账户待核验'} · ${s.enabled?'策略正在运行':'策略已暂停'}`:'尚未连接实盘交易账户'):(data.readonly_connected?'实盘只读连接已配置 · 不发送交易订单':'尚未连接实盘只读账户');
 $('account').textContent=live?'查询实盘账户余额':'检查实盘只读账户';
 // After a restart, a live position may remain while credentials exist only in memory.
 // Reconnecting must stay available so the server can verify ownership and restore protection.
 const credentialsLocked=modeSwitching||(live&&s.enabled);
 $('connect-api').disabled=busy||credentialsLocked;
 $('api-key').disabled=credentialsLocked;$('api-secret').disabled=credentialsLocked;
}
function renderAccountMetrics(data){
 const live=data.mode==='live',bound=!live||(data.live_connected&&data.account_bound);
 const initial=finite(data.initial_equity)?Number(data.initial_equity):finite(data.state.initial_equity)?Number(data.state.initial_equity):data.mode==='paper'?300:null;
 const equity=bound&&finite(data.equity)?Number(data.equity):null;
 const lossLimit=(!live||data.account_bound)&&finite(data.loss_limit)?Number(data.loss_limit):data.mode==='paper'?45:null;
 const dailyLimit=(!live||data.account_bound)&&finite(data.daily_loss_limit)?Number(data.daily_loss_limit):data.mode==='paper'?6:null;
 $('equity-label').textContent=live?'U本位合约 USDT 权益':'策略权益';
 $('equity').textContent=metric(equity,live?8:2);
 $('pnl').textContent=finite(initial)&&finite(equity)?`${live?'相对绑定时权益':'累计'} ${fmt(equity-initial,live?8:2)} USDT · 初始 ${fmt(initial,live?8:2)}`:live?'连接并核验后显示真实账户余额':'等待权益基准';
 const remaining=finite(initial)&&finite(equity)&&finite(lossLimit)?Math.max(0,Math.min(lossLimit,lossLimit-(initial-equity))):null;
 $('capital-budget').hidden=!live;
 const capitalLimit=finite(data.capital_limit)?` · 资金上限 ${fmt(data.capital_limit)} USDT`:'';
 $('capital-budget').textContent=(data.account_bound&&finite(data.capital_budget)?`策略资金预算 ${fmt(data.capital_budget,Number(data.capital_budget)<1?8:2)} USDT`:'策略资金预算待核验')+capitalLimit;
 $('budget').textContent=metric(remaining,live&&remaining>0&&remaining<1?8:2);
 $('budget-description').textContent=finite(lossLimit)?`累计亏损达 ${fmt(lossLimit,live&&lossLimit>0&&lossLimit<1?8:2)} USDT 时停止并尝试平仓`:'账户绑定后计算累计亏损阈值';
 $('daily-limit-description').textContent=`暂停后继续保护已有持仓。${finite(dailyLimit)?`每天亏损达 ${fmt(dailyLimit,live&&dailyLimit>0&&dailyLimit<1?8:2)} USDT 即停止开仓并尝试平仓，按UTC换日。`:'账户绑定后计算每日亏损阈值。'}`;
}

let aiPublicStatus=null,aiFormDirty=false,aiBusy=false,aiStatusRequest=null,aiStatusSequence=0;
const AI_DEFAULT_BASE_URL='http://127.0.0.1:8080/v1',AI_DEFAULT_MODEL='gpt-6.1-sol';
function aiSafeFailure(reason=''){
 if(/证据|引用/.test(reason))return '模型输出未通过证据或引用校验；无需因此重新填写密钥，可仅重试当前失败品种。';
 if(/限流|429|额度/.test(reason))return '模型服务限流或额度不足，请检查服务额度。';
 if(/格式|结构/.test(reason))return '模型返回结构不符合要求，当前结论不可用。';
 if(/超时|网络/.test(reason))return '模型网络请求未完成，可仅重试当前失败品种。';
 return '模型连接或分析未完成；同一输入不会自动反复重试。可测试连接或仅重试当前失败品种。';
}
function aiDirection(value){return ({bullish:'偏多',bearish:'偏空',mixed:'方向分歧',uncertain:'方向不确定'})[value]||'方向未确认';}
function aiSourceURL(value){
 const candidate=safeNewsURL(value);if(!candidate)return null;
 const url=new URL(candidate);return url.username||url.password?null:candidate;
}
function renderAIEvents(html){
 const container=$('ai-events');
 if(container.dataset.version!==html){container.innerHTML=html;container.dataset.version=html;}
}
function renderAIAnalysis(value){
 const result=value&&typeof value==='object'?value:{status:'disabled',trading_authority:false};
 let status=result.status||'disabled';
 if(result.symbol&&latest?.state.symbol&&result.symbol!==latest.state.symbol)status='pending';
 if(result.trading_authority!==false)status='error';
 if(['ok','historical'].includes(status)){
  if(typeof result.analyzed_at!=='number'||!Number.isFinite(result.analyzed_at)||result.analyzed_at>Date.now()/1000+5)status='stale';
  else if(!(typeof result.retained_input_checked_at==='number'&&Date.now()/1000-result.retained_input_checked_at>=0&&Date.now()/1000-result.retained_input_checked_at<90)&&Date.now()/1000-result.analyzed_at>=900)status='historical';
 }
 const labels={standby:'轻量监测 · 节省用量',disabled:'未启用',pending:'等待分析',analyzing:'分析中',ok:'解读已更新',historical:'历史解读',unchanged:'无新增新闻',error:'暂不可用',stale:'结果暂不可用',no_news:'暂无可分析输入'};
 if(!Object.hasOwn(labels,status))status='error';
 latestAI={...result,status};
 if(latestNews?.symbol)renderNewsDrivers(latestNews);
 setBadge('ai-badge',labels[status],['error','stale'].includes(status)?'blocked':'');
 const messages={standby:'未持有品种暂停常规模型分析，新闻采集及本地重大事件监测继续；新重大事件可触发复核。',disabled:'填写模型 API Key 并启用后，自动解读当前品种新闻与统计数据，无需逐条人工确认。',pending:'发现新增新闻或数据修订，等待自动解读，无需人工批准。',analyzing:'模型正在自动解读；完成后直接显示。',ok:'可按需展开原文核验；不核验也会继续自动分析。无新增新闻或数据修订时复用原解读，不重复调用模型。',historical:'以下为历史新闻解读，不代表当前方向。无新增新闻或数据修订，不因时间流逝重调模型。',unchanged:'没有新增新闻或数据修订，不重复调用模型；暂无可展示结果。',error:aiSafeFailure(result.reason),stale:'新闻输入或结果时间异常，暂不显示解读；恢复来源后再检查新增新闻。',no_news:'当前没有可分析的新闻正文或有效统计值，不调用模型。'};
 $('ai-analysis-status').textContent=messages[status];
 $('ai-direction').textContent='—';$('ai-summary').textContent=messages[status];
 $('ai-uncertainties').innerHTML='';
 const model=typeof result.model==='string'&&result.model?result.model:'模型未配置';
 $('ai-analysis-time').textContent=`${model} · ${finite(result.analyzed_at)?newsDate(result.analyzed_at):'尚无分析时间'}${result.cached?' · 复用缓存结果':''}${finite(result.input_asof)?' · 输入截至 '+newsDate(result.input_asof):''}${typeof result.latency_ms==='number'?' · 完整分析 '+fmt(result.latency_ms/1000,2)+'秒':''}${finite(result.queue_wait_ms)?' · 排队 '+fmt(result.queue_wait_ms/1000,2)+'秒':''}${finite(result.processing_ms)?' · 处理中 '+fmt(result.processing_ms/1000,1)+'秒':''}`;
 if(!['ok','historical'].includes(status)){renderAIEvents('');return;}
 $('ai-direction').textContent=status==='historical'?'历史解读 · 非当前方向':aiDirection(result.direction);
 $('ai-summary').textContent=typeof result.summary==='string'&&result.summary?result.summary:'模型未提供摘要。';
 const events=Array.isArray(result.events)?result.events:[];
 if(status==='ok'&&events.some(event=>event?.evidence_valid===false))setBadge('ai-badge','部分引用未通过','blocked');
 const classifications={new_event:'模型识别：新事件',ongoing_background:'模型识别：持续背景',historical:'模型识别：历史事件',uncertain:'模型识别：尚不确定'};
 renderAIEvents(events.map(event=>{
  if(!event||typeof event!=='object')return '';
  const url=aiSourceURL(event.url),classification=classifications[event.classification]||'模型分类未确认';
  if(event.evidence_valid===false)return `<article class="ai-event"><strong>引用校验未通过 · 该事件结论未采纳</strong><p>模型引文与原始正文不匹配；对应事件及依赖它的因素解读已隔离，其他独立分析与原始统计保留。</p><small>来源：${esc(event.source||'未提供')} · 报道发布 ${esc(newsDate(event.published))}</small>${url?`<details class="ai-review"><summary>人工核验（可选）</summary><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看原文来源</a></details>`:''}</article>`;
  const eventTime=typeof event.event_time==='number'&&finite(event.event_time)?newsDate(event.event_time):typeof event.event_time==='string'&&event.event_time?event.event_time:'未提供';
  return `<article class="ai-event"><div class="ai-event-head"><strong>${classification}</strong><span>${aiDirection(event.direction)}</span></div><p>${esc(typeof event.impact==='string'?event.impact:'模型未说明影响')}</p><small>来源：${esc(event.source||'未提供')} · 报道发布 ${esc(newsDate(event.published))}</small><details class="ai-review"><summary>人工核验（可选）</summary>${event.quote_omitted===true?'<small>完整引文已与正文匹配；因展示长度限制省略，请打开原文查看完整上下文。原文匹配不等于事实已独立核实。</small>':typeof event.quote==='string'&&event.quote?`<blockquote>${esc(event.quote)}</blockquote>`:''}<small>模型提及的事件时间（未独立核验）：${esc(eventTime)}</small>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看原文来源</a>`:'<small>没有可安全打开的原文链接</small>'}</details></article>`;
 }).join(''));
 const uncertainties=Array.isArray(result.uncertainties)?result.uncertainties.filter(item=>typeof item==='string'&&item):[];
 $('ai-uncertainties').innerHTML=uncertainties.length?`<h3>模型指出的不确定性</h3><ul>${uncertainties.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>`:'';
}
function renderAIConnection(status){
 const previousModel=aiPublicStatus?.model;
 aiPublicStatus=status&&typeof status==='object'?status:{};
 renderAIMonitoring(aiPublicStatus);
 const configured=aiPublicStatus.configured===true;
 const savedModel=typeof aiPublicStatus.model==='string'?aiPublicStatus.model:'';
 if(!aiFormDirty){$('ai-base-url').value=typeof aiPublicStatus.base_url==='string'&&aiPublicStatus.base_url?aiPublicStatus.base_url:AI_DEFAULT_BASE_URL;$('ai-model').value=typeof aiPublicStatus.model==='string'&&aiPublicStatus.model?aiPublicStatus.model:AI_DEFAULT_MODEL;}
 if(savedModel!==previousModel)$('ai-model-choice').value=['gpt-6-luna','gpt-6.1-sol'].includes(savedModel)?savedModel:AI_DEFAULT_MODEL;
 $('ai-model-context').textContent=configured?`已配置模型：${savedModel||'名称未返回'} · ${aiPublicStatus.enabled?'自动解读已启用':'自动解读已停用'} · 人工核验可选`:'模型尚未连接 · 输入框为待保存配置 · 人工核验可选';
 const labels={disabled:'未配置或已停用',configured:'配置已保存，等待分析',testing:'正在异步测试连接',ready:'模型连接已就绪',error:'模型连接暂不可用'};
 const state=labels[aiPublicStatus.status]||'正在等待模型状态';
 $('ai-connection-status').textContent=`${state}${configured?' · '+(aiPublicStatus.enabled?'AI分析已启用':'AI分析未启用'):''}${(aiPublicStatus.pending||aiPublicStatus.test_pending)?' · 请求已排队':''}${Number.isInteger(aiPublicStatus.request_count)?' · 本次进程总请求 '+aiPublicStatus.request_count+' 次':''}${finite(aiPublicStatus.last_success)&&aiPublicStatus.last_success>0?' · 最近成功 '+newsDate(aiPublicStatus.last_success):''}${finite(aiPublicStatus.next_allowed_at)&&aiPublicStatus.next_allowed_at>Date.now()/1000?' · 最早可再次请求 '+newsDate(aiPublicStatus.next_allowed_at):''}${finite(aiPublicStatus.last_test_latency_ms)?' · 连接测试 '+fmt(aiPublicStatus.last_test_latency_ms/1000,2)+'秒':''}${finite(aiPublicStatus.last_analysis_latency_ms)?' · 最近完整分析 '+fmt(aiPublicStatus.last_analysis_latency_ms/1000,2)+'秒':''}${Number.isInteger(aiPublicStatus.active_count)?' · 进行中 '+aiPublicStatus.active_count+'/2':''}${aiPublicStatus.status==='error'?'。'+aiSafeFailure():''}`;
 $('ai-retry').disabled=aiBusy||aiPublicStatus.retry_supported!==true||latestAI?.status!=='error';
 $('ai-secret-note').textContent=latest?.credential_storage?.backend==='macos_keychain'?'模型密钥按本机钥匙串设置保存，不回显；重启恢复连接时仍保持暂停。':'模型密钥仅保存在本次后台内存，不回显；重启后需重新填写。';
 const health=aiPublicStatus.analysis_health;if(health)$('ai-connection-status').textContent+=` · 各品种：成功 ${health.ok||0}、失败 ${health.error||0}、等待 ${(health.pending||0)+(health.analyzing||0)}、过期 ${(health.stale||0)+(health.historical||0)}`;
 $('ai-draft-status').textContent=aiFormDirty?'有未保存的模型配置；定时刷新不会覆盖你的输入。':configured?`已保存模型 ${savedModel||'名称未返回'}；密钥不回显。重新保存时请填写密钥。`:`已预填 ${AI_DEFAULT_MODEL} 与本机接口，尚未连接；填写模型 API Key 并保存后才会启用。`;
 renderAIComparison(aiPublicStatus.comparison);
 syncAIControls();
}
function renderAIMonitoring(status){
 const monitor=status.monitoring,rows=Array.isArray(monitor?.rows)?monitor.rows:[];
 if(!rows.length){$('ai-monitor-summary').textContent='等待后台返回持仓分析范围';$('ai-monitor-rows').innerHTML='';$('ai-opportunities').innerHTML='';$('ai-token-usage').textContent='';return;}
 const focused=rows.find(r=>['holding','pending'].includes(r.mode)&&r.symbol===r.focus);
 $('ai-monitor-summary').textContent=(status.enabled?'AI已启用 · ':'AI未启用 · ')+(focused?`优先监测 ${focused.symbol}；其他品种仅本地监测，重大事件可触发模型复核。`:'空仓按选品模式分析；已有持仓时自动集中模型用量。');
 const labels={holding:'持仓优先',pending:'订单核对中',flat:'候选分析',event:'重大事件复核',standby:'轻量监测',unavailable:'等待状态'};
 $('ai-monitor-rows').innerHTML=rows.map(r=>`<tr><td>${esc(r.symbol)}</td><td>${esc(labels[r.mode]||'等待状态')}</td><td>${esc(r.reason||'')}${r.inflight?' · 已发出的请求仍在完成':''}</td></tr>`).join('');
 const alerts=rows.filter(r=>r.alert&&r.symbol!==focused?.symbol);
 const alertStates={analyzing:'正在复核',ready:'已完成模型解读',error:'模型复核失败',historical:'历史事件提示'};
 $('ai-opportunities').innerHTML=alerts.length?alerts.map(r=>{
  const a=r.alert,events=Array.isArray(a.events)?a.events:[];
  return `<article class="news-item"><strong>${esc(r.symbol)} · ${esc(alertStates[a.status]||'待核验')}</strong><p>${esc(a.summary||'检测到新的重大事件线索，尚不能据此判断收益。')}</p>${events.map(e=>{const url=aiSourceURL(e.url);return `<p>${esc(e.label||'重大事件')}${url?` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看来源</a>`:''}</p>`;}).join('')}<small>${esc(a.reason||'仅提示关注，不强制平仓或换仓。')}</small></article>`;
 }).join(''):'<p class="muted">暂无未持有品种的新重大事件提示；不代表市场没有事件。</p>';
 const usage=status.usage;
 $('ai-token-usage').textContent=usage&&Number.isInteger(usage.analysis_calls)?`本次模型连接已发起 ${usage.analysis_calls} 次自动分析；${usage.reported_calls?`${usage.reported_calls} 次返回用量，输入 ${usage.prompt_tokens}、输出 ${usage.completion_tokens} tokens。`:'网关尚未返回可统计的 token 用量。'} 未返回用量的请求未计入 token 合计；这里不估算节省金额。`:'';
}
function syncAIControls(){
 const configured=aiPublicStatus?.configured===true;
 const cooldown=finite(aiPublicStatus?.next_allowed_at)&&aiPublicStatus.next_allowed_at>Date.now()/1000;
 $('ai-save').disabled=aiBusy||!latest||!!latest.state?.enabled||!!latest.state?.position||!!latest.state?.pending;
 $('ai-test').disabled=aiBusy||!latest||!configured||aiPublicStatus?.pending===true||aiPublicStatus?.test_pending===true||aiPublicStatus?.status==='testing'||cooldown;
 $('ai-disconnect').disabled=aiBusy||!latest||!configured;
 const official=configured&&aiPublicStatus.base_url==='https://api.openai.com/v1';
 $('ai-provider-note').textContent=configured&&!official?'当前本地网关不支持本程序的官方模型对比和切换；暂停或平仓不会解除此限制。':'';
 $('ai-compare').disabled=aiBusy||!latest||!official||['pending','running'].includes(aiPublicStatus.comparison?.status);
 $('ai-model-switch').disabled=aiBusy||!latest||!official||!!latest.state?.enabled||!!latest.state?.position||!!latest.state?.pending;
 ['ai-base-url','ai-model','ai-api-key'].forEach(id=>$(id).disabled=aiBusy);
}
async function refreshAIStatus(){
 if(aiStatusRequest)return aiStatusRequest;
 const sequence=aiStatusSequence;
 aiStatusRequest=(async()=>{
  try{
   const response=await fetch('/api/ai/status');if(!response.ok)throw Error('status unavailable');
   const data=await response.json();if(sequence!==aiStatusSequence)return;
   renderAIConnection(data);
  }catch{
   if(sequence!==aiStatusSequence)return;
   $('ai-connection-status').textContent='无法读取AI连接状态；执行引擎仍核验方向时效，失效时使用健康K线备用路径。';
   renderAIMonitoring({});
   $('ai-test').disabled=true;$('ai-disconnect').disabled=true;
  }
 })().finally(()=>{aiStatusRequest=null;});
 return aiStatusRequest;
}
$('ai-config').addEventListener('input',()=>{aiFormDirty=true;$('ai-draft-status').textContent='有未保存的模型配置；定时刷新不会覆盖你的输入。';});
$('ai-config').addEventListener('submit',async event=>{
 event.preventDefault();if(aiBusy||busy||!latest)return;
 const body={base_url:$('ai-base-url').value.trim(),model:$('ai-model').value.trim(),api_key:$('ai-api-key').value,enabled:true};
 $('ai-api-key').value='';aiBusy=true;aiStatusSequence++;syncAIControls();$('ai-action-result').textContent='正在保存本机模型配置…';
 try{
  const result=await post('ai/config',body);
  if(result){aiFormDirty=false;renderAIConnection(result);renderAIAnalysis({status:'pending',symbol:latest?.state.symbol,model:result.model,trading_authority:false});$('ai-action-result').textContent='已保存并启用 AI 分析；后台会异步处理公开新闻，交易策略的启停状态不变。';}
  else{$('ai-action-result').textContent=lastError||aiSafeFailure();}
 }finally{body.api_key='';aiBusy=false;syncAIControls();}
});
$('ai-test').onclick=async()=>{
 if(aiBusy||busy||!latest)return;aiBusy=true;aiStatusSequence++;syncAIControls();
 try{
  const result=await post('ai/test');
  if(result){renderAIConnection(result);$('ai-action-result').textContent='连接测试已提交；结果会异步更新，可能产生少量模型调用费用。';}
  else $('ai-action-result').textContent=lastError||aiSafeFailure();
 }finally{aiBusy=false;syncAIControls();}
};
$('ai-disconnect').onclick=async()=>{
 if(aiBusy||busy||!latest)return;aiBusy=true;aiStatusSequence++;$('ai-api-key').value='';syncAIControls();
 try{
  const result=await post('ai/disconnect');
  if(result){aiFormDirty=false;renderAIConnection(result);renderAIAnalysis({status:'disabled',trading_authority:false});$('ai-action-result').textContent='已停用 AI 并清除模型连接、密钥及缓存；AI方向已撤销；继续新闻规则与健康K线备用路径。';}
  else $('ai-action-result').textContent=lastError||aiSafeFailure();
 }finally{aiBusy=false;syncAIControls();}
};
refreshAIStatus();setInterval(refreshAIStatus,5000);

function renderAIComparison(value){
 const target=$('ai-comparison-results');
 if(!value){target.textContent='尚未测试；一次样本不能证明预测准确率。';return;}
 const states={pending:'等待对比',running:'正在对比',complete:'对比完成',expired:'对比输入过期，请等待新材料'};
 const rows=Array.isArray(value.rows)?value.rows:[];
 target.innerHTML=`<p>${esc(value.symbol||'')} · ${esc(states[value.status]||'状态未知')} · 材料冻结 ${esc(newsDate(value.started_at))}</p>`+rows.map(row=>`<article class="factor-data"><strong>${esc(row.model||'模型')}</strong><p>耗时 ${typeof row.latency_ms==='number'?esc(fmt(row.latency_ms/1000,2)):'未知'} 秒 · ${row.status==='ok'?'结构校验完成':'请求或结构校验失败'}</p>${row.status==='ok'?`<p>原文匹配 ${esc(row.matched_events)}/${esc(row.events)} 条（无引文时不计算匹配率）；因素 ${esc(row.factors)} 项，引用未通过 ${esc(row.unverified_factors)} 项。综合方向：${esc(aiDirection(row.direction))}</p><p>${esc(row.summary||'')}</p>`:''}</article>`).join('');
}
$('ai-compare').onclick=async()=>{
 if(aiBusy||busy||!latest)return;aiBusy=true;aiStatusSequence++;syncAIControls();
 try{const result=await post('ai/compare');if(result){renderAIConnection(result);$('ai-action-result').textContent='已排队：同批公开材料各调用一次；正常分析优先，对比不会更改交易决策。';}else $('ai-action-result').textContent=lastError||aiSafeFailure();}
 finally{aiBusy=false;syncAIControls();}
};
$('ai-model-switch').onclick=async()=>{
 if(aiBusy||busy||!latest)return;aiBusy=true;aiStatusSequence++;syncAIControls();
 try{const result=await post('ai/model',{model:$('ai-model-choice').value});if(result){aiFormDirty=false;renderAIConnection(result);renderAIAnalysis({status:'pending',symbol:latest.state.symbol,model:result.model,trading_authority:false});$('ai-action-result').textContent='模型已切换，等待新分析；策略保持暂停。';}else $('ai-action-result').textContent=lastError||aiSafeFailure();}
 finally{aiBusy=false;syncAIControls();}
};

if($('forget-credentials'))$('forget-credentials').onclick=async()=>{const result=await post('credentials/forget',{slot:latest.mode==='live'?'live':'readonly'});$('account-result').textContent=result?'已删除保存的币安密钥；当前连接保留，下次重启需重新输入。':lastError||'保存记录未能清除';};

$('ai-retry').onclick=async()=>{if(aiBusy||busy||!latest)return;aiBusy=true;try{const result=await post('ai/retry',{symbol:latest.state.symbol});if(result){renderAIConnection(result);toast('仅当前失败品种已排队重试，可能产生模型费用');}}finally{aiBusy=false;syncAIControls();}};
