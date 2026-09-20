"""逐段语音指令（豆包 2.0 context_texts）生成 + 装配 + 摘要回归测试。

覆盖：
  VI-1  generate_dialogue_instructions：mock LLM 返回若干条 → 按 (chapter_idx, segment_index) 正确回填
  VI-2  防御式：LLM 漏返回 / 返回不存在的 segment → 缺失补空串、不抛错
  VI-3  整批调用失败 → 全部按空串、不抛错（不拖垮 prepare）
  VI-4  指令 clamp 到 512 字符
  VI-5  VOICE_INSTRUCTION_ENABLED=False → 不调用 LLM
  VI-6  _build_segments_for_chapter：对白自身 instruction 覆盖角色级
  VI-7  _build_segments_for_chapter：对白 instruction 为空 → 回落角色级
  VI-8  _build_segments_for_chapter：一句对白切成多个子段 → 子段共享同一条指令；跨章节各自生效
  VI-9  _calc_content_digest：只改逐段 instruction 会改变 digest

说明：conftest.py 已注入 MockLLMProvider；本文件用自建 _FakeLLM 精确控制 LLM 输出，
通过 monkeypatch 替换 service 模块内的 get_llm 绑定。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeLLM:
    """记录调用次数/prompt；按预设 items 返回 batch 响应，或按配置抛异常。"""

    def __init__(self, items: list[dict] | None = None, raise_exc: Exception | None = None):
        self.items = items if items is not None else []
        self.raise_exc = raise_exc
        self.calls: list[str] = []

    async def chat_structured(self, *, prompt, output_schema, **_kw):
        self.calls.append(prompt)
        if self.raise_exc is not None:
            raise self.raise_exc
        return output_schema.model_validate({"data": self.items})


@pytest.fixture
def vi_env(monkeypatch):
    """替换 voice_instruction 模块内的 get_llm，返回可配置的 _FakeLLM。"""
    from backend.app.services import voice_instruction as vi

    state: dict = {"llm": _FakeLLM()}

    def _set_llm(llm: _FakeLLM) -> _FakeLLM:
        state["llm"] = llm
        monkeypatch.setattr(vi, "get_llm", lambda *a, **k: llm)
        return llm

    monkeypatch.setattr(vi, "get_llm", lambda *a, **k: state["llm"])
    state["set_llm"] = _set_llm
    return state


def _chapters(*texts: str):
    from backend.app.services.chapter import Chapter

    return [Chapter(idx=i, title=f"第{i+1}章", text=t) for i, t in enumerate(texts)]


def _dlg_dict(speaker: str, text: str, segment_index: int | None = None) -> dict:
    """模拟对白归属阶段的原始 attrs：可能带/不带 segment_index。"""
    d = {"speaker": speaker, "text": text}
    if segment_index is not None:
        d["segment_index"] = segment_index
    return d


# ---------------------------------------------------------------------
# VI-1：正确回填
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vi1_maps_instruction_by_chapter_segment(vi_env):
    from backend.app.services.character import Character
    from backend.app.services.voice_instruction import generate_dialogue_instructions

    vi_env["set_llm"](_FakeLLM(items=[
        {"chapter_idx": 0, "segment_index": 0, "instruction": "用颤抖沙哑、带着绝望的哭腔说"},
        {"chapter_idx": 0, "segment_index": 1, "instruction": ""},
        {"chapter_idx": 1, "segment_index": 0, "instruction": "轻声细语、带着试探的语气说"},
    ]))
    chapters = _chapters("「全都没了……」", "「你回来了。」")
    dialogues = {
        0: [_dlg_dict("林若雪", "全都没了……"), _dlg_dict("李明", "嗯，我知道了。")],
        1: [_dlg_dict("李明", "你回来了。")],
    }
    chars = [Character(name="林若雪", gender="女", age="少女", personality="内向")]

    result = await generate_dialogue_instructions(chapters, dialogues, chars)

    assert result[(0, 0)] == "用颤抖沙哑、带着绝望的哭腔说"
    assert result[(0, 1)] == ""
    assert result[(1, 0)] == "轻声细语、带着试探的语气说"
    assert len(vi_env["llm"].calls) == 1  # 2 章 < BATCH_CHAPTERS=6 → 1 批


# ---------------------------------------------------------------------
# VI-2：防御式 —— 漏返回 / 越界键
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vi2_missing_and_unknown_keys_are_safe(vi_env):
    from backend.app.services.character import Character
    from backend.app.services.voice_instruction import generate_dialogue_instructions

    vi_env["set_llm"](_FakeLLM(items=[
        {"chapter_idx": 0, "segment_index": 0, "instruction": "压低嗓音、缓慢地说"},
        # 越界键：chapter/segment 都不存在 → 必须被丢弃
        {"chapter_idx": 99, "segment_index": 88, "instruction": "幻觉指令"},
    ]))
    chapters = _chapters("「一句话。」", "「两句话。」")
    dialogues = {
        0: [_dlg_dict("李明", "一句话。"), _dlg_dict("林若雪", "第二句。")],
        1: [_dlg_dict("王大爷", "第三句。")],
    }
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]

    # 不应抛错
    result = await generate_dialogue_instructions(chapters, dialogues, chars)

    # 漏返回的两段补空串
    assert result[(0, 1)] == ""
    assert result[(1, 0)] == ""
    assert result[(0, 0)] == "压低嗓音、缓慢地说"
    # 越界键不进入结果
    assert (99, 88) not in result
    # 结果只含期望键
    assert set(result.keys()) == {(0, 0), (0, 1), (1, 0)}


# ---------------------------------------------------------------------
# VI-3：整批失败不致命
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vi3_batch_failure_is_not_fatal(vi_env):
    from backend.app.services.character import Character
    from backend.app.services.voice_instruction import generate_dialogue_instructions

    vi_env["set_llm"](_FakeLLM(raise_exc=RuntimeError("LLM 挂了")))
    chapters = _chapters("「你好。」")
    dialogues = {0: [_dlg_dict("李明", "你好。")]}
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]

    result = await generate_dialogue_instructions(chapters, dialogues, chars)

    assert result[(0, 0)] == "", "整批失败时该批对白必须按空串处理"
    assert len(vi_env["llm"].calls) > 0, "应确实尝试过调用（带批级重试）"


# ---------------------------------------------------------------------
# VI-4：clamp 到 512 字符
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vi4_instruction_clamped_to_512(vi_env):
    from backend.app.services.character import Character
    from backend.app.services.voice_instruction import INSTRUCTION_MAX_LEN, generate_dialogue_instructions

    long_instr = "用" + "颤抖" * 400  # 远超 512
    vi_env["set_llm"](_FakeLLM(items=[
        {"chapter_idx": 0, "segment_index": 0, "instruction": long_instr},
    ]))
    chapters = _chapters("「啊。」")
    dialogues = {0: [_dlg_dict("李明", "啊。")]}
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]

    result = await generate_dialogue_instructions(chapters, dialogues, chars)

    assert len(result[(0, 0)]) == INSTRUCTION_MAX_LEN == 512


# ---------------------------------------------------------------------
# VI-5：总开关关闭 → 不调 LLM
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vi5_disabled_skips_llm(vi_env, monkeypatch):
    from backend.app.services import voice_instruction as vi
    from backend.app.services.character import Character

    # 注意：直接 patch voice_instruction 模块自身绑定的 settings 对象，
    # 而不是 backend.app.core.config.settings —— 全量测试里
    # test_path_env_override_red 会 importlib.reload(config)，导致 cfgmod.settings
    # 被换成新对象，而本模块在更早被 import 时已绑定旧对象。patch 模块自身的引用才稳。
    monkeypatch.setattr(vi.settings, "VOICE_INSTRUCTION_ENABLED", False)
    vi_env["set_llm"](_FakeLLM(items=[
        {"chapter_idx": 0, "segment_index": 0, "instruction": "不该出现"},
    ]))
    chapters = _chapters("「你好。」")
    dialogues = {0: [_dlg_dict("李明", "你好。")]}
    chars = [Character(name="李明", gender="男", age="青年", personality="开朗")]

    result = await vi.generate_dialogue_instructions(chapters, dialogues, chars)

    assert len(vi_env["llm"].calls) == 0, "VOICE_INSTRUCTION_ENABLED=False 时不得调用 LLM"
    assert result == {}


# ---------------------------------------------------------------------
# VI-6 / VI-7 / VI-8：分段装配
# ---------------------------------------------------------------------
def _dialogue_row(*, chapter_idx: int, segment_index: int, speaker: str,
                   text: str, instruction: str, anchor_text: str | None = None):
    from backend.app.db.models import ProjectDialogue

    return ProjectDialogue(
        chapter_idx=chapter_idx,
        segment_index=segment_index,
        anchor_start=0,
        anchor_end=len(anchor_text if anchor_text is not None else text),
        anchor_text=anchor_text if anchor_text is not None else text,
        speaker=speaker,
        text=text,
        confidence=1.0,
        instruction=instruction,
    )


def _dialogue_segments(segs):
    return [s for s in segs if s.kind == "dialogue"]


def test_vi6_per_dialogue_instruction_overrides_role_level():
    from backend.app.services.chapter import _build_segments_for_chapter

    ch = _chapters("「你骗我！」")[0]
    dlg = _dialogue_row(
        chapter_idx=0, segment_index=0, speaker="林若雪",
        text="你骗我！", instruction="用尖锐质问、几近失控的语气说", anchor_text="「你骗我！」",
    )
    segs, _ = _build_segments_for_chapter(
        ch, [dlg], narrator_voice_id="narr",
        voice_assignments={"林若雪": "v1"}, segment_overrides=None, start_idx=0,
        speaker_styles={"林若雪": {"emotion": "angry", "instruction": "角色级：平淡地说"}},
    )
    dlgs = _dialogue_segments(segs)
    assert dlgs, "应至少有一个对白段"
    assert all(s.instruction == "用尖锐质问、几近失控的语气说" for s in dlgs)
    # emotion 仍走角色级（本轮不引入逐段 emotion）
    assert all(s.emotion == "angry" for s in dlgs)


def test_vi7_empty_per_dialogue_falls_back_to_role_level():
    from backend.app.services.chapter import _build_segments_for_chapter

    ch = _chapters("「嗯。」")[0]
    dlg = _dialogue_row(
        chapter_idx=0, segment_index=0, speaker="李明",
        text="嗯。", instruction="", anchor_text="「嗯。」",
    )
    segs, _ = _build_segments_for_chapter(
        ch, [dlg], narrator_voice_id="narr",
        voice_assignments={"李明": "v1"}, segment_overrides=None, start_idx=0,
        speaker_styles={"李明": {"emotion": "calm", "instruction": "角色级：温和地说"}},
    )
    dlgs = _dialogue_segments(segs)
    assert dlgs
    assert all(s.instruction == "角色级：温和地说" for s in dlgs)


def test_vi8_subsegments_share_instruction_across_chapters():
    from backend.app.services.chapter import _build_segments_for_chapter

    # 构造超长对白（> TTS_MAX_SEGMENT_CHARS=600），确保被 _split_long_text 切成多个子段
    long_text = "这是一句用来触发切分的长对白。" * 80
    ch0 = _chapters(long_text)[0]
    dlg0 = _dialogue_row(
        chapter_idx=0, segment_index=0, speaker="林若雪",
        text=long_text, instruction="逐段指令A", anchor_text=long_text,
    )
    segs0, _ = _build_segments_for_chapter(
        ch0, [dlg0], narrator_voice_id="narr",
        voice_assignments={"林若雪": "v1"}, segment_overrides=None, start_idx=0,
        speaker_styles={"林若雪": {"emotion": "sad", "instruction": "角色级指令"}},
    )
    dlgs0 = _dialogue_segments(segs0)
    assert len(dlgs0) >= 2, "超长对白应被切成多个子段"
    assert all(s.instruction == "逐段指令A" for s in dlgs0), "子段必须共享同一条指令"

    # 跨章节：另一章的同一角色用各自对白的指令，互不串味
    ch1 = _chapters("「第二句。」")[0]
    dlg1 = _dialogue_row(
        chapter_idx=1, segment_index=0, speaker="林若雪",
        text="第二句。", instruction="逐段指令B", anchor_text="「第二句。」",
    )
    segs1, _ = _build_segments_for_chapter(
        ch1, [dlg1], narrator_voice_id="narr",
        voice_assignments={"林若雪": "v1"}, segment_overrides=None, start_idx=0,
        speaker_styles={"林若雪": {"emotion": "sad", "instruction": "角色级指令"}},
    )
    dlgs1 = _dialogue_segments(segs1)
    assert dlgs1 and all(s.instruction == "逐段指令B" for s in dlgs1)


# ---------------------------------------------------------------------
# VI-9：content digest 对逐段 instruction 敏感
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vi9_content_digest_changes_with_dialogue_instruction(_isolate_data_dir):
    from backend.app.db.models import ProjectDialogue
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services.build import _calc_content_digest
    from backend.app.services.project import create_project
    from sqlalchemy import select

    await init_db()
    pid = (await create_project("VI-9 逐段指令")).project_id
    factory = get_session_factory()
    chapters = _chapters("「你好。」")

    async with factory() as s:
        s.add(ProjectDialogue(
            project_id=pid, chapter_idx=0, segment_index=0,
            anchor_start=1, anchor_end=5, anchor_text="你好",
            speaker="小明", text="你好", confidence=1.0, instruction="",
        ))
        await s.commit()

    async with factory() as s:
        d_before = await _calc_content_digest(s, pid, chapters)

    # 只改逐段 instruction（其余字段不变）
    async with factory() as s:
        # 过滤 project_id：全量测试下 DB 可能因隔离缺陷残留其他项目的数据，
        # 不过滤会改到别的项目那行，导致本断言误判
        row = (await s.execute(
            select(ProjectDialogue).where(ProjectDialogue.project_id == pid)
        )).scalars().first()
        row.instruction = "用颤抖沙哑的哭腔说"
        await s.commit()

    async with factory() as s:
        d_after = await _calc_content_digest(s, pid, chapters)

    assert d_before != d_after, (
        "只改逐段 instruction 也必须改变内容摘要 —— 否则改了指令会命中历史 build 被直接复用"
    )
