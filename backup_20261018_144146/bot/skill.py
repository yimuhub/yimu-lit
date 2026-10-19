"""chinese-novelist 技能运行时加载器（深度绑定：每次创作实时读取技能仓库的文件，
技能升级后机器人自动跟随，无需改代码）"""
import json, os, re, subprocess, sys
from functools import lru_cache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_DIR = os.path.join(ROOT, "secrets")

def _skill_root():
    """技能目录：env NOVEL_BOT_SKILL > config.skill_path（相对则按 ROOT 解析，默认内置 skill/）。
    默认自包含在 skill/，零外部依赖；想跟随上游仓库可把 config.skill_path 指到外部绝对路径。"""
    env = os.environ.get("NOVEL_BOT_SKILL")
    if env:
        return env
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    sp = (cfg.get("skill_path") or "skill").strip()
    return sp if os.path.isabs(sp) else os.path.join(ROOT, sp)

@lru_cache(maxsize=None)
def read_file(relpath):
    p = os.path.join(_skill_root(), relpath)
    with open(p, encoding="utf-8") as f:
        return f.read()

def skill_md():
    return read_file("SKILL.md")

def guide(name):
    """name: chapter-guide / hook-techniques / dialogue-writing / content-expansion /
    plot-structures / title-guide / character-building / chapter-template /
    outline-template / character-template"""
    return read_file(f"references/guides/{name}.md")

def flow(name):
    return read_file(f"references/flows/{name}.md")

def template(name):
    """v3.0 结构模板：story-bible / clue-ledger / chapter-summary / timeline / revision-log。
    缺失时返回空串（调用方做防御性处理，旧项目/未生成时不崩）。"""
    try:
        return read_file(f"references/templates/{name}.md")
    except Exception:
        return ""

def golden_rules():
    """三大黄金法则 / 七条创作底线（v3.0 兼容）。按版本自适应取，取不到回落默认。"""
    txt = skill_md()
    for pat in (r"##\s*三大黄金法则\n(.*?)(?:\n##\s|\Z)",
                r"##\s*七条创作底线\n(.*?)(?:\n##\s|\Z)",
                r"###\s*七条创作底线\n(.*?)(?:\n##\s|\n###\s|\Z)"):
        m = re.search(pat, txt, re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return "1. 展示而非讲述 2. 冲突驱动剧情 3. 悬念承上启下"

def word_count_script():
    return os.path.join(_skill_root(), "scripts", "check_chapter_wordcount.py")

def check_words(path, min_words=3000):
    """调用技能自带的字数检查脚本，返回 (是否通过, 字数)"""
    try:
        r = subprocess.run([sys.executable, word_count_script(), path, str(min_words)],
                           capture_output=True, text=True, timeout=30)
        out = r.stdout + r.stderr
        # 脚本输出形如 "   字数: 4514 (✓ 达标)" 或 "总字数: 4,514"
        m = re.search(r"字数[:：]\s*([\d,]+)", out)
        words = int(m.group(1).replace(",", "")) if m else 0
        if words == 0:
            m2 = re.search(r"(\d{3,6})\s*字", out)
            words = int(m2.group(1)) if m2 else 0
        passed = ("✅" in out and "不足" not in out.split("✅")[-1][:40]) or (words and words >= min_words)
        if "❌" in out:
            passed = False
        return passed, words
    except Exception:
        # 脚本异常时退化为直接统计汉字数
        txt = open(path, encoding="utf-8").read()
        words = len(re.findall(r"[\u4e00-\u9fff]", txt))
        return words >= min_words, words
