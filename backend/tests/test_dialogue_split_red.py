"""H-24 对白批失败对半拆分 + 缺失章不补空占位 测试。

背景（用户 383 章实测 app.log，2026-10-09）：
  1. 对白批 200k+ 字符 prompt → 600s ReadTimeout × 3 / 系统性校验错
     （1188 个 `anchor: Field required`）→ 整批 14 章全废，单批浪费
     22~30 分钟（total_ms 1365340~1803911），最终 3/27 批失败。
  2. 批响应缺失某章 → 旧逻辑补 dialogues=[] 占位 → 该章被标 completed，
     空对白落库且永远不会被补跑（一次跑静默丢 59 章的对白）。

修复的对应测试：
  A. dialogue 层：LLM 输出缺失某章 → 返回不含该章（不补空）。
  B. prepare 端到端：大批重试耗尽 → 对半拆分 → 一半成功一半失败 →
     只拯救成功章节，失败半边用独立 key 记 failed checkpoint。
  C. prepare 端到端：拆分后仍全失败 → failed checkpoint 里不得残留
     拆分子 key（避免与父 key 重复计数）。
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

pytest_plugins = ("pytest_asyncio",)


def _book_text(n_chapters: int) -> str:
    from backend.tests.test_prepare_perf_red import _book_text as _bt

    return _bt(n_chapters, chars_per_ch=300)


async def _make_project(name: str, n_chapters: int = 5):
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file

    await init_db()
    resp = await create_project(name)
    await import_file(
        resp.project_id, _book_text(n_chapters).encode("utf-8"), f"{name}.txt"
    )
    return resp.project_id


async def _read_prog(project_id: str) -> dict:
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        proj = await sess.get(Project, project_id)
        return json.loads(proj.progress_json) if proj and proj.progress_json else {}


def _chapter_idxs_in_prompt(prompt: str) -> set[int]:
    """从 DialogueBatchResponse 的 prompt 里解析本次批包含的 chapter idx 集合。"""
    return {int(m) for m in re.findall(r"CHAPTER idx=(\d+) START", prompt)}


# ---------------------------------------------------------------------------
# A. dialogue 层单测：缺失章不补空占位
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h24_missing_chapter_not_placeholder(_isolate_data_dir, monkeypatch):
    """LLM 只返回 2/3 章 → 返回里不含缺失章（旧逻辑补空占位 = 静默丢对白）。"""
    from backend.app.services import dialogue as dlg_mod
    from backend.app.services.dialogue import attribute_dialogues_batch_with_llm
    from backend.app.services.character import Character

    class _FakeLLM:
        def __init__(self, payload):
            self._payload = payload

        async def chat_structured(self, prompt, output_schema, **kw):
            return output_schema.model_validate(self._payload)

    # LLM 漏掉了 chapter_idx=1
    payload = {"data": [
        {"chapter_idx": 0, "dialogues": []},
        {"chapter_idx": 2, "dialogues": []},
    ]}
    monkeypatch.setattr(dlg_mod, "get_llm", lambda: _FakeLLM(payload))

    chars = [Character(name="李明", gender="男", age="青年", personality="沉稳")]
    results = await attribute_dialogues_batch_with_llm(
        [(0, "第一章内容"), (1, "第二章内容"), (2, "第三章内容")], chars
    )

    got_idxs = {r.chapter_idx for r in results}
    assert got_idxs == {0, 2}, (
        f"缺失章 1 不应被补空占位（应缺席，由调用方保持 pending 补跑），实际 {sorted(got_idxs)}"
    )


# ---------------------------------------------------------------------------
# B/C. prepare 端到端：对半拆分
# ---------------------------------------------------------------------------

def _wrap_llm_with_split_behavior(monkeypatch, fail_idx_sets: set[frozenset]):
    """包装 MockLLM：对 DialogueBatchResponse，当本次批的 chapter idx 集合
    命中 fail_idx_sets 之一时抛错（模拟 600s 超时/系统性校验错），否则走原 mock。

    返回一个对话记录 list，供断言拆分调用链（14→7+7→…）确实发生。
    """
    from backend.app.ai import factory as ai_factory
    from backend.tests.mock_providers import MockLLMProvider

    mock_llm = ai_factory._llm_instance
    assert isinstance(mock_llm, MockLLMProvider)
    orig_chat = mock_llm.chat_structured
    seen_batches: list[set[int]] = []

    async def split_behavior(self, prompt, output_schema, **kw):
        if getattr(output_schema, "__name__", "") == "DialogueBatchResponse":
            idxs = frozenset(_chapter_idxs_in_prompt(prompt))
            seen_batches.append(set(idxs))
            if idxs in fail_idx_sets:
                raise RuntimeError(f"boom: 模拟批 {sorted(idxs)} 整批失败")
        # orig_chat 是实例绑定方法，self 已绑定，不得再传
        return await orig_chat(prompt, output_schema, **kw)

    monkeypatch.setattr(MockLLMProvider, "chat_structured", split_behavior)
    return seen_batches


@pytest.mark.asyncio
async def test_h24_split_salvages_half(_isolate_data_dir, monkeypatch, caplog):
    """大批 5 章重试耗尽 → 拆 2+3 → 后半成功、前半仍失败（len=2 不再拆）
    → 只拯救后半章节；前半保持 pending + 独立子 key 记 failed checkpoint。

    对应 app.log 场景：batch 5/27 全废 → 拆分后 7 章救回，损失从 14 章 → 5 章。
    """
    import logging

    from backend.app.core.config import settings
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import ProjectDialogue
    from sqlalchemy import select

    monkeypatch.setattr(settings, "DIALOGUE_BATCH_CHAPTERS", 5)
    monkeypatch.setattr(settings, "DIALOGUE_BATCH_RETRY_COUNT", 1)

    pid = await _make_project("H24-拆分拯救", n_chapters=5)
    # 全 5 章的大批失败；拆出的前半 {0,1} 也失败（不再拆）；后半 {2,3,4} 成功
    seen = _wrap_llm_with_split_behavior(
        monkeypatch, fail_idx_sets={frozenset({0, 1, 2, 3, 4}), frozenset({0, 1})}
    )

    from backend.app.services.project import prepare_project

    caplog.set_level(logging.INFO, logger="backend.app.services.project")
    await prepare_project(pid)

    prog = await _read_prog(pid)
    completed = set(prog.get("dialogue_completed_chapters", []))
    assert {2, 3, 4} <= completed, f"拆分后成功半边应被拯救，实际 completed={sorted(completed)}"
    assert 0 not in completed and 1 not in completed, "失败半边必须保持 pending（重跑补跑）"

    failed_batches: dict = prog.get("dialogue_failed_batches", {})
    assert "0L" in failed_batches, (
        f"失败半边应用独立子 key 记 failed checkpoint，实际 failed_batches={failed_batches}"
    )
    assert failed_batches["0L"]["chapters"] == [0, 1]
    # 父 key 0 不该在（部分成功走成功分支会 pop 父 key）
    assert "0" not in failed_batches, f"部分成功后父 key 应被 pop，实际 {sorted(failed_batches)}"

    # 拆分调用链确实发生：大批 {0..4} 先整批跑了（重试）再拆 {0,1}+{2,3,4}
    assert seen and seen[0] == {0, 1, 2, 3, 4}, "第一批调用应是全 5 章大批"
    assert {0, 1} in seen and {2, 3, 4} in seen, "重试耗尽后应对半拆分"

    # DB：只有成功半边的章有对白
    factory = get_session_factory()
    async with factory() as sess:
        dlgs = (await sess.execute(
            select(ProjectDialogue).where(ProjectDialogue.project_id == pid)
        )).scalars().all()
    dlg_chapters = {d.chapter_idx for d in dlgs}
    assert dlg_chapters == {2, 3, 4}, f"落库对白应只来自成功半边，实际 {sorted(dlg_chapters)}"
    assert "对半拆分兜底" in caplog.text
    assert "拆分后部分拯救" in caplog.text


@pytest.mark.asyncio
async def test_h24_split_all_failed_no_orphan_keys(_isolate_data_dir, monkeypatch):
    """拆分后仍全失败：failed checkpoint 只留父 key（整批一条记录），
    不得残留 "0L"/"0R"/"0LL"… 子 key（重复计数会虚报 failed_batch_count）。

    对应 app.log 场景：ReadTimeout 批拆两半仍超时 → 该批重跑补跑，
    checkpoint 干净地记一条整批失败。
    """
    from backend.app.core.config import settings
    from backend.app.services.project import prepare_project

    monkeypatch.setattr(settings, "DIALOGUE_BATCH_CHAPTERS", 5)
    monkeypatch.setattr(settings, "DIALOGUE_BATCH_RETRY_COUNT", 0)

    pid = await _make_project("H24-拆分全败", n_chapters=5)
    # 所有 DialogueBatchResponse 调用全部失败（大批 + 任意拆分层级）
    from backend.app.ai import factory as ai_factory
    from backend.tests.mock_providers import MockLLMProvider

    mock_llm = ai_factory._llm_instance
    orig_chat = mock_llm.chat_structured

    async def always_fail_dialogue(self, prompt, output_schema, **kw):
        if getattr(output_schema, "__name__", "") == "DialogueBatchResponse":
            raise RuntimeError("boom: 模拟 600s ReadTimeout")
        return await orig_chat(prompt, output_schema, **kw)

    monkeypatch.setattr(MockLLMProvider, "chat_structured", always_fail_dialogue)

    await prepare_project(pid)

    prog = await _read_prog(pid)
    failed_batches: dict = prog.get("dialogue_failed_batches", {})
    assert sorted(failed_batches.keys()) == ["0"], (
        f"全失败批应只记一条父 key，实际 {sorted(failed_batches.keys())} "
        f"（拆分子 key 未清理 → failed_batch_count 虚报）"
    )
    assert prog.get("dialogue_completed_chapters") == [], "全部章失败时不应有 completed 章"
