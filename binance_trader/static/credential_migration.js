'use strict';
const el=id=>document.getElementById(id),token=document.querySelector('meta[name="csrf"]').content;
function render(s){
 const labels={live:'币安实盘',readonly:'币安只读',model:'模型 API'};
 el('storage-status').textContent=Object.entries(labels).map(([k,n])=>`${n}：${s.saved?.[k]?'已保存并读回核验':'未保存'}${s.errors?.[k]?' · '+s.errors[k]:''}`).join('；');
}
async function save(event,slot,payload,fields,resultId){
 event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;
 el(resultId).textContent='正在检查连接并保存…';
 try{
  const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({slot,...payload})});
  const d=await r.json();if(d.storage)render(d.storage);if(!r.ok)throw Error(d.error);
  for(const id of fields)el(id).value='';
  el(resultId).textContent='已保存到本机钥匙串，并经独立进程读回校验。';
 }catch(e){el(resultId).textContent=e.message||'保存未完成';}finally{button.disabled=false;}
}
el('binance').addEventListener('submit',e=>save(e,'live',{key:el('key').value,secret:el('secret').value},['key','secret'],'binance-result'));
el('model-form').addEventListener('submit',e=>save(e,'model',{base_url:el('base-url').value,model:el('model').value,api_key:el('api-key').value},['api-key'],'model-result'));
fetch('/api/status').then(r=>r.json()).then(render).catch(()=>{el('storage-status').textContent='保存服务不可用';});
