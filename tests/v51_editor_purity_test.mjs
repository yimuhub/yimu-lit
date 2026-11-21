// v5.1.0 编辑器文本模型 × 正文纯净域口径比对（node 直跑，jsdom 仿真，不开浏览器）。
//
// 铁律 0 断言：编辑器（core/editor.js，contenteditable 段落块 + \n\n 纯文本模型）
// 装载/编辑任何章节文本后，getText() 过 core/purity.js 的 cleanChapterText，
// 输出必须与「原文直接过 cleanChapterText」逐字一致——编辑器不改变对外正文口径。
// 顺带守住：模型往返幂等（setText→getText 原样）、软换行/多段粘贴保真、
// 段落键入只动当前段、Enter 分段/删除合并不丢字。
import { JSDOM } from 'jsdom';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(
  '<!doctype html><html><body><div id="host"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.Event = dom.window.Event;
global.KeyboardEvent = dom.window.KeyboardEvent;
global.CustomEvent = dom.window.CustomEvent;
global.Range = dom.window.Range;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);

const { createEditor } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/editor.js')).href);
const { cleanChapterText } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/purity.js')).href);

let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed += 1;
}

function showDiff(label, got, want) {
  console.log(`   ${label} 长度 got=${got.length} want=${want.length}`);
  let i = 0;
  while (i < Math.min(got.length, want.length) && got[i] === want[i]) i += 1;
  console.log(`   首个差异@${i}: got=${JSON.stringify(got.slice(i, i + 24))} want=${JSON.stringify(want.slice(i, i + 24))}`);
}

const host = document.getElementById('host');

function mountEditor() {
  host.textContent = '';
  const events = { input: 0, selections: [] };
  const ed = createEditor({
    host,
    placeholder: '测试占位',
    onChange: () => { events.input += 1; },
    onSelectionChange: (sel) => { events.selections.push(sel); }
  });
  return { ed, events };
}

// ---------------------------------------------------------------- 用例 1：污染章节（概要/备注/修改说明/评分齐全）
const POLLUTED = [
  '# 第03章 副驾驶',
  '## 本章概要',
  '周望载到红雨衣，留下旧车票。',
  '## 章首引子',
  '雨是从傍晚开始下的。',
  '## 正文',
  '他关掉收音机。电台里在放老歌。',
  '窗外有人喊了一嗓子。',
  '## 章节备注',
  '下章埋线：岚字。',
  '## 修改说明',
  '1. 加强了开头的雨声细节。',
  '## 审核评分',
  '综合 86 分。'
].join('\n\n');

