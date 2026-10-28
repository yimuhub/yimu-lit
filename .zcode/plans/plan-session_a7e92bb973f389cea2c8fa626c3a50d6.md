## 升级改造方案（四版递进，每版独立提交推送）

所有根因已定位（探索报告含 file:line）。四个版本：**v3.1.0 数据正确性修复 → v3.2.0 全平台对话统一 → v3.3.0 剧本一致性+拟题泛化 → v4.0.0 UI 统一升级**。

---

### v3.1.0 —— 数据正确性修复（9 个 bug 全部根因修复）

1. **章节标题缺失**（21/28 本书 100% 默认"第N章"）：`writer.py:90` 与 `longform.py:569` 的解析正则放宽（接受 `| 01 |`/`| 1 |`/全角数字）；`workflow.py:119` 大纲提示词强制首列写"第N章"；新增**章名回流**：`_write_chapter` 完成后用已有 chapter_meta 摘要让 LLM 起 4-8 字章名回填计划（不额外读全文），并同步重写章节文件头；每 10 章批量回填已完成章（复用里程碑机制）。老书提供一次性回填（右栏触发）。
2. **"校验中"永久卡死**：启动时 `_lifespan` 加 `_recover_stale`——`validating/releasing` 且线程死亡的书：有内容的 in_progress/failed 章复位 completed、status 回 in_progress；`/api/book-live` 与 `Project.status()` 同样判定 stale，前端立刻不再转圈。长安诡案、民国·半仙、卡片验证三本实测卡死书自动救回。
3. **"差一章"卡住**：校验循环只重写真失败章（`status==failed` 或字数 < 0.6*min）；soft-pass（完成但未达标）直接放行编译；重写进度逐轮落盘可续；单轮重写有界（≤10 章）。并行循环全完成的捷径补跑成书编译。
4. **复制全文含概要/备注**：`GET /api/chapter` 增加 `cleanText`（复用 `_clean_chapter_text`：章题+引子+正文，剥概要/备注，并补"仅概要无引子"分支）；bench.js 复制改用 cleanText。成书/导出已干净，不动。**口径＝引子+正文**（与成书一致）。
5. **邀请 Agent 报"没有 Agent"**：composer.js:109 `res.view.agents` → `res.agents`（后端结构本就是顶层 agents）。
6. **剧本预览"没有场次"**：`/api/script/preview|scenes` 在 plan.scenes 为空时回退现场解析 03-场次清单.md 并回填计划；`_SCENE_ROW` 正则放宽（第N场/全角数字/标题式）。
7. **"继续创作"按钮状态不变化**：script/poetry/prose 引擎维护 `self.running`（写线程启动置位/finally 复位）；`writer.py:1699` library 的 running 不再硬编码 False；按钮按 running 切换「继续创作 ↔ 停止创作」，各引擎阶段写入运行日志（已有）并配合 /api/book-live 式阶段展示。
8. **向导完成后断层**：修 `writer.py:1732` label bug（0 章 plan 显示"已完成"→ 规划中）；向导 submit 成功后把所选配置合成一条用户消息，进入对话后自动发送，Agent 反馈并开写（v3.2 深化，本版先修状态断层与自动开写）。
9. **风格预设可点**：`style_preview` 从只读 preview 改为逐维度可点选 chips（金手指/基调/爽点/节奏/语言/卷），值并入 `payload.style`（后端 `/api/create` 已支持 cfg.style）。

### v3.2.0 —— 全平台对话统一 + 对话改稿同步

1. **创建即对话**：四种文体新建完成后都进入对话界面，向导所选配置自动作为用户消息发出，Agent 确认并开始创作（复用 chat 流的 attached 机制与追章/追进度事件；剧本/诗词/散文的"追进度"用各自引擎的阶段事件映射成 chapter_start/done 风格的 SSE）。
2. **小说工作台加"对话改稿"**：assist 的 MEDIA 白名单加 novel；`docs_for` 加小说分支（章节文件）；段落定位加"第N章第M段"；bench.js 挂 chatfloat。
3. **对话改稿 ↔ 主对话同步**：assist 落盘时同步写 chat_log.jsonl（带 type=assist 徽标），主对话上下文能看到改稿讨论；压缩摘要/@Agent 均可见。
4. **跨文体 @Agent**：assist 与主对话支持 @ Agent 分流；`_chat_group` 上下文按文体取（剧本读场次/圣经，诗词/散文读 docs_for）；被 @ 的 Agent 注入其岗位技能（skillkit stage_inject 已支持 medium）。

### v3.3.0 —— 剧本一致性 + 拟题泛化 + 进度可视

1. **剧本一致性体系**（借鉴小说）：新建剧本状态库（人物表/场景表，来源：场次清单+创作圣经）；逐场体检（人物在场校验/场景重复跳场 + 既有时代校验）落 `22-剧本体检.json`；新端点 `/api/script/audit`；右栏复用体检区 UI；违规场次接现成的 `/api/script/rewrite-scenes` 按设定重写。
2. **拟题泛化**：修 `_apply_title` 只写小说计划的 bug（改走 `_plan_of`/rename_book）；`gen_titles` 加 medium 参数；标题候选区进剧本/诗词/散文的右栏；对话内"换标题"命令三文体通用。
3. **进度可视**：各工作台/对话顶条展示当前阶段（S0-S7/I-P-C-K-R-F/B-S-D-A-P-F），来自运行日志阶段事件的统一解析。

### v4.0.0 —— UI 统一升级

1. **章节工作台美化**：对齐设置页卡片风格（分组卡片、统一间距与层次），编辑器/版本历史/对话改稿三区协调。
2. **作品库 / 技能库 / 技能进化页面**升级为设置页同款风格：左侧或顶部分组导航 + 内容卡片化 + 统计条（复用 set-nav/set-group 样式体系）。
3. **左侧栏作品展开面板**（点开书后）视觉升级：分组清晰、状态徽标统一、操作按钮层级化。

### 交付门槛（每版相同）
pytest 全量回归（现 453 用例）+ 每个修复点带回归用例 + 前端语法检查 + 服务启动浏览器实测（含三本卡死书的救回验证）+ 中文提交 + tag + 推送 GitHub + 问题修复记录.md 逐版补记。