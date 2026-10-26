# 神笔文学创作台

> 一个产品，四类文学创作：**小说 · 剧本 · 诗词 · 散文**。
> 纯文字创作，**不含任何 AI 视频 / 图像生成能力**；剧本面向**真人拍摄**。
> 技能库全部内置，任意电脑解压即跑。

```bash
pip install -r requirements.txt
python run.py          # 默认 http://127.0.0.1:46821
```

打开首页即进入 **v2 新界面**（类 DeepSeek 风格：侧栏会话 + 命令面板 + 三栏可调工作台）；
旧版界面仍保留在 `/classic`。`?mock=1` 可在零配置下体验全部交互（内置模拟数据，不调模型）。

---

## 〇、v2.0 升级了什么

| 能力 | 说明 |
|---|---|
| **DSH 式新界面** | 全新 `static/v2/` 前端（28 个 ES module，无构建步骤）：会话式侧栏、`Ctrl/⌘ K` 命令面板、三栏布局（侧栏 / 对话 / 工作台）可拖拽调宽、浅色 / 深色 / 跟随系统主题、完整键盘快捷键与焦点陷阱、`?mock=1` 演示模式 |
| **百万字长篇引擎** | `bot/longform.py`：总规划 → 卷 → 弧 → 章四层结构，规模档位 30 万 / 100 万 / 300 万 / 500 万字（100–1500 章）；滚动规划与**重规划提案**（结构可中途调整而不失连贯）；写前注入「前情摘要 + 活跃伏笔 + 人物状态 + 设定词典」；字数门禁与章末钩子自检 |
| **参考资料先行** | 开书向导第 3 步即可上传参考（txt / md / docx / pdf / epub ≤20 MB、粘贴文本、网址），也支持作品内「参考」面板随时补充；`bot/references.py` 抽取正文入库，`bot/retrieval.py` 写作时按章检索注入（RAG），用途分「事实 / 文风」两类 |
| **技能进化（越用越好用）** | 对回复打分 / 标签沉淀为反馈样本（`bot/feedback.py`）；聚合为写作画像与长短板（`bot/learn.py`）；一键从反馈生成候选技能 → 评测对比当前版本 → 达标后由你转正（`bot/skill_evo.py`），全程版本化、可回滚 |
| **四体裁统一开书向导** | 小说 / 剧本 / 诗词 / 散文共用三步向导：体裁与模式 → 基本信息 → 参考与文风；创建后自动进入会话视图逐章推进 |

---

## 一、这是什么

以「神笔小说」v0.9.2 为基座，融合**剧本 / 诗词 / 散文**三类创作能力形成的文学创作台。

- **底座完整继承**：多供应商 LLM 路由、内容缓存、300s 超时、429 退避、全局并发闸、
  风格蒸馏、TF-IDF 检索、故事图谱、连续性检查、偏好学习、写作骨架、100 分审校、
  逐字直播改写、并行章节写作、24 篇改写记录。
- **技能库 58 个文件（~108 KB，v1.0.0）**：
  小说 32 · 剧本 12 · 诗词 4 · 散文 5 · 全文体共核 5，全部随产品分发。
- **确定性校验器**：诗词格律（平水韵 105 韵部 · 8232 字）与反 AI 腔检测，
  用规则而非 LLM 猜，出错可复现。
- **7 个 Agent 岗位**（可各自绑定独立模型）：
  规划师 / 作家 / 审稿人 / 改写师 / 拟题师 / **去味师（新增）** / **总编（新增）**。

---

## 二、四条创作流水线

