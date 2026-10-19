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

## API（v0.1.0 新增/增强）

| 端点 | 说明 |
|---|---|
| `GET /api/preview?key=` | 作品预览（成书或已写章节 + 章节清单） |
| `POST /api/validate {key, mode, max_rounds}` | 跑 确定性引擎+五角色+百分制（`report`=诊断 / `autofix`=未达标有界重写），结果写 `13`/`14` |
| `GET /api/quality?key=` | 取 `14-质量报告.md` |
| `GET /api/review?key=` | 取 `13-修订记录.md` |
| `GET /api/version` | 当前版本号（0.1.0） |

网页端：作品点「👁 预览」看内容/选章节；成书后「📝 审稿」一键出**质量报告 + 达标判定**（下拉可看 📊 质量报告 / 📋 修订记录），「⬇ 成书」下载。

## 说明

- 核心功能只增不删：旧作品照常 读 / 续写 / 预览 / 下载 / 审稿 / 质检（缺新文件自动省略）。
- 质检默认 `report`（出报告不改动正文），`defaults.validation` 可调 `mode`（`report`/`autofix`）、`enabled`、`max_fix_rounds`、`pass_line`（默认 90）；`defaults.v3_pipeline` 控制是否生成 4 个连续性文件（默认开）。
- **版权**：代码 MIT，技能文本自有版权；商业授权时连同 `LICENSE` / `NOTICE.md` 交付，`config/providers/secrets/projects` 不入库。
