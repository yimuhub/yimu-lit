"""极速模式（Express）：只求写完、不求写好的执行规格（v4.24.0）。

为什么单独一个模块：本项目的质量环节散落在 9 个写作引擎里，且大多读全局
`config.json`。极速模式要的是**每本书**的开关——开一本书不该把整台机器都变快。
更重要的是「跳哪些环节」必须有唯一解释器：否则以后新增一个质量环节，就得在
9 个地方各补一次判断，漏一处就变成「极速模式里偷偷还在评分」。

约定：所有 writer 只问 `on()` / `skip_stage()`，不自己拼 `cfg.get(...)` 条件。

保留原则（这是本模块最要紧的边界）：
  跳过的只有「评估 / 修订 / 打磨 / 去味 / 多版本」这类**不产出新内容**的环节；
  「章卡规划、章节摘要、连续性索引、格律校验、时代校验、场次结构」一律保留——
  它们产出的是**一致性与结构**，砍掉就不是快，而是写出一本前后矛盾的书。
"""
from __future__ import annotations

#: 极速模式对长篇分章的字数规则。
#: 目标字数照给（少给会让章卡的目标/冲突/转折/兑现装不下，连续性从第 3 章就崩），
#: 但验收线从标准的 90% 降到 60%，且不足时只补写一次（标准是 3 轮）。
WORD_FLOOR_RATIO = 0.60

#: 各文体要跳过的阶段码。只列「评审/打磨」阶段，创作与交付阶段一个不跳。
#: novel 不在此表：它走 writer.py 的逐章分支，不按阶段码调度。
_SKIP_STAGES: dict[str, frozenset[str]] = {
    "novel": frozenset(),
    "script": frozenset(),          # 时代校验是确定性一致性检查（零 LLM 成本），保留
    "poetry": frozenset({"R"}),     # 只跳 R 评改；K 格律校验是纯 CPU 的，必须留着
    "prose": frozenset({"A"}),      # 跳 A 去味；P 精修只是拼接定稿，保留
    "thesis": frozenset({"T4"}),    # 跳降 AIGC 润色
    "children": frozenset(),        # C1-C5 都是创作/交付内容，没有评审阶段可跳
    "news": frozenset({"N3", "N4"}),  # 跳事实核查与多版本
    "copy": frozenset(),            # 全部是创作内容
    "other": frozenset({"O3"}),     # 跳打磨
}


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "express")
    return bool(v)


def on(cfg) -> bool:
    """这本书是不是极速模式。

    两种写法等价：`{"express": true}`（界面开关）与 `{"mode": "express"}`（创作模式卡片）。
    兼容字符串是因为向导的 cards 字段提交的是 `"express"` / `"standard"` 而不是布尔。
    """
    c = cfg if isinstance(cfg, dict) else {}
    if c.get("express") not in (None, ""):
        return _truthy(c["express"])
    return str(c.get("mode") or "").strip().lower() == "express"


def skip_stage(medium: str, code: str) -> bool:
    """该文体的这个阶段在极速模式下是否跳过。未知文体一律不跳（宁可慢，不可残缺）。"""
    return str(code or "") in _SKIP_STAGES.get(str(medium or ""), frozenset())


def stages(medium: str) -> frozenset[str]:
    """给 UI/日志用：该文体在极速模式下会跳过哪些阶段码。"""
    return _SKIP_STAGES.get(str(medium or ""), frozenset())


#: 切回标准模式时，小说「创作模式」卡片回落到的取值。
#: 老三张卡（滚动规划/大纲优先/一次成稿）此前都没有代码读它们，
#: 但保留一个语义正确的默认值，比留着一个「express」更诚实。
STANDARD_MODE = "rolling"


def set_plan_flag(plan: dict, on_: bool, *, sync_mode: bool = True) -> dict:
    """v4.24.13：把极速开关写进 plan，就地返回同一个 plan。

    **所有入口只能走这里**。v4.24.12 之前有两个写入者：
    `/api/express-toggle` 写 `config.express` + 顶层 `plan["express"]`，
    `/api/express-write` 写 `config.express` + `config.mode`。
    同一本书因此可能出现「顶层说开、config 说关」这种自相矛盾的状态；
    顶层字段虽然当前没有读者，但两份状态迟早会被某个新代码读到。

    现在统一：`config.express` 是唯一判据（`on()` 的口径），顶层 `plan["express"]`
    只做同步镜像（兼容老数据），`config.mode` 只在小说路径同步（sync_mode=True）——
    非小说没有「创作模式」卡片，给它们的 config 塞一个 `mode="express"` 是无意义的脏字段。

    切回标准时显式写 `config.express=False`：`on()` 优先看 `express` 键，
    只把 mode 改回 rolling 是关不掉老书的。
    """
    if not isinstance(plan, dict):
        return plan
    cfg = plan.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
        plan["config"] = cfg
    cfg["express"] = bool(on_)
    plan["express"] = bool(on_)      # 顶层镜像：与 config 永远同值，避免两处读出相反状态
    if not sync_mode:
        return plan
    if on_:
        # 记住切换前的创作模式（滚动规划/大纲优先/一次成稿），
        # 切回标准时还原——否则用户选过「大纲优先」，开一次极速就被永久改成 rolling。
        if str(cfg.get("mode") or "").strip().lower() != "express":
            cfg["_mode_before_express"] = cfg.get("mode")
        cfg["mode"] = "express"
    elif str(cfg.get("mode") or "").strip().lower() == "express":
        _prev = cfg.pop("_mode_before_express", None)
        cfg["mode"] = _prev or STANDARD_MODE
    return plan


def plan_flag(plan: dict) -> bool:
    """这本书当前是不是极速模式（读 plan.config，与 on() 同一口径）。"""
    if not isinstance(plan, dict):
        return False
    return on(plan.get("config") or {})
