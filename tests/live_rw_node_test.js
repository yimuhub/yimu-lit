// v0.8.0 对话内重写 + 两个按钮修复 逻辑单测（隔离 fake-DOM + mock）
const fs=require('fs');
const html=fs.readFileSync(__dirname+'/../static/index.html','utf8');
// 抽取 renderRewriteResultCard … window.rwDialogueCheck=rwDialogueCheck; 这一段
const b1=html.match(/function renderRewriteResultCard.*?window\.refreshMsScore=refreshMsScore;/s);
const b2=html.match(/let _rwDlgPoll=\{.*?window\.rwDialogueCheck=rwDialogueCheck;/s);
if(!b1||!b2){console.error('未找到被测块',!!b1,!!b2);process.exit(1);}
const code=b1[0]+'\n'+b2[0];

// ---- fake-DOM ----
class FT{constructor(t){this.tag=t;this._h='';this._t='';this.style={};this.dataset={};}
  set innerHTML(v){this._h=String(v);}get innerHTML(){return this._h;}
  set textContent(v){this._t=String(v);this._h=String(v);}get textContent(){return this._t;}
  get isConnected(){return true;}}
class FE extends FT{constructor(){super('div');this.children={};}
  set innerHTML(v){super.innerHTML=v;for(const k of ['chttl','chstat','chbody','rwbody','bubble','ava'])this.children['.'+k]=new FT(k);}
  get innerHTML(){return super.innerHTML;}
  querySelector(sel){return this.children[sel]||new FT(sel);}}
global.document={createElement:()=>new FE(),querySelectorAll:()=>[],
  querySelector(sel){ // '#msgs .rwbub[data-rwk="KEY"]'
    const m=sel.match(/data-rwk="([^"]+)"/); const k=m?m[1]:null;
    return (global._rwEls||{})[k]||null;
  }};
global.msgBox={ _kids:[], appendChild(c){this._kids.push(c); const m=c.outerKey; if(m)(global._rwEls=global._rwEls||{})[m]=c; c._key=m;}, scrollTop:0, scrollHeight:0 };
// _rwDlgBubble 设 d.setAttribute('data-rwk',key) + appendChild(d) → 记录 outerKey
// 让 FE 支持 setAttribute
FE.prototype.setAttribute=function(n,v){ if(n==='data-rwk')this.outerKey=v; (this.attrs=this.attrs||{})[n]=v; };

// ---- stubs ----
global._viewToken=1; global.curSession='mybook'; global.curKey='mybook';
global.esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.bubble=()=>({}); global.selectBook=k=>{global._sel=k;}; global.switchView=v=>{global._sw=v;};
global.loadManuscript=k=>{global._lms=k;}; global.toast=()=>{}; global.loadLibrary=()=>{};
global._openCalls=[]; global.openPreview=(k,tab)=>{global._openCalls.push([k,tab]);};
global.fetch=async(u)=>{ // /api/rewrite-log
  if(u.includes('/api/rewrite-log'))return{json:async()=>global._rwlog||{running:false}};
  return{json:async()=>({})};
};
global.window={};

(0,eval)(code);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const out=[];
  // 1) 关键修复：结果卡两个按钮必须指向「被重写的书」而非全局 PV_KEY
  const cardHTML=renderRewriteResultCard({before:50,after:88,pass:true,changed:[3]},'mybook');
  out.push('按钮1指向mybook='+/viewQualityReport\('mybook'\)/.test(cardHTML));
  out.push('按钮2指向mybook='+/refreshMsScore\('mybook'\)/.test(cardHTML));
  out.push('不再误用showPv(PV_KEY)='+!/showPv\(/.test(cardHTML));
  // 2) viewQualityReport 调 openPreview(key,'quality')
  viewQualityReport('mybook');
  out.push('viewQualityReport→openPreview(key,quality)= '+(JSON.stringify(global._openCalls[global._openCalls.length-1])==='["mybook","quality"]'));
  // 3) 对话内重写 live：建气泡 + running
  global._rwEls={}; msgBox._kids=[];
  rwDialogueLive('mybook',[3],['节奏']);
  out.push('对话已建重写气泡='+!!(global._rwEls&&global._rwEls['mybook']));
  // 4) 完成态：气泡变结果卡（两个按钮带 mybook）
  global._rwlog={running:false,result:{before:50,after:92,pass:true,changed:[3]}};
  _rwDlgPaint('mybook',{running:false,result:{before:50,after:92,pass:true,changed:[3]}});
  const body=(global._rwEls['mybook']&&global._rwEls['mybook'].children['.rwbody']?global._rwEls['mybook'].children['.rwbody']._h:'');
  out.push('完成后气泡含结果卡(mybook按钮)='+/viewQualityReport\('mybook'\)/.test(body));
  // 5) refreshMsScore 调 loadManuscript+switchView
  refreshMsScore('mybook');
  out.push('refreshMsScore→loadManuscript(mybook)+ms='+ (global._lms==='mybook'&&global._sw==='ms'));
  console.log(out.join('\n'));
  console.log('[debug] _openCalls=',JSON.stringify(global._openCalls),' _sel=',global._sel,' _sw=',global._sw,' _lms=',global._lms);
  const all=out.map(x=>String(x)).every(x=>x.endsWith('true'));
  console.log('\n结果：'+(all?('✅ 全部通过（'+out.length+' 项）'):'❌ 有失败'));
  process.exit(all?0:1);
})();
