"""批次 9（结构性优化）回归测试：F-4 章节正文拆表 + F-8 prepare 耗时提示。

覆盖：
  T-CS1  save_chapters → load_chapter_rows 只拿 idx/title/text_len（**不读正文**）
  T-CS2  load_chapter 单章只取一行；不存在的章返回 None
  T-CS3  load_chapters 取全书正文
  T-CS4  老库兼容：project_chapters 为空时回落到 chapters_json（含 list/单章/计数三条路径）
  T-CS5  启动迁移回填：chapters_json → project_chapters（幂等，已有行时不重复插）
  T-CS6  count_chapters 走聚合，不拉正文
  T-CS7  F-8：prepare LLM 调用量估算（章节数/字数 → 次数；串行且量大时给 WARNING 提示）
  T-CS8  删除项目会清掉 project_chapters（无 relationship 级联，靠显式 DELETE）
"""
from __future__ import annotations

import json
import uuid

import pytest

pytest_plugins = ("pytest_asyncio",)


async def _mk_project(chapters, *, with_rows=True, chapters_json=None):
    """建项目（可选：是否同时写入 project_chapters 行）。返回 project_id。"""
    from backend.app.db.models import Project
    from backend.app.db.session import get_session_factory
    from backend.app.services.chapter import Chapter
    from backend.app.services.chapter_store import save_chapters

    factory = get_session_factory()
    pid = uuid.uuid4().hex
    raw = chapters_json if chapters_json is not None else json.dumps(
        [{"idx": c["idx"], "title": c["title"], "text": c["text"]} for c in chapters],
        ensure_ascii=False,
    )
    async with factory() as s:
        s.add(Project(project_id=pid, name="T", status="ready", chapters_json=raw))
        await s.flush()
        if with_rows:
            await save_chapters(s, pid, [Chapter(**c) for c in chapters])
        await s.commit()
    return pid


