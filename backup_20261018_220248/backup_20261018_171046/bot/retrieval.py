"""P5 语义检索（纯标准库 TF-IDF + 余弦，无外部向量库 → 保持"任何电脑可跑"）。
对 章节摘要/故事圣经/大纲 建轻量索引，按"本章任务"召回相关既往片段，
补充"只看上一章结尾"看不到的跨章长期上下文。"""
import math, re
from collections import Counter

_CJK = re.compile(r"[\u4e00-\u9fff]")

def _tokens(text):
    t = (text or "").lower()
    toks = re.findall(r"[a-z0-9]+", t)
    for run in _CJK.findall(t) or []:
        toks += list(run)                       # 单字
        toks += [run[i] + run[i+1] for i in range(len(run)-1)]  # 二元组
    return [x for x in toks if len(x) >= 1]

def _segments(pdir):
    import os
    def rd(n):
        p = os.path.join(pdir, n)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    segs = []
    # 章节摘要：按 "## 第NN章" 分块
    cs = rd("11-章节摘要.md")
    for m in re.finditer(r"##\s*第(\d+)章.*?(?=\n##\s*第|\Z)", cs, re.S):
        segs.append({"ch": int(m.group(1)), "src": "摘要", "text": m.group(0)})
    # 大纲：按 "### 第NN章" 分块
    out = rd("01-大纲.md")
    for m in re.finditer(r"###?\s*第(\d+)章.*?(?=\n###?\s*第|\Z)", out, re.S):
        segs.append({"ch": int(m.group(1)), "src": "大纲", "text": m.group(0)[:800]})
    # 故事圣经
    sb = rd("09-故事圣经.md")
    if sb:
        segs.append({"ch": 0, "src": "圣经", "text": sb[:4000]})
    return [s for s in segs if len((s["text"] or "").strip()) > 10]

def _tf(text):
    c = Counter(_tokens(text))
    return c

def retrieve(query, pdir, k=3, exclude_ch=None):
    """返回 top-k 相关既往片段 [{ch, src, snippet, score}]，跳过 exclude_ch（通常是上一章）。"""
    segs = _segments(pdir)
    if len(segs) < 1:
        return []
    docs = [_tf(s["text"]) for s in segs]
    df = Counter()
    for d in docs:
        for t in set(d):
            df[t] += 1
    n = len(segs)
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
    q = _tf(query)
    scored = []
    for i, s in enumerate(segs):
        if exclude_ch is not None and s["ch"] == exclude_ch and s["src"] != "圣经":
            continue
        d = docs[i]
        common = set(q) & set(d)
        if not common:
            continue
        dot = sum(d[t] * q[t] * idf[t] for t in common)
        nd = math.sqrt(sum(v * idf.get(t, 1) ** 0 for t, v in d.items()) + 1)
        sq = math.sqrt(sum(v * idf.get(t, 1) ** 0 for t, v in q.items()) + 1)
        scored.append((dot / (nd * sq + 1e-9), s))
    scored.sort(key=lambda x: -x[0])
    return [{"ch": s["ch"], "src": s["src"], "snippet": s["text"].strip()[:400], "score": round(sc, 3)}
            for sc, s in scored[:k]]
