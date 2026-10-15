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



@pytest.mark.asyncio
async def test_default_voices_normalize_gender_and_preserve_assignments():
    from backend.app.db.models import ProjectCharacter
    from backend.app.db.session import get_session_factory
    from backend.app.services.build import _with_unknown_speaker_voices

    pid = await _seed_dialogues(["女子", "男子", "未知", "有人", ""])
    async with get_session_factory()() as session:
        session.add_all([
            ProjectCharacter(project_id=pid, name=name, gender=gender)
            for name, gender in [("女子", "女"), ("男子", "男"), ("未知", " FEMALE "), ("简称", " F "), ("已选", "女")]
        ])
        await session.commit()
        assigned = {"已选": "custom"}
        voices = await _with_unknown_speaker_voices(
            session, pid, assigned, _NARRATOR,
            default_male_voice_id="minimax:male", default_female_voice_id="minimax:female",
        )
    assert voices == {"女子": "minimax:female", "男子": "minimax:male", "未知": "minimax:female", "简称": "minimax:female", "有人": "minimax:male", "": "minimax:male", "已选": "custom"}
    assert assigned == {"已选": "custom"}


@pytest.mark.parametrize("gender,expected", [("女", "female"), ("男", "male"), ("未知", "male"), (None, "male"), ("female", "female"), (" FEMALE ", "female"), ("f", "female"), (" F ", "female"), (" 女 ", "female"), ("女性", "male"), ("m", "male")])
def test_default_segment_voice_gender(gender, expected):
    from types import SimpleNamespace

    from backend.app.services.chapter import Chapter, _build_segments_for_chapter

    dialogue = SimpleNamespace(speaker="角色", anchor_start=0, anchor_end=2, text="你好")
    segments, _ = _build_segments_for_chapter(
        Chapter(idx=0, title="正文", text="你好"), [dialogue], "narrator", {}, None, 0,
        default_male_voice_id="male", default_female_voice_id="female",
        character_genders={"角色": gender},
    )
    assert [s.voice_id for s in segments if s.kind == "dialogue"] == [expected]


@pytest.mark.parametrize("assignments,expected", [({}, "narrator"), ({" 角色 ": "custom"}, "custom"), ({" 角色 ": ""}, "")])
def test_null_defaults_preserve_legacy_segment_lookup(assignments, expected):
    from types import SimpleNamespace

    from backend.app.services.chapter import Chapter, _build_segments_for_chapter

    dialogue = SimpleNamespace(speaker=" 角色 ", anchor_start=0, anchor_end=2, text="你好")
    segments, _ = _build_segments_for_chapter(
        Chapter(idx=0, title="正文", text="你好"), [dialogue], "narrator", assignments, None, 0,
    )
    assert [s.voice_id for s in segments if s.kind == "dialogue"] == [expected]