| 文体 | 入口 | 阶段表 | 评估（100 分） | 目录前缀 |
|---|---|---|---|---|
| **小说** | 网文 / 通用长篇 | 规划 → 黄金三章 → 逐章写 → 连续性 → 审校（自动 deslop 去味）→ 定稿 | 结构20/因果20/人物15/连续性20/语言15/体验10 | `projects/<key>/` |
| **剧本** | 长片 / 剧集 / 短片 / 舞台剧 / 戏曲 | S0 引擎 → S1 人物网与季弧 → S2 文档 → S3 试播结构 → S4 分集大纲 → S5 写作 → S6 修订 → S7 交付 | 前提25/结构25/人物20/对白15/格式10/可行性5 | `projects/s_<key>/` |
| **诗词** | 近体诗 / 词 / 现代诗 | 释读 → 规划 → 起草 → 校验（三态 pass/fail/unclear）→ 评改 → 定稿 | 意象30/章法25/声律25/情感20 | `projects/p_<key>/` |
| **散文** | 随笔 / 抒情 / 议论 / 叙事散文 / 书评 / 人物特写 | brief → 素材 → 起草 → 去味（slop 报告）→ 精修 → 定稿 | 表达30/结构25/真实度25/音律20 | `projects/e_<key>/` |

**散文四操作**：`write`（从 brief 起草）/ `review`（只诊断不改稿）/ `refactor`（就地最小修改）/ `recreate`（整篇重写）。

---

## 三、核心概念

### 3.1 最小上下文注入

技能库 ~108 KB，**绝不整库塞进 prompt**。`bot/skillkit.py` 按「文体 + 阶段 + 题材」
精确取用到的 3–6 个文件拼装 system 上下文；校验器走 tool call 而非注入；
案例库（剧本原文 / 戏曲全本）默认不挂载，仅在用户显式点名时加载。

**题材定向注入**（`genre=` 参数）：
- 小说：`历史` / `科幻` / `仙侠` / `职场` / `军事` / `悬疑` / `言情` / `恐怖` / `都市`
- 散文：`随笔` / `抒情` / `议论` / `叙事` / `书评` / `特写`

### 3.2 技能文件格式

每个技能文件带 YAML frontmatter：

```yaml
---
name: sw-premise-theme
title: 前提 · 立意 · 戏核
medium: [script]
stages: [S0, S1, P0]
kind: method          # method | case | data | template
weight: 3
license: MIT
source: 方法借鉴自 jtydhr88/screenwriting-skills (MIT)，原创重写
---
```

### 3.3 确定性校验

| 校验器 | 覆盖 | 输出 |
|---|---|---|
| `bot/poetry_checker.py` | 五绝/七绝/五律/七律平仄、对仗、押韵（平水韵 105 部 · 8232 字）、孤平 | YAML 报告，`pass` / `fail` / `unclear` 三态（多音字/古音变体不装确定） |
| `bot/slop_checker.py` | 反 AI 腔：夸大词、模糊权威、空洞过渡、元话语、空洞形容、套话开场/结尾、机械排比、电报体、句长均一、抽象名词密度、高频 2-gram 复现 | `blocking` / `advisory` 两级 + 行号定位 + `top_words()` |

### 3.4 deslop 定点去味（全文体通用）

`workflow.deslop_chapter(ch_body, ctx)` 两阶段，**不装确定**：

1. **纯规则层**：`slop_checker.scan` → blocking/advisory findings + 高频 2-gram（不调 LLM，零误报定位）
2. **LLM 最小改法**：findings 交给去味师（deslopper 岗位），只修被指出的位置，
   保留情节/钩子/字数量级；LLM 失败自动保留原文

小说流水线 `writer._auto_rewrite_chapter` 在 LLM 重写前自动跑一次 deslop（沿用原章还原逻辑防改坏）。

---

## 四、目录结构

