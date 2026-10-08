"""H-12：对白归属 anchor.text 本地回填（用户 app.log 实测问题）。

app.log 现象（2026-10-08）：
    [LLM] FAIL schema=DialogueBatchResponse attempt=1/3 status=200 this_ms=344870
    ValidationError: 142 validation errors for DialogueBatchResponse
    data.0.dialogues.0.anchor.text Field required [input_value={'start': 141, 'end': 158}]
    ...（× 142 条，每条 3 行日志）

MiniMax-M3 会省略 anchor.text 只给 {start, end}（模型输出风格变化，重试也一样失败），
导致 344s/次的大批调用整批作废 × 3 次重试全废 ≈ 17 分钟纯浪费，且日志刷屏。

修复：Anchor.text / DialogueAttribution.text 可选化 + _backfill_anchor_text
用 chapter_text[start:end] 本地回填（位置合法时），越界条目丢弃。
"""
from __future__ import annotations

import pytest

pytest_plugins = ("pytest_asyncio",)


def _mk_chapter_text() -> str:
    """一章带 3 段对白的文本（与 _mk_attrs 的偏移严格对应）。"""
    return (
        "林若雪低着头，手里捏着衣角。"
        "李明走过来拍她肩膀：「你怎么了？」"
        "「没……没什么。」她小声说。"
        "「还说没什么，眼睛都红了。」王大爷从远处走来。"
    )


def _find_quote_pos(text: str, quote: str) -> tuple[int, int]:
    s = text.index(quote)
    return s, s + len(quote)


_CH_TEXT = _mk_chapter_text()
_Q1 = "「你怎么了？」"
_Q2 = "「没……没什么。」"
_Q3 = "「还说没什么，眼睛都红了。」"


class _FakeLLM:
    """直接返回预构造的 DialogueBatchResponse（绕过 provider 校验/重试）。"""

    def __init__(self, payload):
        self._payload = payload

    async def chat_structured(self, prompt, output_schema, **kw):
        return output_schema.model_validate(self._payload)


@pytest.mark.asyncio
async def test_h12_backfills_missing_anchor_text(_isolate_data_dir, monkeypatch):
    """核心场景（app.log 复现）：anchor 只有 {start, end} → 本地回填成功、不炸。"""
    from backend.app.services import dialogue as dlg_mod
    from backend.app.services.dialogue import attribute_dialogues_batch_with_llm
    from backend.app.services.character import Character

    s1, e1 = _find_quote_pos(_CH_TEXT, _Q1)
    s2, e2 = _find_quote_pos(_CH_TEXT, _Q2)
    # 模拟 M3：anchor 缺 text、对白 text 有给（与 app.log 一致）
    payload = {
        "data": [
            {
                "chapter_idx": 0,
                "dialogues": [
                    {"anchor": {"start": s1, "end": e1}, "speaker": "李明", "confidence": 0.9, "text": "你怎么了？"},
                    {"anchor": {"start": s2, "end": e2}, "speaker": "林若雪", "confidence": 0.8, "text": "没……没什么。"},
                ],
            }
        ]
    }
    monkeypatch.setattr(dlg_mod, "get_llm", lambda: _FakeLLM(payload))

    chars = [Character(name="李明", gender="男", age="青年", personality="沉稳"),
             Character(name="林若雪", gender="女", age="青年", personality="文静")]
    results = await attribute_dialogues_batch_with_llm([(0, _CH_TEXT)], chars)

    assert len(results) == 1
    dlgs = results[0].dialogues
    assert len(dlgs) == 2, "缺 anchor.text 的条目不应被丢弃（应回填）"
    # 回填正确性：anchor.text == chapter_text[start:end]
    assert dlgs[0].anchor.text == _Q1
    assert dlgs[1].anchor.text == _Q2


@pytest.mark.asyncio
async def test_h12_drops_out_of_range_and_backfills_text_too(_isolate_data_dir, monkeypatch):
    """越界条目丢弃 + 对白 text 也缺失时用 anchor 切片去引号兜底。"""
    from backend.app.services import dialogue as dlg_mod
    from backend.app.services.dialogue import attribute_dialogues_batch_with_llm
    from backend.app.services.character import Character

    s3, e3 = _find_quote_pos(_CH_TEXT, _Q3)
    payload = {
        "data": [
            {
                "chapter_idx": 5,
                "dialogues": [
                    # 合法但 anchor.text 与对白 text 都缺 → 双回填
                    {"anchor": {"start": s3, "end": e3}, "speaker": "王大爷", "confidence": 0.95},
                    # 越界：end 超过章长 → 丢弃
                    {"anchor": {"start": 0, "end": 99999}, "speaker": "李明", "confidence": 0.9, "text": "x"},
                    # 越界：end <= start → 丢弃
                    {"anchor": {"start": 10, "end": 10}, "speaker": "李明", "confidence": 0.9, "text": "x"},
                    # 负数 → 丢弃
                    {"anchor": {"start": -5, "end": 8}, "speaker": "李明", "confidence": 0.9, "text": "x"},
                ],
            }
        ]
    }
    monkeypatch.setattr(dlg_mod, "get_llm", lambda: _FakeLLM(payload))

    chars = [Character(name="王大爷", gender="男", age="老年", personality="慈祥"),
             Character(name="李明", gender="男", age="青年", personality="沉稳")]
    results = await attribute_dialogues_batch_with_llm([(5, _CH_TEXT)], chars)

    dlgs = results[0].dialogues
    assert len(dlgs) == 1, "3 条越界应被丢弃，1 条合法应回填保留"
    assert dlgs[0].anchor.text == _Q3
    assert dlgs[0].text == "还说没什么，眼睛都红了。", "对白 text 缺失时应从 anchor 切片去引号兜底"


