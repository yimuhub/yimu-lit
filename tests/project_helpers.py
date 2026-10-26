"""tests/project_helpers.py —— v2.1 两套项目契约测试共用的假项目搭建器。

test_v21_library / test_v21_mediumbench 各自保留 four_works fixture（计划内容与断言
口径不同，不合并），但「建项目目录 + 写计划文件 + 可选文档文件」「把四个引擎的
PROJECTS_DIR 指到 tmp」这两件事完全同构，收拢到本模块避免两份副本各自漂移。

文件名纪律：计划文件名以各引擎模块的 F_PLAN 常量为唯一来源，绝不写死字符串；
小说没有 F_PLAN 常量，02-写作计划.json 是与前端约定的文件名，保持字面量。

本模块不做 sys.path hack：它只从测试文件（已把仓库根插入 sys.path）导入后使用。
"""
import json
import os

from bot import poetry_writer, prose_writer, script_writer, writer

NOVEL_PLAN = "02-写作计划.json"
# 计划文件名以各引擎模块的 F_PLAN 常量为唯一来源，绝不硬编码字符串。
PLAN_BY_MEDIUM = {"novel": NOVEL_PLAN, "script": script_writer.F_PLAN,
                  "poetry": poetry_writer.F_PLAN, "prose": prose_writer.F_PLAN}


def mk_project(tmp_path, key, plan_name, plan, files=()):
    """在 tmp_path 下建一个项目目录：写入计划文件，再写可选的额外文件。

    files 为 (相对文件名, 文本) 列表，支持子目录（如 04-正文/第1场.md）。
    """
    pdir = os.path.join(str(tmp_path), key)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, plan_name), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    for fname, text in files:
        fp = os.path.join(pdir, fname)
        os.makedirs(os.path.dirname(fp), exist_ok=True)  # 正文在 04-正文/ 子目录
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
    return pdir


def patch_all_projects(monkeypatch, tmp_path):
    """引擎端点读各自的模块级 PROJECTS_DIR，四个都要指向 tmp，否则打到真实 projects/。"""
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(poetry_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(prose_writer, "PROJECTS_DIR", str(tmp_path))