def test_default_voice_digest_null_compatibility_and_changes():
    from backend.app.services.build import _calc_config_digest

    args = ("narrator", 1.0, {})
    legacy = _calc_config_digest(*args)
    assert legacy == _calc_config_digest(*args, default_male_voice_id=None, default_female_voice_id=None)
    male = _calc_config_digest(*args, default_male_voice_id="male")
    female = _calc_config_digest(*args, default_female_voice_id="female")
    both = _calc_config_digest(*args, default_male_voice_id="male", default_female_voice_id="female")
    assert len({legacy, male, female, both}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("defaults", [(None, None), ("minimax:male", "minimax:female")])
async def test_start_and_retry_inherit_nullable_voice_snapshots(monkeypatch, defaults):
    import asyncio

    from backend.app.db.models import Build
    from backend.app.db.session import get_session_factory
    from backend.app.services import build as build_mod

    gate = asyncio.Event()

    async def blocked(**kwargs):
        await gate.wait()

    monkeypatch.setattr(build_mod, "_run_build_inner", blocked)
    pid = await _seed_prepared_project()
    resp = await build_mod.start_build(
        pid, {}, _NARRATOR, tts_provider="minimax",
        default_male_voice_id=defaults[0], default_female_voice_id=defaults[1],
    )
    try:
        async with get_session_factory()() as session:
            row = await session.get(Build, resp.build_id)
            assert (row.default_male_voice_id, row.default_female_voice_id) == defaults
            assert (resp.default_male_voice_id, resp.default_female_voice_id) == defaults
            digest = row.config_digest
            snapshot = row.voice_assignments_json
            narrator = row.narrator_voice_id
            row.narrator_emotion = "happy"
            row.narrator_instruction = "平静叙述"
            row.status = "failed"
            row.failed_chapters_json = "[0]"
            await session.commit()
        build_mod._ACTIVE_BUILDS.clear()
        from backend.app.core.config import settings

        monkeypatch.setattr(settings, "PROVIDERS_CONFIG", json.dumps({"active": {"tts": {"provider_id": "doubao", "model_id": "volcano_tts"}}}))
        retry = await build_mod.retry_failed_build(resp.build_id)
        async with get_session_factory()() as session:
            row = await session.get(Build, retry.build_id)
            assert (row.default_male_voice_id, row.default_female_voice_id) == defaults
            assert row.config_digest == digest
            assert row.tts_provider == "minimax"
            assert row.voice_assignments_json == snapshot
            assert row.narrator_voice_id == narrator
            assert row.narrator_emotion == "happy"
            assert row.narrator_instruction == "平静叙述"
            detail = build_mod._build_to_detail(row, [])
            assert (detail.default_male_voice_id, detail.default_female_voice_id) == defaults
    finally:
        gate.set()
        await asyncio.sleep(0.15)


@pytest.mark.asyncio
async def test_active_tts_controls_default_provider_and_narrator(monkeypatch):
    import asyncio

    from backend.app.core.config import settings
    from backend.app.services import build as build_mod

    settings.PROVIDERS_CONFIG = json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-hd"}}, "providers": []})
    providers = []

    class Voices:
        async def list_voices(self):
            return [{"id": "minimax:first"}, {"id": "minimax:male-qn-jingying"}]

    def get_tts(provider):
        providers.append(provider)
        return Voices()

    async def no_worker(**kwargs):
        return None

    monkeypatch.setattr(build_mod, "get_tts", get_tts)
    monkeypatch.setattr(build_mod, "_run_build_inner", no_worker)
    pid = await _seed_prepared_project()
    resp = await build_mod.start_build(pid, {}, "")
    assert resp.tts_provider == "minimax"
    assert resp.narrator_voice_id == "minimax:male-qn-jingying"
    assert providers == ["minimax"]
    await asyncio.sleep(0.15)


def test_minimax_model_cache_matches_provider(monkeypatch):
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSProvider
    from backend.app.core.config import settings
    from backend.app.services.build import (
        _seg_cache_key,
        _tts_model_param,
        _validate_tts_namespace,
        _voice_model_lookup,
    )

    _validate_tts_namespace("minimax", "classic", "minimax:male", {"女子": "doubao:female"})
    settings.PROVIDERS_CONFIG = json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-hd"}}})
    first = _tts_model_param("minimax")
    assert first == MiniMaxTTSProvider()._model() == _voice_model_lookup("minimax:male")
    settings.PROVIDERS_CONFIG = json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-turbo"}}})
    second = _tts_model_param("minimax")
    assert second == MiniMaxTTSProvider()._model()
    assert _seg_cache_key("minimax:male", 1.0, "文本", model=first, sample_rate=32000) != _seg_cache_key("minimax:male", 1.0, "文本", model=second, sample_rate=32000)
    settings.PROVIDERS_CONFIG = json.dumps({"active": {"tts": {"provider_id": "doubao", "model_id": "volcano_tts"}}})
    assert _tts_model_param("minimax") == MiniMaxTTSProvider()._model()


@pytest.mark.asyncio
@pytest.mark.parametrize("billed,error,expected", [(None, False, 2), (7, False, 7), (0, False, 0), (7, True, 7), (None, True, 0)])
async def test_provider_context_usage_consumed_even_on_failure(billed, error, expected):
    import asyncio

    from backend.app.services.build import _synthesize_with_usage, tts_usage_scope
    from backend.tests.mock_providers import MockTTSProvider

    class Provider(MockTTSProvider):
        async def synthesize_to_bytes(self, text, voice_id, **kwargs):
            self._set_usage(None if billed is None else {"usage_characters": billed})
            if error:
                raise RuntimeError("audio failed")
            return b"audio", 100

    provider = Provider()
    provider._set_usage({"usage_characters": 999})
    with tts_usage_scope() as usage:
        if error:
            with pytest.raises(RuntimeError, match="audio failed"):
                await _synthesize_with_usage(provider, "你好", "voice", asyncio.Semaphore(1))
        else:
            await _synthesize_with_usage(provider, "你好", "voice", asyncio.Semaphore(1))
    assert usage["chars"] == expected
    assert usage["calls"] == (1 if billed is not None or not error else 0)
    assert provider.consume_usage() is None


@pytest.mark.asyncio
async def test_usage_concurrent_scopes_and_direct_retry_reporting():
    import asyncio

    from backend.app.services.build import (
        _synthesize_with_usage,
        _tts_usage_scope,
        track_tts_usage,
        tts_usage_scope,
    )
    from backend.tests.mock_providers import MockTTSProvider

    class Provider(MockTTSProvider):
        async def synthesize_to_bytes(self, text, voice_id, **kwargs):
            self._set_usage({"usage_characters": len(text) * 10})
            await asyncio.sleep(0.01)
            return b"audio", 100

    provider = Provider()

    async def run(text):
        with tts_usage_scope() as usage:
            await _synthesize_with_usage(provider, text, "voice", asyncio.Semaphore(1))
        assert _tts_usage_scope.get() is None
        return usage["chars"]

    assert await asyncio.gather(run("甲"), run("甲乙")) == [10, 20]

    class ReportingProvider(MockTTSProvider):
        async def synthesize_to_bytes(self, text, voice_id, **kwargs):
            track_tts_usage(calls=2, chars=15)
            self._set_usage({"usage_characters": 999})
            return b"audio", 100

    with tts_usage_scope() as usage:
        await _synthesize_with_usage(ReportingProvider(), "文本", "voice", asyncio.Semaphore(1))
    assert (usage["calls"], usage["chars"]) == (2, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_worker_finally_records_usage_without_masking_original_error(monkeypatch, cancelled):
    import asyncio

    from backend.app.db.session import init_db
    from backend.app.services import build as build_mod

    await init_db()
    records = []

    async def record(*args):
        records.append(args)
        raise RuntimeError("record failed")

    monkeypatch.setattr(build_mod, "_record_build_usage", record)

    @build_mod._with_build_usage
    async def worker(build_id, project_id):
        build_mod.track_tts_usage(calls=2, chars=17)
        if cancelled:
            raise asyncio.CancelledError
        raise ValueError("packaging failed")

    with pytest.raises(asyncio.CancelledError if cancelled else ValueError):
        await worker(build_id="missing", project_id="project")
    assert records == [("missing", "project", "classic", 2, 17)]
    assert build_mod._tts_usage_scope.get() is None


@pytest.mark.asyncio
async def test_build_migration_nullable_defaults_and_icl_drop():
    import sqlite3

    from backend.app.core.config import settings
    from backend.app.db.models import Base
    from backend.app.db.session import init_db, schema_issues

    path = settings.DATABASE_URL.split("///")[-1]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE builds (build_id VARCHAR(64) PRIMARY KEY)")
        conn.execute("INSERT INTO builds VALUES ('legacy')")
        conn.execute("CREATE TABLE icl_training_tasks (task_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO icl_training_tasks VALUES ('old')")
    await init_db()
    await init_db()
    with sqlite3.connect(path) as conn:
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(builds)")}
        for name in ("default_male_voice_id", "default_female_voice_id"):
            assert columns[name][3] == 0
            assert columns[name][4] is None
        assert conn.execute("SELECT default_male_voice_id, default_female_voice_id FROM builds").fetchone() == (None, None)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='icl_training_tasks'").fetchone() is None
    assert "icl_training_tasks" not in Base.metadata.tables
    assert schema_issues() == []


@pytest.mark.asyncio
async def test_icl_drop_failure_enters_h29_schema_issues(monkeypatch):
    import sqlite3

    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from backend.app.core.config import settings
    from backend.app.db.session import get_engine, init_db, schema_issues

    path = settings.DATABASE_URL.split("///")[-1]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE icl_training_tasks (task_id TEXT)")

    def fail_drop(conn, cursor, statement, parameters, context, executemany):
        if statement == "DROP TABLE IF EXISTS icl_training_tasks":
            raise OperationalError(statement, parameters, RuntimeError("denied"))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", fail_drop)
    try:
        await init_db()
        assert schema_issues() == ["icl_training_tasks"]
    finally:
        event.remove(engine, "before_cursor_execute", fail_drop)
    await init_db()
    assert schema_issues() == []


@pytest.mark.asyncio
async def test_worker_minimax_usage_cache_hit_and_model_change(monkeypatch):
    import asyncio

    from sqlalchemy import select

    from backend.app.ai import factory as ai_factory
    from backend.app.core.config import settings
    from backend.app.db.models import Build, UsageEvent
    from backend.app.db.session import get_session_factory
    from backend.app.services import build as build_mod
    from backend.tests.mock_providers import MockTTSProvider

    pid = await _seed_prepared_project()
    settings.PROVIDERS_CONFIG = json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-hd"}}})
    records = []
    requests = []

    class Provider(MockTTSProvider):
        provider = "minimax"

        async def synthesize_to_bytes(self, text, voice_id, **kwargs):
            self._set_usage({"usage_characters": 7})
            requests.append((text, voice_id))
            return await super().synthesize_to_bytes(text, voice_id, **kwargs)

    provider = Provider()
    monkeypatch.setattr(ai_factory, "get_tts", lambda name=None: provider)
    monkeypatch.setattr(ai_factory, "get_tts_by_voice_id", lambda voice: provider)
    original = build_mod._record_build_usage

    async def record(*args):
        await original(*args)
        records.append(args)

    monkeypatch.setattr(build_mod, "_record_build_usage", record)

    async def run(overwrite_status, cached=False):
        before = len(records)
        response = await build_mod.start_build(pid, {}, "minimax:male-qn-jingying")
        for _ in range(200):
            if response.build_id not in build_mod._ACTIVE_BUILDS:
                break
            await asyncio.sleep(0.025)
        assert len(records) == before + (0 if cached else 1)
        async with get_session_factory()() as session:
            row = await session.get(Build, response.build_id)
            assert row.status == "success"
            totals = (row.tts_calls, row.tts_chars)
            if overwrite_status:
                row.status = "failed"
                await session.commit()
        for _ in range(100):
            if response.build_id not in build_mod._ACTIVE_BUILDS:
                break
            await asyncio.sleep(0.01)
        return response, totals

    first, totals = await run(True)
    count = len(requests)
    assert count > 0
    assert totals == (count, count * 7)
    second, totals = await run(False, cached=True)
    assert second.build_id != first.build_id
    assert len(requests) == count
    assert totals == (0, 0)
    settings.PROVIDERS_CONFIG = json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-turbo"}}})
    third, totals = await run(False)
    assert third.build_id != second.build_id
    assert len(requests) == count * 2
    assert totals == (count, count * 7)
    await asyncio.sleep(0.1)
    async with get_session_factory()() as session:
        events = list((await session.execute(select(UsageEvent).where(UsageEvent.project_id == pid))).scalars())
    assert sum(e.chars for e in events) == count * 14


@pytest.mark.asyncio
@pytest.mark.parametrize("billed,error", [(None, False), (7, False), (0, False), (7, True), (None, True), (True, False), (-1, False), ("7", False)])
async def test_minimax_usage_requires_real_characters(billed, error):
    import asyncio

    from backend.app.services.build import _synthesize_with_usage, tts_usage_scope
    from backend.tests.mock_providers import MockTTSProvider

    class Provider(MockTTSProvider):
        provider = "minimax"

        async def synthesize_to_bytes(self, text, voice_id, **kwargs):
            self._set_usage({"usage_characters": billed, "word_count": 999})
            if error:
                raise RuntimeError("audio failed")
            return b"audio", 100

    provider = Provider()
    provider._set_usage({"usage_characters": 999})
    with tts_usage_scope() as usage:
        if error:
            with pytest.raises(RuntimeError, match="audio failed"):
                await _synthesize_with_usage(provider, "你好", "minimax:voice", asyncio.Semaphore(1))
        else:
            await _synthesize_with_usage(provider, "你好", "minimax:voice", asyncio.Semaphore(1))
    valid = isinstance(billed, int) and not isinstance(billed, bool) and billed >= 0
    assert (usage["calls"], usage["chars"]) == ((1, billed) if valid else (0 if error else 1, 0))
    assert provider.consume_usage() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,voice_id,expected", [("minimax", "voice", (1, 0)), ("base", "minimax:voice", (1, 0)), ("doubao", "doubao:voice", (1, 2))])
async def test_usage_without_consume_method(provider_name, voice_id, expected):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.app.services.build import _synthesize_with_usage, tts_usage_scope

    provider = SimpleNamespace(provider=provider_name, synthesize_to_bytes=AsyncMock(return_value=(b"audio", 100)))
    with tts_usage_scope() as usage:
        await _synthesize_with_usage(provider, "你好", voice_id, asyncio.Semaphore(1))
    assert (usage["calls"], usage["chars"]) == expected


@pytest.mark.asyncio
async def test_minimax_segment_cache_roundtrip_and_model_invalidation(monkeypatch):
    from backend.app.core.config import settings
    from backend.app.services.build import (
        _tts_model_param,
        tts_segment_cache_get,
        tts_segment_cache_put,
    )

    monkeypatch.setattr(settings, "PROVIDERS_CONFIG", json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-hd"}}}))
    params = {"model": _tts_model_param("minimax"), "tts_model": _tts_model_param("minimax"), "sample_rate": 32000}
    await tts_segment_cache_put("minimax:voice", 1.0, "你好", b"audio", 100, **params)
    assert await tts_segment_cache_get("minimax:voice", 1.0, "你好", **params) == (b"audio", 100)
    monkeypatch.setattr(settings, "PROVIDERS_CONFIG", json.dumps({"active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-turbo"}}}))
    changed = dict(params, model=_tts_model_param("minimax"), tts_model=_tts_model_param("minimax"))
    assert await tts_segment_cache_get("minimax:voice", 1.0, "你好", **changed) is None
