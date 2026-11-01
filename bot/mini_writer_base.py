"""v4.10-4.12：children/news/copy 三文体共用的小型写作引擎基类。

与 thesis_writer 同构：线性阶段、产物全落盘、resume_project 断点恢复。
子类只需定义 类常量（文件名/字段名） 与 stage_fns（阶段函数表）。
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_json, safe_join


class MiniWriterProject:
    PLAN_FILE = "计划.json"
    NAME_KEY = "name"
    FORM_KEY = "form"
    BODY_DIR = "02-正文"
    FINAL_FILE = "04-定稿.md"
    PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.running = False

    def log(self, msg: str):
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
        try:
            with open(os.path.join(self.path, "运行日志.txt"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def plan(self):
        p = os.path.join(self.path, self.PLAN_FILE)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def save_plan(self, plan):
        plan["updatedAt"] = datetime.datetime.now().isoformat()
        atomic_write_json(os.path.join(self.path, self.PLAN_FILE), plan)

    def status(self) -> dict:
        plan = self.plan() or {}
        meta = plan.get("meta") or {}
        total = int(plan.get("totalChapters") or len((plan.get("outline") or {}).get("chapters") or []))
        done = sum(1 for v in (plan.get("chapterStatus") or {}).values() if v == "completed")
        if plan.get("status") == "completed":
            done = total
        return {
            "state": plan.get("status", "unknown"),
            "done": done, "total": total,
            "words": meta.get("words", 0),
            "name": plan.get(self.NAME_KEY, self.name), "medium": self.MEDIUM,
            "form": plan.get(self.FORM_KEY, ""),
            "stage": meta.get("stage", ""),
        }

    MEDIUM = ""


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", name or "")[:40] or "未命名"


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def make_get_project(cls):
    def get_project(key: str):
        try:
            pdir = safe_join(cls.PROJECTS_DIR, key, label="作品 key")
        except ValueError:
            return None
        if not os.path.isdir(pdir):
            return None
        if not os.path.exists(os.path.join(pdir, cls.PLAN_FILE)):   # v4.13.0：只认本引擎的计划文件，防止别的文体的 key 误匹配
            return None
        return cls(pdir, key)
    return get_project


def run_pipeline(proj, cfg: dict, stages: list, plan: dict | None = None,
                 done_msg: str = "✅ 全部阶段完成"):
    """线性执行阶段表 stages = [(code, label, fn)…]（fn 为零参闭包，捕获各自的阶段上下文）。
    每阶段完成后 save_plan（断点恢复时跳过已完成阶段，失败不阻断后续）。
    阶段间检查暂停位——用户点「暂停」后跑完当前阶段即停（v4.20.0：儿童/新闻/文案共用）。"""
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
    meta = plan["meta"]
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan.setdefault("chapterStatus", {})
    from . import steps as _steps
    import time as _time
    _bk = os.path.basename(proj.path)
    for code, label, fn in stages:
        if (plan.get("chapterStatus") or {}).get(code) == "completed":
            continue
        if (plan.get("meta") or {}).get("paused"):
            plan["status"] = "paused"
            proj.save_plan(plan)
            proj.log("⏸ 已暂停，点「继续」可从当前进度恢复")
            _steps.emit(_bk, "system", "warn", title="已暂停", output=f"停在阶段 {code} {label}")
            return plan
        proj.log(f"📋 {code} {label}…")
        # v4.21.5：阶段埋点入创作活动流（儿童/新闻/文案/其他全文体统一「谁在干什么」）
        _t0 = _time.time()
        _steps.emit(_bk, "writer", phase="start", title=f"{code} {label}",
                    thinking=f"按体裁规范推进「{label}」阶段，产出落盘后进入下一步")
        meta["stage"] = code
        proj.save_plan(plan)
        _stage_err = ""
        try:
            fn()
            plan["chapterStatus"][code] = "completed"
        except NotImplementedError:
            plan["chapterStatus"][code] = "skipped"
        except Exception as e:
            proj.log(f"⚠️ {code} 失败（不阻断后续）：{e}")
            plan["chapterStatus"][code] = "failed"
            _stage_err = str(e)[:120]
        meta["stage"] = code
        plan["meta"] = meta
        proj.save_plan(plan)
        _steps.emit(_bk, "writer",
                    phase="warn" if _stage_err else "done",
                    title=f"{code} {label} 完成" if not _stage_err else f"{code} 失败（不阻断后续）",
                    chapter=0, ms=int((_time.time() - _t0) * 1000),
                    output=_stage_err or "")
    plan["status"] = "completed"
    meta["stage"] = "F"
    proj.save_plan(plan)
    proj.log(done_msg)
