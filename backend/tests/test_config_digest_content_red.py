"""A-7（整体优化批次 2）— config_digest 必须把「内容」纳入摘要。

历史缺陷：
    `_calc_config_digest` 只哈希 narrator / speed / voice_assignments / mode /
    tts_provider / 情感，**不含任何内容数据**。而 start_build 的第三层去重是
    「同 project + 同 digest + 历史成功 build → 直接复用」。

后果：用户润色正文、重新 prepare（对白被重识别）、增删发音规则后再次点「合成」，
只要音色/语速/情感没变，digest 就不变 → 直接返回历史成功 build，
**新内容永远不会被合成**，用户还以为已经重新生成过了（静默数据错误）。

修复：`_calc_content_digest` 把「章节正文 + 对白 + 发音规则」哈希后并入 digest。
"""
from __future__ import annotations

import pytest


def _chapters(text: str = "他们出发了。"):
    from backend.app.services.chapter import Chapter

    return [Chapter(idx=0, title="第一章 出发", text=text)]


# ---------------------------------------------------------------------
# 单元层：_calc_content_digest 对三类内容都敏感
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_content_digest_changes_with_chapter_text(_isolate_data_dir):
    """章节正文变化 → 内容摘要必须变化。"""
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services.build import _calc_content_digest
    from backend.app.services.project import create_project

    await init_db()
    pid = (await create_project("A-7 正文")).project_id
    factory = get_session_factory()

    async with factory() as s:
        d_before = await _calc_content_digest(s, pid, _chapters("他们出发了。"))
    async with factory() as s:
        d_after = await _calc_content_digest(s, pid, _chapters("他们终于出发了。"))

    assert d_before != d_after, "正文变化必须改变内容摘要，否则会复用旧产物"
    # 同内容必须稳定
    async with factory() as s:
        d_same = await _calc_content_digest(s, pid, _chapters("他们出发了。"))
    assert d_same == d_before


@pytest.mark.asyncio
async def test_content_digest_changes_with_dialogue(_isolate_data_dir):
    """对白归属变化 → 内容摘要必须变化。"""
    from backend.app.db.models import ProjectDialogue
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services.build import _calc_content_digest
    from backend.app.services.project import create_project

    await init_db()
    pid = (await create_project("A-7 对白")).project_id
    factory = get_session_factory()
    chapters = _chapters("「你好。」他说。")

    async with factory() as s:
        d_before = await _calc_content_digest(s, pid, chapters)

    async with factory() as s:
        s.add(ProjectDialogue(
            project_id=pid, chapter_idx=0, segment_index=0,
            anchor_start=1, anchor_end=5, anchor_text="你好",
            speaker="小明", text="「你好。」", confidence=1.0,
        ))
        await s.commit()

    async with factory() as s:
        d_after = await _calc_content_digest(s, pid, chapters)

    assert d_before != d_after, "对白变化必须改变内容摘要，否则会复用旧产物"


@pytest.mark.asyncio
async def test_content_digest_changes_with_pronunciation_rule(_isolate_data_dir):
    """发音规则变化 → 内容摘要必须变化。"""
    from backend.app.db.models import ProjectPronunciationRule
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services.build import _calc_content_digest
    from backend.app.services.project import create_project

    await init_db()
    pid = (await create_project("A-7 发音规则")).project_id
    factory = get_session_factory()
    chapters = _chapters("重明鸟")

    async with factory() as s:
        d_before = await _calc_content_digest(s, pid, chapters)

    async with factory() as s:
        s.add(ProjectPronunciationRule(
            project_id=pid, rule_type="alias", pattern="重明",
            replacement="chong2 ming2", priority=0, enabled=True,
        ))
        await s.commit()

    async with factory() as s:
        d_after = await _calc_content_digest(s, pid, chapters)

    assert d_before != d_after, "发音规则变化必须改变内容摘要，否则会复用旧产物"


# ---------------------------------------------------------------------
# 组合层：content_digest 必须真的影响最终 config_digest
# ---------------------------------------------------------------------
def test_config_digest_incorporates_content_digest():
    """相同音色配置 + 不同 content_digest → 最终 digest 必须不同。"""
    from backend.app.services.build import _calc_config_digest

    base = dict(mode="classic", tts_provider="doubao")
    d1 = _calc_config_digest(
        "doubao:BV001_streaming", 1.0, {"李明": "doubao:BV002_streaming"},
        content_digest="aaaa", **base,
    )
    d2 = _calc_config_digest(
        "doubao:BV001_streaming", 1.0, {"李明": "doubao:BV002_streaming"},
        content_digest="bbbb", **base,
    )
    assert d1 != d2, (
        "content_digest 必须参与最终 digest —— 否则改了正文/对白/发音规则仍会复用旧 build"
    )
    # 不传 content_digest 时保持与历史默认一致（空串）
    d3 = _calc_config_digest(
        "doubao:BV001_streaming", 1.0, {"李明": "doubao:BV002_streaming"}, **base,
    )
    d4 = _calc_config_digest(
        "doubao:BV001_streaming", 1.0, {"李明": "doubao:BV002_streaming"},
        content_digest="", **base,
    )
    assert d3 == d4
