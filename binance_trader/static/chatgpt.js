/* Chat UI has no order actions and never receives OAuth tokens. */
(()=>{
 'use strict';
 const el=id=>document.getElementById(id);
 let state=null,posting=false,polling=false,viewKey='',modelKey='',accountKey='',welcomeId='',restoreModels='',previewAt=0;
 const safeURL=value=>{try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:null;}catch{return null;}};
 const escape=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 function markdown(value){
  // Escape all text, then allow a small fixed set of tags and HTTPS links.
  const format=s=>escape(s).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
  const inline=s=>{
   let out='',at=0;const matches=String(s).matchAll(/\[([^\]\n]{1,250})\]\(([^\s)]{1,2048})\)/g);
   for(const m of matches){out+=format(s.slice(at,m.index));const url=safeURL(m[2]);out+=url?'<a href="'+escape(url)+'" rel="noreferrer noopener">'+format(m[1])+'</a>':format(m[0]);at=m.index+m[0].length;}
   return out+format(s.slice(at));
  };
  const lines=String(value).split('\n'),result=[];
  for(let i=0;i<lines.length;i++){
   if(lines[i].trim().startsWith('|')&&i+1<lines.length&&/^\s*\|?[\s:|-]+\|[\s:|-]+$/.test(lines[i+1])){
    const cells=s=>s.trim().replace(/^\||\|$/g,'').split('|').map(x=>x.trim());
    let table='<div class="chatgpt-table"><table><thead><tr>'+cells(lines[i]).map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>';i++;
    while(i+1<lines.length&&lines[i+1].trim().startsWith('|')){i++;table+='<tr>'+cells(lines[i]).map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>';}
    result.push(table+'</tbody></table></div>');
   }else if(/^#{1,4}\s/.test(lines[i]))result.push('<p class="chatgpt-subtitle">'+inline(lines[i].replace(/^#{1,4}\s/,''))+'</p>');
   else if(lines[i].trim())result.push('<p>'+inline(lines[i])+'</p>');
  }
  return result.join('');
 }
 function error(message){el('chatgpt-error').textContent=message||'';}
 async function action(name,body={}){
  if(posting)return null;
  posting=true;sync();error('');
  try{
   const context=requestContext();
   if(!contextMatches(context))throw Error('交易器状态尚未载入，请稍后再试。');
   const r=await fetch('/api/chatgpt/'+name,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({...context,...body})});
   const d=await r.json();if(!r.ok)throw Error(d.error||'ChatGPT 操作未完成。');render(d);return d;
  }catch(e){error(e.message||'ChatGPT 操作未完成；没有自动重试。');return null;}
  finally{posting=false;sync();}
 }
 function sync(){
  const a=state?.auth||{},working=!!state?.busy||!!state?.operation||!!a.login_pending;
  el('chatgpt-login').disabled=posting||working;
  el('chatgpt-logout').disabled=posting||!!state?.operation||(!a.connected&&!a.login_pending);
  el('chatgpt-models-refresh').disabled=posting||working||!a.plan_enabled;
  el('chatgpt-model').disabled=posting||working||!(a.models||[]).length;
  el('chatgpt-send').disabled=posting||working||!a.plan_enabled||!el('chatgpt-model').value;
  el('chatgpt-cancel').disabled=posting||!state?.busy;
  el('chatgpt-clear').disabled=posting;
  el('chatgpt-account').disabled=posting||working;
  el('chatgpt-web').disabled=posting||!!state?.busy;
 }
 function render(d){
  state=d;if(d.unavailable){error('当前后台版本未载入 ChatGPT 聊天，请重启新版本 App。');sync();return;}
  const a=d.auth||{};
  el('chatgpt-sol-status').textContent=(a.models||[]).some(m=>m.slug==='gpt-6.1-sol')?
   'GPT-6.1 Sol 已由当前账户授权，可在上方选择；发送后会显示实际返回的模型。':
   'GPT-6.1 Sol 尚未出现在当前 ChatGPT 账户的可用列表中，无法在此启用。可刷新列表重新检查；下方策略 API 的同名模型属于另一条连接。';
  el('chatgpt-badge').textContent=a.login_pending?'等待登录':a.plan_enabled?'使用 ChatGPT 套餐':a.connected?'未授权套餐':'未登录';
  el('chatgpt-account-status').textContent=(a.email?'当前账户：'+a.email+'。':'')+(a.login_pending?'请在官方浏览器页面完成登录与授权。':a.plan_enabled?'已连接 ChatGPT，发送消息后开始分析。':a.connected?'已登录，仍需授权使用套餐。':'点击 Continue with ChatGPT 登录。')+(a.error?' '+a.error:'');
  el('chatgpt-plan-label').textContent=a.plan_enabled?'Using ChatGPT plan · 使用现有套餐额度':'登录后使用 ChatGPT 套餐';
  const ak=JSON.stringify([a.accounts,a.account_id]);
  if(ak!==accountKey){accountKey=ak;el('chatgpt-account').replaceChildren();
   const add=document.createElement('option');add.value='';add.textContent='添加账户 / 工作区';el('chatgpt-account').append(add);
   for(const item of a.accounts||[]){const o=document.createElement('option');o.value=item.id;o.textContent=item.label;el('chatgpt-account').append(o);}
   el('chatgpt-account').value=a.account_id||'';
  }
  const mk=JSON.stringify([a.account_id,a.models]);
  if(mk!==modelKey){modelKey=mk;const old=el('chatgpt-model').value;el('chatgpt-model').replaceChildren();
   for(const m of a.models||[]){const o=document.createElement('option');o.value=m.slug;o.textContent=m.display_name+' · '+m.slug;el('chatgpt-model').append(o);}
   const slugs=(a.models||[]).map(m=>m.slug);
   if(!slugs.length){const o=document.createElement('option');o.value='';o.textContent='登录后读取账户可用模型';el('chatgpt-model').append(o);}
   el('chatgpt-model').value=slugs.includes(old)?old:slugs.includes('gpt-6-astra')?'gpt-6-astra':slugs[0]||'';
  }
  const login=safeURL(a.login_url);
  el('chatgpt-login-link').hidden=!login;
  if(login&&new URL(login).hostname==='auth.openai.com')el('chatgpt-login-link').href=login;
  const key=JSON.stringify([d.session,d.turns]);
  if(key!==viewKey){viewKey=key;const box=el('chatgpt-messages'),atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<100;box.replaceChildren();
   for(const t of d.turns||[]){
    const article=document.createElement('article');article.className='chatgpt-turn';
    const q=document.createElement('div');q.className='chatgpt-question';q.textContent=t.question;article.append(q);
    const meta=document.createElement('p');meta.className='muted';
    const labels={preparing:'正在整理五品种资料',running:'正在分析 · 尚未完成',complete:'分析已完成',error:'未完成',cancelled:'已停止接收'};
    meta.textContent=(t.actual_model||t.model)+' · '+(labels[t.status]||t.status)+(t.context?' · 快照 '+new Date(t.context.asof_utc).toLocaleString():'')+(t.elapsed_ms?' · '+(t.elapsed_ms/1000).toFixed(1)+'秒':'');article.append(meta);
    const answer=document.createElement('div');answer.className='chatgpt-answer';answer.innerHTML=markdown(t.answer||'Preparing…');article.append(answer);
    if(t.error){const p=document.createElement('p');p.className='chatgpt-error';p.textContent=t.error;article.append(p);}
    if(t.context){const p=document.createElement('p');p.className='muted';p.textContent='已附 '+t.context.symbols.length+' 个品种 · '+t.context.articles+' 篇去重新闻 · '+t.context.metrics+' 项去重统计。';article.append(p);}
    for(const c of t.citations||[]){const url=safeURL(c.url);if(!url)continue;const link=document.createElement('a');link.href=url;link.rel='noreferrer noopener';link.className='chatgpt-link';link.textContent=c.title||'查看来源';article.append(link);}
    box.append(article);
   }
   if(!(d.turns||[]).length){const p=document.createElement('p');p.className='muted';p.textContent='可以问：比较五个品种，哪些有明确趋势？哪些消息仍需核实？';box.append(p);}
   if(atBottom)box.scrollTop=box.scrollHeight;
  }
  if(d.error)error(d.error);
  if(a.welcome&&welcomeId!==a.account_id){welcomeId=a.account_id;const dialog=el('chatgpt-welcome');if(!dialog.open)dialog.showModal();}
  sync();
  if(a.plan_enabled&&!a.login_pending&&!(a.models||[]).length&&!d.operation&&restoreModels!==a.account_id){restoreModels=a.account_id;setTimeout(()=>action('models'),500);}
 }
 async function poll(){
  if(previewAt&&Date.now()-previewAt>15000){el('chatgpt-context-time').textContent='· 预览已过期，请刷新；发送时会重新采集';el('chatgpt-context-rows').textContent='旧预览已隐藏，不能据此判断当前资料新鲜度。';}
  if(polling)return;polling=true;
  try{const r=await fetch('/api/chatgpt/status');if(!r.ok)throw Error();render(await r.json());}
  catch{state=null;sync();error('聊天服务暂不可用，请确认新版交易器正在运行。');}
  finally{polling=false;}
 }
 async function preview(){
  try{const r=await fetch('/api/chatgpt/context');if(!r.ok)throw Error();const c=await r.json();
   previewAt=Date.parse(c.asof_utc)||0;
   el('chatgpt-context-time').textContent='· 该时刻的预览 '+new Date(c.asof_utc).toLocaleTimeString();el('chatgpt-context-rows').replaceChildren();
   const labels={fresh:'新鲜',stale:'过期',unavailable:'缺失 / 异常'};
   for(const s of c.symbols||[]){const p=document.createElement('p');p.textContent=s.symbol+' · K线 '+(labels[s.kline_status]||'未知')+' · 报价 '+(labels[s.quote_status]||'未知')+' · 新闻 '+(labels[s.news_status]||'未知')+' · '+s.metrics+'项统计';el('chatgpt-context-rows').append(p);}
  }catch{el('chatgpt-context-rows').textContent='暂时无法读取公开资料。';}
 }
 el('chatgpt-login').onclick=()=>action('login',{account_id:el('chatgpt-account').value||null});
 el('chatgpt-logout').onclick=()=>action('logout');
 el('chatgpt-models-refresh').onclick=()=>action('models');
 el('chatgpt-cancel').onclick=()=>action('cancel');
 el('chatgpt-clear').onclick=()=>action('clear');
 el('chatgpt-context-refresh').onclick=preview;
 el('chatgpt-welcome-ok').onclick=async()=>{el('chatgpt-welcome').close();await action('welcome');};
 el('chatgpt-model').onchange=sync;
 el('nav-chatgpt').onclick=()=>el('chatgpt-panel').scrollIntoView({behavior:'smooth',block:'start'});
 el('chatgpt-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!state||posting)return;
  const question=el('chatgpt-question').value.trim();if(!question)return;
  const d=await action('send',{message:question,model:el('chatgpt-model').value,web_search:el('chatgpt-web').checked,session:state.session,request_id:crypto.randomUUID()});
  if(d)el('chatgpt-question').value='';
 });
 poll();preview();setInterval(poll,1500);
})();
