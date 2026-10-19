# novel-bot · v0.1.0

聊天即开写：丢一个主题 → 自动 规划 → 逐章创作 → 质检 → 成书。内置**「一幕·神笔 · ShenBi NovelCraft」原创创作系统**（全平台通配网文），支持**多模型供应商**（自定义绑定、按任务选模型）、**续写**、**内容预览**、**确定性引擎 + 五角色审校 + 百分制达标**。

> 本版目标：① **任何一台装好 Python 3.9+ 的电脑（Win/macOS/Linux）直接跑，不依赖任何用户目录或外部技能仓库**；② **全部原创，可安全商业授权**（代码 MIT，技能文本自有版权，见 `LICENSE` / `NOTICE.md`）；③ **写作标准对标网文平台排行榜前列**。

## 快速开始

```bash
# 需要 Python 3.9+
python run.py            # 自动建 .venv、装依赖、起服务
```

- 打开 `http://127.0.0.1:46821`（默认端口，`python run.py -p 5000` 或 `NOVEL_BOT_PORT=5000` 可改）。
- 首次运行：① 生成 `config.json`（相对路径、无密钥）；② 网页右上角「模型供应商」绑定一个供应商 + 填 API key（或用环境变量）；③ 左侧丢主题开写。

各平台一键脚本：`./start.sh`（Linux/macOS）、`start.bat` / `start.ps1`（Windows）。

## 模型供应商（密钥按优先级自动取，缺了不崩）

