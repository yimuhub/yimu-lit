#!/usr/bin/env python3
"""给 skillkit/novel 下的技能文件批量加 frontmatter（一次性工具，幂等）。

给已有 md 加 YAML 头：name / title / medium / stages / kind / weight / license / source。
已带 frontmatter 的跳过。stage_inject 依赖这些元数据做最小上下文注入。
"""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skillkit", "novel")

# 文件 → (title, kind, weight, stages)
PLAN = {
    "SKILL.md": ("小说创作总纲", "method", 5, ["P0", "P1", "P2", "P3", "P4", "P5"]),
    "references/guides/anti-slop.md": ("反 AI 腔 · 分级黑名单与结构性反模式", "method", 5, ["P2", "P4"]),
    "references/guides/golden3.md": ("黄金三章门", "method", 5, ["P1"]),
    "references/guides/chapter-guide.md": ("章节写作指南", "method", 4, ["P2"]),
    "references/guides/character-building.md": ("人物塑造", "method", 4, ["P0"]),
    "references/guides/character-template.md": ("人物档案模板", "template", 3, ["P0"]),
    "references/guides/content-expansion.md": ("内容扩充", "method", 3, ["P2"]),
    "references/guides/dialogue-writing.md": ("对话写作", "method", 4, ["P2", "P4"]),
    "references/guides/hook-techniques.md": ("钩子技巧", "method", 4, ["P2"]),
    "references/guides/outline-template.md": ("大纲模板", "template", 4, ["P0"]),
    "references/guides/plot-structures.md": ("情节结构", "method", 4, ["P0", "P2"]),
    "references/guides/title-guide.md": ("书名与标题", "method", 2, ["P0"]),
    "references/templates/story-bible.md": ("故事圣经模板", "template", 5, ["P0"]),
    "references/templates/clue-ledger.md": ("线索账本模板", "template", 4, ["P0", "P3"]),
    "references/templates/timeline.md": ("时间线模板", "template", 3, ["P0", "P3"]),
    "references/templates/chapter-summary.md": ("章节摘要模板", "template", 4, ["P2", "P3"]),
    "references/templates/quality-report.md": ("质量报告模板", "template", 3, ["P4", "P5"]),
    "references/templates/revision-log.md": ("修订记录模板", "template", 2, ["P4"]),
    "references/genre/_index.md": ("通用题材默认", "method", 3, ["P2"]),
    "references/genre/mystery.md": ("推理 / 悬疑", "method", 4, ["P2"]),
    "references/genre/romance.md": ("言情", "method", 4, ["P2"]),
    "references/genre/horror.md": ("恐怖", "method", 4, ["P2"]),
    "references/genre/urban.md": ("都市爽文", "method", 4, ["P2"]),
    "references/genre/xuanhuan.md": ("玄幻仙侠", "method", 4, ["P2"]),
    "README.md": ("小说技能说明", "method", 1, []),
}

FM_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


def slug(rel: str) -> str:
    name = rel[:-3] if rel.endswith(".md") else rel
    return "novel-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main() -> None:
    changed = skipped = 0
    for rel, (title, kind, weight, stages) in PLAN.items():
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            print(f"  ! 缺失 {rel}")
            continue
        text = open(p, encoding="utf-8").read()
        if FM_RE.match(text):
            skipped += 1
            continue
        fm = (
            "---\n"
            f"name: {slug(rel)}\n"
            f"title: {title}\n"
            "medium: [novel]\n"
            f"stages: [{', '.join(stages)}]\n"
            f"kind: {kind}\n"
            f"weight: {weight}\n"
            "license: MIT\n"
            "source: 原创（神笔小说 ShenBi NovelCraft，方法论吸收业界通用创作思想）\n"
            "---\n\n"
        )
        open(p, "w", encoding="utf-8").write(fm + text)
        changed += 1
        print(f"  ✓ {rel}")
    print(f"\n完成：新增 {changed}，跳过 {skipped}")


if __name__ == "__main__":
    main()
