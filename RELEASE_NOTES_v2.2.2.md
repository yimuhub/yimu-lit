# 神笔文学创作台 v2.2.2 发布说明

小版本升级。三件事：**五个界面 UI 全面对齐 DSH 设计语言**、**章节状态显示修正（写完就是写完）**、**小说对话框追章——每写完一章，正文实时弹进对话**。

---

## 一、UI 统一（本轮主任务）

全部对齐 `tokens.css` 既有设计语言：token 字号阶梯、0.5px hairline 边框、语义色、统一按钮/开关/进度条/空态原语。不引入新视觉，只收敛和补齐。

### 设置界面

- 修复分节标题失效（JS 用 `h2` 而 CSS 只写了 `h3`，标题落到浏览器默认样式）→ 统一 `h3`
- 裸 checkbox 全部换成 `.switch` 开关原语（tokens 实际 DOM：`button.switch[role=switch][aria-checked]`）
- 供应商操作区 5 按钮窄屏（<760px）溢出 → 可换行布局
- 12 处内联样式全部迁入语义类（`.set-page/.set-actions/.set-loading/.set-empty/.set-skill-chips/.set-error`）
- 33 处杂字号（9.5/10.5/11.5/12.5px）收敛到 11/12/13px 阶梯；默认供应商行渐变拍平为 `--accent-soft`

### 技能库界面

- 页面大标题 19px → 22px，与作品库/进化视图对齐
- 删除冗余的 `.skill-preview-wrap` 双框包裹，预览卡与列表统一为平面卡片
- 文件上传按钮补 hover 与 `:focus-visible` 态，对齐 `.btn` 语义
- 提交按钮删掉「空 field-label 占位」hack，改正经 `.form-actions` 行
- 两套并存的面板表单样式（自定义/蒸馏）合一；空态接 `.empty-state`

### 作品库界面

- **修复类名漂移**：CSS 写的是 `card-top/card-name/...`，JS 渲染的是 `lib-main/...`，行内操作按钮 hover 样式全部失效 → 统一为一套，按钮 hover 才显示
- 搜索框补齐 wrapper+图标结构，focus 环走 accent
- 硬编码星标色 `#e0a53a` → `var(--warn)`（深浅色自动适配）
- 空/错误态接 `.empty-state`；内联样式迁语义类

### 技能进化界面

- 修复双层容器嵌套（`.evo-view` 的 max-width/padding 叠在 `.view-pad` 上）→ 四周留白与其他视图一致
- 未定义类 `.evo-card-title`/`.tag-chip` → 复用 `h2` 标题原语与 `.badge`
- 统计卡统一到 `.stat-card/.stat-k/.stat-v`；彩点换 `.dot-*`；空态接 `.empty-state`

### 右侧作品弹窗（六 pane）

- 协调 pane 任务卡全内联 `border:1px` + `fontWeight:600` → 新建 `.collab-card`（hairline、token 圆角、字重≤500）
- 后台任务状态点补 `[data-tone]` 配色（此前所有状态都是灰色）；预算条换 `.progress-track[data-tone="danger"]`
- `.live-log` 日志流补样式（等宽、滚动、最新行高亮）
- 规划提案卡去渐变，平面软语言；章节筛选/按钮行/空态全部原语化
- 进度条三套实现（progress-track / eval-track / gr-bar）统一为同一视觉

### 全局

- 死 token（`--bg-1/--bg-2/--fg-2`）全部替换为真实 token；`#d14343` 等硬编码色清零
- panels.css 全文件字号收敛（含中台工作台区块）；skills/settings.css 同步清零

---

## 二、章节状态显示修正

**问题**：右侧作品弹窗的章节列表里，写完的章仍显示「待写」。

**根因（两层）**：

1. 前端 `statusLabel` 不认识章卡词表 `done/writing/changed`，全部落到「待写」
2. 后端 `_plan_view` 只读章卡（02d），不合并 02-写作计划.json（writer 落盘的事实源）里的真实状态与字数

