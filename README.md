<div align="center">

# 📖 一幕·YIMU 神笔文学创作台 (YIMU Lit)

**面向小说作者、编剧与文学创作者的九文体全流程 AI 创作台——从一句话简介到成书导出**

[![Version](https://img.shields.io/badge/version-4.15.12-orange.svg?style=flat-square)](CHANGELOG.md)
[![Release](https://img.shields.io/badge/release-v4.15.12-blueviolet.svg?style=flat-square)](https://github.com/yimuhub/yimu-lit/releases)
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
- **专业导出**：Markdown / Word / EPUB / 打印排版 / 剧本 Fountain / FDX，署名自动嵌入。
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
  llm.py               多供应商网关（缓存/限流/预算/wall-clock 守卫）
static/v2/             前端（ES module，无构建步骤）
tests/                 470 用例回归
```

---

## 📜 开源许可与版权主体

本项目采用 **BUSL-1.1（Business Source License 1.1）** 授权，版权归属 **yimu.hyipf.com**。

- 个人学习、研究、非商业用途、个人创作（含个人账号收益）：**免费，无需申请**
- 连续 12 个月内直接商业价值低于人民币 10 万元：免费
- 达到或超过阈值需商业授权（免费登记获取，联系 yimu@hyipf.com）
- 每个版本首次发布满四年后自动转为 Apache License 2.0

详见 [LICENSE](LICENSE)。ICP/主体信息：[yimu.hyipf.com](https://yimu.hyipf.com)
