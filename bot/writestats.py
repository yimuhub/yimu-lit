"""码字统计全局账本（v5.4.0）。

设计（docs/系统化升级方案-v5.1 第 6.1 节，全部自研）：
  · 全局账本 ``data/writestats.json``（放在 projects/ 之外，跨作品、跨端共享）：
      days['YYYY-MM-DD'][bookKey] = {manual, ai}
      sessions['YYYY-MM-DD'] = {slots: {会话id: {key, chars, updated}}, done: [已并入的会话id]}
      chapterBase[bookKey][章号] = 基线字数
      goals = {manual, ai}（日目标；月目标 = 日目标 × 当月天数，实时算不落盘）
  · 手写走会话口径：编辑会话实时净增幂等写入 session 槽位，定稿（commit）时并入 manual；
      删字不回扣已入账（槽位只增不减）；
  · AI 走基线差：AI 插字 = 并入当日 ai 槽 + 抬升章节基线，落盘求差只剩手写部分，不双记；
  · 崩溃兜底：跨天遗留的未并入会话在读取时自动结算（settle_stale_sessions）；
  · 零 LLM、确定性、可单测；写入统一走 safeio.atomic_write_json + 进程内锁 + fcntl 文件锁。

会话幂等（关键约定）：
  put_session / commit_session 都以「会话 id」为幂等键；
  commit 会把会话 id 记进该日的 done 墓碑表——即使客户端崩溃后重放
  [upsert → commit] 序列，也绝不会重复并入 manual。
"""
from __future__ import annotations

import calendar
import contextlib
import datetime
import json
import re
import threading

from .paths import data
from .safeio import atomic_write_json

# 全局账本文件（tests 里 monkeypatch 本常量即可整体隔离）
DATA_FILE = data("writestats.json")

# 防膨胀：账本只保留最近约 2 年的日明细
MAX_DAYS = 800
# 单日会话墓碑上限（正常一天几十个会话，200 足够）
MAX_DONE_PER_DAY = 200

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def today_str(now: datetime.datetime | None = None) -> str:
    """本地时区的 YYYY-MM-DD。"""
    return (now or datetime.datetime.now()).strftime("%Y-%m-%d")


def _norm_date(value, now: datetime.datetime | None = None) -> str:
    """客户端传来的日期：格式不合法 → 今天；未来日期 → 今天（只许补记过去，不许预记未来）。"""
    s = str(value or "").strip()
    if not DATE_RE.match(s):
        return today_str(now)
    today = today_str(now)
    return today if s > today else s


def _norm_key(value) -> str:
    """作品 key：只留安全字符，防脏数据把账本撑爆。"""
    s = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", str(value or "").strip())
    return (s or "_unknown")[:80]


def _norm_chapter(value) -> str:
    try:
        n = int(value)
        return str(max(1, n))
    except (TypeError, ValueError):
        return "1"


