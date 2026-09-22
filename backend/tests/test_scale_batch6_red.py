"""批次 6（规模化止血）回归测试：F-1 / F-2 / F-3。

覆盖：
  F-1  批量生成章节 LRC 只加载一次（不再逐章重解析全书 → 消除 O(N²)）
  F-2  project_dialogues 的 (project_id, chapter_idx) 复合索引存在，
       且老库（已有表）启动时会被补建
  F-3  角色识别按「完整章节」装桶：不切章、不超限、尽量装满、偏移与 full_text 对齐；
       旧口径 checkpoint 必须被判为不兼容（否则续跑会静默漏识）
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


# =====================================================================
# F-1 打包阶段 LRC：只加载一次
# =====================================================================
async def test_f1_bulk_lrc_loads_segments_once(monkeypatch):
    """逐章生成会退化成 O(N²)；批量入口必须只调用一次 _collect_build_segments。"""
    from app.services import subtitles

    calls: list[int | None] = []

    async def _fake_collect(build_id, *, ch_idx=None, require_final=True):
        calls.append(ch_idx)
        # 两章的 cue：章内相对时间
        return [
            {"chapter_idx": 0, "title": "第一章", "kind": "narrator",
             "speaker": "", "text": "林若雪低着头。", "start_ms": 0, "dur_ms": 1000},
            {"chapter_idx": 1, "title": "第二章", "kind": "dialogue",
             "speaker": "李明", "text": "怎么了？", "start_ms": 500, "dur_ms": 800},
        ]

    monkeypatch.setattr(subtitles, "_collect_build_segments", _fake_collect)

    out = await subtitles.generate_chapters_lrc("b1", require_final=False)

    assert calls == [None], "批量入口只能加载一次（ch_idx=None 全量）"
    assert set(out) == {0, 1}
    assert out[0] == "[00:00.00]林若雪低着头。"
    assert out[1] == "[00:00.50]李明：怎么了？"


async def test_f1_bulk_lrc_skips_chapters_without_content(monkeypatch):
    """没有 cue 的章节不出现在结果里（调用方按缺失处理 → ZIP 不写 .lrc）。"""
    from app.services import subtitles

    async def _fake_collect(build_id, *, ch_idx=None, require_final=True):
        return [
            {"chapter_idx": 3, "title": "第三章", "kind": "narrator",
             "speaker": "", "text": "正文。", "start_ms": 0, "dur_ms": 100},
        ]

    monkeypatch.setattr(subtitles, "_collect_build_segments", _fake_collect)
    out = await subtitles.generate_chapters_lrc("b1")
    assert set(out) == {3}


async def test_f1_single_chapter_lrc_still_works(monkeypatch):
    """单章接口（对外下载用）行为不变。"""
    from app.services import subtitles

    async def _fake_collect(build_id, *, ch_idx=None, require_final=True):
        assert ch_idx == 7, "单章接口必须把 ch_idx 透传下去（据此按章过滤 SQL）"
        return [
            {"chapter_idx": 7, "title": "第八章", "kind": "narrator",
             "speaker": "", "text": "内容。", "start_ms": 0, "dur_ms": 100},
        ]

    monkeypatch.setattr(subtitles, "_collect_build_segments", _fake_collect)
    fname, content = await subtitles.generate_chapter_lrc("b1", 7, title="第八章")
    assert fname == "第八章.lrc"
    assert content == "[00:00.00]内容。"


def test_f1_single_chapter_query_filters_by_chapter():
    """_collect_build_segments 在给定 ch_idx 时必须带 chapter_idx 过滤条件。

    否则「看一章歌词」仍要拉全项目对白（数千章下是十万行级扫描）。
    """
    import inspect

    from app.services import subtitles

    src = inspect.getsource(subtitles._collect_build_segments)
    assert "ProjectDialogue.chapter_idx == ch_idx" in src


# =====================================================================
# F-2 复合索引
# =====================================================================
def test_f2_model_declares_composite_index():
    from app.db.models import ProjectDialogue

    indexes = {
        (ix.name, tuple(c.name for c in ix.columns))
        for ix in ProjectDialogue.__table__.indexes
    }
    assert (
        "ix_project_dialogues_project_chapter",
        ("project_id", "chapter_idx"),
    ) in indexes


def test_f2_migration_ddl_targets_the_composite_index():
    from app.db import session as sess

    names = {name for name, _table, _ddl in sess._NEW_INDEXES}
    assert "ix_project_dialogues_project_chapter" in names
    for _name, table, ddl in sess._NEW_INDEXES:
        assert table == "project_dialogues"
        assert "IF NOT EXISTS" in ddl, "补建索引必须幂等"


async def test_f2_index_is_created_for_legacy_db(tmp_path, monkeypatch):
    """老库（表已存在）跑 init_db 必须补建索引 —— create_all 不会补已存在表的索引。"""
    from app.db import session as sess

    await sess.init_db()
    engine = sess.get_engine()

    async def _index_names() -> set[str]:
        async with engine.begin() as conn:
            rows = await conn.execute(text("PRAGMA index_list('project_dialogues')"))
            return {r[1] for r in rows.fetchall()}

    assert "ix_project_dialogues_project_chapter" in await _index_names()

    # 模拟「老库」：索引不存在
    async with engine.begin() as conn:
        await conn.execute(
            text("DROP INDEX IF EXISTS ix_project_dialogues_project_chapter")
        )
    assert "ix_project_dialogues_project_chapter" not in await _index_names()

    # 再次启动 → 迁移逻辑必须补建
    await sess.init_db()
    assert "ix_project_dialogues_project_chapter" in await _index_names()


# =====================================================================
# F-3 按完整章节装桶
# =====================================================================
def _chapters(*texts: str):
    from app.services.chapter import Chapter

    return [Chapter(idx=i, title=f"第{i+1}章", text=t) for i, t in enumerate(texts)]


def test_f3_buckets_never_split_a_chapter():
    """桶内必须是完整章节：拼回来等于原文，且每章只出现在一个桶里。"""
    chapters = _chapters("甲" * 100, "乙" * 100, "丙" * 100, "丁" * 100)
    from app.services.project import _bucket_chapters_by_chars

    buckets = _bucket_chapters_by_chars(chapters, 250)

    seen: list[int] = []
    for b in buckets:
        for idx in b.chapter_idxs:
            assert idx not in seen, "同一章不能落在多个桶里"
            seen.append(idx)
        # 桶文本 = 这些章原文（按序）以 \n 连接 —— 没有任何截断
        assert b.text == "\n".join(chapters[i].text for i in b.chapter_idxs)
    assert sorted(seen) == [0, 1, 2, 3], "所有章节都必须被覆盖且不重不漏"


def test_f3_buckets_respect_limit_and_pack_greedily():
    chapters = _chapters("甲" * 100, "乙" * 100, "丙" * 100)
    from app.services.project import _bucket_chapters_by_chars

    # 100+1+100=201 ≤ 250，再加丙(1+100) 会到 302 > 250 → 前两章一桶
    buckets = _bucket_chapters_by_chars(chapters, 250)
    assert [b.chapter_idxs for b in buckets] == [[0, 1], [2]]
    for b in buckets:
        assert len(b.text) <= 250


def test_f3_oversized_single_chapter_gets_own_bucket_unsplit():
    """单章本身超限时独占一桶，且**不被切开**。"""
    big = "长" * 900
    chapters = _chapters("小" * 10, big, "小" * 10)
    from app.services.project import _bucket_chapters_by_chars

    buckets = _bucket_chapters_by_chars(chapters, 100)
    assert [b.chapter_idxs for b in buckets] == [[0], [1], [2]]
    assert buckets[1].text == big, "超限章必须原样保留，不得截断"


def test_f3_offsets_align_with_full_text_join():
    """start/end 必须与 full_text = '\\n'.join(章节正文) 的区间一致（进度显示依赖它）。"""
    chapters = _chapters("甲" * 60, "乙" * 60, "丙" * 60)
    from app.services.project import _bucket_chapters_by_chars

    full_text = "\n".join(c.text for c in chapters)
    for b in _bucket_chapters_by_chars(chapters, 130):
        assert full_text[b.start : b.end] == b.text


def test_f3_empty_chapter_list_yields_no_bucket():
    from app.services.project import _bucket_chapters_by_chars

    assert _bucket_chapters_by_chars([], 50000) == []


# ---- checkpoint 兼容性（防「静默漏识」）----
def test_f3_legacy_checkpoint_is_incompatible():
    from app.services.project import _char_checkpoint_incompatible

    # 旧口径：有片号但没有 mode 标记
    assert _char_checkpoint_incompatible({"char_slice_completed": [0, 1]}) is True
    # 旧口径：已累积原始结果
    assert _char_checkpoint_incompatible({"char_extract_raw_list": [{"name": "甲"}]}) is True
    # 明确标着别的口径
    assert _char_checkpoint_incompatible(
        {"char_slice_mode": "offset", "char_slice_completed": [0]}
    ) is True


def test_f3_same_mode_and_fresh_project_are_compatible():
    from app.services.project import _char_checkpoint_incompatible

    # 全新项目：什么都没有 → 不需要重置
    assert _char_checkpoint_incompatible({}) is False
    # 新口径的 checkpoint → 正常复用
    assert _char_checkpoint_incompatible(
        {"char_slice_mode": "chapter", "char_slice_completed": [0, 1]}
    ) is False
    # 新口径但还没跑到角色识别（无 checkpoint）→ 不重置
    assert _char_checkpoint_incompatible({"char_slice_mode": "chapter"}) is False
