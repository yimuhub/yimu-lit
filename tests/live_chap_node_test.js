// v0.7.x 对话区「逐章直播打字机」逻辑单测（隔离 fake-DOM + mock fetch，node 直跑，无需浏览器/jsdom）
const fs=require('fs');
const html=fs.readFileSync(__dirname+'/../static/index.html','utf8');
const block=html.match(/const CL=\{.*?window\.toggleChatMode=toggleChatMode;/s);
if(!block){console.error('未找到 CL 块');process.exit(1);}
const code=block[0];

// ---- 极简 fake-DOM（只实现本功能用到的 API）----
class FakeText{constructor(tag){this.tag=tag;this._t='';this._h='';this.style={};this.dataset={};}
  set textContent(v){this._t=String(v);this._h=String(v);}get textContent(){return this._t;}
  set innerHTML(v){this._h=String(v);}get innerHTML(){return this._h;}get isConnected(){return true;}}
class FakeEl extends FakeText{
  constructor(){super('div');this.children={};}
  set innerHTML(v){super.innerHTML=v;for(const k of ['chttl','chstat','chbody','chmore'])this.children[k]=new FakeText(k);}
  get innerHTML(){return super.innerHTML;}
  querySelector(sel){return this.children[sel.replace('.','')]||new FakeText(sel);}
}
global.document={createElement:()=>new FakeEl(),querySelectorAll:()=>[]};
global.msgBox={appendChild:(c)=>{(global._bubbles=global._bubbles||[]).push(c);},scrollTop:0,scrollHeight:0};
// ---- stubs ----
global._viewToken=1; global.curSession='k'; global.curKey='k';
global.esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.window={_chatModeBtn:null};
// mock fetch：/api/status=writing，/api/preview=可控的 chapters_full
global._fake={ state:'in_progress', cf:[ {n:1,title:'开局',status:'completed',text:'A'.repeat(400)}, {n:2,title:'冲突',status:'in_progress',text:'B'.repeat(600)} ] };
global.fetch=async(u)=>{
  if(u.includes('/api/status'))return{json:async()=>({state:global._fake.state,done:1,total:3})};
  if(u.includes('/api/preview'))return{json:async()=>({key:'k',name:'测试书',chapters_full:global._fake.cf,total_words:1000})};
  return{json:async()=>({})};
};

// ---- 载入被测代码 ----
(0,eval)(code);

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const out=[];
  // 1) 初始：ch1 完成 + ch2 活跃逐字
  loadChatLive('k'); await sleep(350);
  const bub=Object.keys(window===global?{}:{}).length; // noop
  const els=global._bubbles;
  const ch1=els.find(b=>b.dataset&&b.dataset.chapn==1), ch2=els.find(b=>b.dataset&&b.dataset.chapn==2);
  out.push('气泡数='+els.length+'(期望2)');
  out.push('ch2 有光标='+/twcur/.test(ch2?ch2.children.chbody._h:''));
  out.push('ch2 部分文字(逐字中)='+(ch2? (ch2.children.chbody._h.replace(/<[^>]+>/g,'').length<600):'X'));
  out.push('ch1 已完成文案='+(ch1?/已完成/.test(ch1.children.chstat._h):'X'));
  const r2a=JSON.parse(JSON.stringify(revealedSnapshot()));
  await sleep(900);
  const r2b=JSON.parse(JSON.stringify(revealedSnapshot()));
  out.push('逐字推进(ch2 revealed 增长)='+ (r2b['2']>r2a['2'])+' ('+r2a['2']+'→'+r2b['2']+')');
  // 2) 直显模式
  toggleChatMode(); await sleep(60);
  out.push('直显:ch2 全文='+((ch2.children.chbody._h.replace(/<[^>]+>/g,'').length)===600));
  // 3) 写满 ch2 出 ch3（新气泡）
  toggleChatMode(); // 回逐字
  global._fake.cf.push({n:3,title:'高潮',status:'in_progress',text:'C'.repeat(500)});
  loadChatLive('k'); await sleep(500);
  const ch3=global._bubbles.find(b=>b.dataset&&b.dataset.chapn==3);
  out.push('ch3 新气泡已出现='+!!ch3);
  out.push('ch2 转已完成='+(ch2?/已完成/.test(ch2.children.chstat._h):'X'));
  out.push('ch3 活跃逐字(有光标)='+(ch3?/twcur/.test(ch3.children.chbody._h):'X'));
  // 4) 完成态（全部拉满）
  global._fake.state='completed'; loadChatLive('k'); await sleep(50);
  out.push('完成后 ch3 无光标(全文)='+(ch3? !/twcur/.test(ch3.children.chbody._h)&&ch3.children.chbody._h.replace(/<[^>]+>/g,'').length===500 :'X'));
  console.log(out.join('\n'));
  const need=['气泡数=2','ch2 有光标=true','ch2 部分文字(逐字中)=true','ch1 已完成文案=true',
             '逐字推进(ch2 revealed 增长)=true','直显:ch2 全文=true','ch3 新气泡已出现=true',
             'ch2 转已完成=true','ch3 活跃逐字(有光标)=true','完成后 ch3 无光标(全文)=true'];
  const joined=out.join('\n');
  const fails=need.filter(n=>!joined.includes(n));
  console.log('\n结果：'+(fails.length?('❌ 失败：'+fails.join(' | ')):'✅ 全部通过（'+need.length+' 项）'));
  process.exit(fails.length?1:0);
})();
// 读取 revealed（被测代码把 CL 放在全局 eval 作用域 → 取不到；用旁路：直接看气泡 body 长度推断。这里只导出占位）
function revealedSnapshot(){ // 从 body 长度反推 revealed
  const o={};
  for(const b of global._bubbles){ const n=b.dataset.chapn; if(!n)continue; o[n]=(b.children.chbody._h||'').replace(/<[^>]+>/g,'').length; }
  return o;
}
