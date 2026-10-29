'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../static/chatgpt.js'),'utf8');
function harness(){
 const nodes=new Map(),calls=[];
 function element(id){return {id,value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,open:false,children:[],scrollHeight:100,scrollTop:0,clientHeight:100,
  listeners:{},append(x){this.children.push(x);},replaceChildren(){this.children=[];this.value='';},showModal(){this.open=true;},close(){this.open=false;},scrollIntoView(){this.scrolled=true;},addEventListener(e,h){this.listeners[e]=h;}};}
 const node=id=>{if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id);};
 const storage=new Proxy({}, {get(){throw Error('Must not use browser credential storage');},set(){throw Error('Must not save tokens');}});
 let respond;
 const context=vm.createContext({document:{getElementById:node,createElement:tag=>element(tag)},URL,Date,JSON,Object,String,Promise,Error,crypto:{randomUUID:()=> 'request-1234'},token:'csrf-fixture',requestContext:()=>({context_id:'ctx',mode:'paper'}),contextMatches:()=>true,
  fetch(url,options){calls.push({url,options});if(respond)return respond(url,options);return new Promise(()=>{});},setTimeout(){},setInterval(){},localStorage:storage,sessionStorage:storage});
 vm.runInContext(source.replace(' poll();preview();setInterval(poll,1500);',' globalThis.testChat={render,markdown,safeURL,action,poll,preview};poll();preview();setInterval(poll,1500);'),context);
 return {node,nodes,calls,context,respond:fn=>{respond=fn;}};
}
function status(overrides={}){return {session:'session1',turns:[],auth:{connected:true,plan_enabled:true,login_pending:false,account_id:'one',email:'fixture@example.test',accounts:[{id:'one',label:'Account'}],models:[{slug:'test-model',display_name:'Test'}]},...overrides};}

test('initial load only reads status and public preview, never sends analysis',()=>{
 const h=harness();assert.deepEqual(h.calls.map(x=>x.url),['/api/chatgpt/status','/api/chatgpt/context']);assert.ok(h.calls.every(c=>!c.options));
});
test('HTML and unsafe citations remain inert while tables render',()=>{
 const h=harness(),api=h.context.testChat;
 const out=api.markdown('<img src=x onerror=alert(1)>\n| A | B |\n|---|---|\n| 1 | 2 |');
 assert.ok(!out.includes('<img'));assert.ok(out.includes('&lt;img'));assert.ok(out.includes('<table>'));
 assert.equal(api.safeURL('javascript:alert(1)'),null);assert.equal(api.safeURL('https://name:password@example.com'),null);
});
test('models follow authorized catalogue and account switch clears old models',()=>{
 const h=harness();h.context.testChat.render(status());assert.equal(h.node('chatgpt-model').value,'test-model');assert.equal(h.node('chatgpt-send').disabled,false);
 h.context.testChat.render(status({auth:{connected:false,plan_enabled:false,account_id:'two',accounts:[{id:'two',label:'Two'}],models:[]}}));
 assert.equal(h.node('chatgpt-model').value,'');assert.equal(h.node('chatgpt-send').disabled,true);
});
test('login pending, missing scope, and running request disable sends',()=>{
 for(const value of [status({busy:true}),status({auth:{...status().auth,login_pending:true}}),status({auth:{...status().auth,plan_enabled:false}})]){
  const h=harness();h.context.testChat.render(value);assert.equal(h.node('chatgpt-send').disabled,true);
 }
});
test('first authorization displays plan welcome and usage link',()=>{
 const h=harness();h.context.testChat.render(status({auth:{...status().auth,welcome:true}}));assert.equal(h.node('chatgpt-welcome').open,true);assert.match(h.node('chatgpt-plan-label').textContent,/Using ChatGPT plan/);
});
test('send is explicit, includes CSRF/current context, and blocks duplicate clicks',async()=>{
 const h=harness();h.context.testChat.render(status());h.node('chatgpt-question').value='比较五个';h.node('chatgpt-web').checked=true;
 let release;h.respond(()=>new Promise(r=>release=r));
 const first=h.node('chatgpt-form').listeners.submit({preventDefault(){}});await h.node('chatgpt-form').listeners.submit({preventDefault(){}});
 const writes=h.calls.filter(c=>c.options);assert.equal(writes.length,1);assert.equal(writes[0].url,'/api/chatgpt/send');assert.equal(writes[0].options.headers['X-CSRF-Token'],'csrf-fixture');
 const data=JSON.parse(writes[0].options.body);assert.equal(data.context_id,'ctx');assert.equal(data.session,'session1');assert.equal(data.message,'比较五个');assert.equal(data.web_search,true);
 release({ok:true,json:async()=>status({busy:true})});await first;assert.equal(h.node('chatgpt-question').value,'');
});
test('partial failure visibly remains unconfirmed and never renders model HTML',()=>{
 const h=harness();h.context.testChat.render(status({turns:[{model:'test-model',status:'error',question:'test',answer:'<script>evil()</script>',error:'连接中断',citations:[]}]}));
 const turn=h.node('chatgpt-messages').children[0];assert.match(turn.children[1].textContent,/未完成/);assert.ok(!turn.children[2].innerHTML.includes('<script>'));assert.equal(turn.children[3].textContent,'连接中断');
});
test('sidebar navigation reaches actual chat panel',()=>{const h=harness();h.node('nav-chatgpt').onclick();assert.equal(h.node('chatgpt-panel').scrolled,true);});


test('Astra is preferred when the account catalogue also contains Sol',()=>{
 const h=harness();const d=status();d.auth.models=[{slug:'gpt-6-sol',display_name:'Sol'},{slug:'gpt-6-astra',display_name:'Astra'}];h.context.testChat.render(d);
 assert.equal(h.node('chatgpt-model').value,'gpt-6-astra');
});

test('GPT-6.1 Sol follows account authorization and never appears as a fabricated choice',()=>{
 const h=harness(),d=status();h.context.testChat.render(d);
 assert.match(h.node('chatgpt-sol-status').textContent,/尚未出现在/);
 assert.ok(!h.node('chatgpt-model').children.some(o=>o.value==='gpt-6.1-sol'));
 d.auth.models.push({slug:'gpt-6.1-sol',display_name:'GPT-6.1 Sol'});h.context.testChat.render(d);
 assert.match(h.node('chatgpt-sol-status').textContent,/已由当前账户授权/);
 assert.ok(h.node('chatgpt-model').children.some(o=>o.value==='gpt-6.1-sol'));
});

test('old public preview expires without sending chat or paid requests',async()=>{
 const h=harness();h.respond(()=>Promise.resolve({ok:true,json:async()=>({asof_utc:new Date(Date.now()-16000).toISOString(),symbols:[]})}));
 await h.context.testChat.preview();await h.context.testChat.poll();
 assert.match(h.node('chatgpt-context-time').textContent,/预览已过期/);
 assert.match(h.node('chatgpt-context-rows').textContent,/旧预览已隐藏/);
 assert.ok(h.calls.every(c=>!c.options));
});