@pytest.mark.asyncio
async def test_h12_single_chapter_api_backfills(_isolate_data_dir, monkeypatch):
    """单章接口 attribute_dialogues_with_llm 同样回填。"""
    from backend.app.services import dialogue as dlg_mod
    from backend.app.services.dialogue import attribute_dialogues_with_llm
    from backend.app.services.character import Character

    s1, e1 = _find_quote_pos(_CH_TEXT, _Q1)
    # 单章版 schema 是内联 _Wrapper {data: [...]}
    payload = {"data": [
        {"anchor": {"start": s1, "end": e1}, "speaker": "李明", "confidence": 0.9, "text": "你怎么了？"}
    ]}
    monkeypatch.setattr(dlg_mod, "get_llm", lambda: _FakeLLM(payload))

    chars = [Character(name="李明", gender="男", age="青年", personality="沉稳")]
    out = await attribute_dialogues_with_llm(_CH_TEXT, chars)
    assert len(out) == 1
    assert out[0].anchor.text == _Q1


@pytest.mark.asyncio
async def test_h12_end_to_end_db_anchor_text_nonempty(_isolate_data_dir, monkeypatch):
    """端到端：Mock LLM（省略 anchor.text 模式）跑 prepare → 落库 anchor_text 非空。

    覆盖 project.py 落库路径（_f duck-typing 读 anchor.text=None 的容错）
    —— 此前若 schema 挂掉根本到不了这一步；现在必须能走到落库且值正确。
    """
    import json as _json

    from backend.app.core.config import settings
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Project, ProjectDialogue
    from sqlalchemy import select

    import re as _re

    # 用最小书：2 章，每章 1 段对白（Mock DialogueBatchResponse 按 prompt 解析引号）
    book = (
        "第1章 测试\n\n"
        "「你怎么了？」李明拍了拍她的肩膀。\n\n"
        "第2章 测试\n\n"
        "「没什么。」林若雪小声说。\n"
    )

    await init_db()
    monkeypatch.setattr(settings, "LLM_MAX_CONCURRENCY", 2)

    from backend.app.services.project import create_project, import_file, prepare_project

    resp = await create_project("H12-端到端")
    pid = resp.project_id
    await import_file(pid, book.encode("utf-8"), "h12e2e.txt")

    # 让 Mock 的 DialogueBatchResponse 省略 anchor.text（复现 M3 行为）
    from backend.tests.mock_providers import MockLLMProvider

    orig = MockLLMProvider.chat_structured

    async def strip_anchor_text(self, prompt, output_schema, **kw):
        out = await orig(self, prompt, output_schema, **kw)
        if output_schema.__name__ == "DialogueBatchResponse":
            for chd in out.data:
                for d in chd.dialogues:
                    d.anchor = type(d.anchor)(text=None, start=d.anchor.start, end=d.anchor.end)
        return out

    from backend.app.ai import factory as ai_factory
    mock_llm = ai_factory._llm_instance
    assert isinstance(mock_llm, MockLLMProvider)
    monkeypatch.setattr(MockLLMProvider, "chat_structured", strip_anchor_text)

    await prepare_project(pid)

    factory = get_session_factory()
    async with factory() as sess:
        dlgs = (await sess.execute(
            select(ProjectDialogue).where(ProjectDialogue.project_id == pid)
        )).scalars().all()
        assert len(dlgs) >= 2, f"对白应正常落库（H-12 回填后不再整批失败），实际 {len(dlgs)} 条"
        for d in dlgs:
            assert d.anchor_text, f"落库 anchor_text 不应为空：chapter={d.chapter_idx} seg={d.segment_index}"
            # 回填的 anchor_text 必须能在章节文本内切片对齐
            ch_text_match = _re.search(_re.escape(d.anchor_text), book)
            assert ch_text_match, f"anchor_text 不在原书文本中：{d.anchor_text!r}"
