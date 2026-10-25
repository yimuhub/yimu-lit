"""参考资料抽取层：把用户上传的「设定集 / 历史资料 / 同类作品节选 / 采访 / 大纲 / 剧本样例」
变成干净纯文本，并做确定性的体裁判定、切片与元信息摘要。

职责边界
- 只负责「文件 → 文本 / 文本 → 切片」，不碰全局参考库目录、不调 LLM、不发网络请求。
- docx / pdf 依赖懒加载：缺库时返回 ok:false + 中文 warning，调用方（references.py）决定提示。
- 安全：扩展名白名单、文件大小上限、文件头粗校验、EPUB 解压总量上限、可选的目录前缀校验。
  不执行宏、不落盘解压、不读取白名单外的路径。

格式支持：.txt .md .markdown .html .htm .docx .pdf .epub
"""
from __future__ import annotations

import html
import os
import posixpath
import re
import zipfile
from html.parser import HTMLParser

ALLOWED_EXTS = (".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".pdf", ".epub")
MAX_BYTES = 30 * 1024 * 1024
# EPUB 逐文件累计的解压后上限，防压缩炸弹（30MB 的 zip 可膨胀到几十 GB）。
MAX_EPUB_UNCOMPRESSED = 80 * 1024 * 1024
# 扫描件常见特征：整本 PDF 抽出的文字少于这个数，基本可判定“无文本层”。
_MIN_PDF_TEXT = 20

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_KIND_PRIORITY = ("script", "poetry", "prose", "novel")


# ---------------------------------------------------------------- 基础清洗

def clean_text(text: str) -> str:
    """统一换行、去零宽/控制字符、去行尾空白、最多保留 1 个空行。

    保留空行是有意的：切片按段落边界走，空行是段落信号，不能全部抹掉。
    """
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    t = _ZERO_WIDTH.sub("", t)
    t = _CONTROL.sub("", t)
    lines = [ln.rstrip(" \u3000") for ln in t.split("\n")]
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip("\n")


def _headings(text: str, limit: int = 20) -> list[str]:
    """抽片段内的标题行（Markdown 标题 / 第X章回场 / 词牌名），给检索结果做结构提示。"""
    out: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or len(s) > 60:
            continue
        if (re.match(r"^#{1,6}\s+\S", s)
                or re.match(r"^第[0-9一二三四五六七八九十百千两零〇]+[章回节卷场幕]", s)
                or re.match(r"^(INT|EXT)[\.\s]", s, re.I)):
            out.append(s.lstrip("# ").strip())
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- 编码探测

def _decode_bytes(data: bytes) -> tuple[str, str]:
    """按中文环境最常见的顺序探测编码，返回 (文本, 命中编码)；latin-1 永不失败。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


# ---------------------------------------------------------------- HTML 清洗

class _HTMLTextExtractor(HTMLParser):
    """标准库 HTMLParser：丢弃 script/style/nav/footer，块级标签转换行。"""

    _SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript",
             "form", "svg", "iframe", "template"}
    _BLOCK = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4",
              "h5", "h6", "li", "tr", "table", "blockquote", "pre", "br", "hr",
              "ul", "ol", "dl", "dt", "dd", "figure", "figcaption"}
    _VOID = {"br", "hr", "img", "meta", "link", "input"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if (tag or "").lower() in self._VOID:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in self._SKIP:
            if self._skip:
                self._skip -= 1
            return
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data:
            self.parts.append(data)


_NOISE_LINE = re.compile(
    r"^(首页|主页|登录|注册|退出|搜索|更多|上一页|下一页|上一篇|下一篇|目录|导航|返回|"
    r"分享|收藏|评论|广告|版权|关于我们|联系我们|友情链接|网站地图|手机版|客户端|"
    r"copyright|all rights reserved|home|login|register|menu|search|next|prev)$",
    re.I,
)


def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if len(s) >= 8:
        return False
    if _NOISE_LINE.match(s):
        return True
    if any(w in s for w in ("广告", "版权", "登录", "注册", "扫码")):
        return True
    return bool(re.fullmatch(r"[\W_]+", s))


def _html_to_text(raw: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw or "")
        parser.close()
    except Exception:
        # HTMLParser 极少抛错；真抛了也尽量保住已解析部分。
        pass
    text = html.unescape("".join(parser.parts))
    lines = [ln.strip() for ln in text.splitlines()]
    kept = [ln for ln in lines if ln and not _is_noise_line(ln)]
    return "\n".join(kept)


# ---------------------------------------------------------------- EPUB

def _epub_spine_files(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """解析 META-INF/container.xml → content.opf 的 manifest/spine，返回正文文件顺序。

    失败时按文件名排序兜底：EPUB 规范允许 OPF 写法多样，不能因为解析失败就整本放弃。
    """
    import xml.etree.ElementTree as ET

    opf_path = ""
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", errors="ignore")
        root = ET.fromstring(container)
        for el in root.iter():
            if el.tag.split("}")[-1] == "rootfile":
                opf_path = (el.attrib.get("full-path") or "").lstrip("/")
                if opf_path:
                    break
    except Exception:
        opf_path = ""

    html_names = [n for n in names
                  if n.lower().endswith((".xhtml", ".html", ".htm")) and not n.startswith("__MACOSX/")]
    if not opf_path or opf_path not in names:
        candidates = [n for n in names if n.lower().endswith(".opf")]
        opf_path = candidates[0] if candidates else ""
    if not opf_path or opf_path not in names:
        return sorted(html_names)

    try:
        root = ET.fromstring(zf.read(opf_path).decode("utf-8", errors="ignore"))
    except Exception:
        return sorted(html_names)

    base = posixpath.dirname(opf_path)
    manifest: dict[str, str] = {}
    spine_ids: list[str] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "item":
            iid = el.attrib.get("id") or ""
            href = el.attrib.get("href") or ""
            if iid and href:
                manifest[iid] = posixpath.normpath(posixpath.join(base, href))
        elif tag == "itemref":
            idref = el.attrib.get("idref") or ""
            if idref:
                spine_ids.append(idref)

    ordered = [manifest[i] for i in spine_ids if i in manifest]
    ordered = [n for n in ordered if n in names and n.lower().endswith((".xhtml", ".html", ".htm"))]
    if not ordered:
        return sorted(html_names)
    # spine 未覆盖的正文文件附在后面，避免漏内容。
    seen = set(ordered)
    return ordered + [n for n in sorted(html_names) if n not in seen]


def _extract_epub(path: str) -> dict:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return _fail(".epub", "EPUB 文件损坏或不是合法 zip 包")

    with zf:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos if not i.is_dir())
        if total > MAX_EPUB_UNCOMPRESSED:
            return _fail(".epub", "EPUB 解压后超过 80MB 上限，已拒绝（防压缩炸弹）")
        names = [i.filename for i in infos if not i.is_dir()]
        files = _epub_spine_files(zf, names)
        parts: list[str] = []
        for name in files:
            if ".." in name.replace("\\", "/").split("/"):
                continue
            try:
                raw = zf.read(name).decode("utf-8", errors="ignore")
            except Exception:
                continue
            body = _html_to_text(raw)
            if body.strip():
                parts.append(body)
    text = clean_text("\n\n".join(parts))
    if not text.strip():
        return _fail(".epub", "EPUB 未提取到正文文本")
    return _ok(text, ".epub", warnings=["已按 spine 顺序合并 EPUB 正文"])


# ---------------------------------------------------------------- DOCX / PDF

def _extract_docx(path: str) -> dict:
    try:
        from docx import Document  # 懒加载：没装依赖时不影响其他格式
    except Exception:
        return _fail(".docx", "未安装 python-docx，无法解析 .docx；请安装 requirements.txt 中的依赖",
                     warnings=["缺少 python-docx，docx 解析已跳过"])
    try:
        doc = Document(path)
    except Exception:
        return _fail(".docx", "DOCX 文件损坏或不是合法 Word 文档")

    parts: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            parts.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            line = " | ".join(c for c in cells if c)
            if line:
                parts.append(line)
    text = clean_text("\n\n".join(parts))
    if not text.strip():
        return _fail(".docx", "DOCX 未提取到正文文本")
    return _ok(text, ".docx")


def _extract_pdf(path: str) -> dict:
    try:
        from pypdf import PdfReader
    except Exception:
        return _fail(".pdf", "未安装 pypdf，无法解析 .pdf；请安装 requirements.txt 中的依赖",
                     warnings=["缺少 pypdf，pdf 解析已跳过"])

    warnings: list[str] = []
    try:
        reader = PdfReader(path)
    except Exception:
        return _fail(".pdf", "PDF 文件损坏或不是合法 PDF")
    try:
        if reader.is_encrypted:
            try:
                ok = bool(reader.decrypt(""))
            except Exception:
                ok = False
            if not ok:
                return _fail(".pdf", "PDF 已加密，无法读取（不支持破解密码）",
                             warnings=["加密 PDF 需要先解除密码保护再上传"])
    except Exception:
        return _fail(".pdf", "PDF 加密状态无法判断，已拒绝")

    parts: list[str] = []
    pages = 0
    empty_pages = 0
    for page in reader.pages:
        pages += 1
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
            empty_pages += 1
        if t.strip():
            parts.append(t.strip())
        else:
            empty_pages += 1
    text = clean_text("\n\n".join(parts))
    if len(text.strip()) < _MIN_PDF_TEXT:
        warnings.append("PDF 无可提取文本，可能是扫描件；当前未支持 OCR，请先转成文字版再上传")
        res = _fail(".pdf", "PDF 无可提取文本（可能是扫描件，OCR 未支持）", warnings=warnings)
        res["pages"] = pages
        return res
    if empty_pages:
        warnings.append(f"有 {empty_pages} 页未提取到文本（可能是插图页或扫描页）")
    res = _ok(text, ".pdf", warnings=warnings)
    res["pages"] = pages
    return res


# ---------------------------------------------------------------- 主入口

def _ok(text: str, ext: str, warnings: list[str] | None = None) -> dict:
    clean = clean_text(text)
    return {
        "ok": True,
        "text": clean,
        "ext": ext,
        "kind": detect_kind(clean),
        "warnings": list(warnings or []),
        "chars": len(clean),
    }


def _fail(ext: str, error: str, warnings: list[str] | None = None) -> dict:
    return {
        "ok": False,
        "text": "",
        "ext": ext,
        "kind": "",
        "warnings": list(warnings or []),
        "chars": 0,
        "error": error,
    }


def magic_error(ext: str, head: bytes) -> str:
    """文件头粗校验：不改 zip 只认 PK，PDF 认 %PDF。返回中文错误，空串表示通过。"""
    if ext == ".pdf" and not head.startswith(b"%PDF"):
        return "文件头不是 PDF，已拒绝"
    if ext in (".docx", ".epub") and not head.startswith(b"PK\x03\x04"):
        return f"文件头不是合法 {ext[1:].upper()} 压缩包，已拒绝"
    return ""


def _resolve_file(path: str, root: str | None) -> tuple[str, str]:
    """返回 (真实路径, 错误)。root 非空时要求真实路径位于 root 之下。"""
    try:
        real = os.path.realpath(str(path))
    except Exception:
        return "", "文件路径非法"
    if root:
        rroot = os.path.realpath(str(root))
        try:
            inside = os.path.commonpath([rroot, real]) == rroot
        except ValueError:
            inside = False
        if not inside:
            return "", "文件路径越界，已拒绝"
    if not os.path.exists(real):
        return "", "文件不存在"
    if not os.path.isfile(real):
        return "", "路径不是文件"
    return real, ""


def extract_text(path: str, filename: str | None = None, *, root: str | None = None,
                 max_bytes: int | None = None) -> dict:
    """抽取任意白名单格式为纯文本。

    filename 用于判定扩展名（上传时浏览器给的名字），根目录校验交给 root：
    references.py 会传入参考目录，确保不会跟随符号链接读到目录外。
    max_bytes 默认读模块常量（运行时读取，便于测试/部署调整上限）。
    """
    limit = MAX_BYTES if max_bytes is None else int(max_bytes)
    real, err = _resolve_file(path, root)
    if err:
        return _fail("", err)
    name = filename or os.path.basename(real)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return _fail(ext, f"不支持的格式：{ext or '（无扩展名）'}；支持 "
                          + " ".join(ALLOWED_EXTS))
    try:
        size = os.path.getsize(real)
    except OSError:
        return _fail(ext, "无法读取文件大小")
    if size > limit:
        return _fail(ext, f"文件超过 {limit // (1024 * 1024)}MB 上限")

    try:
        with open(real, "rb") as f:
            head = f.read(8)
    except OSError:
        return _fail(ext, "文件读取失败")
    bad = magic_error(ext, head)
    if bad:
        return _fail(ext, bad, warnings=[bad])

    if ext in (".txt", ".md", ".markdown"):
        try:
            data = open(real, "rb").read(limit + 1)
        except OSError:
            return _fail(ext, "文件读取失败")
        text, enc = _decode_bytes(data)
        warnings = [] if enc in ("utf-8", "utf-8-sig") else [f"已按 {enc} 解码"]
        return _ok(text, ext, warnings=warnings)

    if ext in (".html", ".htm"):
        try:
            data = open(real, "rb").read(limit + 1)
        except OSError:
            return _fail(ext, "文件读取失败")
        raw, _enc = _decode_bytes(data)
        return _ok(_html_to_text(raw), ext)

    if ext == ".docx":
        return _extract_docx(real)
    if ext == ".pdf":
        return _extract_pdf(real)
    return _extract_epub(real)


# ---------------------------------------------------------------- 体裁判定

def detect_kind(text: str) -> str:
    """用格式/词汇启发式判定体裁；拿不准一律 reference（资料），宁缺毋滥。"""
    t = (text or "")[:200000]
    if len(t.strip()) < 20:
        return "reference"
    score = {"novel": 0, "script": 0, "poetry": 0, "prose": 0}

    # 剧本：场景头、场次、镜头术语、角色名+台词、旁白/字幕
    scenes = len(re.findall(
        r"(?m)^\s*(?:INT\.?|EXT\.?|INT/EXT|内景|外景|内/外景|日景|夜景)[^\n]{0,40}$", t, re.I))
    if scenes:
        score["script"] += 5 if scenes >= 2 else 3
    if re.search(r"(?m)^\s*第[0-9一二三四五六七八九十百]+场", t):
        score["script"] += 3
    if re.search(r"(?m)^\s*(?:FADE (?:IN|OUT)|CUT TO|DISSOLVE TO|淡入|淡出|切至)", t, re.I):
        score["script"] += 3
    speaker_lines = len(re.findall(
        r"(?m)^[\u4e00-\u9fffA-Za-z0-9·]{1,10}[（(]?[^：:\n]{0,12}[）)]?[：:]\s*\S", t))
    if speaker_lines >= 8:
        score["script"] += 2
    if t.count("旁白") + t.count("字幕") >= 2:
        score["script"] += 2
    if "剧本" in t[:800]:
        score["script"] += 2

    # 诗词：词牌/格律术语 + 大量短行
    poetry_terms = ("词牌", "平仄", "押韵", "格律", "平水韵", "词林正韵", "七律", "五律",
                    "七绝", "五绝", "绝句", "律诗", "填词", "上阕", "下阕", "上片", "下片")
    ph = sum(1 for w in poetry_terms if w in t)
    score["poetry"] += min(ph, 4) * 2
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 4:
        short = sum(1 for ln in lines if 3 <= len(ln) <= 30)
        # 只有“短行且基本无句读”才像古典诗词；否则普通短段落资料会被误判。
        plain = sum(1 for ln in lines
                    if 3 <= len(ln) <= 20
                    and not re.search(r"[。！？，、；：,.;:!?]", ln))
        if short / len(lines) >= 0.7 and plain / len(lines) >= 0.7 and len(t) < 6000:
            score["poetry"] += 3

    # 小说：章节标记 + 密集对白
    chapters = len(re.findall(r"(?m)^\s*第[0-9一二三四五六七八九十百千两零〇]+[章回节卷]", t))
    if chapters:
        score["novel"] += min(chapters, 5) * 2 + 2
    if "小说" in t[:800]:
        score["novel"] += 1
    dialogue = len(re.findall(r"[“「][^”」\n]{2,80}[”」]", t))
    if dialogue >= 10:
        score["novel"] += 2

    # 散文：自称体裁 + 篇幅适中
    for w in ("散文", "随笔", "札记", "游记", "杂文", "小品文", "抒情散文"):
        if w in t[:2000]:
            score["prose"] += 3
        elif w in t:
            score["prose"] += 1
    if score["prose"] and len(t) < 20000:
        score["prose"] += 1

    best = max(score.values())
    if best < 3:
        return "reference"
    for kind in _KIND_PRIORITY:
        if score[kind] == best:
            return kind
    return "reference"


# ---------------------------------------------------------------- 切片

def chunk_text(text: str, size: int = 1200, overlap: int = 150,
               max_chunks: int = 5000) -> list[dict]:
    """按段落边界切片，保留原文偏移，便于查重/引用回查。

    关键约束
    - start/end 指向传入 text 的真实下标，``text[start:end]`` 就是片段原文。
    - overlap 只在段落边界回退；退不动就放弃重叠，保证每轮 start 严格前进（不会死循环）。
    - 单个超长段落先找句末标点，再不行硬切，并标 ``hard_split``。
    """
    text = str(text or "")
    n = len(text)
    if not text.strip() or size <= 0 or max_chunks <= 0:
        return []
    size = max(120, int(size))
    overlap = max(0, min(int(overlap), size // 2))

    # 段落 = 连续非空行块；start/end 去掉尾部换行，让片段边界干净。
    blocks: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:[^\n]+(?:\n|$))+", text):
        s, e = m.start(), m.end()
        while e > s and text[e - 1] == "\n":
            e -= 1
        if e > s and text[s:e].strip():
            blocks.append((s, e))
    if not blocks:
        return []
    starts = [b[0] for b in blocks]
    ends = [b[1] for b in blocks]

    chunks: list[dict] = []
    pos = 0
    cursor = 0  # blocks 中第一个 start >= pos 的下标，避免每次全表扫描

    def _advance_cursor(p: int) -> int:
        i = cursor
        while i < len(starts) and starts[i] < p:
            i += 1
        return i

    while pos < n and len(chunks) < max_chunks:
        while pos < n and text[pos] in " \n":
            pos += 1
        if pos >= n:
            break
        cursor = _advance_cursor(pos)
        target = pos + size
        hard = False
        if target >= n:
            end = n
        else:
            # 最后一个段落尾 <= target；blocks 有序，可用二分。
            import bisect
            idx = bisect.bisect_right(ends, target) - 1
            if idx >= cursor and ends[idx] > pos:
                end = ends[idx]
            else:
                end = target
                hard = True
                window_start = pos + size // 2
                cut = max(text.rfind(ch, window_start, target) for ch in "。！？；!?;\n")
                if cut > window_start:
                    end = cut + 1
        while end > pos and text[end - 1] in " \n":
            end -= 1
        if end <= pos:  # 理论上不可达；兜底防死循环
            break
        body = text[pos:end]
        item = {"no": len(chunks) + 1, "text": body, "start": pos, "end": end,
                "headings": _headings(body)}
        if hard:
            item["hard_split"] = True
        chunks.append(item)

        if end >= n:
            break
        # 下一片：从 end-overlap 起，回退到其前最近的段落起点（只复用已出现过的段落）；
        # 找不到可回退的段落就放弃重叠，保证 start 严格前进。
        ns = end - overlap
        if ns <= pos:
            ns = end
        else:
            import bisect
            j = bisect.bisect_right(starts, ns) - 1
            if j >= 0 and starts[j] <= pos:
                j = -1
            if j >= 0:
                ns = starts[j]
            else:
                k = bisect.bisect_left(starts, ns)
                while k < len(starts) and starts[k] <= pos:
                    k += 1
                ns = starts[k] if k < len(starts) and starts[k] < end else end
        if ns <= pos:
            ns = end
        pos = ns
    return chunks


# ---------------------------------------------------------------- 元信息

def summarize_meta(text: str, filename: str = "") -> dict:
    """无 LLM 的确定性摘要：上传后立刻能在列表里显示标题/前 200 字/行数/有无对白。"""
    clean = clean_text(text or "")
    lines = [ln for ln in clean.splitlines() if ln.strip()]
    stem = os.path.splitext(safe_filename(filename or ""))[0] if filename else ""
    title = stem or "未命名参考"
    if lines:
        first = lines[0].strip().lstrip("#").strip()
        if (2 <= len(first) <= 40
                and not first.endswith(("。", "，", "、", "；", "："))
                and not re.match(r"^第[0-9一二三四五六七八九十百千两零〇]+[章节回场]", first)):
            title = first
    excerpt = re.sub(r"\s+", " ", clean).strip()[:200]
    has_dialogue = bool(re.search(r"[“「\"][^”」\"\n]{2,80}[”」\"]", clean))
    return {
        "title": title[:80],
        "excerpt": excerpt,
        "lines": len(lines),
        "has_dialogue": has_dialogue,
        "chars": len(clean),
        "kind": detect_kind(clean),
        "headings": _headings(clean, limit=10),
    }


def safe_filename(name: str) -> str:
    """只保留文件名本体与安全扩展名，限长 120；用户输入绝不直接进路径。"""
    raw = str(name or "").replace("\\", "/")
    base = raw.split("/")[-1]
    base = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", base).strip().strip(".")
    if not base:
        return "upload"
    stem, ext = os.path.splitext(base)
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)[:10].lower()
    if ext == ".":
        ext = ""
    limit = max(1, 120 - len(ext))
    stem = stem[:limit].strip() or "upload"
    return stem + ext