对每个已绑定供应商，API key 依次取：
1. 网页「绑定/编辑供应商」里填的 inline key（存本地 `providers.json`）；
2. 环境变量：`AGNES_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `QWEN_API_KEY` / `GLM_API_KEY` / `KIMI_API_KEY`，或通用 `NOVEL_BOT_API_KEY`；
3. 本地 `secrets/<slug>.key`（项目内 `secrets/`，已 gitignore）；
4. 兼容旧 `api_key_file` 绝对路径。

> "像 git 那样"：`config.json` / `providers.json` / `secrets/` / `projects/` 都在 `.gitignore`——**每台电脑各自生成、各配各 key，不共享、不入库**。

## 创作技能：全原创，可商业授权

- 技能随包放在 `skill/`，**「一幕·神笔 · ShenBi NovelCraft」全部文字为原创**（`SKILL.md` + 9 篇 guide + 6 个模板 + 5 个题材 craft + 反 AI 腔 + 黄金三章门），**零第三方文本依赖**。
- 创作方法论吸收的是**业界通用思想**（故事圣经/伏笔账本/连续性核对/黄金三章/爽点密度/反 AI 腔等），不复制任何第三方技能文本；详见 `NOTICE.md`。
- `config.json.skill_path` 默认相对路径 `"skill"`；`bot/skill.py` 实时读取，改技能文本无需改代码。

## 目录

```
app.py              FastAPI 服务（路由 + 会话状态机）
bot/                创作引擎（llm / workflow / writer / providers / skill / continuity / wordcount）
skill/              内置「一幕·神笔 · ShenBi NovelCraft」原创技能（SKILL.md + guides + templates + genre）
static/             网页端（聊天 + 供应商管理 + 作品/预览/续写/审稿/质量报告）
projects/           运行期作品（gitignore，每台电脑各自）
providers.json      运行期供应商（gitignore）
LICENSE / NOTICE.md 代码 MIT + 技能原创声明 / 借鉴说明
requirements.txt    依赖（fastapi / uvicorn / httpx）
run.py              跨平台启动器
VERSION             版本号（0.1.0）
```

## 创作流水线（对标排行前列）

**规划**：生成 `00 人物` / `01 大纲` / `03 文风` + 4 个连续性结构文件（`defaults.v3_pipeline` 可关）：
- `09-故事圣经.md`（全书唯一事实来源）、`10-线索账本.md`（悬念/伏笔 + 兑现计划，随章追加）、
- `11-章节摘要.md`（随章补写）、`12-时间线与连续性.md`。

**逐章创作**：写前注入 故事圣经/线索账本/章节摘要/时间线 + **题材 craft（推理/言情/恐怖/都市/玄幻，全平台通配）** + **反 AI 腔清单**；前 3 章过**黄金三章门**。

**质检（三层）**：
1. **L1 确定性引擎**（`bot/continuity.py`，零 LLM）：伏笔闲置/超期未兑现/主角失踪/字数·爽点阈值/黄金三章门；
2. **L2 五角色 LLM 审校**（Architect/Scribe/Editor/Guardian/Curator）；
3. **L3 百分制**（结构20/因果20/人物15/连续性20/语言15/读者体验10），**达标线 90**；未达标 → 多轮 autofix → 重评。

产物：`13-修订记录.md`（五角色结论）+ `14-质量报告.md`（L1 + 五角色 + 百分制 + 达标判定）。

**P4 分阶段模型路由**：`config.json → defaults.stage_models.{planning,writing,review}`（空=沿用默认；可填 `{"provider_id","model"}`）。写稿/规划/审校各自可用不同模型/供应商（如写稿用 3.0-pro、审校用 flash）。

**P5 故事图谱 + 语义检索**（纯标准库，无外部 DB/向量库，仍"任何电脑可跑"）：
- **故事图谱** `bot/graph.py` → `15-故事图谱.json`：抽 人物/术语/伏笔为节点，按章取"相关实体 + 活跃伏笔"注入写作；
- **语义检索** `bot/retrieval.py`：TF-IDF 召回跨章既往片段，补"只看上一章"看不到的长期上下文；
- **读者人格 beta 读** `reader_panel`（默认关，按需开）：3 类读者试读→追读/弃书/建议，写入 14。

## v0.2：作品库管理 + 审查后定向重写

## v0.2.1：放行门禁 + 书名 ABC 弹选 + 开书向导
- **开书向导**（顶部「✍ 开书向导」）：题材/视角/基调/章数/卖点/设定/书名/供应商模型 几乎全可选（下拉+chips+输入），`GET /api/onboarding` 出枚举。
- **书名 ABC 弹选**：8 个候选 A/B/C…卡点选或自定义；不带 title 开书会停在 `awaiting_title`，`POST /api/pick-title` 选名开写；聊天报书名也弹可点卡。
- **放行门禁**：成书后自动带要求重写到 ≥达标线（默认90）才标记"放行✅"；未放行下载标"beta·未放行(分)"（仍可下载）。`POST /api/release` + `GET /api/release-log`；审稿视图「🔓 放行门禁」。

## v0.3.1：中档升级三点 + 稿件视图 + Token 统计

- **开书向导下拉修复**：`color-scheme:dark` + 原生 `select option` 显式深色样式 + 去掉 `.modal` 背景模糊，深色下拉可选可读。
- **中间工作台「稿件」视图**：`💬对话 / 📄稿件` 切换，稿件直接看已写正文（章节高亮）+「写到第 N/总章 · 共 X 字 · 累计 Y token（Z 次调用）」；右侧「产物」加 **Token 账本**（全书/各阶段/全局累计），「作品库」加全局 token 合计，当前书信息条加累计 token。
- **Token 统计后端**：`llm` 捕获每次调用的 usage → 按书/按阶段落盘（`usage.json` + `usage_global.json`，并发安全），`GET /api/usage`。真实 LLM 调用已验证采集正确。
- **左上角**改名「一幕·YIMU · 神笔小说工作台」。
- 提交人 `yimuhub <yimu@hyipf.com>`；tag `v0.3.1`。

## v0.3.0：三栏工作台 + 5 项能力升级

界面从单页升级为**左·中·右三栏工作台**（Hermes 风格，三栏宽可**拖动**、左栏可**折叠**为图标条），全部 v0.2.x 功能保留（兼容 superset）：
- **左 · 会话 = 书**：每本书一个"会话"，可**同时多本**（并发写，默认 2 本同时）；每本可**绑定不同模型/供应商**（输入区下拉）；**置顶**（📌）；状态点（进行中/待选名/已成书/已放行）；**完结(已成书+已放行)自动归档**到右侧作品库（可关）。
- **中 · 该书对话**：针对当前书的对话 + 快捷操作（续写/审稿/门禁/下载/预览，按书的状态动态显示）；"当前书"上下文条。`?book=<key>` 深链直接定位某本书。
- **右 · 3 个 tab**：产物（该书进度/预览/质量报告）/ 设置（该书 门禁·定时·题材×风格技能 + 模型供应商 + 模型规则）/ 作品库（全局，按状态/题材）。

**5 项能力**：
1. **并发 + 定时任务**（默认关）：`defaults.concurrency=2` 并发信号量（最多 2 本同时写，超出排队）；每本可设「每 N 天写一批 N 章」，`schedules.json` + 60s 调度器巡检触发，已完成书自动停用。`GET/POST /api/schedule`。
2. **完结自动归档**：已成书且放行的书从左栏移入右侧作品库（`localStorage.nbot_autoarch` 可关）。
3. **题材×风格技能**（16 题材 × 金手指/基调/视角/节奏/语言/卷）：开书向导选题材→自动填推荐风格（可改）；写入 `style.json`，**craft 注入**到逐章创作；设置 tab 可换该书技能。`GET/PUT /api/style` + `GET /api/onboarding`（含 style 目录）。
4. **模型规则引擎**（`bot/model_rules.py`）：按 `base_url` 自动识别 厂商（Agnes/DeepSeek/OpenAI/Gemini/通义千问/智谱GLM/**通用**）→ 套 上下文/max_tokens/RPM/TPM/429退避/extra_body/免费限额；**通用=用户自由配置**（`model_rules.json`）。`GET/POST /api/model_rules`。
5. **GitHub 私有仓库**：`github.com/yimuhub/yimushenbi`，每个版本打 tag（`v0.3.0`）。

**P4 端点**：`/api/schedule`（定时）、`/api/style`（该书技能）、`/api/model_rules`（模型规则）。原有 `/api/*` 全部保留。


**作品库管理**（`/api/library` + 前端工具条）：富元数据（题材/时间/字数/质量分/达标/状态/收藏/标签/归档）；**多键排序**（时间/题材/完成度/质量分/字数/章节/状态，升降序）；**筛选**（状态/题材/达标性/搜索）；**统计 + 题材分组**；**收藏/标签/归档（回收站）/批量**（批量审稿/对比/收藏/归档/恢复）；**两本对比**。

**审查后定向重写**（未达标一键带要求重写）：`POST /api/rewrite-plan` 从 13/14/L1/读者 提炼**全局主攻方向（最低维度）+ 逐章待修**，写 `16-重写要求书.md`；`POST /api/rewrite` 把要求书**逐条注入**目标章定向重写（后台线程）→ 重评出**前后对比** → 写 `16-重写记录.md`。前端：审稿视图「🔁 重写」→ 方向卡片（勾选章节）→ 执行 → 前后分对比。

## API（v0.2 新增/增强）

| 端点 | 说明 |
|---|---|
| `POST /api/create {title?, genre?, total_chapters?, cfg_overrides?}` | REST 直接开书（`cfg_overrides` 可带 `stage_models`/`p5_pipeline`/`reader_panel`），返回 `{project,pid,key}` |
| `GET /api/library?sort&order&genre&status&q` | 作品库：多键排序 + 筛选 + 统计 + 题材分组（`status=archived`=回收站） |
| `POST /api/library/{key}/favorite\|tags\|archive` | 收藏 / 标签 / 归档（回收站） |
| `POST /api/library/batch {keys, action}` | 批量：审稿/对比/收藏/归档/恢复 |
| `GET /api/compare?keys=a,b` | 两本对比 |
| `GET /api/onboarding` | 开书向导枚举（题材/基调/卖点/视角/章数 + 默认门禁/P5） |
| `GET /api/titles?key=` | 8 个书名候选（A/B/C…） |
| `POST /api/pick-title {key,title}` | 选名（改项目名 + 开写） |
| `POST /api/release?key` | 放行门禁（后台自动带要求重写到 ≥达标线） |
| `GET /api/release-log?key=` | 取门禁进度/结果/放行判定 |
| `POST /api/rewrite-plan {key}` | 生成重写方向（主攻维度 + 逐章待修），写 `16-重写要求书.md`，不改正文 |
| `POST /api/rewrite {key, chapters?, dimensions?}` | 审查后定向重写（后台线程）+ 重评前后对比，写 `16-重写记录.md` |
| `GET /api/rewrite-log?key=` | 取重写进度/结果/记录 |
| `GET /api/preview?key=` | 作品预览（成书或已写章节 + 章节清单） |
| `POST /api/validate {key, mode, max_rounds}` | 跑 确定性引擎+五角色+百分制（`report`=诊断 / `autofix`=未达标有界重写），结果写 `13`/`14` |
| `GET /api/quality?key=` | 取 `14-质量报告.md` |
| `GET /api/review?key=` | 取 `13-修订记录.md` |
| `GET /api/version` | 当前版本号（0.2.0） |

网页端：侧栏**作品库工具条**（🔍搜索 / 多键排序 / 状态·题材·达标·回收站筛选 / 统计 / ★收藏 / ☑批量·对比）；作品点「👁 预览」看内容/选章节；成书后「📝 审稿」出**质量报告 + 达标判定**，未达标再点「🔁 重写」按审查结论 + 主攻方向定向重写并看**前后分对比**；「⬇ 成书」下载。

## 说明

- 核心功能只增不删：旧作品照常 读 / 续写 / 预览 / 下载 / 审稿 / 质检（缺新文件自动省略）。
- 质检默认 `report`（出报告不改动正文），`defaults.validation` 可调 `mode`（`report`/`autofix`）、`enabled`、`max_fix_rounds`、`pass_line`（默认 90）；`defaults.v3_pipeline` 控制是否生成 4 个连续性文件（默认开）。
- P4/P5 开关（`defaults`）：`stage_models`（分阶段模型路由，见上）、`p5_pipeline`（故事图谱+语义检索，默认开）、`reader_panel`（读者人格 beta 读，默认关）、`use_prefix_cache`（稳定前缀利于供应商提示词缓存）。
- **节奏/超时（防限流）**：`call_gap`（两次 LLM 调用最小间隔秒，默认 2，自动平滑批量节奏、规避 429）、`llm_timeout`（每调用超时秒，默认 900）、`planning_timeout`（规划环节单独放宽，默认 1200）。
- **版权**：代码 MIT，技能文本自有版权；商业授权时连同 `LICENSE` / `NOTICE.md` 交付，`config/providers/secrets/projects` 不入库。


> 门禁开关：`config.json → defaults.release_gate`（true=成书自动跑；false=仅手动）。每轮改写 top-3 章（`max_release_rewrite`）。
