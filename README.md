<div align="center">

# 📖 一幕·YIMU 神笔文学创作台 (YIMU Lit)

**面向小说作者、编剧与文学创作者的九文体全流程 AI 创作台——从一句话简介到成书导出**

[![Version](https://img.shields.io/badge/version-4.23.1-orange.svg?style=flat-square)](CHANGELOG.md)
[![Release](https://img.shields.io/badge/release-v4.23.1-blueviolet.svg?style=flat-square)](https://github.com/yimuhub/yimu-lit/releases)
[![License: BSL 1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg?style=flat-square)](LICENSE)
[![Licensor](https://img.shields.io/badge/%E8%AE%B8%E5%8F%AF%E6%96%B9-yimu.hyipf.com-0284c7.svg?style=flat-square)](https://yimu.hyipf.com)
[![Copyright](https://img.shields.io/badge/%E7%89%88%E6%9D%83%E5%BD%92%E5%B1%9E-%C2%A9%202026%20yimu.hyipf.com-orange.svg?style=flat-square)](https://yimu.hyipf.com)
[![Python](https://img.shields.io/badge/python-3.9%2B-green.svg?style=flat-square)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688.svg?style=flat-square)](https://fastapi.tiangolo.com)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows%20%7C%20Docker-blueviolet.svg?style=flat-square)](#-快速启动)

<p align="center">
  <b>一念起 · 一幕开 · 万象生</b><br>
  <i>One Thought. One Scene. Infinite Worlds.</i>
</p>

**简体中文** • [English](README_EN.md)

[✨ 功能特性](#-功能特性) • [🚀 快速启动](#-快速启动) • [🏛️ 系统架构](#-系统架构) • [📜 开源许可](#-开源许可与版权主体)

</div>

---

## 💡 这是什么？

**一幕·YIMU 神笔文学创作台**是一个纯文字的 AI 文学创作平台：对话即开写，从一句话简介到成书导出全流程贯通。覆盖**九种文体**——小说、剧本、诗词、散文、论文、儿童文学、新闻稿、文案、其他内容（剧本杀/播客/课程稿/演讲稿/拆书稿/影评）。

核心差异化能力：
- **百万字长篇引擎**：总规划 → 卷 → 弧 → 章四层结构，支持 100–1500 章规模；滚动规划与重规划提案，写前注入前情摘要、活跃伏笔、人物状态与设定词典，配合一致性纲领保障人物/事件/世界观全程连贯。
- **批量写作节奏自控**：逐章/逐场推进，或按 5/10/20/50 章为一批；对话里说「再写 N 章」「写完全部」即可控制，长篇不再被迫一口气写完。
- **多岗位并行创作**：多个创作岗位同时分章齐写，总架构对齐 + 连续性入库 + 低分章自动还原。
- **反 AI 腔体系**：三层去味扫描、手法账本防钩子重复、与近章相似度门禁——正文产出拒绝「AI 腔」与复制粘贴。
- **正文纯净域（P0 铁律）**：复制与导出只有「章题 + 章首引子 + 正文」——本章概要、章节备注、修改说明、审核评分、字数统计等过程产物，在任何复制/预览/成书/导出里都不出现。
- **作者笔名体系**：多笔名管理、按文体指定默认、开书向导就地自定义，署名自动进入成书与 Word/EPUB/Markdown 导出。

---

## ✨ 功能特性

### 📚 九文体全流程
每种文体独立引擎：专属向导、专属创作岗位与技能、专属工作台页签、专属规划与状态视图。

| 文体 | 亮点 |
|---|---|
| 小说 | 百万字分层规划、伏笔账本、时间线、故事圣经、手法账本 |
| 剧本 | 场次清单、时代一致性红线、人物在场体检、分镜头拆解（含 AI 短剧/漫剧） |
| 诗词 | 近体诗/词牌格律校验、意象密度、炼字建议 |
| 散文 | 三层去 AI 腔（表达/用笔/故事逻辑）、结构与细节密度 |
| 论文 | 提纲、逐章撰写、引用文献池、降 AIGC 润色 |
| 儿童文学 | 年龄分级语言、价值观红线、亲子互动、插图提示词 |
| 新闻稿 | 倒金字塔、5W1H 事实核查、多版本输出 |
| 文案 | 钩子库（前 3 秒 10 候选）、平台适配、AIDA/PAS 框架 |
| 其他 | 剧本杀/播客/课程稿/演讲稿/拆书稿/影评六模板 |

### 🎛️ 创作中台
- **对话即改稿**：聊天框直接下修改指令；@ 岗位即群聊；改稿全程留档可回溯。
- **一致性体检 + 一键修复**：扫描人物/伏笔/设定冲突，单条或全部一键修复（改稿类自动定向重写）。
- **技能进化**：反馈打分 → 画像沉淀 → 候选技能 → 评测转正，越用越贴合你的文风。
- **参考资料 RAG**：txt/md/docx/pdf/epub 参考先行入库，按章检索注入，用途分事实/文风两类。
- **专业导出**：Markdown / Word / EPUB / 打印排版 / 剧本 Fountain / FDX，署名自动嵌入；
  整本导出在「章节工作台 → 导出全书」或作品库右键「导出」，导出内容与复制口径完全一致（只含正文）。
- **创作预算**：单本书 token 预算护栏，支持不限制模式。

---

## 🚀 快速启动

```bash
pip install -r requirements.txt
python run.py          # 默认 http://127.0.0.1:46821
```

- macOS / Linux：`./start.sh`；Windows：双击 `start.bat`
- 首次运行在「设置」里填入 OpenAI 兼容接口（DeepSeek / Kimi / 本地 Ollama 均可）
- `?mock=1` 零配置体验全部交互（内置模拟数据，不调模型）
- Docker：`docker-compose up -d`

界面为类 DeepSeek 风格三栏工作台（会话侧栏 + 对话 + 工作台），旧版界面保留在 `/classic`。

---

## 🏛️ 系统架构

```
app.py                 FastAPI 主应用（225 条路由）
bot/
  writer.py            小说引擎（百万字分层规划/并行/质量门禁）
  script_writer.py     剧本引擎（场次/分镜/时代校验）
  poetry|prose|thesis_writer.py   诗词/散文/论文引擎
  children|news|copy|other_writer.py  轻文体引擎（mini_writer_base 模板）
  longform.py          分层规划（卷/弧/章卡/滚动重规划）
  context.py           每章上下文编译器（章卡/状态/伏笔/蓝图/一致性纲领）
  continuity.py        一致性体检与修复
  skillkit.py          技能库加载与阶段注入（9 文体 × 阶段）
  purity.py            正文纯净域（复制/预览/成书/导出的唯一取文口径）
  llm.py               多供应商网关（缓存/限流/预算/wall-clock 守卫）
static/v2/             前端（ES module，无构建步骤）
tests/                 480+ 用例回归
```

### 正文纯净域（开发铁律，不可回退）

**正文 = 章题 + 章首引子 + 正文内容。** 除此之外的一切——`## 本章概要`、`## 章节备注`、
`## 修改说明`、`## 审核评分`、字数统计、审稿意见、模型思考标签——都属于过程元信息，
**不允许出现在剪贴板、预览、成书与任何导出格式里**。

- **正文契约标记（v4.22.3）**：生成时把正文框在
  `=== 一幕·YIMU 神笔工作台·开篇 ===` 与 `=== 一幕·YIMU 神笔工作台·结尾 ===` 之间，
  框外只允许本章概要与章节备注。框外的东西**在结构上就不是正文**，不靠词表猜。
- 唯一实现：后端 `bot/purity.py`，前端 `static/v2/js/core/purity.js`（逐条同口径）。
- 唯一取文入口：`chapter_text()`（复制/展示）、`chapter_body()`（导出/成书）、`sanitize_chapter()`（写盘前）。
- 守门测试：`python -m pytest tests/test_v422_purity.py tests/test_v4222_resume_plan.py -q`。
- 旧稿自愈：`python tools/purify_data.py`（只读体检）→ `--apply` 写回并自动备份。
- 新增导出格式或复制入口时必须走上述入口；**禁止再写黑名单式清洗**（漏一个小节名就漏一整类污染）。

### 断点续跑与长篇统一性（v4.22.3 → v4.23.0）

- **「继续」= 接着上次那件事做完**：`bot/journal.py` 把任务（写作/重写/补评分/补章卡）、进度、
  当前章、中断原因写进 `<项目>/任务档案.json`；章内还留 `.draft/第NN章.partial.md` 草稿检查点。
  续跑时优先恢复任务，其次从草稿末尾续写，绝不再从第一段重来。
- **任何长任务都能续跑（v4.23.0）**：`bot/tasks.py` 是一张注册表，`@runs("kind")` 挂一个函数
  就具备中断续跑能力；`tasks.resume()` 是「继续」的唯一入口。认不出的任务类型**如实说不认识**，
  绝不偷偷改成写新章——认错人比忘事更糟。
- **按章号记账**：并行重写是一章一个线程、乱序完成，所以记的是「哪几章做完了」而不是
  「做完几章」。否则续跑会跳过没做的、重做已做好的。
- **失败的章不算完成**：留在待办里等「继续」重试；同一章连挂 3 次才跳过
  （多半是文件坏了，不是模型的事），避免整本书永远收不了尾。
- **僵尸任务自动救活**：进程被上游断流/关机杀掉时来不及写「已暂停」，启动对账
  （`journal.reconcile_all`）把停在 running 的任务改标为可续跑，否则「继续」会以为还在跑。
- **没有大纲不许动笔**：滚动规划在 LLM 返回空时会兜出「承接本弧…」的占位章卡，等于无大纲写作，
  长篇必漂（《入赘三年》第 41–50 章实证）。现在写每章前都过 `_ensure_card_planned`：
  占位卡先单章补真卡，补不出来就跳过并暂停，而不是静默跑偏。

### 提交与 CI（v4.23.0）

- 每次改完直接落库：`./tools/commit.sh "新增: v4.23.0 一句话说明"`。
  说明里必须带 `v版本号`、CHANGELOG 里必须有该版条目，否则脚本拒绝提交（防「代码进了库、版本没动」）。
- **CI 不挂 push**：`.github/workflows/ci.yml` 改为手动触发。体检在本地做
  （`bash tools/static_check.sh` + `python -m pytest -q`），秒级不排队。
- 费用：本仓库是公开仓库，标准 runner 的 Actions 分钟数**免费不限量**，
  改手动是因为每次 push 都跑、lint 先挂导致测试从未跑起来，零信号加噪音，不是省钱。

开发者细则见 [`功能表.md`](功能表.md) 的「铁律 0 / 1 / 2 / 3」。

---

## 📜 开源许可与版权主体

本项目采用 **BUSL-1.1（Business Source License 1.1）** 授权，版权归属 **yimu.hyipf.com**。

- 个人学习、研究、非商业用途、个人创作（含个人账号收益）：**免费，无需申请**
- 连续 12 个月内直接商业价值低于人民币 10 万元：免费
- 达到或超过阈值需商业授权（免费登记获取，联系 yimu@hyipf.com）
- 每个版本首次发布满四年后自动转为 Apache License 2.0

详见 [LICENSE](LICENSE)。ICP/主体信息：[yimu.hyipf.com](https://yimu.hyipf.com)
