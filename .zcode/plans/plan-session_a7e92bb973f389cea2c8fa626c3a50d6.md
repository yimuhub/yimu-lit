## 升级改造方案（v4.x 系列，6 个版本递进，每版独立提交推送打 tag）

GitHub 对标调研结论：论文写作采纳 [lit-review-agent-tools](https://github.com/brycewang-stanford/lit-review-agent-tools)（检索→引用核查全流程）与引用管理排版思路（真实文献、APA/GB 格式），降 AI 检测率参考 [humanizer-zh-academic](https://github.com/redbaronyyyyy-eng/humanizer-zh-academic) 与 [paper-ai-humanizer](https://github.com/xzy111222333/paper-ai-humanizer)（拟人化改写、知网/Turnitin AIGC 场景）；AI 短剧/漫剧参考 [Toonflow](https://github.com/topics/short-drama) 与 [ai-storyboard](https://github.com/topics/ai-storyboard) 的"剧本→分镜→资产"管线。

探索确认的根因（全部有 file:line）：邀请面板白底是因为引用了 tokens.css 里**不存在的变量**（--panel-bg 等 4 个）；关不掉是 `display:flex` 压过 `hidden` 属性；模式选完换不回是 **popoverMenu 泄漏 document 监听**（体裁/附件菜单同样中招）；版本号显示 3.2 是 bot/version.py import 时快照；预览偏左是 bench-paper 限宽未居中；剧本右栏 tab 被一行过滤代码拦死。

---

### v4.1.0 —— 交互 bug 批量修复（8 项）

1. **popoverMenu 监听泄漏**（ui.js）：选中菜单项时残留 document mousedown 捕获监听，第二次打开弹层被旧监听提前关闭 → closePopover 统一移除。修好后"讨论/写作/重写"、体裁、附件菜单都能随意切换。
2. **邀请 Agent 面板**：4 个未定义 CSS 变量替换为真实 token（深色/浅色都正常）+ `[hidden]{display:none}` 可关闭 + Escape/点外部关闭 + **顶部搜索框**（按名称/职责实时筛选）。
3. **版本号动态读**：bot/version.py 加 current()，/api/version 与 check-update 每次重读 VERSION 文件，升级不再显示旧版本。
4. **章节预览/阅读居中**：bench-paper 加 `margin: 0 auto`。
5. **时间线节点点击弹窗**：节点恢复可点，屏幕中央弹窗显示 事件全文+章节/时间/地点/人物（右栏列表保留提要）。
6. **协作"添加 Agent"弹窗换 openModal**：主题自适应 + Esc/遮罩关闭 + 三步分组（想干什么→示例 chips 一键填充→提交）。
7. **章名回填跑完**：后端循环有界直到无默认标题章（长安诡案 240 章自动跑完），右栏加"回填章名"按钮。
8. **同类未定义 CSS 变量清理**：chapter-card/evo-card 等处 `--panel-2/--bg-2/--surface/--border` 等全部替换为真实 token（顺带修复浅色主题黑框）。

### v4.2.0 —— 作品库/全站卡片精修 + 左栏文体头像

1. **评分星级**：作品卡评分改为 ★ + 分数 + 达标徽章（有分的书才显示）。
2. **文体彩色标签**：小说/剧本/诗词/散文 全部显示彩色 chip（此前小说不显示标签），按 medium 上色。
3. **题材标签化**：genre 从纯文本升级为 chip；用户自定义 tags 一并展示。
4. **左栏文体头像**：小/剧/散/诗（词牌含"词"显示"词"）/论/赋/歌 按 medium+form 映射，一眼识别文体（作品库封面同步）。

### v4.3.0 —— 非小说右栏对齐小说 + 创作过程可视

1. **剧本/诗词/散文右栏放开 参考/协作 tab**（数据本就文体无关）；**剧本状态 tab** 接 18-剧本状态库 + 22-剧本体检（script_state 已有）。
2. **mediumWorkPane 加改名入口**（pen 按钮，复用小说的 startTitleEdit，四文体改名已修好）。
3. **创作过程对话可视**：`/api/chat/stream` 等待/追进度心跳顺带推送引擎 proj.log 增量（log 帧），对话流实时显示"Agent 正在做什么/怎么想"；非小说工作台顶部加运行日志带；四文体（小说/剧本/诗词/散文）创建与写作过程全程在对话区可见。

### v4.4.0 —— 写作符号净化

1. 提示词约束：写作/重写系统提示明确"正文只用正常中文标点，禁止 \*、#、>、\`、列表语法"。
2. 确定性清洗 `_strip_md_style`：加粗/斜体/行首 #/列表符/反引号在 成书、导出、预览 三条路径统一净化（防模型不听话）。

### v4.5.0 —— AI 短剧/漫剧剧本 + 分镜头

1. **剧本新形式**：short_drama（AI 短剧）与 comic_drama（AI 漫剧）加入 script_writer FORMS——每集时长/钩子节奏按短剧标准（3-5 分钟一集、前 3 秒钩子）。
2. **分镜头创作**：新引擎阶段——把每场拆成分镜表（镜号/景别/画面描述/台词/音效/BGM/时长秒数），落 `08-分镜表.md` + 结构化 JSON，工作台新「分镜」页签可逐镜编辑。
3. **小说一键转短剧/漫剧**：新端点 `POST /api/novel-to-drama`——把小说（整本或指定章）转成短剧分集剧本 + 分镜表（集数/节奏可配），生成独立剧本项目并在作品库可见。

### v4.6.0 —— 论文文体（medium=thesis）

1. **论文引擎**（bot/thesis_writer.py，借鉴 lit-review-agent-tools 管线）：T1 选题与提纲 → T2 大纲（章节+小节）→ T3 分章撰写（引言/文献综述/方法/分析/结论）→ T4 **引用管理**：参考文献真实性问题用"引用池+核查"机制（正文引注 [n] ↔ 文献表一一对应、自动核验格式 APA/GB-T7714）→ T5 **降 AIGC 润色**（humanizer 思路：句式节奏扰动/术语口语化平衡/连接词多样化，产出降味报告）。产物：论文计划.json + 分章 md + 参考文献.md + 查重报告.md。
2. **接入全链**：新建向导出现「论文」选项（学历层次/学科/字数/引用格式选项）；工作台=论文章节页签+引用文献页签；右栏=引用文献面板（条目/格式/核查状态）+ 查重/AIGC 面板；断点恢复、library、对话、@Agent 全部支持 thesis。
3. **左栏头像「论」**、工作台纸张样式、候选标题（论文题目）生成。

### 交付门槛（每版相同）
pytest 全量回归（现 463 用例）+ 每项带回归用例 + 前端语法检查 + 浏览器实测（含深浅色两主题）+ 中文提交 + tag + 推送 GitHub + 问题修复记录.md 逐版补记。**全程不升 V5，V4 内递进。**