**修复**：

- 前端：`chaptersFrom` 归一化两套词表（done→completed、writing→in_progress、changed→planned），五态显示——**已写 / 正在创作 / 待写 / 待规划 / 失败**，状态点配色五态齐全
- 后端：`_plan_view` 从 `proj.plan()`（02-写作计划.json）读真实状态，**只升不降**地合进章卡（planned+completed→done、+in_progress→writing、done 不降级），并附带 `word_count` 与缺失标题
- 过滤器（已写/待写/失败）谓词同步归一

实测（《跨越到清朝当皇子》100 章）：done 10、writing 1、planned 89，字数全部挂上。

---

## 三、小说对话框追章（DSH 式创作回馈）

**现状**：写作在后台线程跑，对话框里只有「正在思考…」心跳和一句「正在疯狂创作中」，章节内容从不进对话。

**现在**：对话框里的回馈和 DSH 一样——**要么在思考，要么章节内容一条条弹出来**：

- 回复后若该书正在后台写作，流自动进入追章模式
- 每写完一章，聊天里弹一条独立气泡：头部「第N章 · 标题」→ 正文逐字流出（40 字/片、12ms 间隔）→ 尾部「N 字」
- 章与章之间穿插思考心跳：「正在创作第N章：标题…」
- 全部写完收口一句小结（书名/新增章数/进度/总字数）；暂停给暂停提示；30 分钟超时给仍在创作提示
- 开局快照里已完成的章节不重推（历史章节不刷屏）；同一章防重复气泡；点停止/出错时未落完的章节气泡正常收口

**协议**（`POST /api/chat/stream` 新增事件，请求新增可选 `key` 字段，仅小说且已打开作品时携带）：

| 事件 | data |
|---|---|
| `chapter_start` | `{no, title}` |
| `chapter_delta` | `{no, text}` |
| `chapter_done` | `{no, words}` |

---

## 四、其它

- 版本号升级到 v2.2.2（`VERSION` 文件是唯一来源）
- 静态资源缓存号 `?v=35` → `?v=36`

---

## 门禁

```bash
PYTHONPATH=. .venv/bin/python -m pytest -p no:warnings   # 412 passed
.venv/bin/ruff check app.py bot tests run.py tools build.py  # All checks passed
.venv/bin/python tools/check_frontend_js.py                  # 旧版 1 个内联 script + v2 34 个 module 语法通过
```

新增测试：`tests/test_v222_chapters.py`（5 条）——`_plan_view` 合并规则（只升不降/word_count/无 chapters 防御）、追章流（不重推历史章、逐字切片、暂停与无 key 收尾）。

---

## 真实浏览器验收清单

- [x] 设置界面：分节标题样式正常、开关为 switch 原语、窄屏供应商操作区不溢出
- [x] 技能进化：四周留白与其他视图一致、空态为 empty-state
- [x] 技能库：大标题 22px、上传按钮 hover/focus、两套表单合一
- [x] 作品库：卡片行内按钮 hover 显示、搜索框图标与 focus 环、星标色适配深色
- [x] 右栏六 pane：协作任务卡 hairline 卡片（实测 borderW=1px radius=12px weight=400）、任务状态点有色、预算条红、日志流有样式
- [x] 章节状态：写完的章显示「已写」带字数、在写的显示「正在创作」、未规划显示「待规划」（真实作品实测 done 10 / writing 1 / planned 89，字数取自 `02-写作计划.json`）
- [x] 追章：对话框里新完成章节以气泡弹出（头部章号标题、正文流式、尾部字数），章间有思考心跳（实测 08:33:05 心跳「正在创作第N章」、08:34:54 第 11 章气泡出现；截图脚本点 tab 的选择器误命中侧栏「新建作品」，同屏遮了一层向导弹窗，视觉证据在 v2.3.0 验收里重拍）