{
  const { ed } = mountEditor();
  ed.setText(POLLUTED, { silent: true });
  const got = ed.getText();
  check('污染章节：编辑器模型往返幂等（setText→getText 原样）', got === POLLUTED);
  if (got !== POLLUTED) showDiff('模型', got, POLLUTED);
  const a = cleanChapterText(got);
  const b = cleanChapterText(POLLUTED);
  check('污染章节：getText 过 cleanChapterText 与原口径输出一致', a === b, a.slice(0, 60));
  check('污染章节：清洗后仍含章题/引子/正文', a.startsWith('# 第03章 副驾驶') && a.includes('雨是从傍晚开始下的。') && a.includes('他关掉收音机。'));
  for (const junk of ['本章概要', '章节备注', '修改说明', '审核评分', '综合 86 分']) {
    check(`污染章节：清洗后不含「${junk}」`, !a.includes(junk));
  }
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 2：正文契约标记（v4.22.3 框结构）
const MARKED = [
  '=== 一幕·YIMU 神笔工作台·开篇 ===',
  '## 章首引子',
  '码头 lights 昏黄。',
  '## 正文',
  '他把伞收了起来，雨停了。',
  '雨停了，路还长。',
  '=== 一幕·YIMU 神笔工作台·结尾 ==='
].join('\n\n');

{
  const { ed } = mountEditor();
  ed.setText(MARKED, { silent: true });
  check('契约标记：模型往返幂等', ed.getText() === MARKED);
  const a = cleanChapterText(ed.getText());
  check('契约标记：编辑器口径 = 原口径（框内正文，标记行剥除）',
        a === cleanChapterText(MARKED), JSON.stringify(a.slice(0, 40)));
  check('契约标记：标记行不进对外正文', !a.includes('开篇') && !a.includes('结尾'));
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 3：软换行 / 空段 / 三连换行归一
{
  const { ed } = mountEditor();
  const src = '第一段第一行\n第一段第二行（软换行）\n\n第二段\n\n\n第四段（前面有空段折叠）';
  ed.setText(src, { silent: true });
  // \n{2,} 切段会把三连换行折叠成一个段界 —— 与 purity 的 \n{3,}→\n\n 归一同向
  const expected = '第一段第一行\n第一段第二行（软换行）\n\n第二段\n\n第四段（前面有空段折叠）';
  check('软换行：段内单换行保真', ed.getText() === expected, JSON.stringify(ed.getText().slice(0, 50)));
  check('软换行：清洗口径一致', cleanChapterText(ed.getText()) === cleanChapterText(src));
  check('软换行：段数正确', ed.paraCount() === 3, `paras=${ed.paraCount()}`);
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 4：模拟键入——只动当前段，聚合模型同步
{
  const { ed, events } = mountEditor();
  ed.setText('第一段。\n\n第二段。\n\n第三段。', { silent: true });
  const paras = [...host.querySelectorAll('.edp')];
  check('键入前段数', paras.length === 3);
  // 模拟用户在第二段打字：改 DOM 文本后派发 input（编辑器只重读当前段）
  const target = paras[1];
  target.textContent = '第二段改了七十二个字。';
  target.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('键入：onChange 触发', events.input >= 1);
  check('键入：getText 聚合了当前段改动', ed.getText() === '第一段。\n\n第二段改了七十二个字。\n\n第三段。');
  check('键入：其他段未被动过', paras[0].textContent === '第一段。' && paras[2].textContent === '第三段。');
  // 纯净域：改后正文口径仍一致
  const src = '第一段。\n\n第二段。\n\n第三段。';
  const after = ed.getText();
  check('键入：清洗口径一致（改一个字不破坏口径）',
        cleanChapterText(after) === cleanChapterText(src.replace('第二段。', '第二段改了七十二个字。')));
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 5：Enter 分段 / Backspace 合并（结构操作不丢字）
{
  const { ed } = mountEditor();
  ed.setText('他在门口停了半步。', { silent: true });
  const p = host.querySelector('.edp');
  const textNode = p.firstChild;
  // 光标放在第 5 个字符之后（「他在门口停」|「了半步。」）
  const range = document.createRange();
  range.setStart(textNode, 5);
  range.collapse(true);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  p.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  const afterEnter = ed.getText();
  check('Enter：段内光标处分段', afterEnter === '他在门口停\n\n了半步。', JSON.stringify(afterEnter));
  check('Enter：分段后段数=2', ed.paraCount() === 2);

  // 在第二段段首按 Backspace → 与前段合并
  const p2 = host.querySelectorAll('.edp')[1];
  const tn2 = p2.firstChild;
  const range2 = document.createRange();
  range2.setStart(tn2, 0);
  range2.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range2);
  p2.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
  check('Backspace：段首删除合并回一段', ed.getText() === '他在门口停了半步。', JSON.stringify(ed.getText()));
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 6：多段粘贴 → 纯文本落模型（富文本不进正文）
{
  const { ed } = mountEditor();
  ed.setText('开头一段。', { silent: true });
  const p = host.querySelector('.edp');
  const tn = p.firstChild;
  const range = document.createRange();
  range.setStart(tn, 5);   // 粘到段末
  range.collapse(true);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  const pasteText = '剪来的第一段。\n\n剪来的第二段。';
  const ev = new dom.window.Event('paste', { bubbles: true, cancelable: true });
  ev.clipboardData = { getData: () => pasteText };
  p.dispatchEvent(ev);
  const got = ed.getText();
  check('粘贴：多段纯文本按 \n\n 落模型（光标后插入，无富文本残留）',
        got === '开头一段。剪来的第一段。\n\n剪来的第二段。', JSON.stringify(got));
  // 口径：粘贴内容过纯净域与原口径一致（只比对可导出正文）
  check('粘贴：清洗口径一致',
        cleanChapterText(got).includes('剪来的第二段。') && cleanChapterText(got).includes('开头一段。'));
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 7：划词替换校验（原选区变了就拒绝）
{
  const { ed } = mountEditor();
  ed.setText('第一章的正文第一句。\n\n第二段在这里。', { silent: true });
  const p = host.querySelector('.edp');
  const tn = p.firstChild;
  const range = document.createRange();
  range.setStart(tn, 0);
  range.setEnd(tn, 10);   // 整段「第一章的正文第一句。」（10 字）
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  const cur = ed.currentSel();
  check('选区：全局偏移换算正确', cur && cur.text === '第一章的正文第一句。' && cur.start === 0 && cur.end === 10,
        cur ? `got=${JSON.stringify(cur.text)}` : 'null');
  // 替换：预期文本一致 → 成功
  const r1 = ed.replaceSelectionChecked('第一章的正文第一句。', '替换后的开头。');
  check('替换：校验通过并生效', r1.ok === true && ed.getText().startsWith('替换后的开头。'));
  // 原选区已变 → 拒绝
  const p2 = host.querySelector('.edp');
  const range2 = document.createRange();
  range2.setStart(p2.firstChild, 0);
  range2.setEnd(p2.firstChild, Math.min(9, p2.firstChild.nodeValue.length));
  sel.removeAllRanges();
  sel.addRange(range2);
  const stale = ed.currentSel();
  const r2 = ed.replaceSelectionChecked(stale.text + '（用户此刻又打了字）', '不该被写入');
  check('替换：原选区已变 → 拒绝（防覆盖新输入）', r2.ok === false && r2.reason === 'changed');
  check('替换：拒绝后正文未被改动', ed.getText().startsWith('替换后的开头。'));
  // 口径兜底：替换后的正文过纯净域不受影响
  check('替换：清洗口径仍一致', cleanChapterText(ed.getText()) === cleanChapterText('替换后的开头。\n\n第二段在这里。'));
  ed.destroy();
}

// ---------------------------------------------------------------- 用例 8：replaceRange 跨段替换 + 光标恢复
{
  const { ed } = mountEditor();
  ed.setText('甲段。\n\n乙段。\n\n丙段。', { silent: true });
  ed.replaceRange(0, 8, '甲段重写。\n\n甲段补充。');   // 覆盖「甲段。\n\n乙段。」
  const got = ed.getText();
  check('跨段替换：模型正确', got === '甲段重写。\n\n甲段补充。\n\n丙段。', JSON.stringify(got));
  check('跨段替换：段数=3', ed.paraCount() === 3);
  const caret = ed.getCaretOffset();
  check('跨段替换：光标落在替换文本之后', caret === '甲段重写。\n\n甲段补充。'.length, `caret=${caret}`);
  ed.destroy();
}

console.log();
if (failed) {
  console.log(`FAIL ${failed} 项未通过`);
  process.exit(1);
}
console.log('PASS 编辑器文本模型与 core/purity.js 口径一致（8 组用例）');
process.exit(0);
