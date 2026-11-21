"""v5.5.0 测试数据根隔离验证：全量 pytest 不得触碰真实 projects/ 与 data/。

污染史（2026-11-21 实测）：conftest 未隔离数据根时，一次全量 pytest 会往仓库
真实 projects/ 灌约 13 个测试书目录（p_/e_/s_ 前缀文体书 + 20261121-*-一个关于
灯塔的故事 等，多为空目录或仅 任务档案.json/创作事件.jsonl 残留）。根因：
bot/paths.py 在开发模式 DATA_ROOT = YIMU_DATA_DIR or 仓库根，import 期一次解析，
而大量用例直接 writer.create_project(...) 或 POST /api/create。

conftest 在任何 bot import 之前把 YIMU_DATA_DIR 指向进程级临时目录。本文件钉住：
1. env/DATA_ROOT 已重定向到系统临时区（非仓库根）；
2. 全部文体引擎 + llm/chief 的 projects/data 派生路径都落在临时数据根；
3. 端到端：writer.create_project 与 POST /api/create（无 title 骨架）创建的书
   全部落 tmp，真实 projects/ 与 data/ 前后快照零新增、零改动。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app as appmod
from bot import (paths, writer, poetry_writer, prose_writer, script_writer,
                 thesis_writer, children_writer, news_writer, copy_writer,
                 other_writer, llm, chief)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_PROJECTS = os.path.join(REPO_ROOT, "projects")
REAL_DATA = os.path.join(REPO_ROOT, "data")


def _snapshot_projects():
    return set(os.listdir(REAL_PROJECTS)) if os.path.isdir(REAL_PROJECTS) else set()


def _snapshot_data():
    """data/ 的 (名字, 类型, 大小) 快照：目录比名字，文件比字节，防止追加式改动漏网。"""
    out = set()
    if not os.path.isdir(REAL_DATA):
        return out
    for name in os.listdir(REAL_DATA):
        p = os.path.join(REAL_DATA, name)
        if os.path.isdir(p):
            out.add((name, "d", -1))
        else:
            out.add((name, "f", os.path.getsize(p)))
    return out


def test_data_root_redirected_to_temp():
    env = os.environ.get("YIMU_DATA_DIR")
    assert env, "conftest 必须在任何 bot import 前设置 YIMU_DATA_DIR"
    assert paths.DATA_ROOT == env, "DATA_ROOT 必须等于 YIMU_DATA_DIR（import 期解析）"
    assert os.path.realpath(paths.DATA_ROOT) != os.path.realpath(REPO_ROOT), \
        "测试数据根不得指向仓库根"
    tmp_root = os.path.realpath(tempfile.gettempdir())
    assert os.path.realpath(paths.DATA_ROOT).startswith(tmp_root), \
        f"数据根应落在系统临时区 {tmp_root}，实际 {paths.DATA_ROOT}"
    assert os.path.isdir(paths.DATA_ROOT)
    assert "yimu-pytest-data-" in os.path.basename(paths.DATA_ROOT)


def test_all_engines_projects_dir_under_temp_data_root():
    expected = os.path.join(paths.DATA_ROOT, "projects")
    for mod in (writer, poetry_writer, prose_writer, script_writer, thesis_writer,
                children_writer, news_writer, copy_writer, other_writer):
        assert getattr(mod, "PROJECTS_DIR") == expected, f"{mod.__name__}.PROJECTS_DIR 未随数据根隔离"
    assert llm._USAGE_GLOBAL.startswith(paths.DATA_ROOT)
    assert llm._TRACE_FILE.startswith(paths.DATA_ROOT)
    assert llm.LLM_ERROR_LOG.startswith(os.path.join(paths.DATA_ROOT, "data"))
    assert paths.config_file().startswith(paths.DATA_ROOT)


def test_create_project_and_api_create_stay_in_tmp():
    real_before = _snapshot_projects()
    data_before = _snapshot_data()

    # ① 直连写作引擎（此前污染主力路径：test_v1_poetry/prose/script 等）
    cfg = {"idea": "隔离验证用例书", "genre": "都市", "total_chapters": 2,
           "min_words_per_chapter": 10}
    proj = writer.create_project(cfg, [{"title": "隔离验证书"}], name="隔离验证书")
    try:
        assert os.path.dirname(proj.path) == os.path.join(paths.DATA_ROOT, "projects"), \
            f"create_project 落在 {proj.path}，未进临时数据根"
        assert os.path.isfile(os.path.join(proj.path, "02-写作计划.json"))
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)

    # ② REST /api/create（test_p0_regressions 的污染路径）：无 title → awaiting_title 骨架
    client = TestClient(appmod.app)
    r = client.post("/api/create", json={"idea": "隔离验证第二本", "total_chapters": 2})
    assert r.status_code == 200, r.text[:200]
    body = r.json() or {}
    key = body.get("key") or body.get("project") or ""
    if key:
        pdir = os.path.join(paths.DATA_ROOT, "projects", str(key))
        assert os.path.isdir(pdir) and pdir.startswith(paths.DATA_ROOT)
        shutil.rmtree(pdir, ignore_errors=True)

    # ③ 真实数据目录零新增、零改动
    assert _snapshot_projects() == real_before, "真实 projects/ 出现新增（隔离失效）"
    assert _snapshot_data() == data_before, "真实 data/ 出现新增或改动（隔离失效）"
