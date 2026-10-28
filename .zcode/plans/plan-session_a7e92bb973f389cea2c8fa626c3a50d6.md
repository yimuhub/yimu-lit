## 升级改造方案（三个版本，逐版实现、逐版提交推送）

基于代码勘察确认的事实：`fixConsistency` 报错根因是 rightpanel.js:324 用了不存在的 `ctx.api`（应为已导入的 `api`）；时间线空白根因是前端读 `item.at/item.text` 而后端字段是 `item.event/item.chapter` 且后端只回 30 条；聊天记录丢失根因是 `/api/chat/stream` 客户端断开时 AI 回复不落盘（用户消息已落盘，造成"只有问没有答"）。

---

### v2.8.0 —— 修复 + 设置界面重设计 + 时间线全节点 + 聊天留档

1. **一致性体检修复链路修复**
   - rightpanel.js:324 `ctx.api.fixConsistency` → `api.fixConsistency`（修崩溃）。
   - 修复弹窗补全体检上下文（scope、体检时间、问题等级）；`/api/fix-consistency`（v2api.py:1027）对无确定性修复的问题返回明确说明而非假装成功。
2. **时间线全节点渲染**（rightpanel.js:1465-1483 + v2api.py:547）
   - 后端放开 `[-30:]` 截断（返回全部，上限 2000 条）；前端按字段 `chapter/time/event/location/characters` 渲染成竖向时间轴：章号徽标 + 故事内时间 + 事件全文 + 地点/人物标签；空数据时给出明确空态。
3. **聊天记录完整保留**
   - `/api/chat/stream`（v2api.py:1208 附近）：客户端断开时在 finally 里把已生成的部分回复落盘（带 `partial: true` 标记），不再"只有问没有答"。
   - session.js `loadChatHistory`（128-157）：去掉"已有消息就跳过"，每次进入作品都重新拉取；`/api/chat/history` limit 1000 → 5000。
4. **设置页重设计为分组标签页**（settings.js 重构 + settings.css）
   - 左侧竖向导航 + 右侧内容区，分组：①外观 ②创作质量 ③模型与供应商 ④创作岗位（全局并发上限、岗位×模型、岗位×技能、新增「添加自定义 Agent」入口，调已有 /api/agents/add）⑤技能与数据（进化入口+本地说明）⑥高级（经典版入口等）。
   - 修正现有混乱：并发从"生成质量"迁到"创作岗位"；call_gap 归位；命名与内容对齐。
   - 预留「对话与上下文」分组（v2.9.0 填充）。
   - 全部复用现有 tokens.css 组件（btn/switch/segmented/badge），新样式进 settings.css。

### v2.9.0 —— 对话式创作工作台（章节卡片 + 分支成书 + 上下文自动压缩）

1. **每章 token 统计**：writer.py `_write_chapter` 写前/写后 diff `usage.json` 总量，写入计划章节 `meta.tokens`；后端不再需要新表。
2. **章节完成卡片**：扩展 `/api/chat/stream` 的 `chapter_done` 事件，携带 `{n, title, words, tokens, quality, ts}`；session.js 在对话流中渲染卡片——默认折叠（标题+完成时间+字数/token/质量分+正文前 3 行），点击展开全文、再点收起；卡片操作按钮：📋 复制本章、🌿 从此章分支、📖 在章节工作台打开；卡片事件本身也写入 chat_log.jsonl（带 type 字段），刷新后历史完整重放。
3. **分支成新书**：新增 `POST /api/branch {key, chapter}`——复制项目目录中第 1..N 章正文 + 写作计划/章卡/状态库/故事圣经等，新 key 加 `-分支N` 后缀、状态 in_progress，在作品库可见；对话卡片返回新书 key，前端可直接跳转；原书不受影响。
4. **对话上下文自动压缩（只影响展示对话，不影响后台创作）**：
   - 设置页新分组「对话与上下文」：上下文窗口档位 200K/400K/500K/1M（`defaults.chat_context_limit`，默认 200K）+ 压缩开关。
   - `/api/chat/stream` 组装历史时按 token 估算检查：超限的早期消息用 LLM 压缩成「创作进度摘要」存 `chat_summary.json`，作为 system 上下文注入；对话顶部显示"已压缩 N 条早期对话"胶囊，点击可查看摘要。
   - 后台写作流水线完全不经此路径，行为不变。

### v3.0.0 —— Agent 技能体系（默认技能包 + 全局/按作品双维度绑定）

1. **内置默认技能包**：agents.py 为 7 个岗位（规划师/作家/审稿人/改写师/拟题师/去味师/总编）配置默认技能名集合，首次读取时若该岗位未绑定过则自动填入（不覆盖用户已绑定的）。
2. **全局绑定**：设置页「创作岗位」的绑技能弹窗增强（按文体分组、搜索、显示技能简介），绑定写入 `defaults.agent_skills`（现有 /api/agents-skills）。
3. **按作品绑定**：计划文件 `meta.agent_skills = {agent: [skills]}`；新增 `GET/POST /api/book-skills`（key + agent + skills）；`workflow.agent_skills_block`（workflow.py:76）合并维度：本书绑定 ∪ 全局绑定（去重）；`/api/skills/applicable` 标注每个技能来源（全局/本书）。
4. **右栏协作面板**（collab.js 岗位行）：每个岗位加「绑技能」按钮，作用域为当前作品；显示 已绑技能 chips（本书/全局来源徽标区分）。

### 交付与质量门槛（每版相同）
- 每版完成：pytest 全量回归（当前 439 用例）+ 新增针对修复点的回归用例 + `tools/check_frontend_js.py` 语法检查 + 启动服务浏览器实测（设置页逐项操作、体检弹窗、时间线、章节卡片、分支、压缩胶囊、技能绑定）。
- 每版单独提交（中文规范信息）+ 打 tag（v2.8.0 / v2.9.0 / v3.0.0）+ 推送 GitHub；VERSION 单一来源同步更新；问题修复记录.md 逐版补记录。