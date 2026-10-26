"""小说技能运行时加载器（v1.0.0 · 神笔文学创作台）

v1.0 起小说技能位于 skillkit/novel/；本模块保留原有 API（skill_md / guide / template /
genre / golden_rules / golden3 / anti_slop / check_words）供既有代码无感调用。

多文体的统一加载与注入走 bot/skillkit.py。
全部技能文本为原创（见 NOTICE.md），不依赖任何外部技能仓库。
"""
import json, os, re
from functools import cache

from . import skillkit, wordcount

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


# v2.1：题材映射统一到 bot/skillkit.py 单一实现（旧版这里有一份只覆盖 5 个题材的
# 重复表，导致 历史/科幻/军事/职场/仙侠 craft 永远命中不到）。skill._genre_key
# 名字保留供既有代码调用，实际就是 skillkit._genre_key 本体。
_genre_key = skillkit._genre_key


def _genre_filename(key):
    """题材 key → references/genre/ 下的实际文件名。

    v2.1 新增：craft 文件存在中文文件名（历史.md / 科幻.md …），不能再用
    key + '.md' 直拼；按 skillkit._genre_key 对文件名归一后匹配。
    找不到对应 craft（如 wuxia 暂无文件）回落 _index.md。
    """
    d = os.path.join(_skill_root(), "references", "genre")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for fn in names:
        if fn.endswith(".md") and skillkit._genre_key(fn[:-3]) == key:
            return fn
    return "_index.md"


def genre(genre):
    """返回该题材的 craft 套文本；无法命中返回通用默认（_index）。"""
    fn = _genre_filename(_genre_key(genre))
    try:
        return read_file(f"references/genre/{fn}")
    except Exception:
        return read_file("references/genre/_index.md")


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
