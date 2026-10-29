'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync(__dirname+'/../static/app.js','utf8');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function harness(){
 const nodes=new Map(),requests=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',options:[],dataset:{},style:{},classList:{toggle(){},add(){}},addEventListener(){},append(){},setAttribute(){}});return nodes.get(id);};
 const context=vm.createContext({document:{getElementById:node,querySelector:()=>({content:'csrf-fixture'}),querySelectorAll:()=>[],createElement:()=>node('unused')},fetch(url,options){const response=deferred();requests.push({url,options,...response});return response.promise;},setInterval(){},setTimeout(){},clearTimeout(){},Date,URL,Number,String,Math,JSON,Promise,Object,window:{}});
 vm.runInContext(source,context);
 vm.runInContext('latest={mode:"live",context_id:"original",state:{symbol:"XAUUSDT",enabled:true}};syncStartControls=()=>{}',context);
 return {context,node,requests,post:(path,body)=>context.post(path,body)};
}
const ok={ok:true,json:async()=>({ok:true})};
test('pause bypasses an account request and acknowledges without waiting for state refresh',async()=>{
 const h=harness();h.post('account');
 const paused=h.post('pause');
 const request=h.requests.find(r=>r.url==='/api/pause');assert.ok(request);
 assert.equal(JSON.parse(request.options.body).context_id,'original');
 assert.equal(request.options.headers['X-CSRF-Token'],'csrf-fixture');
 assert.equal(h.node('pause').textContent,'正在暂停…');
 request.resolve(ok);assert.ok(await paused);
 assert.equal(vm.runInContext('busy',h.context),true);
 assert.equal(vm.runInContext('latest.state.enabled',h.context),false);
 assert.equal(h.node('running').textContent,'已暂停');
});
test('duplicate pause clicks share one request and block start while acknowledgement is pending',async()=>{
 const h=harness(),first=h.post('pause'),second=h.post('pause');
 assert.equal(h.requests.filter(r=>r.url==='/api/pause').length,1);
 assert.equal(await h.post('start'),null);
 assert.equal(h.requests.filter(r=>r.url==='/api/start').length,0);
 h.requests.find(r=>r.url==='/api/pause').resolve(ok);
 assert.ok(await first);assert.ok(await second);
});
test('failed pause preserves running state and permits an explicit retry',async()=>{
 const h=harness(),first=h.post('pause');
 h.requests.find(r=>r.url==='/api/pause').reject(Error('connection lost'));
 assert.equal(await first,null);
 assert.equal(vm.runInContext('latest.state.enabled',h.context),true);
 assert.match(h.node('toast').textContent,/暂停尚未确认/);
 assert.equal(h.node('pause').disabled,false);
 const retry=h.post('pause');const requests=h.requests.filter(r=>r.url==='/api/pause');
 assert.equal(requests.length,2);requests[1].resolve(ok);assert.ok(await retry);
});
test('server rejection cannot claim success and stale context cannot send a pause',async()=>{
 const h=harness(),first=h.post('pause');
 h.requests.find(r=>r.url==='/api/pause').resolve({ok:false,json:async()=>({error:'环境已改变'})});
 assert.equal(await first,null);assert.match(h.node('toast').textContent,/环境已改变/);
 assert.equal(await h.post('pause',{context_id:'old'}),null);
 assert.equal(h.requests.filter(r=>r.url==='/api/pause').length,1);
});
test('acknowledgement for an old environment does not mutate the new environment',async()=>{
 const h=harness(),first=h.post('pause');
 vm.runInContext('latest.context_id="new"',h.context);
 h.requests.find(r=>r.url==='/api/pause').resolve(ok);await first;
 assert.equal(vm.runInContext('latest.state.enabled',h.context),true);
});
test('an already submitted start completes before pause is sent, even if its refresh stalls',async()=>{
 const h=harness();h.post('start');const paused=h.post('pause');
 assert.equal(h.requests.filter(r=>r.url==='/api/pause').length,0);
 h.requests.find(r=>r.url==='/api/start').resolve(ok);
 await new Promise(resolve=>setImmediate(resolve));
 const pause=h.requests.find(r=>r.url==='/api/pause');assert.ok(pause);
 pause.resolve(ok);assert.ok(await paused);
 assert.equal(vm.runInContext('latest.state.enabled',h.context),false);
});
