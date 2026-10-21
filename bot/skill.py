"""小说技能运行时加载器（v1.0.0 · 神笔文学创作台）

v1.0 起小说技能位于 skillkit/novel/；本模块保留原有 API（skill_md / guide / template /
genre / golden_rules / golden3 / anti_slop / check_words）供既有代码无感调用。

多文体的统一加载与注入走 bot/skillkit.py。
全部技能文本为原创（见 NOTICE.md），不依赖任何外部技能仓库。
"""
import json, os, re
from functools import cache

from . import wordcount

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_DIR = os.path.join(ROOT, "secrets")

# 小说技能在技能库中的相对根
NOVEL_ROOT = os.path.join("skillkit", "novel")


def _skill_root():
    """技能目录：env NOVEL_BOT_SKILL > config.skill_path（相对则按 ROOT 解析）。
    v1.0 默认指向 skillkit/novel/；config.skill_path 仍可指向外部绝对路径。"""
    env = os.environ.get("NOVEL_BOT_SKILL")
    if env:
        return env
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    sp = (cfg.get("skill_path") or "").strip()
    if not sp:
        return os.path.join(ROOT, NOVEL_ROOT)
    return sp if os.path.isabs(sp) else os.path.join(ROOT, sp)


@cache
def read_file(relpath):
    p = os.path.join(_skill_root(), relpath)
    with open(p, encoding="utf-8") as f:
        return f.read()


def skill_md():
    return read_file("SKILL.md")


def guide(name):
    """name: chapter-guide / hook-techniques / dialogue-writing / content-expansion /
    plot-structures / title-guide / character-building / chapter-template /
    outline-template / character-template / anti-slop / golden3"""
    return read_file(f"references/guides/{name}.md")


def flow(name):
    try:
        return read_file(f"references/flows/{name}.md")
    except Exception:
        return ""


def template(name):
    """ShenBi 结构模板：story-bible / clue-ledger / chapter-summary / timeline / revision-log。
    缺失时返回空串（调用方做防御性处理，旧项目/未生成时不崩）。"""
    try:
        return read_file(f"references/templates/{name}.md")
    except Exception:
        return ""


def golden_rules():
    """三大黄金法则 / 七条创作底线（ShenBi 总纲）。按版本自适应取，取不到回落默认。"""
    txt = skill_md()
    for pat in (r"##\s*三大黄金法则\n(.*?)(?:\n##\s|\Z)",
                r"##\s*七条创作底线\n(.*?)(?:\n##\s|\Z)",
                r"###\s*七条创作底线\n(.*?)(?:\n##\s|\n###\s|\Z)"):
        m = re.search(pat, txt, re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return "1. 展示而非讲述 2. 冲突驱动剧情 3. 悬念承上启下"


def _genre_key(genre):
    """cfg 的题材字符串 → 题材 craft 文件 key；未命中回落 _index（全平台通配默认）。"""
    g = (genre or "").lower()
    table = [
        (("推理", "悬疑", "犯罪", "mystery", "惊悚"), "mystery"),
        (("言情", "甜宠", "都市情感", "romance"), "romance"),
        (("恐怖", "惊悚", "horror", "灵异"), "horror"),
        (("玄幻", "仙侠", "修仙", "修真", "xuanhuan", "玄幻仙侠"), "xuanhuan"),
        (("都市", "现实", "职场", "商战", "重生", "赘婿", "urban", "爽文"), "urban"),
    ]
    for keys, val in table:
        for k in keys:
            if k in g:
                return val
    return "_index"


def genre(genre):
    """返回该题材的 craft 套文本；无法命中返回通用默认（_index）。"""
    return read_file(f"references/genre/{_genre_key(genre)}.md")


def anti_slop():
    return read_file("references/guides/anti-slop.md")


def golden3():
    return read_file("references/guides/golden3.md")


def check_words(path, min_words=3000):
    """原创字数统计（汉字口径）。返回 (是否通过, 字数)。"""
    try:
        words = wordcount.file_count(path)
        return words >= min_words, words
    except Exception:
        return False, 0
