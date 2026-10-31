# NOTICE · 原创、借鉴与授权说明

> 一幕·YIMU 神笔文学创作台 v1.0.0
> 本文件分三段：**一、原创声明** → **二、第三方来源表** → **三、不覆盖声明**。
> 交付时应连同本 NOTICE 一并提供。

---

## 一、原创声明

### 1.1 代码

`app.py`、`bot/`、`static/`、`run.py`、`tests/` 等全部代码：**BUSL-1.1 协议**，见 [LICENSE](LICENSE)。

### 1.2 技能库

`skillkit/` 中标注 `source: 原创` 或未标注来源的文件：**本项目原创撰写**，版权归本项目团队所有。

创作方法论吸收的是**业界通用、不受著作权保护的创作思想**（故事圣经 / 伏笔账本 /
连续性核对 / 黄金三章 / 爽点密度 / 反 AI 腔 / 百分制质检 / 最小上下文注入 /
三态判定 / 韵书比对等），未引用、未复刻、未附带任何第三方技能的**文本、prompt、
模板或文档**，不含第三方署名或来源。

### 1.3 数据

`config.json` / `providers.json` / `secrets/` / `projects/` / `data/` / `styles/`：
**不随版权交付**，每台电脑各自生成、各配各密钥（已 `.gitignore`）。

### 1.4 韵书与格律数据

`skillkit/poetry/data/` 下的**平水韵字表**与**词牌格律结构表**（句读字数序列、韵位、
平仄标注）属于**事实性数据**——它们记录的是韵书与词谱中既存的客观规律，不构成受
著作权保护的表达。本项目自行整理与校验，来源已在对应文件的 frontmatter 中标注。

---

## 二、第三方来源表

下表逐条列明**借鉴了什么**、**来自哪里**、以及**明确没有借鉴什么**。
所有来源均在其自身许可范围内使用；凡来源无明确许可者，本项目**只借鉴不受著作权
保护的通用方法与事实性数据，不复制其文字**。

| 来源 | 许可 | 借鉴内容 | 明确未借鉴 |
|---|---|---|---|
| `jtydhr88/screenwriting-skills` | MIT（作者 Terry Jia） | 四层 skill 架构、入口路径/媒介边界表设计、编号原则与检查表结构、剧本方法论（前提立意 / 结构 / 人物冲突 / 对白 / 场景 / 格式改编 / 类型解剖 / Truby 有机结构 / 剧集引擎与单集结构 / 编剧室 / 情景喜剧 / 戏曲板腔体与曲牌体） | **全部剧本引文、戏曲全本、craft 书段落、已出版剧集分集结构统计、译本文字**（该仓 NOTICE 明确 MIT 不覆盖这些） |
| `zenstory-ai/oh-story-claudecode` | MIT | 分层 skill 组织方式、作者记忆双层设计、去 AI 腔「表达 / 用笔 / 故事」三层诊断结构、确定性预检脚本的 blocking / advisory 两级设计 | 其运行时实现、其仓库的作者笔记与工程文档 |
| `PenglongHuang/chinese-novelist-skill` | MIT | 三层递进问答的阶段切分、断点续写状态机、写作计划 JSON 协调模式、flow 文档组织方式 | 其具体 prompt 文本与其示例文稿 |
| `Nanako0129/sepia` | MIT | write / review / refactor / recreate 四操作路由模型、专业文本域分流思路、中文语言包的重校准思路 | 其检测器实现、其论文引文、其 `model-fingerprints` 具体条目 |
| `forjd/better-writing` | MIT | 六段 reference 的组织结构（voice-and-context / ai-writing-patterns / structures-and-phrases / genre-tells / preflight / sources）、voice calibration 的「先采样指纹再改」流程、preflight 终检思路 | 其词表原文、其改写示例句对 |
| `adewale/anti-slop-writing` | MIT | doctrine / flow-by-relation / rewrite-patterns 三层结构、「具体细节压过膨胀意义」的原则表述 | 其词表与 runbook 原文 |
| `dripips/plain-prose` | MIT | 朴素散文语域判定与「先审后改」流程 | 其 `check.js` 实现与词表 |
| `kylehughes/writing-prose-like-a-human-for-agents` | MIT | 「以人类写作特征为目标」的审校视角 | 其实现与词表 |
| `Shmilyol/classical-chinese-poetry` | 无 LICENSE（仅代码） | 近体诗平仄模板设计（四套变体）、三态判定（pass / fail / unclear）思路、韵部比对流程 | 其 `examples/master-example.md` 示例文稿原文；其仓库无 LICENSE，故**其代码与文稿不复制**，本项目独立实现校验器 |
| `chiyu-star499/classical-poetry-writing` | 无 LICENSE | 词牌格律表的信息结构（句读 / 韵位 / 平仄 / 分片 / 叠句）、七条创作公理的**问题意识** | 其 `cipai.md` 中的《全宋词》范例原文、其文字表述；本项目**自研**格律表，只取客观格律数据 |
| `cli1980/manuscript-studio` | MIT | 可组合 skill 架构（同一 skill 承担多角色操作）的组织思路 | 其具体 skill 文本 |
| `tance-mang/chinese-webnovel-skills` | MIT | 题材 craft 套的组织方式 | 其文本 |
| `xiaofeng-928/chinese-longnovel-skill` | MIT | 分层上下文组装、伏笔状态回证的设计思路 | 其文本 |

### 2.1 上游致谢

本项目基座「神笔小说 novel-bot」由 yimuhub 团队独立开发，v0.9.2 起并入本项目，
git 历史、提交规范与作者署名完整保留。

---

## 三、不覆盖声明

以下内容**不在**本项目的授权与分发范围内，本产品**不随附**这些内容：

1. **引文**。craft 书、已出版剧本、戏曲全本、曲谱、已出版剧集中的段落，
   版权属于各自的作者、译者、整理者与出版社。本产品的技能库中不含这些引文。
2. **译本**。外文作品的中译本有独立著作权，归译者与出版社所有，与原作版权状态无关。
   契诃夫的剧本原文虽已进入公有领域，其中译本仍受保护；戏曲古本同理，
   引用现代整理本时整理本另有权利人。
3. **结构统计**。页码地图、幕点位置、场次计数、分集结构统计记录的是从出版物上量出的事实。
   事实本身不受著作权保护，但本产品**不以这些表格替代原书**。
4. **第三方语料挂载**。若使用者自行将上述材料放入 `skillkit/_external/`（默认 gitignore、
   不随产品分发），其分发责任由放置者自行承担；产品不对该目录的内容做任何授权承诺。

---

## 四、权利人

本项目在借鉴与重写过程中，来源仓库均保留其原始署名。若您是某处借鉴内容的权利人，
认为本产品的表述超出了「方法借鉴 / 事实性数据」的合理范围，请提 issue 说明，
我们会在当天核实并修订 —— 本项目为纯文本工程，修订成本与印出来的书不同。

---

## 五、商业授权

授权范围 = **代码**（MIT，或双方另行约定的商用条款）+
**技能库**（随产品授权交付，其中标注来源的部分保留其原始许可要求）。

交付时应连同本 NOTICE、`LICENSE` 一并提供，并保留「原创重写」与「数据不入库」两条边界。