```
yimu-lit/
├── app.py                  FastAPI 入口（180+ 端点；v2 新内核路由经 bot/v2api.py 注册，缺失依赖时 501 降级）
├── bot/                    引擎层
│   ├── llm.py              LLM 层：缓存/超时/重试/stage→agent 路由/预算/调用日志
│   ├── providers.py        多供应商 + 全局并发闸
│   ├── longform.py         百万字长篇引擎：总规划/卷/弧/章、规模档位、重规划提案、前情注入
│   ├── references.py       参考资料上传/抽取/关联（txt md docx pdf epub ≤20 MB）
│   ├── ingest.py           参考正文抽取与分块入库
│   ├── retrieval.py        写作前按章检索参考资料（RAG 注入）
│   ├── skill_evo.py        技能进化：候选生成 → 评测 → 转正/驳回 → 版本回滚
│   ├── learn.py            写作画像与长短板聚合（ strengths / weaknesses / focus ）
│   ├── feedback.py         反馈样本沉淀（评分 / 标签 / 意见）
│   ├── v2api.py            v2 新内核 API 装配层
│   ├── writer.py           小说主引擎（自动 deslop 去味 / 预算暂停 / 成书重建）
│   ├── workflow.py         小说流程编排 + deslop_chapter（两阶段）
│   ├── medium.py           文体注册表（阶段表/评估维度/目录规范/自动路由）
│   ├── skillkit.py         技能库索引 + stage_inject（genre 定向）
│   ├── agents.py           7 岗位注册表（含 deslopper / editor）
│   ├── script_writer.py    剧本流水线
│   ├── poetry_writer.py    诗词流水线
│   ├── prose_writer.py     散文流水线（四操作）
│   ├── poetry_checker.py   诗词格律校验器（平水韵 105 部）
│   ├── slop_checker.py     反 AI 腔检测器（纯标准库零依赖）
│   ├── safeio.py           路径安全（safe_join）+ 原子写（tmp+fsync+replace）
│   ├── security.py         启动令牌鉴权 + TrustedHost 白名单
│   ├── jobs.py             可查询/可持久化的后台任务表
│   ├── versions.py         章节版本快照 / 差异 / 回滚
│   ├── export.py           DOCX / EPUB3 / 打印 HTML / Fountain / FDX 导出
│   ├── lorebook.py         设定词典关键词检索（按章精准注入）
│   └── ...                 骨架/图谱/检索/蒸馏/学习/连续性/模型规则
├── skillkit/               多文体技能库（58 文件）
│   ├── _core/              全文体共核：反AI腔总纲 / 文体总则 / 文风指纹与校准 / 组合协议
│   ├── novel/              小说（10 题材 craft + 追读曲线/书名简介/黄金三章 + 指南）
│   ├── script/             剧本（通用/剧集/戏曲 + 案例/大师占位）
│   ├── poetry/             诗词（近体诗/词/现代诗/平水韵数据/词牌表）
│   └── prose/              散文（总纲/随笔/抒情/议论/叙事）
├── static/v2/              v2 新界面（类 DSH）：index.html + 28 ES module + 6 CSS + mock 演示数据
│   └── js/{core,components,views}/   状态/路由/SSE/主题 | 侧栏/顶栏/命令面板/工作台 | 会话/向导/作品库/进化/设置/章节台
├── static/index.html       旧版 SPA 外壳（/classic 仍可访问）
├── samples/示例书-雨夜出租车/  内置示例书（3 章，可一键加载）
├── tools/check_frontend_js.py  前端 JS 语法门禁（旧版内联 + v2 27 module，CI 用）
├── tests/                  pytest 185 项（含 longform / references / skill_evo / v2api 回归）
├── .github/workflows/ci.yml   CI：ruff / pytest / 前端语法 / pip-audit
├── Dockerfile / docker-compose.yml  非 root 容器 + 健康检查
├── data/  providers.json  config.json  secrets/  projects/   ← 运行时数据，不入库
└── VERSION                 版本号单一来源（v2.1.0）
```

---

## 五、配置

首次运行复制示例配置：

```bash
cp config.example.json config.json
```

在 `config.json` 里填 LLM 供应商与密钥。`config.json` / `providers.json` / `secrets/` /
`projects/` / `data/` 全部在 `.gitignore` 内，**每台电脑各自生成**。

