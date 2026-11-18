"""v4.19.0：系统级操作日志（与会话级创作日志分离）。

会话级记录（各作品目录下的 运行日志.txt / chat_log.jsonl）只覆盖单本书的创作过程；
本模块补上系统侧的关键操作流水：服务启停、设置变更、模型供应商增删改、
技能添加/删除、创作岗位调整、笔名管理、自更新等，写入 data/system_log.jsonl。
供「设置 → 系统日志」面板展示与排查。

设计约束：
- 追加写 JSON Lines，超 2MB 自动裁剪保留最新 1200 条；
- 写入失败一律静默——日志永远不能影响主流程；
- detail 里不允许出现 api_key 明文（调用方负责脱敏，这里再兜底拦一层字段名）。
"""
import json, os, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(ROOT, "data", "system_log.jsonl")
MAX_BYTES = 2 * 1024 * 1024   # 超过即裁剪
KEEP = 1200                   # 裁剪后保留的最新条数

_LOCK = threading.Lock()

# detail 中出现这些字段名（不区分大小写）时整体抹掉，防止密钥入库
_SENSITIVE_KEYS = {"api_key", "key", "secret", "token", "password", "authorization"}


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in _SENSITIVE_KEYS else _sanitize(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def log_event(kind, action, detail=None, summary=""):
    """追加一条系统事件。kind：system / settings / provider / skill / agent / penname。"""
    try:
        entry = {
            "ts": int(time.time()),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": str(kind or "system"),
            "action": str(action or ""),
            "summary": str(summary or action or ""),
            "detail": _sanitize(detail if isinstance(detail, (dict, list, str, int, float, bool)) else str(detail)),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with _LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trim_if_needed()   # 追加后裁剪，保证文件始终不超过 KEEP 条
    except Exception:
        pass


def _trim_if_needed():
    try:
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) <= MAX_BYTES:
            return
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP:])
    except Exception:
        pass


def tail(limit=300, kind="", q=""):
    """读取最新 N 条（新→旧）。kind 精确过滤一类；q 对整条 JSON 做大小写不敏感包含匹配。"""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    try:
        limit = max(1, min(int(limit or 300), 1000))
    except (TypeError, ValueError):
        limit = 300
    ql = str(q or "").strip().lower()
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if kind and e.get("kind") != kind:
            continue
        if ql and ql not in json.dumps(e, ensure_ascii=False, default=str).lower():
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def count():
    """当前日志总条数（面板展示用；读不到按 0）。"""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return sum(1 for l in f if l.strip())
    except OSError:
        return 0


def clear():
    try:
        os.remove(LOG_FILE)
    except OSError:
        pass