def _int_ge0(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _lock_for(path: str) -> threading.RLock:
    with _LOCK_GUARD:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[path] = lk
        return lk


@contextlib.contextmanager
def _locked():
    """进程内 RLock + 跨进程 fcntl 文件锁（fcntl 不可用退化为纯线程锁），对齐 bot/state.py。"""
    with _lock_for(DATA_FILE):
        fh = None
        try:
            try:
                import fcntl  # POSIX only
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fh = open(DATA_FILE + ".lock", "a+", encoding="utf-8")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fh is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass


def _empty() -> dict:
    return {
        "version": 1,
        "days": {},          # 'YYYY-MM-DD' -> {bookKey: {manual, ai}}
        "sessions": {},      # 'YYYY-MM-DD' -> {slots: {sid: {...}}, done: [...]}
        "chapterBase": {},   # bookKey -> {章号: 基线字数}
        "goals": {"manual": 2000, "ai": 500},
        "updated": "",
    }


def _sanitize(raw) -> dict:
    """读进来的账本做形状修复：坏字段一律回到默认，绝不抛错（统计是附加能力）。"""
    d = _empty()
    if not isinstance(raw, dict):
        return d
    days = raw.get("days")
    if isinstance(days, dict):
        for date, books in days.items():
            if not isinstance(books, dict):
                continue
            bucket = {}
            for key, v in books.items():
                if not isinstance(v, dict):
                    continue
                bucket[_norm_key(key)] = {"manual": _int_ge0(v.get("manual")), "ai": _int_ge0(v.get("ai"))}
            if bucket:
                d["days"][str(date)] = bucket
    sessions = raw.get("sessions")
    if isinstance(sessions, dict):
        for date, v in sessions.items():
            slots = v.get("slots") if isinstance(v, dict) else None
            done = v.get("done") if isinstance(v, dict) else None
            clean_slots = {}
            for sid, slot in (slots or {}).items():
                if isinstance(slot, dict):
                    clean_slots[str(sid)[:64]] = {
                        "key": _norm_key(slot.get("key")),
                        "chars": _int_ge0(slot.get("chars")),
                        "updated": str(slot.get("updated") or ""),
                    }
            clean_done = [str(x)[:64] for x in (done or []) if isinstance(x, str)]
            if clean_slots or clean_done:
                d["sessions"][str(date)] = {"slots": clean_slots, "done": clean_done[:MAX_DONE_PER_DAY]}
    base = raw.get("chapterBase")
    if isinstance(base, dict):
        for key, chs in base.items():
            if isinstance(chs, dict):
                d["chapterBase"][_norm_key(key)] = {
                    _norm_chapter(no): _int_ge0(w) for no, w in chs.items() if _int_ge0(w) > 0
                }
    goals = raw.get("goals")
    if isinstance(goals, dict):
        d["goals"] = {"manual": _int_ge0(goals.get("manual")), "ai": _int_ge0(goals.get("ai"))}
    return d


def load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _empty()
    return _sanitize(raw)


def save(d: dict) -> None:
    """落盘前做防膨胀裁剪（days/sessions 各自只留最近 MAX_DAYS 天），再原子写。"""
    days = d.get("days") or {}
    sessions = d.get("sessions") or {}
    keep_days = sorted(days)[-MAX_DAYS:]
    d["days"] = {k: days[k] for k in keep_days}
    keep_sess = sorted(sessions)[-MAX_DAYS:]
    d["sessions"] = {k: sessions[k] for k in keep_sess}
    d["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    atomic_write_json(DATA_FILE, d)


# ---------------------------------------------------------------- 会话槽位（手写）

def put_session(date: str, key: str, session: str, chars: int, *, now=None) -> dict:
    """幂等覆盖写当日「手写会话」槽位（同一会话 id 后写覆盖前写）。

    「删字不回扣已入账」的只增不减由客户端（static/v2/js/core/writestats-client.js）
    在会话累计上保证；服务端做纯幂等覆盖，是为了配合 AI 插入的归账修正
    （AI 文本若已随 update 进过手写累计，客户端需要把这部分从槽位里退回）。
    已定稿会话（done 墓碑）不接受任何回写，防崩溃重放双记。"""
    date = _norm_date(date, now)
    key = _norm_key(key)
    sid = str(session or "").strip()[:64] or "default"
    chars = _int_ge0(chars)
    with _locked():
        d = load()
        bucket = d["sessions"].setdefault(date, {"slots": {}, "done": []})
        if sid in bucket.get("done", []):
            # 已定稿的会话不允许再回写槽位（防崩溃重放双记）
            save(d)
            return {"ok": True, "idempotent": True, "chars": 0}
        if date != today_str(now):
            # 跨天补报：直接并入那一天的 manual，并记墓碑（重放也不会双记）
            day = d["days"].setdefault(date, {}).setdefault(key, {"manual": 0, "ai": 0})
            day["manual"] += chars
            if sid not in bucket.setdefault("done", []):
                bucket["done"].append(sid)
            save(d)
            return {"ok": True, "merged": True, "chars": chars}
        slots = bucket.setdefault("slots", {})
        slots[sid] = {"key": key, "chars": chars, "updated": datetime.datetime.now().isoformat(timespec="seconds")}
        save(d)
        return {"ok": True, "chars": chars}


def commit_session(date: str, key: str, session: str, *, now=None) -> dict:
    """把会话槽位并入当日 manual（幂等：done 墓碑里的会话绝不二次入账）。"""
    date = _norm_date(date, now)
    key = _norm_key(key)
    sid = str(session or "").strip()[:64] or "default"
    with _locked():
        d = load()
        bucket = d["sessions"].setdefault(date, {"slots": {}, "done": []})
        done = bucket.setdefault("done", [])
        slots = bucket.setdefault("slots", {})
        moved = 0
        if sid in done:
            pass  # 已并入过，幂等空转
        elif sid in slots:
            slot = slots.pop(sid)
            day = d["days"].setdefault(date, {}).setdefault(slot.get("key") or key, {"manual": 0, "ai": 0})
            moved = _int_ge0(slot.get("chars"))
            day["manual"] += moved
            done.append(sid)
        else:
            # 槽位不存在（没来得及上报就 commit / 崩溃重放）也记墓碑，
            # 防止迟到的 upsert 把槽位复活后再次 commit 造成双记。
            done.append(sid)
        del done[:-MAX_DONE_PER_DAY]
        save(d)
        return {"ok": True, "moved": moved}


def settle_stale_sessions(d: dict | None = None, *, today: str | None = None) -> dict:
    """崩溃兜底：把「不是今天」的未并入会话槽全部结算进各自的 manual。

    在 load/读取路径调用即可自愈；今天的槽位是活会话，不动。发生结算时落盘。"""
    today = today or today_str()
    d = d if d is not None else load()
    with _locked():
        sessions = d.get("sessions") or {}
        changed = False
        for date in [x for x in sessions if str(x) < today]:
            bucket = sessions[date] or {}
            slots = bucket.get("slots") or {}
            done = bucket.setdefault("done", [])
            for sid, slot in list(slots.items()):
                key = _norm_key(slot.get("key"))
                moved = _int_ge0(slot.get("chars"))
                day = d["days"].setdefault(date, {}).setdefault(key, {"manual": 0, "ai": 0})
                day["manual"] += moved
                if sid not in done:
                    done.append(sid)
                del done[:-MAX_DONE_PER_DAY]
                changed = True
            slots.clear()
        if changed:
            save(d)
        return d


# ---------------------------------------------------------------- AI 槽与章节基线

def add_ai(date: str, key: str, chars: int, *, now=None) -> dict:
    """AI 字数并入当日 ai 槽（只增不减）。"""
    date = _norm_date(date, now)
    key = _norm_key(key)
    chars = _int_ge0(chars)
    with _locked():
        d = load()
        day = d["days"].setdefault(date, {}).setdefault(key, {"manual": 0, "ai": 0})
        day["ai"] += chars
        save(d)
        return {"ok": True, "ai": day["ai"]}


def raise_baseline(key: str, chapter, *, chars: int | None = None, count: int | None = None) -> dict:
    """抬升章节基线。两种口径：
      · chars  —— 本次 AI 插入的字数（精确增量）；
      · count  —— 编辑器当前总字数（基线抬到 max(旧基线, count)，重试天然幂等）。"""
    key = _norm_key(key)
    ch = _norm_chapter(chapter)
    has_count = count is not None
    count = _int_ge0(count)
    chars = _int_ge0(chars)
    with _locked():
        d = load()
        bases = d["chapterBase"].setdefault(key, {})
        old = _int_ge0(bases.get(ch))
        new = max(old, count) if has_count else old + chars
        bases[ch] = new
        save(d)
        return {"ok": True, "base": new, "prev": old, "delta": max(0, new - old)}


def baseline_of(key: str, chapter) -> int:
    key = _norm_key(key)
    ch = _norm_chapter(chapter)
    d = load()
    return _int_ge0((d.get("chapterBase") or {}).get(key, {}).get(ch))


def record_ai_insert(date: str, key: str, chapter, chars: int, *, now=None) -> dict:
    """AI 插入的一次性入账：ai 槽 += chars，同时抬升章节基线（不双记的关键）。"""
    chars = _int_ge0(chars)
    date = _norm_date(date, now)
    key = _norm_key(key)
    base_res = raise_baseline(key, chapter, chars=chars)
    ai_res = add_ai(date, key, chars, now=now)
    return {"ok": True, "chars": chars, "base": base_res.get("base"), "ai": ai_res.get("ai")}


# ---------------------------------------------------------------- 目标

def set_goals(manual=None, ai=None) -> dict:
    with _locked():
        d = load()
        g = d.setdefault("goals", {"manual": 0, "ai": 0})
        if manual is not None:
            g["manual"] = min(_int_ge0(manual), 1000000)
        if ai is not None:
            g["ai"] = min(_int_ge0(ai), 1000000)
        save(d)
        return {"ok": True, "goals": dict(g)}


# ---------------------------------------------------------------- 统计口径

def day_total(d: dict, date: str, *, key: str | None = None) -> int:
    """某日总码字（manual + ai；含未定稿会话槽——它是即将入账的手写）。可按书过滤。"""
    n = 0
    for k, v in (d.get("days") or {}).get(date, {}).items():
        if key and k != key:
            continue
        n += _int_ge0(v.get("manual")) + _int_ge0(v.get("ai"))
    bucket = (d.get("sessions") or {}).get(date) or {}
    for sid, slot in (bucket.get("slots") or {}).items():
        if sid in (bucket.get("done") or []):
            continue
        if key and slot.get("key") != key:
            continue
        n += _int_ge0(slot.get("chars"))
    return n


def streak(d: dict | None = None, *, today: str | None = None) -> dict:
    """连续码字天数：今天写了从今天数，今天没写从昨天回数（昨天也没写就是 0）。"""
    today = today or today_str()
    d = d if d is not None else load()
    dt = datetime.date.fromisoformat(today)
    wrote_today = day_total(d, today) > 0
    start = dt if wrote_today else dt - datetime.timedelta(days=1)
    n = 0
    cur = start
    while n < 3660 and day_total(d, cur.isoformat()) > 0:  # 上限 ~10 年，防坏数据死循环
        n += 1
        cur -= datetime.timedelta(days=1)
    return {"days": n, "wrote_today": wrote_today}


def month_days(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def report(days: int = 30, *, month: str | None = None, now: datetime.datetime | None = None) -> dict:
    """给 GET /api/writestats 的汇总视图。
      rows      —— 最近 days 天（含今天），旧→新，缺的日子补零；
      month     —— 日历用的月视图（默认当月；month='YYYY-MM' 可看任意历史月）。"""
    days = min(max(1, _int_ge0(days)) or 30, 400)
    today_dt = now or datetime.datetime.now()
    today = today_dt.strftime("%Y-%m-%d")
    d = settle_stale_sessions(load())

    rows = []
    for i in range(days - 1, -1, -1):
        date = (today_dt - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        books = (d.get("days") or {}).get(date) or {}
        detail = {}
        manual = ai = 0
        for k, v in books.items():
            m = _int_ge0(v.get("manual"))
            a = _int_ge0(v.get("ai"))
            detail[k] = {"manual": m, "ai": a}
            manual += m
            ai += a
        sess_chars = 0
        for sid, slot in (((d.get("sessions") or {}).get(date) or {}).get("slots") or {}).items():
            if sid in (((d.get("sessions") or {}).get(date) or {}).get("done") or []):
                continue
            sess_chars += _int_ge0(slot.get("chars"))
            if slot.get("key"):
                cell = detail.setdefault(slot["key"], {"manual": 0, "ai": 0})
                cell["session"] = cell.get("session", 0) + _int_ge0(slot.get("chars"))
        rows.append({"date": date, "manual": manual, "ai": ai, "session": sess_chars, "books": detail})

    totals = {
        "manual": sum(r["manual"] for r in rows),
        "ai": sum(r["ai"] for r in rows),
        "session": sum(r["session"] for r in rows),
    }
    totals["all"] = totals["manual"] + totals["ai"]

    # 日历月视图（默认当月）
    mkey = str(month or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})$", mkey)
    if m:
        year, mon = int(m.group(1)), int(m.group(2))
    else:
        year, mon = today_dt.year, today_dt.month
    mrows = []
    month_manual = month_ai = 0
    for dd in range(1, month_days(year, mon) + 1):
        date = f"{year:04d}-{mon:02d}-{dd:02d}"
        books = (d.get("days") or {}).get(date) or {}
        detail = {k: {"manual": _int_ge0(v.get("manual")), "ai": _int_ge0(v.get("ai"))} for k, v in books.items()}
        manual = sum(x["manual"] for x in detail.values())
        ai = sum(x["ai"] for x in detail.values())
        sess_chars = 0
        for sid, slot in (((d.get("sessions") or {}).get(date) or {}).get("slots") or {}).items():
            if sid in (((d.get("sessions") or {}).get(date) or {}).get("done") or []):
                continue
            sess_chars += _int_ge0(slot.get("chars"))
        mrows.append({"date": date, "manual": manual, "ai": ai, "session": sess_chars, "books": detail})
        month_manual += manual
        month_ai += ai

    goals = dict(d.get("goals") or {"manual": 0, "ai": 0})
    mdays = month_days(year, mon)
    today_row = rows[-1] if rows else {"date": today, "manual": 0, "ai": 0, "session": 0, "books": {}}

    return {
        "ok": True,
        "days": rows,
        "totals": totals,
        "streak": streak(d, today=today),
        "goals": {
            "manual": goals.get("manual", 0),
            "ai": goals.get("ai", 0),
            "monthly_manual": goals.get("manual", 0) * mdays,
            "monthly_ai": goals.get("ai", 0) * mdays,
        },
        "month": {
            "key": f"{year:04d}-{mon:02d}",
            "year": year,
            "month": mon,
            "day_count": mdays,
            "manual": month_manual,
            "ai": month_ai,
            "rows": mrows,
        },
        "today": today_row,
        "chapter_base": d.get("chapterBase") or {},
    }
