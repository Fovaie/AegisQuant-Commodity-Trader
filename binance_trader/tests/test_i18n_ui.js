'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/i18n.js'),'utf8');
const {translate}=require('../static/i18n.js');
test('English warnings preserve the live-funds and unknown-order safeguards',()=>{
 assert.equal(translate('确认启动真实资金自动交易？'),'Start automated trading with real funds?');
 assert.match(translate('未核验前保持锁定。'),/Remain locked until verified/);
 assert.equal(translate('暂停新开仓'),'Pause new entries');
 assert.equal(translate('K线分析'),'Candle Analysis');
 assert.match(translate('单笔计划亏损 / USDT'),/Planned loss per trade/);
 assert.equal(translate('XAUUSDT 300.00 2× 1m'),'XAUUSDT 300.00 2× 1m');
});
test('presentation translates dynamic text and attributes without touching input values or source content',()=>{
 let deliver;
 const node=(text,skip=false)=>({nodeType:3,nodeValue:text,parentElement:{closest:()=>skip?{}:null}});
 const label=node('已暂停');const original=node('黄金 新闻',true);
 const input={nodeType:1,childNodes:[],value:'secret-黄金',attrs:{placeholder:'等待填写模型密钥'},
  matches:()=>false,getAttribute(k){return this.attrs[k]??null},setAttribute(k,v){this.attrs[k]=v}};
 const document={nodeType:9,documentElement:{lang:'zh-CN'},childNodes:[label,original,input]};
 const window={};
 vm.runInNewContext(source,{document,window,MutationObserver:class{constructor(fn){deliver=fn}observe(){}}});
 assert.equal(document.documentElement.lang,'en');
 assert.equal(label.nodeValue,'Paused');assert.equal(original.nodeValue,'黄金 新闻');
 assert.equal(input.value,'secret-黄金');assert.equal(input.attrs.placeholder,'Enter a model API key');
 const state={enabled:true,pending:{kind:'entry'}};
 const dynamic=node('核对锁定');deliver([{type:'childList',addedNodes:[dynamic]}]);
 assert.equal(dynamic.nodeValue,'Reconciliation lock');
 assert.deepEqual(state,{enabled:true,pending:{kind:'entry'}});
});
