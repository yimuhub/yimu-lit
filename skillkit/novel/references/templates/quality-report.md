---
name: novel-references-templates-quality-report
title: 质量报告模板
medium: [novel]
stages: [P4, P5]
kind: template
weight: 3
license: MIT
source: 原创（神笔小说 ShenBi NovelCraft，方法论吸收业界通用创作思想）
---

# 质量报告 · 模板（原创）

> 成书前自动产出（默认 `14-质量报告.md`）。三层质检结果 + 是否达排行线。

## 1) 确定性规则（bot/continuity.py，零成本）
| 检查 | 命中 | severity | 位置 |
|---|---|---|---|
| 伏笔闲置 >3 章 | | warning | L# |
| 超期未兑现 | | critical | L# |
| 埋了未回收 | | warning | L# |
| 主角失踪 >N 章 | | warning | 第 N 章 |
| 字数/爽点密度 | | | 第 N 章 |
| 黄金三章门 | | | 1-3 章 |

## 2) 五角色 LLM 审校
| 角色 | 结论 | 证据 |
|---|---|---|
| Architect（结构/节拍） | | |
| Scribe（正文） | | |
| Editor（语言/去 AI 腔） | | |
| Guardian（连续性，含 L1 复核） | | |
| Curator（题材口吻一致） | | |

## 3) 百分制评分与达标
| 维度 | 权重 | 得分 |
|---|---|---|
| 结构 | 20 | |
| 因果可信 | 20 | |
| 人物 | 15 | |
| 连续性 | 20 | |
| 语言 | 15 | |
| 读者体验 | 10 | |
| **合计** | 100 | **__** |

- 达标线：**90**。达标：__；未达标：已修订 __ 轮 → 现 __。
- 是否可出书：**是 / 否**
