"""H-17 临时调试：完整打印 prepare 后的 progress_json。"""
from __future__ import annotations

import json
import logging

import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.mark.asyncio
async def test_h17_debug_progress(_isolate_data_dir, monkeypatch, caplog):
    from backend.tests.test_prepare_perf_red import _h17_book_text
    from backend.app.core.config import settings
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Project
    from backend.app.services.project import create_project, import_file, prepare_project

    caplog.set_level(logging.INFO, logger="backend.app.services.project")

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)

    resp = await create_project("H17-调试")
    pid = resp.project_id
    await import_file(pid, _h17_book_text(12).encode("utf-8"), "dbg.txt")

    await prepare_project(pid)

    factory = get_session_factory()
    async with factory() as sess:
        p = await sess.get(Project, pid)
        prog = json.loads(p.progress_json) if p.progress_json else {}
    print("\n===== FINAL progress_json keys =====")
    print(sorted(prog.keys()))
    print("polish keys:", {k: v for k, v in prog.items() if k.startswith("polish")})
    print("===== polish log lines =====")
    for line in caplog.text.splitlines():
        if "polish" in line:
            print(line)
