"""H-29 schema 漂移三连修复测试。

背景（用户实测 2026-10-12，账单 5 小时连续 7M/小时）：
1. 旧版角色模型有 emotion 字段（角色绑定语气），重构删除后 ORM 没了它，
   但旧库 project_characters 表里 `emotion TEXT NOT NULL` 还在——迁移
   机制只「补列」从不「删列」。
2. prepare 全流程跑完（大书一轮上千万字符 LLM）→ 收尾写库 INSERT 缺列 →
   IntegrityError → 事务回滚、全部成果报废。
3. H-28 配额重试再拉起 → 又全量烧 → 又炸 → 「日志塞满巨型 traceback +
   不间断循环 + 大量 token」三连。

修复三件套：
- 启动迁移自动 DROP 僵尸列（表有/ORM 无/NOT NULL 无默认 → INSERT 必炸）
- 删不掉时（老 SQLite）记入 schema_issues → prepare fail-fast 拒跑
  （零 LLM 消耗 + last_error 指引手动修复）
- 日志降噪：失败 traceback 只打一遍
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

import pytest

from backend.app.ai.base import LLMQuotaExhaustedError

pytest_plugins = ("pytest_asyncio",)


def _book_text(n_chapters: int) -> str:
    from backend.tests.test_prepare_perf_red import _book_text as _bt

    return _bt(n_chapters)


async def _get_project_row(project_id: str):
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        return await sess.get(Project, project_id)


async def _make_project(name: str, n_chapters: int = 3) -> str:
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file

    await init_db()
    resp = await create_project(name)
    await import_file(resp.project_id, _book_text(n_chapters).encode("utf-8"), f"{name}.txt")
    return resp.project_id


def _table_cols(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    finally:
        conn.close()


def _create_legacy_characters_table(db_path: str) -> None:
    """模拟旧 schema：project_characters 带 NOT NULL emotion 列（无默认值）。

    直接用 sqlite3 建表（绕过 ORM），列集与旧版模型一致 + emotion。SQLite
    未开 FK 强制（见 session._install_sqlite_pragmas 注释），无 FK 也能模拟。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE project_characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                gender VARCHAR(16) DEFAULT '未知',
                age VARCHAR(64) DEFAULT '',
                personality VARCHAR(512) DEFAULT '',
                canonical_name VARCHAR(128),
                assigned_voice_id VARCHAR(128),
                emotion VARCHAR(32) NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_h29_legacy_emotion_column_dropped_and_prepare_ok(_isolate_data_dir, caplog):
    """主链路：旧库残留 emotion NOT NULL → init_db 自动 DROP → prepare 全流程
    跑到 ready（收尾写库不再炸 IntegrityError）。"""
    from backend.app.core.config import settings
    from backend.app.db.session import init_db, schema_issues
    from backend.app.services.project import (
        _run_prepare_project_in_background as run_prepare,
    )

    # 1) 造旧 schema 库：先建表再 init_db（create_all 见表已存在不重建）
    db_path = settings.DATABASE_URL.split("///")[-1]
    _create_legacy_characters_table(db_path)
    assert "emotion" in _table_cols(db_path, "project_characters")

    caplog.set_level(logging.WARNING, logger="backend.app.db.session")
    await init_db()

    # 2) 迁移把僵尸列删了，健康检查通过
    cols = _table_cols(db_path, "project_characters")
    assert "emotion" not in cols, f"emotion 僵尸列应被 DROP，实际列: {cols}"
    assert "name" in cols, "ORM 需要的列不能被误删"
    assert schema_issues() == [], "清理成功后不应有残留问题"
    assert "已删除" in caplog.text and "emotion" in caplog.text

    # 3) prepare 全链路：收尾写库不再 IntegrityError → ready
    pid = await _make_project("H29-迁移后可写", n_chapters=3)
    await run_prepare(pid)

    proj = await _get_project_row(pid)
    assert proj.status == "ready", f"迁移后 prepare 应成功，实际 status={proj.status}"
    prog = json.loads(proj.progress_json or "{}")
    assert "last_error_type" not in prog, prog


