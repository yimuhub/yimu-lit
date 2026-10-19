"""原创字数统计：正文汉字数（网文"字数"口径=汉字，标点/英文不计）。
替代原外部字数脚本，纯 Python 跨平台，零依赖。"""
import re

def count(text, include_latin=False):
    n = len(re.findall(r"[\u4e00-\u9fff]", text))
    if include_latin:
        n += len(re.findall(r"[A-Za-z0-9]+", text))
    return n

def file_count(path, include_latin=False):
    with open(path, encoding="utf-8") as f:
        return count(f.read(), include_latin)

if __name__ == "__main__":
    import sys
    p, m = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    w = file_count(p)
    print(f"字数: {w}（{'达标' if w >= m else '不足'}，目标 {m}）")
