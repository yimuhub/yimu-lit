"""chinese-novelist 技能运行时加载器（深度绑定：每次创作实时读取技能仓库的文件，
技能升级后机器人自动跟随，无需改代码）"""
import json, os, re, subprocess
from functools import lru_cache

def _skill_root():
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
    return cfg["skill_path"]

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

def golden_rules():
    """三大黄金法则（SKILL.md 固定段落）"""
    txt = skill_md()
    m = re.search(r"## 三大黄金法则\n(.*?)\n## ", txt, re.S)
    return m.group(1).strip() if m else "1. 展示而非讲述 2. 冲突驱动剧情 3. 悬念承上启下"

def word_count_script():
    return os.path.join(_skill_root(), "scripts", "check_chapter_wordcount.py")

def check_words(path, min_words=3000):
    """调用技能自带的字数检查脚本，返回 (是否通过, 字数)"""
    try:
        r = subprocess.run(["python3", word_count_script(), path, str(min_words)],
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
