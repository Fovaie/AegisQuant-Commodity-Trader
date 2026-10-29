'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync(__dirname+'/../static/app.js','utf8');
function harness(){
 const nodes=new Map(),requests=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',options:[],dataset:{},style:{},classList:{toggle(){},add(){}},addEventListener(){},append(){},setAttribute(){}});return nodes.get(id);};
 const context=vm.createContext({document:{getElementById:node,querySelector:()=>({content:'token'}),querySelectorAll:()=>[],createElement:()=>node('unused')},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},clearTimeout(){},Date,URL,Number,String,Math,JSON,Promise,Object,window:{}});
 vm.runInContext(source,context);
 vm.runInContext('latest={mode:"live",context_id:"current",pending_recovery_supported:true,state:{enabled:false,pending:{kind:"entry",symbol:"XAUUSDT",id:"btfixture"}}}',context);
 context.post=async(path,body)=>{requests.push({path,body});return {can_resolve:true,pending_id:'btfixture',review_id:'review',checked_at:Date.now()/1000,reason:'已核验<证据>'};};
 context.askConfirmation=async()=>true;
 return {context,node,requests};
}
test('pending controls distinguish leverage entry close and require supported backend',()=>{
 const h=harness();h.context.renderPendingRecovery();
 assert.equal(h.node('pending-panel').hidden,false);assert.match(h.node('pending-description').textContent,/开仓/);
 assert.equal(h.node('resolve-pending').disabled,true);
 vm.runInContext('latest.pending_recovery_supported=false',h.context);h.context.renderPendingRecovery();
 assert.equal(h.node('inspect-pending').disabled,true);assert.match(h.node('pending-description').textContent,/重启/);
});
test('inspect enables only reviewed result and resolve sends identity and explicit local confirmation',async()=>{
 const h=harness();await h.node('inspect-pending').onclick();
 assert.equal(h.requests[0].path,'pending/inspect');assert.equal(h.requests[0].body.context_id,'current');
 assert.equal(h.node('pending-result').textContent,'已核验<证据>');assert.equal(h.node('resolve-pending').disabled,false);
 await h.node('resolve-pending').onclick();
 const r=h.requests[1];assert.equal(r.path,'pending/resolve');
 assert.equal(r.body.expected_pending_id,'btfixture');assert.equal(r.body.review_id,'review');assert.equal(r.body.confirm_reconcile,true);
 assert.equal(r.body.confirm_live,undefined);
});
test('blocked review and changed environment cannot be resolved',async()=>{
 const h=harness();h.context.post=async()=>({can_resolve:false,pending_id:'btfixture',reason:'仍有残余订单',checked_at:Date.now()/1000});
 await h.node('inspect-pending').onclick();assert.equal(h.node('resolve-pending').disabled,true);
 await h.node('resolve-pending').onclick();assert.equal(h.requests.length,0);
 const other=harness();await other.node('inspect-pending').onclick();
 vm.runInContext('latest.context_id="changed"',other.context);other.context.renderPendingRecovery();
 await other.node('resolve-pending').onclick();assert.equal(other.requests.length,1);
});
test('cancelled confirmation never updates ledger and expired review disables resolve',async()=>{
 const h=harness();await h.node('inspect-pending').onclick();h.context.askConfirmation=async()=>false;
 await h.node('resolve-pending').onclick();assert.equal(h.requests.length,1);
 vm.runInContext('pendingReview.checked_at-=61',h.context);h.context.renderPendingRecovery();
 assert.equal(h.node('resolve-pending').disabled,true);
});
