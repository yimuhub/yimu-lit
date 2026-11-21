"""tests/conftest.py —— v0.7 R3 大厂工程化：隔离外部 LLM 边界。

所有测试不烧真 LLM：monkeypatch bot.llm 的 chat / chat_json 为确定性 stub。
（rewrite-plan 等端点内部会触发 five_role_review → chat_json；mock 后返回 {} 即可走通。）
同时关闭本地一次性令牌鉴权：TestClient 不带浏览器 Cookie，否则所有 /api/* 都会 401。

v5.5.0 数据根隔离：bot/paths.py 在非冻结（开发）模式下
  DATA_ROOT = os.environ.get("YIMU_DATA_DIR") or 仓库根
且在 bot.paths 被 import 的那一刻一次性解析；writer/llm/chief/各文体引擎的
PROJECTS_DIR、data/ 各运行时文件全部由它派生。此前测试没有设置该变量，
直接 writer.create_project(...) / POST /api/create 的用例会把测试书
（p_/e_/s_ 前缀与 20261121-* 等）灌进真实 projects/——一次全量 pytest
约 13 个目录。这里必须在任何 bot/app import 之前把数据根重定向到进程级
一次性临时目录，保证真实 projects/ 与 data/ 零新增、零改动；桌面端
（~/Library/Application Support/YiMuLit/）走 FROZEN 分支，本就与此无关。
临时根会用仓库根的配置/运行时数据做只读快照（读取口径与真实环境一致，
写坏只落 tmp），进程退出时整体清理（YIMU_KEEP_TEST_DATA=1 可保留排查）。
"""
import os

os.environ.setdefault("SHENBI_AUTH", "0")

# ---- v5.5.0：数据根隔离（必须先于 from bot import llm）----
import atexit
import shutil
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST_DATA_ROOT = os.environ.get("YIMU_TEST_DATA_DIR") or tempfile.mkdtemp(prefix="yimu-pytest-data-")
os.environ["YIMU_DATA_DIR"] = _TEST_DATA_ROOT


def _seed_repo_file(name: str, link: bool = False) -> None:
    src = os.path.join(_REPO_ROOT, name)
    dst = os.path.join(_TEST_DATA_ROOT, name)
    if not os.path.exists(src):
        return
    try:
        if link:
            # config.json 特殊：app.py 的写路径（/api/settings 等）走 RESOURCE_ROOT
            # （开发模式=仓库根，不随 YIMU_DATA_DIR 重定向），读路径走 DATA_ROOT。
            # 只有让两处指向同一个真实文件，读写才自洽（与未隔离前行为一致）；
            # 代价是测试仍会写真实 config.json——这是 app.py 层的既有行为，不在此扩大战场。
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
        else:
            shutil.copy2(src, dst)
    except OSError:
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass  # 快照失败不阻断测试（缺省路径都有兜底）


# config.json 用符号链接（读写同一真实文件，见上）；其余快照为副本，写坏只落 tmp。
_seed_repo_file("config.json", link=True)
for _name in ("providers.json", "model_rules.json",
              "user-preferences.json", "usage_global.json",
              "schedules.json", "jobs.json"):
    _seed_repo_file(_name)

# data/ 运行时数据快照（敏感词表/学习档案/系统日志/技能开关等）。
# update_backup（~83MB 自动更新备份）与测试无关，跳过。
_repo_data = os.path.join(_REPO_ROOT, "data")
_test_data_sub = os.path.join(_TEST_DATA_ROOT, "data")
if os.path.isdir(_repo_data) and not os.path.isdir(_test_data_sub):
    try:
        shutil.copytree(_repo_data, _test_data_sub, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("update_backup"))
    except OSError:
        pass


def _cleanup_test_data_root() -> None:
    if os.environ.get("YIMU_KEEP_TEST_DATA") == "1":
        return
    shutil.rmtree(_TEST_DATA_ROOT, ignore_errors=True)


atexit.register(_cleanup_test_data_root)

import pytest
from bot import llm


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)

    def _chat_stream_stub(messages, *a, **k):
        # chat_stream 缺省 mock：委托给当前生效的 llm.chat（测试可自行覆盖 chat），整段回调一次，绝不连网。
        kw = {x: v for x, v in k.items() if x != "on_delta"}
        text = llm.chat(messages, *a, **kw)
        cb = k.get("on_delta")
        if callable(cb) and text:
            cb(text)
        return text

    monkeypatch.setattr(llm, "chat_stream", _chat_stream_stub, raising=False)
    # v4.26.0：测试进程禁孵总编辑看护线程（tick_once 仍可显式调用做断言）
    from bot import supervisor as _sv
    monkeypatch.setattr(_sv, "TEST_MODE", True, raising=False)


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """v4.24.16：测试间共享全局状态统一清理——此前只有个别文件自己清（test_v230 的
    _RUNNING_BOOKS 随机红教训），其余文件共享 SESSIONS/PROJECT_INDEX/各运行标志注册表，
    同一 pytest 进程里前一个用例的状态会串到后一个（顺序依赖）。
    名字唯一（_clean_shared_state），不会被模块级同名 fixture 遮蔽。"""
    import app as _app
    from bot import writer as _writer
    from bot import livejob as _livejob
    regs = [
        (_app, "SESSIONS"), (_app, "PROJECT_INDEX"),
        (_writer, "_RUNNING_BOOKS"), (_writer, "_PARA_RW_PROGRESS"),
        # v4.33.3：v4.33.1 新增的两份全局状态也要登记，否则前一个用例遗留的
        # 「正在跑」会让 /api/status 的 running 断言串味（本地跑两遍才现形，
        # CI 上表现为莫名其妙的红）。
        (_writer, "_PIPELINE_COUNT"),
    ]
    for owner, name in regs:
        d = getattr(owner, name, None)
        if isinstance(d, dict):
            d.clear()
    _livejob.reset_for_tests()
    # v4.33.5：正文抽取结果按目录签名缓存，临时项目目录会被复用/删掉，不能跨用例留着
    from bot import script_entities as _se
    _se.reset_cache()
    # app.py 的运行标志注册表（_PARALLEL_RUNNING/_REWRITING/_RELEASE_RUNNING/_ONESHOT_RUNNING/_PARA_RW_RUNNING）
    for name in ("_PARALLEL_RUNNING", "_REWRITING", "_RELEASE_RUNNING",
                 "_ONESHOT_RUNNING", "_PARA_RW_RUNNING"):
        d = getattr(_app, name, None)
        if isinstance(d, dict):
            d.clear()
    yield
    for owner, name in regs:
        d = getattr(owner, name, None)
        if isinstance(d, dict):
            d.clear()
    _livejob.reset_for_tests()
    for name in ("_PARALLEL_RUNNING", "_REWRITING", "_RELEASE_RUNNING",
                 "_ONESHOT_RUNNING", "_PARA_RW_RUNNING"):
        d = getattr(_app, name, None)
        if isinstance(d, dict):
            d.clear()
