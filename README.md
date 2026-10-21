# 一幕·YIMU 神笔文学创作台

> 一个产品，四类文学创作：**小说 · 剧本 · 诗词 · 散文**。
> 纯文字创作，**不含任何 AI 视频 / 图像生成能力**；剧本面向**真人拍摄**。
> 技能库全部内置，任意电脑解压即跑。

```bash
pip install -r requirements.txt
python run.py          # 默认 http://127.0.0.1:46821
```

---

## 一、这是什么

以「神笔小说」v0.9.2 为基座，融合**剧本 / 诗词 / 散文**三类创作能力形成的文学创作台。

- **底座完整继承**：多供应商 LLM 路由、内容缓存、300s 超时、429 退避、全局并发闸、
  风格蒸馏、TF-IDF 检索、故事图谱、连续性检查、偏好学习、写作骨架、100 分审校、
  逐字直播改写、并行章节写作、24 篇改写记录。
- **技能库从 28 KB 扩到 ~600 KB**：从「网文 5 题材」扩为四文体技能库，
  全部随产品分发，不依赖任何外部 skill 仓库。
- **确定性校验器**：诗词格律（平水韵 106 部 + 四套近体诗平仄模板）与反 AI 腔检测，
  用规则而非 LLM 猜，出错可复现。

---

## 二、四条创作流水线

| 文体 | 入口 | 阶段表 | 评估（100 分） | 目录前缀 |
|---|---|---|---|---|
| **小说** | 网文 / 通用长篇 | 规划 → 黄金三章 → 逐章写 → 连续性 → 审校 | 结构20/因果20/人物15/连续性20/语言15/体验10 | `projects/<key>/` |
| **剧本** | 长片 / 剧集 / 短片 / 舞台剧 / 戏曲 | S0 引擎 → S1 人物网与季弧 → S2 文档 → S3 试播结构 → S4 分集大纲 → S5 写作 → S6 修订 → S7 交付 | 前提25/结构25/人物20/对白15/格式10/可行性5 | `projects/s_<key>/` |
| **诗词** | 近体诗 / 词 / 现代诗 | 释读 → 规划 → 起草 → 校验 → 评改 → 定稿 | 意象30/章法25/声律25/情感20 | `projects/p_<key>/` |
| **散文** | 随笔 / 抒情 / 议论 / 叙事散文 | brief → 素材 → 起草 → 去味 → 精修 → 定稿 | 表达30/结构25/真实度25/音律20 | `projects/e_<key>/` |

---

## 三、核心概念

### 3.1 最小上下文注入

技能库 600 KB，**绝不整库塞进 prompt**。`bot/skillkit.py` 按「文体 + 阶段」精确取用到的
3–6 个文件（8–25 KB）拼装 system 上下文；校验器走 tool call 而非注入；
案例库（剧本原文 / 戏曲全本）默认不挂载，仅在用户显式点名时加载。

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
| `bot/poetry_checker.py` | 五绝/七绝/五律/七律平仄、对仗、押韵（平水韵 106 部）、孤平 | YAML 报告，`pass` / `fail` / `unclear` 三态 |
| `bot/slop_checker.py` | 反 AI 腔：电报体、碎句、套路词、机械复现、总结句 | `blocking` / `advisory` 两级 + 位置定位 |

---

## 四、目录结构

```
yimu-lit/
├── app.py                  FastAPI 入口（~70 端点，v1.0 扩展剧本/诗词/散文端点）
├── bot/                    引擎层
│   ├── llm.py              LLM 层：缓存/超时/重试/stage→agent 路由
│   ├── providers.py        多供应商 + 全局并发闸
│   ├── writer.py           小说主引擎
│   ├── workflow.py         小说流程编排
│   ├── medium.py           【新】文体注册表（阶段表/评估维度/目录规范）
│   ├── skillkit.py         【新】技能库索引 + stage_inject
│   ├── script_writer.py    【新】剧本流水线
│   ├── poetry_writer.py    【新】诗词流水线
│   ├── prose_writer.py     【新】散文流水线
│   ├── poetry_checker.py   【新】诗词格律校验器
│   ├── slop_checker.py     【新】反 AI 腔检测器
│   └── ...                 骨架/图谱/检索/蒸馏/学习/连续性/模型规则
├── skillkit/               【新】多文体技能库
│   ├── _core/              跨文体共核（反AI腔/文风指纹/体裁通则）
│   ├── novel/              小说（题材/指南/模板）
│   ├── script/             剧本（通用/剧集/戏曲/案例/大师）
│   ├── poetry/             诗词（近体诗/词/曲/现代诗/data）
│   └── prose/              散文（随笔/抒情/议论/通用）
├── skill/                  旧技能目录（PR1 迁入 skillkit/novel/，兼容期内保留）
├── static/index.html       SPA 外壳
├── tests/                  pytest + jsdom 脚本
├── data/  providers.json  config.json  secrets/  projects/   ← 运行时数据，不入库
└── VERSION                 版本号单一来源
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

---

## 六、脚本

```bash
python run.py            # 启动
./start.sh               # macOS/Linux 启动脚本
start.bat / start.ps1    # Windows 启动脚本
```

---

## 七、许可与版权

见 [NOTICE.md](NOTICE.md)。要点：

- **代码**：MIT。
- **技能库**：方法论层为原创重写并逐条标注来源；语料层（剧本原文 / 戏曲全本 / 已出版作品引文）
  **不随产品分发**。
- **数据**：运行时数据不入库。

---

## 八、版本

| 版本 | 内容 |
|---|---|
| v1.0.0 | 立项：基座 v0.9.2 → 神笔文学创作台，四文体路由骨架，PR0 交付 |
| v0.9.2 | 神笔小说 v0.9.2：大厂级代码终极审查（基座） |

版本号单一来源 = `VERSION` 文件（前端徽标 / `/api/version` / 测试同步）。