@pytest.mark.asyncio
async def test_h29_failfast_when_column_uncleanable(_isolate_data_dir, monkeypatch, caplog):
    """DROP 失败（老 SQLite）兜底：schema_issues 非空 → trigger_prepare 与
    _do_prepare 双入口都 fail-fast，零 LLM 调用，last_error 给出修复指引。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.db import session as sessmod
    from backend.app.services.project import (
        trigger_prepare_project,
        _do_prepare_project_async,
    )

    pid = await _make_project("H29-failfast")
    monkeypatch.setattr(sessmod, "_SCHEMA_ISSUES", ["project_characters.emotion"])

    # trigger 入口：抛 RuntimeError + 项目置 failed + last_error_type=SchemaIssues
    with pytest.raises(RuntimeError, match="表结构与代码不一致"):
        await trigger_prepare_project(pid)
    proj = await _get_project_row(pid)
    assert proj.status == "failed"
    prog = json.loads(proj.progress_json or "{}")
    assert prog.get("last_error_type") == "SchemaIssues"
    assert "ALTER TABLE" in prog.get("last_error") or "手动修复" in prog.get("last_error")

    # 0 次 LLM 调用（烧 token 前拦截是本修复的核心价值）
    assert len(ai_factory._llm_instance.calls) == 0

    # _do_prepare 入口（看门狗/自动重试拉起路径）同样拒跑
    calls_before = len(ai_factory._llm_instance.calls)
    with pytest.raises(RuntimeError, match="表结构与代码不一致"):
        await _do_prepare_project_async(pid)
    assert len(ai_factory._llm_instance.calls) == calls_before


@pytest.mark.asyncio
async def test_h29_failfast_blocks_quota_auto_retry(_isolate_data_dir, monkeypatch):
    """schema 残留时 H-28 自动重试也会被 _do 入口的 fail-fast 挡住：
    拉起 → 立即 RuntimeError → last_error_type 非配额 → H-28 不再拉。
    整个闭环 0 次 LLM 调用。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.db import session as sessmod
    from backend.app.services.project import _scan_quota_failed_and_retry

    pid = await _make_project("H29-自动重试挡板")
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project
    from datetime import datetime, timedelta

    factory = get_session_factory()
    async with factory() as sess:
        proj = await sess.get(Project, pid)
        prog = json.loads(proj.progress_json or "{}")
        prog.update({
            "last_error_type": "LLMQuotaExhausted",
            "last_error": "LLMQuotaExhausted: LLM 配额已耗尽（测试）",
            "quota_retry": {
                "attempts": 1,
                "next_at": (datetime.now() - timedelta(minutes=1)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
        })
        proj.progress_json = json.dumps(prog, ensure_ascii=False)
        proj.status = "failed"
        await sess.commit()

    monkeypatch.setattr(sessmod, "_SCHEMA_ISSUES", ["project_characters.emotion"])
    # probe 放行（模拟配额已恢复——挡板必须挡在 _do 而不是 probe）
    async def _probe_ok():
        return True
    monkeypatch.setattr(
        "backend.app.services.project._probe_llm_quota", _probe_ok
    )

    await _scan_quota_failed_and_retry(trigger="watchdog")

    # 拉起后 _do 立刻 fail-fast → 项目重新 failed，错误类型非配额
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        proj = await _get_project_row(pid)
        if proj.status == "failed":
            break
        await asyncio.sleep(0.1)
    prog = json.loads((await _get_project_row(pid)).progress_json or "{}")
    assert "表结构与代码不一致" in (prog.get("last_error") or ""), prog
    # H-28 不再把 SchemaIssues 项目当配额失败重试
    from backend.app.services.project import _is_quota_failed_prog
    assert not _is_quota_failed_prog(prog)
    # 0 次 LLM 调用
    assert len(ai_factory._llm_instance.calls) == 0