`config.example.json` 关键项：

| 键 | 说明 |
|---|---|
| `llm.base_url` / `api_key` / `model` | 默认供应商 |
| `defaults.chapters` | 默认章数 |
| `defaults.validation.pass_line` | 审校通过线（默认 90） |
| `defaults.llm_timeout` / `planning_timeout` | 超时秒数（默认 300） |
| `defaults.chapter_workers` | 并行写章并发数 |
| `defaults.max_global_agents` | 全局 Agent 并发上限 |
| `defaults.token_budget` | 单本 token 预算（0=不限；超限自动暂停，可在设置页热改） |
| `defaults.lorebook` | 是否按章检索设定词典并注入（默认开） |
| `defaults.llm_trace` | 是否写结构化调用日志 `llm_calls.jsonl`（不含 prompt/密钥） |
| `defaults.llm_cache` | 是否启用确定性 prompt 内容缓存（默认关） |

以上开关也可以在界面「⚙ 设置 → 🧪 质量·预算·日志」里改，保存到本机 `config.json`。

---

## 六、脚本

```bash
python run.py            # 启动
./start.sh               # macOS/Linux 启动脚本
start.bat / start.ps1    # Windows 启动脚本

# 开发与验证
pip install -r requirements-dev.txt
ruff check app.py bot tests run.py tools build.py
python -m pytest -q
python tools/check_frontend_js.py    # 校验前端内联 script 语法（需要 node）

# Docker
docker build -t yimu-lit .
docker compose up -d                 # http://127.0.0.1:46821
```

Docker 镜像以非 root 用户运行，内置 `/api/version` 健康检查；`projects/`、`secrets/`、
`config.json`、`providers.json` 通过 volume 挂载，不写进镜像。CI 见 `.github/workflows/ci.yml`。

---

## 七、许可与版权

见 [NOTICE.md](NOTICE.md)。要点：

- **代码**：MIT。
- **技能库方法论层**：原创重写并逐条标注来源（MIT）。
- **数据层**：`skillkit/poetry/data/pingshui.json`（平水韵 8232 字）为公版数据整理；
  `skillkit/poetry/词/词牌格律表.md`（52 词牌）为原创整理。
- **语料层**（剧本原文 / 戏曲全本 / 已出版作品引文）：默认不随产品分发，
  仅在用户显式提供时挂载到 `skillkit/_external/`（gitignored）。

---

## 八、版本

| 版本 | 内容 |
|---|---|
| v2.1.0 | 剧本读得懂了：场次卡片点开即读正文（←/→ 翻场、Esc 关闭）、整本连续预览、新增 `GET /api/script/preview` 一次取回全部场次正文；`/api/script/scene` 补齐项目校验与场次文件名兜底。对话不再"假死"：SSE 每 2 秒心跳并显示已等待秒数（8 秒升为「深入思考中」，60 秒提示网关慢）、未配置模型时直接回兜底文案并引导去设置、终稿以 `done.text` 权威覆盖、中断时标注「已显示 N 字」 |
| v2.0.0 | 新一代内核与界面：DSH 式 v2 前端（会话侧栏 / 命令面板 / 三栏可调工作台 / 主题 / mock 演示）；百万字长篇引擎（总规划-卷-弧-章四层 + 滚动重规划 + 前情注入 + 字数门禁）；参考资料上传与 RAG 检索（开书向导先行、按章注入、事实/文风分途）；技能自进化（反馈沉淀 → 写作画像 → 候选技能 → 评测转正 → 版本回滚）；四体裁统一三步开书向导；章节工作台（版本对比 / 恢复 / 自动保存）；作品库（ facet 筛选 / 网格列表 / 重命名 / 导出 / 回收站） |
| v1.4.0 | 安全与可靠性整改：路径穿越 / SSRF / 存储型 XSS / 启动令牌与 Host 白名单；异步分发 + 持久化任务表 + SSE 实时；章节编辑器 + 版本快照 / 差异 / 回滚；DOCX / EPUB / 打印 / Fountain / FDX 导出；单本 token 预算暂停；Lorebook 设定检索；逐章评分证据；结构化 LLM 调用日志；全局设置页；内置示例书；Docker / CI / 前端语法门禁 |
| v1.0.0 | 立项 + PR1–PR6 全部完成：四文体路由 / 剧本+诗词+散文流水线 / 58 文件技能库 / 7 Agent 岗位 / deslop 定点去味 / 文体自动分流 / 散文 UI |
| v0.9.2 | 神笔小说 v0.9.2：大厂级代码终极审查（基座） |