# =====================================================================
# T-CS1 / T-CS6
# =====================================================================
@pytest.mark.asyncio
async def test_cs1_rows_only_expose_len_not_text(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.chapter_store import load_chapter_rows

    await init_db()
    pid = await _mk_project([
        {"idx": 0, "title": "第一章", "text": "甲" * 10},
        {"idx": 1, "title": "第二章", "text": "乙" * 25},
    ])
    factory = get_session_factory()
    async with factory() as s:
        rows = await load_chapter_rows(s, pid)
    assert [(r.idx, r.title, r.text_len) for r in rows] == [
        (0, "第一章", 10), (1, "第二章", 25)
    ]
    # ChapterRow 结构里根本没有 text 字段 → 不可能因为「顺手读正文」拖慢列表
    assert not hasattr(rows[0], "text")


@pytest.mark.asyncio
async def test_cs6_count_chapters_aggregates(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.chapter_store import count_chapters

    await init_db()
    pid = await _mk_project([
        {"idx": 0, "title": "a", "text": "x" * 7},
        {"idx": 1, "title": "b", "text": "y" * 3},
    ])
    factory = get_session_factory()
    async with factory() as s:
        n, total = await count_chapters(s, pid)
    assert (n, total) == (2, 10)


# =====================================================================
# T-CS2 / T-CS3
# =====================================================================
@pytest.mark.asyncio
async def test_cs2_load_single_chapter(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.chapter_store import load_chapter

    await init_db()
    pid = await _mk_project([
        {"idx": 0, "title": "第一章", "text": "一二三"},
        {"idx": 5, "title": "第六章", "text": "四五六"},
    ])
    factory = get_session_factory()
    async with factory() as s:
        ch = await load_chapter(s, pid, 5)
        assert ch is not None and ch.title == "第六章" and ch.text == "四五六"
        assert await load_chapter(s, pid, 99) is None


@pytest.mark.asyncio
async def test_cs3_load_all_chapters(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.chapter_store import load_chapters

    await init_db()
    pid = await _mk_project([
        {"idx": 0, "title": "甲", "text": "AAA"},
        {"idx": 1, "title": "乙", "text": "BBB"},
    ])
    factory = get_session_factory()
    async with factory() as s:
        chs = await load_chapters(s, pid)
    assert [(c.idx, c.title, c.text) for c in chs] == [
        (0, "甲", "AAA"), (1, "乙", "BBB")
    ]


# =====================================================================
# T-CS4 老库回落
# =====================================================================
@pytest.mark.asyncio
async def test_cs4_falls_back_to_chapters_json_when_no_rows(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.chapter_store import (
        count_chapters, load_chapter, load_chapter_rows, load_chapters,
    )

    await init_db()
    # with_rows=False → 模拟尚未回填的老库
    pid = await _mk_project(
        [{"idx": 0, "title": "旧一", "text": "q" * 4},
         {"idx": 1, "title": "旧二", "text": "w" * 6}],
        with_rows=False,
    )
    factory = get_session_factory()
    async with factory() as s:
        rows = await load_chapter_rows(s, pid)
        assert [(r.idx, r.text_len) for r in rows] == [(0, 4), (1, 6)]
        chs = await load_chapters(s, pid)
        assert [c.text for c in chs] == ["qqqq", "wwwwww"]
        one = await load_chapter(s, pid, 1)
        assert one is not None and one.title == "旧二"
        assert await count_chapters(s, pid) == (2, 10)


@pytest.mark.asyncio
async def test_cs4_corrupt_json_does_not_crash(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.chapter_store import load_chapters

    await init_db()
    pid = await _mk_project([], with_rows=False, chapters_json="{不是合法 JSON")
    factory = get_session_factory()
    async with factory() as s:
        assert await load_chapters(s, pid) == []


# =====================================================================
# T-CS5 启动迁移回填
# =====================================================================
@pytest.mark.asyncio
async def test_cs5_backfill_is_idempotent(_isolate_data_dir):
    from sqlalchemy import func, select, text
    from backend.app.db.models import ProjectChapter
    from backend.app.db.session import init_db, get_engine, get_session_factory

    await init_db()
    pid = await _mk_project(
        [{"idx": 0, "title": "一", "text": "x" * 3}], with_rows=False
    )
    engine = get_engine()

    async def _count() -> int:
        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text("SELECT COUNT(*) FROM project_chapters WHERE project_id = :p"),
                    {"p": pid},
                )
            ).scalar_one()

    assert await _count() == 0
    await init_db()          # 触发 _migrate_existing_sync 回填
    assert await _count() == 1
    await init_db()          # 幂等：已有行不再重复插
    assert await _count() == 1

    factory = get_session_factory()
    async with factory() as s:
        n = (await s.execute(
            select(func.count(ProjectChapter.id)).where(ProjectChapter.project_id == pid)
        )).scalar_one()
    assert n == 1


# =====================================================================
# T-CS7 F-8 估算
# =====================================================================
def test_cs7_prepare_llm_estimate_counts_and_warns(monkeypatch, caplog):
    from backend.app.services.chapter import Chapter
    from backend.app.services.project import _log_prepare_llm_estimate

    class _Cfg:
        LLM_CHAR_EXTRACT_SLICE_SIZE = 50000
        DIALOGUE_BATCH_CHAPTERS = 14
        VOICE_INSTRUCTION_BATCH_CHAPTERS = 6
        POLISH_ENABLED = False
        LLM_MAX_CONCURRENCY = 1

    # 100 章 × 2500 字 = 25 万字 → 5 片；对白 ceil(100/14)=8；指令 ceil(100/6)=17；+1 推荐
    chapters = [Chapter(idx=i, title=f"第{i}章", text="字" * 2500) for i in range(100)]
    with caplog.at_level("INFO"):
        n = _log_prepare_llm_estimate("pid12345", chapters, _Cfg)
    assert n == 5 + 8 + 17 + 1

    msgs = [r.getMessage() for r in caplog.records]
    assert any("LLM 调用量估算" in m for m in msgs), msgs
    # 100 章调用量 31 < 200 → 不刷 WARNING
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_cs7_warns_when_serial_and_large(caplog):
    from backend.app.services.chapter import Chapter
    from backend.app.services.project import _log_prepare_llm_estimate

    class _Cfg:
        LLM_CHAR_EXTRACT_SLICE_SIZE = 50000
        DIALOGUE_BATCH_CHAPTERS = 14
        VOICE_INSTRUCTION_BATCH_CHAPTERS = 6
        POLISH_ENABLED = False
        LLM_MAX_CONCURRENCY = 1

    chapters = [Chapter(idx=i, title=f"第{i}章", text="字" * 2500) for i in range(2000)]
    with caplog.at_level("WARNING"):
        _log_prepare_llm_estimate("pid12345", chapters, _Cfg)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "数千章 + 串行时必须给出 WARNING"
    assert "LLM_MAX_CONCURRENCY=1" in warnings[0]
    assert "限流配置" in warnings[0], "提示要指出去哪里调"


def test_cs7_polish_calls_counted_once_per_chapter(caplog):
    from backend.app.services.chapter import Chapter
    from backend.app.services.project import _log_prepare_llm_estimate

    class _Cfg:
        LLM_CHAR_EXTRACT_SLICE_SIZE = 50000
        DIALOGUE_BATCH_CHAPTERS = 14
        VOICE_INSTRUCTION_BATCH_CHAPTERS = 6
        POLISH_ENABLED = True
        LLM_MAX_CONCURRENCY = 2

    chapters = [Chapter(idx=i, title="t", text="字" * 100) for i in range(10)]
    with caplog.at_level("INFO"):
        n = _log_prepare_llm_estimate("pid12345", chapters, _Cfg)
    # 1 片 + 1 批 + 2 批 + 1 推荐 + 10 润色
    assert n == 1 + 1 + 2 + 1 + 10


# =====================================================================
# T-CS9 单章详情（回归：load_chapter 返回 Chapter 对象，不能再按 dict 取值）
# =====================================================================
@pytest.mark.asyncio
async def test_cs9_chapter_detail_returns_title_and_text(_isolate_data_dir):
    from backend.app.db.session import init_db
    from backend.app.services.project import get_project_chapter_detail

    await init_db()
    pid = await _mk_project([
        {"idx": 0, "title": "第一章 初遇", "text": "林若雪低着头。\n「怎么了？」"},
    ])
    detail = await get_project_chapter_detail(pid, 0)
    assert detail.idx == 0
    assert detail.title == "第一章 初遇"
    assert detail.text == "林若雪低着头。\n「怎么了？」"
    assert detail.dialogues == []


@pytest.mark.asyncio
async def test_cs9_chapter_detail_missing_chapter_raises(_isolate_data_dir):
    import pytest as _pytest

    from backend.app.db.session import init_db
    from backend.app.services.project import get_project_chapter_detail

    await init_db()
    pid = await _mk_project([{"idx": 0, "title": "一", "text": "x"}])
    with _pytest.raises(ValueError):
        await get_project_chapter_detail(pid, 42)


# =====================================================================
# T-CS8 删项目清章节行
# =====================================================================
@pytest.mark.asyncio
async def test_cs8_delete_project_removes_chapter_rows(_isolate_data_dir):
    from sqlalchemy import func, select
    from backend.app.db.models import ProjectChapter
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.project import delete_project

    await init_db()
    pid = await _mk_project([
        {"idx": 0, "title": "一", "text": "abc"},
        {"idx": 1, "title": "二", "text": "def"},
    ])
    factory = get_session_factory()
    async with factory() as s:
        before = (await s.execute(
            select(func.count(ProjectChapter.id)).where(ProjectChapter.project_id == pid)
        )).scalar_one()
    assert before == 2

    await delete_project(pid)

    async with factory() as s:
        after = (await s.execute(
            select(func.count(ProjectChapter.id)).where(ProjectChapter.project_id == pid)
        )).scalar_one()
    assert after == 0, "删项目必须清掉取不到的孤儿章节行（外键 PRAGMA 未开启）"
