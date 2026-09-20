"""未归属对白的非旁白兜底音色 RED（2026-09-20）。

需求（用户原话）：
    「原文：…脚步声传入耳朵，有人来到…『哈哈，知道吗，那个叫林轩的废物，得到了
      两瓶废丹。』『这有什么好奇怪，一个没有灵根的凡人…』另一人毫不顾忌的嘲笑道…
      类似于这种原文，对白没有明确的角色归属，全文中对于这种没有归属的对白，
      自动分配和旁白不同的音色。」

问题根因：这类对白的 speaker 是「有人」「另一人」等**不在角色表里**的临时说话人，
`chapter.py` 用 `voice_assignments.get(speaker, narrator_voice_id)` 直接回退到
**旁白音色** → 听感上变成旁白在自言自语，完全没有「有人在说话」的层次。

处理：`build.py` 在算 config_digest 之前，为每个未知 speaker 稳定地兜一个**非旁白**
音色，写进本次 Build 的 voice_assignments 快照。

覆盖：
  US-1 候选池的口径：非空、全部中文可用、不含旁白音色、不含纯外语音色
  US-2 同一 speaker 稳定拿到同一音色，且永远不等于旁白音色
  US-3 同一章里不同的临时 speaker 拿到不同音色（不会全撞成同一个人）
  US-4 补全逻辑：未知 speaker 被补上非旁白音色；已有角色分配不被覆盖
  US-5 补全逻辑的两种「不动」：空映射不动；没有对白记录时映射原样返回
  US-6 端到端：start_build 落库的 voice_assignments_json 快照里带上了兜底音色
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_NARRATOR = "doubao:zh_male_liufei_uranus_bigtts"


# --------------------------------------------------------------------------
# US-1 ~ US-3：纯函数（候选池 / 稳定分配）
# --------------------------------------------------------------------------

def test_us1_candidate_pool_is_zh_capable_and_excludes_narrator():
    from backend.app.ai.providers.doubao.tts import _BUILTIN_VOICES
    from backend.app.services.build import _unknown_speaker_voice_candidates

    cands = _unknown_speaker_voice_candidates(_NARRATOR)

    assert cands, "候选池不能为空，否则未归属对白仍然只能退回旁白音色"
    assert _NARRATOR not in cands, "候选池必须排除旁白音色（否则等于没兜底）"
    assert len(set(cands)) == len(cands), "候选池不应有重复 id"

    by_id = {f"doubao:{v['id']}": v for v in _BUILTIN_VOICES}
    for vid in cands:
        v = by_id[vid]
        # 中文正文配上纯外语音色会命中上游「成功码但音频为空」（见 tts.py 的
        # _v3_empty_audio_hint），等于把对白变成静音。
        langs = [str(x).lower() for x in (v.get("languages") or [])]
        assert (not langs) or ("zh" in langs), f"{vid} 未声明中文能力，不应入池"
        assert "通用" in (v.get("scene") or []), f"{vid} 不是通用场景音色"


def test_us2_same_speaker_is_stable_and_never_narrator():
    from backend.app.services.build import _fallback_voice_for_unknown_speaker

    first = _fallback_voice_for_unknown_speaker("有人", _NARRATOR)
    assert first
    assert first != _NARRATOR
    # 稳定性：同一名字跨调用/跨 build 必须一致，否则同一段对白会换嗓子，
    # 段缓存全失效且听感漂移。
    for _ in range(5):
        assert _fallback_voice_for_unknown_speaker("有人", _NARRATOR) == first


def test_us3_distinct_speakers_get_distinct_voices():
    from backend.app.services.build import _fallback_voice_for_unknown_speaker

    speakers = ["有人", "另一人", "众人", "路人甲", "一个年轻女子的声音"]
    got = [_fallback_voice_for_unknown_speaker(s, _NARRATOR) for s in speakers]

    assert all(got), "每个临时说话人都应拿到音色"
    assert _NARRATOR not in got
    # 一屋里同时出现「有人」和「另一人」时不能是同一个人。
    assert len(set(got)) > 1, f"不同说话人撞成同一音色：{dict(zip(speakers, got))}"


# --------------------------------------------------------------------------
# US-4 ~ US-5：DB 补全逻辑
# --------------------------------------------------------------------------

async def _seed_dialogues(speakers: list[str]) -> str:
    from backend.app.db.models import Project, ProjectDialogue
    from backend.app.db.session import get_session_factory, init_db

    await init_db()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    async with factory() as s:
        s.add(Project(
            project_id=pid,
            name="未归属对白用例",
            status="prepared",
            owner_user_id=1,
            source_file_path="/nonexistent.txt",
            source_filename="t.txt",
            source_charset="utf-8",
            chapters_json="[]",
        ))
        for i, spk in enumerate(speakers):
            s.add(ProjectDialogue(
                project_id=pid,
                chapter_idx=0,
                segment_index=i,
                anchor_start=0,
                anchor_end=0,
                anchor_text="",
                speaker=spk,
                text="这是一句对白。",
                confidence=1.0,
            ))
        await s.commit()
    return pid


@pytest.mark.asyncio
async def test_us4_unknown_speakers_filled_without_overwriting_known():
    from backend.app.db.session import get_session_factory
    from backend.app.services.build import _with_unknown_speaker_voices

    pid = await _seed_dialogues(["有人", "另一人", "林轩"])
    known = {"林轩": "doubao:zh_male_taocheng_uranus_bigtts"}

    factory = get_session_factory()
    async with factory() as s:
        out = await _with_unknown_speaker_voices(s, pid, known, _NARRATOR)

    # 已有角色分配原样保留（不得被兜底覆盖）
    assert out["林轩"] == known["林轩"]
    # 未归属对白被补上，且都不是旁白音色
    for spk in ("有人", "另一人"):
        assert spk in out, f"{spk} 未被兜底"
        assert out[spk] != _NARRATOR, f"{spk} 仍拿到旁白音色"
    assert out["有人"] != out["另一人"], "两个临时说话人撞成同一音色"
    # 不修改传入的 dict（返回新 dict）
    assert known == {"林轩": "doubao:zh_male_taocheng_uranus_bigtts"}


@pytest.mark.asyncio
async def test_us5_noop_when_empty_or_no_dialogues():
    from backend.app.db.session import get_session_factory
    from backend.app.services.build import _with_unknown_speaker_voices

    factory = get_session_factory()

    # (a) 项目完全没有角色分配 → 不动（连主角都没音色时逐句兜底没意义，
    #     只会让全书对白变成同一个路人音色）
    pid = await _seed_dialogues(["有人"])
    async with factory() as s:
        assert await _with_unknown_speaker_voices(s, pid, {}, _NARRATOR) == {}

    # (b) 没有任何项目级对白记录 → 映射原样返回
    pid2 = await _seed_dialogues([])
    async with factory() as s:
        out = await _with_unknown_speaker_voices(
            s, pid2, {"林轩": "doubao:zh_male_taocheng_uranus_bigtts"}, _NARRATOR
        )
    assert out == {"林轩": "doubao:zh_male_taocheng_uranus_bigtts"}


# --------------------------------------------------------------------------
# US-6：端到端 —— 快照落库
# --------------------------------------------------------------------------

# 用户给的那段原文的浓缩版：「有人」「另一人」是原文里没有角色归属的临时说话人，
# 「苏小小」是普通角色（已有音色分配，不能被兜底覆盖）。
_CHAPTER_TEXT = (
    "他走进屋。"
    "「哈哈，知道吗，那个叫林轩的废物，得到了两瓶废丹。」"
    "「废物配废丹，岂不是正好？」另一人嘲笑道。"
    "「谁？」苏小小问道。"
)
_KNOWN_SPEAKER = "苏小小"
_KNOWN_VOICE = "doubao:zh_female_vv_uranus_bigtts"


async def _seed_prepared_project() -> str:
    """直接落一份「已 prepare」的项目（chapters_json + 对白记录），够 start_build 用。"""
    from backend.app.db.models import Project, ProjectDialogue
    from backend.app.db.session import get_session_factory, init_db

    await init_db()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    dialogues = [
        ("有人", "「哈哈，知道吗，那个叫林轩的废物，得到了两瓶废丹。」"),
        ("另一人", "「废物配废丹，岂不是正好？」"),
        (_KNOWN_SPEAKER, "「谁？」"),
    ]
    async with factory() as s:
        s.add(Project(
            project_id=pid,
            name="端到端兜底音色用例",
            status="prepared",
            owner_user_id=1,
            source_file_path="/nonexistent.txt",
            source_filename="t.txt",
            source_charset="utf-8",
            chapters_json=json.dumps(
                [{"idx": 0, "title": "第一章 废丹", "text": _CHAPTER_TEXT}],
                ensure_ascii=False,
            ),
        ))
        for i, (spk, anchor) in enumerate(dialogues):
            start = _CHAPTER_TEXT.index(anchor)
            s.add(ProjectDialogue(
                project_id=pid,
                chapter_idx=0,
                segment_index=i,
                anchor_start=start,
                anchor_end=start + len(anchor),
                anchor_text=anchor,
                speaker=spk,
                text=anchor.strip("「」"),
                confidence=1.0,
            ))
        await s.commit()
    return pid


@pytest.fixture
def _mock_providers():
    from backend.app.ai import factory as aifact
    from backend.app.services.build import _ACTIVE_BUILDS
    from backend.tests.mock_providers import MockLLMProvider, MockTTSProvider

    prev_tts, prev_llm = aifact._tts_instance, aifact._llm_instance
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    try:
        yield
    finally:
        aifact._tts_instance, aifact._llm_instance = prev_tts, prev_llm
        _ACTIVE_BUILDS.clear()


@pytest.mark.asyncio
async def test_us6_start_build_persists_fallback_voices(_mock_providers):
    from sqlalchemy import select as sa_select

    from backend.app.db.models import Build, ProjectDialogue  # noqa: F401
    from backend.app.db.session import get_session_factory
    from backend.app.services.build import cancel_build, start_build

    pid = await _seed_prepared_project()
    resp = await start_build(
        project_id=pid,
        voice_assignments={_KNOWN_SPEAKER: _KNOWN_VOICE},
        narrator_voice_id=_NARRATOR,
    )

    factory = get_session_factory()
    async with factory() as s:
        b = (await s.execute(
            sa_select(Build).where(Build.build_id == resp.build_id)
        )).scalar_one()
    snapshot = json.loads(b.voice_assignments_json or "{}")

    # 已有角色分配原样进快照（不被兜底覆盖）
    assert snapshot.get(_KNOWN_SPEAKER) == _KNOWN_VOICE
    # 未归属对白进快照，且都不是旁白音色
    for spk in ("有人", "另一人"):
        assert spk in snapshot, f"{spk} 未写进 build 快照，合成时仍会退回旁白音色"
        assert snapshot[spk] != _NARRATOR
    assert snapshot["有人"] != snapshot["另一人"]

    try:
        await cancel_build(resp.build_id)
    except Exception:
        pass