版本号单一来源 = `VERSION` 文件（前端徽标 / `/api/version` / 测试同步）。

---

## 九、用户快速上手

### 9.0 v2 界面速览
- `#/welcome` 新会话：一句话 premise 开书，或点「已有作品」继续；
- `Ctrl/⌘ K` 命令面板：搜作品 / 跳章节 / 全部动作；`N` 新建，`Ctrl/⌘ B` 收侧栏，`Ctrl/⌘ J` 开关工作台；
- 右侧工作台五个页签：作品（进度 + 章节格 + 实时日志）/ 规划（卷弧章 + 重规划）/ 状态（人物 / 伏笔 / 时间线）/ 参考（上传 / 检索）/ 技能；
- `#/library` 作品库：题材 / 状态 facet 筛选、网格 / 列表、重命名 / 导出 / 回收站；
- `#/evolution` 技能进化：反馈 → 候选 → 评测 → 转正，版本可回滚；
- 首次体验无模型：加 `?mock=1` 或在设置里按 9.7 配 Ollama。

### 9.1 写小说
对话里说「都市悬疑 · 退休刑警翻盘，50 章」→ 配置菜单 → 开写。
或点左侧「＋新建」/ 命令面板「新建作品」走三步开书向导（规模档位选到 500 万字 / 1500 章，
引擎会按卷 / 弧分层规划并在写作中持续重规划）。

### 9.2 写剧本
向导第一步选「剧本」→ 生成剧本规划（S0 引擎 → S7 交付）；
参考剧本资料（人物小传 / 场景笔记）可在第 3 步先行上传。

### 9.3 写诗词
对话里说「七律 · 秋夜」或「水调歌头 · 中秋」→ 自动路由到诗词流水线（不经小说流程）。

### 9.4 写散文
向导选「散文」→ 选文体（7 种）× 操作（4 种）+ 需求描述 → 六阶段生成。

### 9.5 带参考资料创作
开书向导第 3 步上传 / 粘贴参考（≤20 MB，支持 txt / md / docx / pdf / epub），标注用途
（事实 → 进设定检索；文风 → 进风格对齐）；作品内「参考」面板可随时补充并即时检索试写。

### 9.5 去 AI 腔
`POST /api/slop/scan` 纯规则扫描（零 LLM 调用）；
小说每章审校自动注入 slop 定位；定稿前手动跑一次 slop/scan 确认。

### 9.6 看示例书（零配置）
左侧任务区为空时，点「🎁 加载示例书」→ 复制《雨夜出租车》到作品库 →
自动打开对话区，可直接预览、编辑、看评分证据、导出。示例书不依赖任何模型。
也可以用 `POST /api/sample` 在脚本里加载。

