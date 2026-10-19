# novel-bot · v0.0.3

聊天即开写：绑一个主题 → 自动 规划→逐章创作→自动校验→成书。内置 chinese-novelist v3.0 创作系统，支持**多模型供应商**（可自定义绑定、按任务选模型）、**续写**、**内容预览**、**分层校验**。

> 本版目标：**任何一台装好 Python 3.9+ 的电脑（Win / macOS / Linux）都能直接跑，不依赖任何特定用户目录或外部技能仓库。**

## 快速开始

```bash
# 需要 Python 3.9+（已装则直接）
python run.py            # 自动建 .venv、装依赖、起服务
```

- 打开 `http://127.0.0.1:46821`（默认端口，可用 `python run.py -p 5000` 或 `NOVEL_BOT_PORT=5000` 改）。
- 首次运行：
  1. 生成 `config.json`（相对路径、无密钥）；
  2. 你在网页右上角「模型供应商」里**绑定一个供应商 + 填 API key**（或用环境变量，见下）；
  3. 直接在左侧丢主题开写。

各平台一键脚本：`./start.sh`（Linux/macOS）、`start.bat` / `start.ps1`（Windows）。

## 提供模型供应商（密钥按优先级自动取，缺了不崩）

对每个已绑定供应商，API key 依次取：
1. 网页「绑定/编辑供应商」里填的 inline key（存本地 `providers.json`）；
2. 环境变量（按供应商 slug，大写）：`AGNES_API_KEY`、`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`QWEN_API_KEY`、`GLM_API_KEY`、`KIMI_API_KEY`，或通用的 `NOVEL_BOT_API_KEY`；
3. 本地 `secrets/<slug>.key` 文件（项目内 `secrets/` 目录，已 gitignore）；
4. 兼容旧配置 `api_key_file` 绝对路径（向后保留）。

> “像 git 那样”：`config.json` / `providers.json` / `secrets/` / `projects/` 都在 `.gitignore` 里——**每台电脑各自生成、各自配 key，不共享、不入库**。

## 内置创作技能（自包含，可再指向外部仓库）

- 技能随包放在项目内 `skill/`（chinese-novelist **v3.0** 为主、融合 v2 模板），`config.json.skill_path` 默认为相对路径 `"skill"`，**零外部依赖**。
- 想跟随上游升级：把 `skill_path` 改成你本地的技能仓库绝对路径即可（`bot/skill.py` 实时读取，升级无需改代码）。

## 目录

```
app.py            FastAPI 服务（路由 + 会话状态机）
bot/              创作引擎（llm/workflow/writer/providers/skill）
skill/            内置 chinese-novelist v3.0 技能（SKILL.md + guides + scripts）
static/           网页端（聊天 + 供应商管理 + 作品/预览/续写）
projects/         运行期作品（gitignore，每台电脑各自）
providers.json    运行期供应商（gitignore）
requirements.txt  依赖（fastapi/uvicorn/httpx）
run.py            跨平台启动器
VERSION           版本号（0.0.3）
```

## v3.0 创作流水线（本版核心）

规划阶段除 00 人物/01 大纲/03 文风外，自动生成 4 个**连续性结构**文件（`defaults.v3_pipeline` 可关）：
- `09-故事圣经.md`：全书唯一事实来源（故事引擎/承诺/世界规则/主线结局/设定词典）；
- `10-线索账本.md`：悬念与伏笔登记表（含真实答案与兑现计划），每章写后自动追加新线索；
- `11-章节摘要.md`：每章写后自动补写，作为跨章承接的连续性来源；
- `12-时间线与连续性.md`：事件时间线 + 连续性核对清单。

这些文件会**注入每一章的写作上下文**（人物姓名/知情范围/时间/伤势/规则前后一致，章节开头承接上一章结尾，不复述旧情）。成书后自动跑 **v3.0 六轮分层编辑审稿**（结构/因果与智斗/人物与情绪/连续性/语言人味/读者体验），证据化的结论写入 `13-修订记录.md`。

## API（v0.0.3 新增/增强）

| 端点 | 说明 |
|---|---|
| `GET /api/preview?key=` | 作品预览（成书或已写章节 + 章节清单） |
| `POST /api/validate {key, mode, max_rounds}` | 手动跑六轮分层审稿（`report`=只诊断 / `autofix`=有界重写 high 项），结果写 `13-修订记录.md` |
| `GET /api/review?key=` | 取 `13-修订记录.md` 文本 |
| `GET /api/version` | 当前版本号 |

网页端：作品列表点「👁 预览」可看内容 + 选章节；成书后可点「📝 审稿」一键跑六轮编辑并查看修订记录，「⬇ 成书」下载。

## 说明

- 核心功能只增不删：旧作品（无新字段）照常 读 / 续写 / 预览 / 下载 / 审稿。
- 分层校验默认 `report`（只诊断、出 `13-修订记录.md`，不改动正文）；`config.json` 的 `defaults.validation` 可调 `mode`（`report`/`autofix`）、`enabled`、`max_fix_rounds`；`defaults.v3_pipeline` 控制是否生成 4 个连续性文件（默认开）。
