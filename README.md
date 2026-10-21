# 神笔文学创作台

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
├── app.py                  FastAPI 入口（~80 端点，含剧本/诗词/散文/slop 端点）
├── bot/                    引擎层
│   ├── llm.py              LLM 层：缓存/超时/重试/stage→agent 路由
│   ├── providers.py        多供应商 + 全局并发闸
│   ├── writer.py           小说主引擎（自动 deslop 去味）
│   ├── workflow.py         小说流程编排 + deslop_chapter（两阶段）
│   ├── medium.py           文体注册表（阶段表/评估维度/目录规范/自动路由）
│   ├── skillkit.py         技能库索引 + stage_inject（genre 定向）
│   ├── agents.py           7 岗位注册表（含 deslopper / editor）
│   ├── script_writer.py    剧本流水线
│   ├── poetry_writer.py    诗词流水线
│   ├── prose_writer.py     散文流水线（四操作）
│   ├── poetry_checker.py   诗词格律校验器（平水韵 105 部）
│   ├── slop_checker.py     反 AI 腔检测器（纯标准库零依赖）
│   └── ...                 骨架/图谱/检索/蒸馏/学习/连续性/模型规则
├── skillkit/               多文体技能库（58 文件）
│   ├── _core/              全文体共核：反AI腔总纲 / 文体总则 / 文风指纹与校准 / 组合协议
│   ├── novel/              小说（10 题材 craft + 追读曲线/书名简介/黄金三章 + 指南）
│   ├── script/             剧本（通用/剧集/戏曲 + 案例/大师占位）
│   ├── poetry/             诗词（近体诗/词/现代诗/平水韵数据/词牌表）
│   └── prose/              散文（总纲/随笔/抒情/议论/叙事）
├── static/index.html       SPA 外壳（对话 / 稿件 / 剧本 / 散文 四视图）
├── tests/                  pytest（100 项）
├── data/  providers.json  config.json  secrets/  projects/   ← 运行时数据，不入库
└── VERSION                 版本号单一来源（v1.0.0）
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
- **技能库方法论层**：原创重写并逐条标注来源（MIT）。
- **数据层**：`skillkit/poetry/data/pingshui.json`（平水韵 8232 字）为公版数据整理；
  `skillkit/poetry/词/词牌格律表.md`（52 词牌）为原创整理。
- **语料层**（剧本原文 / 戏曲全本 / 已出版作品引文）：默认不随产品分发，
  仅在用户显式提供时挂载到 `skillkit/_external/`（gitignored）。

---

## 八、版本

| 版本 | 内容 |
|---|---|
| v1.0.0 | 立项 + PR1–PR6 全部完成：四文体路由 / 剧本+诗词+散文流水线 / 58 文件技能库 / 7 Agent 岗位 / deslop 定点去味 / 文体自动分流 / 散文 UI |
| v0.9.2 | 神笔小说 v0.9.2：大厂级代码终极审查（基座） |

版本号单一来源 = `VERSION` 文件（前端徽标 / `/api/version` / 测试同步）。

---

## 九、用户快速上手

### 9.1 写小说
对话里说「都市悬疑 · 退休刑警翻盘，50 章」→ 配置菜单 → 开写。
或点左侧「＋新建」走开书向导。

### 9.2 写剧本
顶部「🎬 剧本」视图 → 新建对话框：选入口路径（有素材/有想法/改编/改初稿/续写）
+ 选媒介（电影/剧集/短片/舞台剧/戏曲/自定义）→ 生成剧本规划。

### 9.3 写诗词
对话里说「七律 · 秋夜」或「水调歌头 · 中秋」→ 自动路由到诗词流水线（不经小说流程）。

### 9.4 写散文
顶部「✒️ 散文」视图 → 「＋新建散文」→ 选文体（7 种）× 操作（4 种）+ 需求描述 → 六阶段生成。

### 9.5 去 AI 腔
`POST /api/slop/scan` 纯规则扫描（零 LLM 调用）；
小说每章审校自动注入 slop 定位；定稿前手动跑一次 slop/scan 确认。
