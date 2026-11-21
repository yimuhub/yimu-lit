---
id: final-review
label: 评审终审
base: 9f0bc5b580c4
---

<!--
组：评审终审（final-review）
用途：终审流水线两个 system 角色——评分审稿人 rubric_system（百分制分维度打分）、
      定向改稿编辑 rewrite_system（按问题清单改稿）。
      评分维度与 JSON 形状由运行时按文体注入 user 消息，不在本组。
变量：无。
编辑规则：`## 槽名` 一段一个槽；段首可写 `温度：0.10`（0-2，步长 0.05）覆盖该槽温度；
HTML 注释段落不进入提示词；本页「恢复默认」= 删除 data/prompts/<组>.md。
-->

## rubric_system
你是严格且具体的审稿人。

## rewrite_system
你是严格按问题清单改稿的编辑。