### 9.7 用 Ollama 本地模型（不需要 API Key）
1. 安装 [Ollama](https://ollama.com/) 并运行 `ollama serve`；
2. 下载模型：`ollama pull qwen2.5:7b`（7B 约 4.7 GB，机器较弱可换 `qwen2.5:3b`）；
3. 打开「⚙ 设置 → 🤖 模型规则 → ＋ 新增供应商」，预设选「Ollama 本地」；
4. Base URL 保持 `http://127.0.0.1:11434/v1`，API Key 留空，默认模型 `qwen2.5:7b`；
5. 保存 →「🔍 测试连通」→「设为默认」，即可开写。

### 9.8 编辑、版本与导出
- v2 章节工作台（作品 → 章节格 → 章节）与旧版编辑器均支持：停止输入后自动保存，`Ctrl/Cmd+S` 立即保存；
- 「版本历史」查看快照、逐行差异、一键恢复（每次改写/编辑/修复前自动留快照，保留 30 份）；
- 导出支持 Markdown / DOCX / EPUB3 / 打印 HTML；剧本支持 Fountain / Final Draft XML（`.fdx`）；
- 长任务在后台跑，任务面板显示排队/运行/完成/失败；对话区通过 SSE `/api/live/{key}` 实时刷新，断线自动回落轮询。

### 9.9 让它越用越好用（技能进化）
1. 在会话里对回复打分 / 打标签 / 留一句意见（如「结尾太仓促」）；
2. `#/evolution` 页查看写作画像（常写题材、长处 / 待改进、下一步建议）；
3. 点「从反馈生成候选」→ 系统蒸馏出候选技能并给出与当前版本的对照 diff；
4. 点「评测」跑同一基准对比 → 达标后「转正」，不满意可「驳回」或随时回滚到任意历史版本。

---

## 十、安全、可靠性与可观测性（v1.4.0）

### 10.1 默认只对本机开放
- 启动时生成一次性访问令牌，首页下发 `HttpOnly + SameSite=Strict` Cookie；没有令牌的 `/api/*` 请求返回 401。
- `TrustedHostMiddleware` 默认只接受 `127.0.0.1` / `localhost` / `::1`；需要局域网访问时设置
  `SHENBI_TRUSTED_HOSTS=192.168.1.10,myhost.local`，并自行评估暴露风险。
- 追求极致方便可在可信环境设置 `SHENBI_AUTH=0` 关闭令牌（不建议在共享网络上使用）。

### 10.2 文件与网络边界
- 所有作品 key、章节文件名、版本 id 都经过 `safe_join` / basename 校验，拒绝 `../`、绝对路径、NUL。
- 章节/计划写盘统一走 `atomic_write_json/text`（临时文件 + fsync + 原子替换），断电或并发不会留下半截 JSON。
- 风格蒸馏抓取网址走 SSRF 防护：拒绝回环、私网、云元数据地址与非 http(s) 协议。
- 前端所有动态文本经 `esc` / `escAttr` 转义；错误信息按文本渲染，避免存储型 XSS。
- `providers.json` / `secrets/` / `config.json` / `projects/` / `jobs.json` / `llm_calls.jsonl` 全部 gitignore；
  LLM 日志只记录阶段、模型、token、耗时、错误，字段白名单，不写 prompt 正文与密钥。

### 10.3 成本与质量
- `defaults.token_budget`：单本累计 token 达上限后，写作线程在下一章前自动暂停并标记 `paused`，
  调高预算或设为 0 后点继续即可恢复。
- `defaults.lorebook`：写每章前从 `09-故事圣经.md` 的「设定词典」按关键词挑 3–6 条注入，
  比整本设定塞进上下文更省 token，也降低设定冲突。
- 逐章评分新增「评分证据」：每条低分项附原文引句与理由，可在预览弹窗查看 tooltip。
- 「⚙ 设置 → 🧪 质量·预算·日志」可热改审稿强度、预算、Lorebook、调用日志，无需重启。

### 10.4 可观测性
- `GET /api/jobs?key=&kind=` 查询后台任务生命周期；`GET /api/live/{key}` 订阅 SSE。
- `GET /api/llm-calls?limit=50` 查看最近的模型、阶段、token、耗时、缓存命中。
- `GET /api/usage?key=` 查看单本用量、预算、剩余额度；不带 key 查看全局。
- 运行日志、质量报告、修订记录仍在每本书目录内，便于事后复盘。
