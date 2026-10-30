/* Fixed-edition desktop layout. Reuses all existing forms and event handlers. */
(()=>{
 'use strict';
 const zh=document.documentElement.lang==='zh-CN',t=(en,cn)=>zh?cn:en;
 const main=document.querySelector('main'),workspace=document.querySelector('.workspace');
 const section=id=>document.getElementById(id)?.closest('section');
 const chart=section('chart-title'),selection=section('selection-title'),decision=section('decision-title');
 const candle=section('kline-status')||document.getElementById('kline-analysis')?.closest('section');
 const control=document.querySelector('.control'),risk=section('risk-title');
 const position=section('position'),account=section('api-credentials'),journal=document.querySelector('.journal');
 const rules=document.querySelector('.rules')?.closest('section');
 const research=[section('news-title'),section('ai-title'),section('ai-monitor-title')].filter(Boolean);
 const chat=section('chatgpt-title');
 const pane=(name)=>{const node=document.createElement('div');node.className='terminal-view';node.id='view-'+name;node.hidden=true;main.append(node);return node;};
 const market=pane('market'),news=pane('news'),chatView=pane('chat'),accounts=pane('accounts'),logs=pane('logs');
 const left=document.createElement('div'),right=document.createElement('div');left.className='terminal-market';right.className='terminal-orders';
 for(const item of [chart,candle,selection,decision,rules])if(item)left.append(item);
 for(const item of [control,risk])if(item)right.append(item);
 // Keep any future panel reachable rather than discarding an unknown control.
 for(const item of [...workspace.querySelectorAll('.left-column > section,.right-column > section')]){
  if(![...research,chat,position,account].includes(item))left.append(item);
 }
 workspace.replaceChildren(left,right);market.append(workspace);if(position)market.append(position);
 research.forEach(node=>news.append(node));if(chat)chatView.append(chat);if(account)accounts.append(account);if(journal)logs.append(journal);
 const title=document.querySelector('header h1');title.textContent=t('Commodity Trading Terminal','商品交易终端');
 document.querySelector('.eyebrow').textContent='AegisQuant';
 const nav=document.querySelector('aside nav');
 const show=(id)=>{for(const view of [market,news,chatView,accounts,logs])view.hidden=view.id!=='view-'+id;for(const button of nav.querySelectorAll('[data-view]')){const active=button.dataset.view===id;button.classList.toggle('selected',active);button.setAttribute('aria-selected',String(active));}window.scrollTo(0,0);};
 const bind=(id,view,label)=>{const button=document.getElementById(id);button.textContent=label;button.dataset.view=view;button.onclick=()=>show(view);};
 bind('nav-workspace','market',t('Market & Trading','行情与交易'));bind('nav-ai','news',t('News & Research','新闻与研究'));bind('nav-chatgpt','chat',t('Chat Analysis','聊天分析'));
 const add=(id,label)=>{const button=document.createElement('button');button.type='button';button.textContent=label;button.dataset.view=id;button.onclick=()=>show(id);nav.append(button);};
 add('accounts',t('API Connections','API 连接'));add('logs',t('Journal','交易日志'));
 document.getElementById('nav-products').textContent=t('Instruments…','选择品种…');
 const footer=main.querySelector('footer');if(footer)main.append(footer);
 show('market');
})();
