"""v4.10-4.12：children/news/copy 三文体共用的小型写作引擎基类。

与 thesis_writer 同构：线性阶段、产物全落盘、resume_project 断点恢复。
子类只需定义 类常量（文件名/字段名） 与 stage_fns（阶段函数表）。
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_text, atomic_write_json, safe_join

from . import llm


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


def run_pipeline(proj, cfg: dict, stages: list, plan: dict | None = None):
    """线性执行阶段表 stages = [(code, label, fn(proj, pdir, cfg, plan, meta))...]。
    每阶段完成后 save_plan（断点恢复时跳过已完成阶段）。"""
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
    meta = plan["meta"]
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan.setdefault("chapterStatus", {})
    done_set = {k for k, v in (plan.get("chapterStatus") or {}).items() if v == "completed"}
    pdir = proj.path
    for i, (code, label, fn) in enumerate(stages):
        if code in done_set and i < len(stages) - 1:
            continue
        proj.log(f"📋 {code} {label}…")
        meta["stage"] = code
        proj.save_plan(plan)
        try:
            fn(proj, pdir, cfg, plan, meta)
            plan["chapterStatus"][code] = "completed"
        except NotImplementedError:
            plan["chapterStatus"][code] = "skipped"
        except Exception as e:
            proj.log(f"⚠️ {code} 失败（不阻断后续）：{e}")
            plan["chapterStatus"][code] = "failed"
        meta["stage"] = code
        plan["meta"] = meta
        proj.save_plan(plan)
    plan["status"] = "completed"
    meta["stage"] = "F"
    proj.save_plan(plan)
    proj.log("✅ 全部阶段完成")
