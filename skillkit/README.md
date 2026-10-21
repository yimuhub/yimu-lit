# skillkit · 多文体技能库

神笔文学创作台的技能库。**全部随产品分发，不依赖任何外部 skill 仓库。**

## 目录

| 路径 | 内容 | 文体 |
|---|---|---|
| `_core/` | 跨文体共核：反 AI 腔总纲、文风指纹、体裁通则 | 全部 |
| `novel/` | 小说：题材 craft / 指南 / 模板（由神笔小说 v1.0 迁入） | 小说 |
| `script/` | 剧本：前提立意 / 结构 / 人物 / 对白 / 场景 / 格式 / 类型 / 剧集 / 戏曲 | 剧本 |
| `poetry/` | 诗词：近体诗 / 词 / 曲 / 现代诗 / 韵书数据 | 诗词 |
| `prose/` | 散文：随笔 / 抒情 / 议论 / 叙事散文 | 散文 |

## 文件格式

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

- `medium`：适用文体，`all` 表示通用
- `stages`：适用阶段代号（见 `bot/medium.py` 的阶段表）
- `kind`：`method` 方法论 / `case` 案例（默认不注入）/ `data` 数据 / `template` 模板
- `weight`：注入排序权重，大的优先

## 加载

`bot/skillkit.py` 负责索引与**最小上下文注入**：

```python
from bot import skillkit
ctx = skillkit.stage_inject("S5", "script")   # 只取该阶段必需的文件
```

绝不整库塞进 prompt：600 KB 的库，单次注入 8–25 KB。
