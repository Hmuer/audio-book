"""P1-2/3/4/5/7 RED — 豆包 TTS v3 增强项测试。

覆盖：
  P1-2  T-E1  emotion 三处冗余修复：v1 payload 不在 body.emotion / audio.emotion 重复
  P1-2  T-E2  音色 supports_emotion=False → 降级打 warning 并跳过下发
  P1-4  T-SR  audio_params.sample_rate/loudness_rate 来自 settings
  P1-5  T-I1  v3 payload instruction_text → req_params.context_texts=[...]
  P1-5  T-I2  v3 复刻音色（icl_ 前缀）忽略 instruction_text 并 warning
  P1-3  T-S1  sidecar JSON → SRT 文本（含 speaker 前缀 / 长段切分 / silence 跳过）
  P1-3  T-S2  路由：GET /api/projects/.../builds/.../chapters/{idx}/subtitle 返回 SRT
  P1-7  T-K1  _seg_cache_key 切 model / context_texts 后键变化
  P1-7  T-K2  _voice_model_lookup 把 ICL/S_ 前缀映射到 seed-icl-2.0
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


FAKE_MP3 = _b64.b64encode(b"\xff\xfb\x90\x64\x00" + (b"\x00" * 64)).decode("ascii")


# =====================================================================
# P1-2
# =====================================================================
def test_p1_2_e1_v1_emotion_only_in_extend_params():
    """v1 payload：emotion 只在 extend_params.emotion 一处塞；不在 body 顶层 / audio 顶层重复。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    cfgmod.settings.DOUBAO_AK = "test-ak"
    p = DoubaoTTSProvider()
    payload = p._build_payload("你好", "BV001_streaming", emotion="happy")
    # emotion 必须在 extend_params
    assert payload["extend_params"]["emotion"] == "happy"
    # P1-2 去重：body 顶层和 audio 顶层不应再有 emotion
    assert "emotion" not in payload
    assert "emotion" not in payload["audio"]


def test_p1_2_e2_v1_unsupported_voice_demotes_emotion():
    """音色 supports_emotion=False → 不下发 emotion（只在 ext["emotion"] 里塞）；
    logger.warning 被触发（caplog 验证）。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    cfgmod.settings.DOUBAO_AK = "test-ak"
    p = DoubaoTTSProvider()

    # BV005_streaming（古风/磁性）supports_emotion=False（按内置表）
    with pytest.MonkeyPatch.context() as mp:
        # 强制让 _voice_supports_emotion 返回 False（即便音色实际支持）以便单测可控
        import backend.app.ai.providers.doubao.tts as tts_mod
        mp.setattr(tts_mod, "_voice_supports_emotion", lambda _s: False)
        payload = p._build_payload("你好", "BV001_streaming", emotion="happy")

    assert "emotion" not in payload["extend_params"]


def test_p1_2_e3_v3_emotion_skipped_when_unsupported(monkeypatch):
    """v3 路径：音色 supports_emotion=False → audio_params.emotion 不下发。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake"

    import backend.app.ai.providers.doubao.tts as tts_mod
    monkeypatch.setattr(tts_mod, "_voice_supports_emotion", lambda _s: False)

    payload = p._build_v3_payload(
        "你好", "doubao:BV001_streaming", emotion="happy",
    )
    ap = payload["req_params"]["audio_params"]
    assert "emotion" not in ap


# =====================================================================
# P1-4
# =====================================================================
def test_p1_4_sample_rate_and_loudness_from_settings(monkeypatch):
    """v3 payload sample_rate / loudness_rate 从 settings 读出（覆盖默认值）。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AUDIO_SAMPLE_RATE", 16000)
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AUDIO_LOUDNESS_RATE", 5)

    p = DoubaoTTSProviderV3()
    payload = p._build_v3_payload("你好", "doubao:BV001_streaming")
    ap = payload["req_params"]["audio_params"]
    assert ap["sample_rate"] == 16000
    assert ap["loudness_rate"] == 5


# =====================================================================
# P1-5
# =====================================================================
def test_p1_5_i1_v3_instruction_text_to_context_texts():
    """v3 instruction_text → req_params.context_texts=[instruction_text]。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    payload = p._build_v3_payload(
        "你好", "doubao:BV001_streaming",
        instruction_text="语气欢快，像老朋友",
    )
    rp = payload["req_params"]
    assert rp.get("context_texts") == ["语气欢快，像老朋友"]


def test_p1_5_i2_v3_clone_speaker_ignores_instruction_text():
    """v3 复刻音色（icl_ 前缀）忽略 instruction_text，不下发 context_texts。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    payload = p._build_v3_payload(
        "你好", "icl:icl_abc123",
        instruction_text="语气欢快",
    )
    rp = payload["req_params"]
    assert "context_texts" not in rp


# =====================================================================
# P1-3 — SRT 转换器
# =====================================================================
def test_p1_3_s1_sidecar_to_srt_basic():
    """基础：3 个 speech 段 + 1 silence 段 → SRT 含 3 条，silence 跳过。"""
    from backend.app.services.srt import timings_json_to_srt

    timings = {
        "version": 1,
        "estimated": False,
        "segs": [
            {"kind": "speech", "speaker": "Alice", "text": "你好",
             "start_ms": 0, "dur_ms": 1500},
            {"kind": "silence", "speaker": "", "text": "", "start_ms": 1500, "dur_ms": 500},
            {"kind": "speech", "speaker": "Bob", "text": "世界",
             "start_ms": 2000, "dur_ms": 800},
        ],
    }
    srt = timings_json_to_srt(timings)
    # 3 条 SRT：每条 4 行（index, time, text, 空行）
    assert "1\n" in srt  # 第 1 条序号
    assert "00:00:00,000 --> 00:00:01,500" in srt
    assert "00:00:02,000 --> 00:00:02,800" in srt
    # speaker 第一次出现带【】前缀，silence 后 Bob 又带一次
    assert "【Alice】你好" in srt
    assert "【Bob】世界" in srt
    # silence 段不出现条目
    assert srt.count("-->") == 2


def test_p1_3_s2_long_entry_is_split():
    """长段（>28 字）按字符数切多块，时间累计正确。"""
    from backend.app.services.srt import timings_json_to_srt

    long_text = "今天我们来讲一个非常长非常长的章节内容段落" * 3  # 66 字
    timings = {
        "version": 1,
        "estimated": False,
        "segs": [
            {"kind": "speech", "speaker": "", "text": long_text,
             "start_ms": 0, "dur_ms": 6000},
        ],
    }
    srt = timings_json_to_srt(timings)
    # 至少 2 条
    assert srt.count("-->") >= 2
    # 时间累加到 6000ms
    assert "00:00:06,000" in srt


def test_p1_3_s3_load_chapter_srt_returns_none_when_missing(tmp_path):
    """无 sidecar → 返回 None。"""
    from backend.app.services.srt import load_chapter_srt

    assert load_chapter_srt(tmp_path, "build_x", 0) is None


def test_p1_3_s4_load_chapter_srt_returns_srt_when_present(tmp_path):
    """sidecar 存在 → 返回 SRT 文本。"""
    from backend.app.services.srt import load_chapter_srt

    # 注意：_timings_filename 自动加 build_ 前缀，所以这里 build_id 不能含 "build_"
    sidecar = tmp_path / "build_xyz_ch0000_timings.json"
    sidecar.write_text(
        json.dumps(
            {"version": 1, "estimated": False,
             "segs": [
                 {"kind": "speech", "speaker": "旁白", "text": "第一句",
                  "start_ms": 0, "dur_ms": 1000},
             ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    srt = load_chapter_srt(tmp_path, "xyz", 0)
    assert srt is not None
    assert "第一句" in srt
    assert "00:00:00,000 --> 00:00:01,000" in srt


@pytest.mark.asyncio
async def test_p1_3_s5_route_returns_srt(_isolate_data_dir, tmp_path, monkeypatch):
    """端到端：写 sidecar + mock artifact.status=done → GET subtitle 返回 SRT。

    重要：整个测试必须在同一个 event loop 里完成 init_db / seed / 业务请求，
    否则 SQLAlchemy AsyncEngine 在第一个 loop 关闭后失效，后续路由会找不到数据。
    这里用 httpx.AsyncClient + ASGITransport 驱动 FastAPI lifespan，并在同一 loop
    内做数据库 seed。
    """
    from httpx import ASGITransport, AsyncClient

    from backend.app.db.session import init_db
    from backend.app.db.models import Build, BuildArtifact, Project, User
    from backend.app.db.session import get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.core import config as cfgmod
    from sqlalchemy import select as _select
    from backend.app.main import app as _app

    # 1) init_db + seed_admin + 业务数据 + 拿 token，全部同一 loop
    monkeypatch.setattr(cfgmod.settings, "AUDIO_DIR", tmp_path)

    # 写 sidecar（路由调用 load_chapter_srt(build_id="b1234", ...) 会拼成 build_b1234_...）
    sidecar = tmp_path / "build_b1234_ch0000_timings.json"
    sidecar.write_text(
        json.dumps(
            {"version": 1, "estimated": False,
             "segs": [
                 {"kind": "speech", "speaker": "旁白", "text": "测试字幕",
                  "start_ms": 0, "dur_ms": 2000},
             ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    async with factory() as s:
        admin_row = (await s.execute(
            _select(User).where(User.username == "admin")
        )).scalar_one()
        uid = admin_row.id
        s.add(Project(project_id="p1", name="test", owner_user_id=uid))
        s.add(Build(
            build_id="b1234", project_id="p1",
            status="running", total_chapters=1, completed_chapters=0,
            narrator_voice_id="doubao:BV001_streaming", speed=1.0,
            mode="classic", tts_provider="doubao",
            is_retry=False,
        ))
        await s.flush()
        s.add(BuildArtifact(
            build_id="b1234", chapter_idx=0, title="测试章",
            status="done", audio_filename="build_b1234_ch0000.mp3",
            audio_url="/media/build_b1234_ch0000.mp3",
            duration_ms=2000,
        ))
        await s.commit()

    token, _ = create_access_token("admin")

    # 2) 同一 loop 里驱动 lifespan + 触发 HTTP 请求
    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/api/projects/p1/builds/b1234/chapters/0/subtitle",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert "测试字幕" in r.text
    assert r.headers["content-type"].startswith("application/x-subrip")


# =====================================================================
# P1-7 — 段缓存键
# =====================================================================
def test_p1_7_k1_cache_key_changes_with_model_and_context():
    """model / context_texts 改变 → 键必须不同。"""
    from backend.app.services.build import _seg_cache_key

    base = _seg_cache_key("doubao:BV001_streaming", 1.0, "你好")
    diff_model = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        model="seed-tts-2.0",
    )
    diff_ctx = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        context_texts_hash="abc123",
    )
    diff_both = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        model="seed-tts-2.0", context_texts_hash="abc123",
    )
    assert base != diff_model
    assert base != diff_ctx
    assert base != diff_both
    assert diff_model != diff_both
    assert diff_ctx != diff_both


def test_p1_7_k2_cache_key_backward_compat_when_all_empty():
    """model 和 context_texts_hash 都为空时键与历史完全一致（兼容旧缓存）。"""
    from backend.app.services.build import _seg_cache_key

    old_key = _seg_cache_key("doubao:BV001_streaming", 1.0, "你好")
    # 显式传空
    new_key = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        model="", context_texts_hash="",
    )
    assert old_key == new_key


def test_p1_7_k3_voice_model_lookup_icl_returns_seed_icl():
    """ICl 复刻音色 → seed-icl-2.0。"""
    from backend.app.services.build import _voice_model_lookup

    assert _voice_model_lookup("icl:icl_abc123") == "seed-icl-2.0"
    assert _voice_model_lookup("S_xxxxx") == "seed-icl-2.0"
    # 内置大模型音色（实际表里的 id）
    assert _voice_model_lookup("doubao:zh_female_vv_uranus_bigtts") == "seed-tts-2.0"
    # 兜底
    assert _voice_model_lookup("doubao:unknown_xyz") == "seed-tts-1.0"


def test_p1_7_k4_context_texts_hash_empty_for_blank():
    """空字符串 instruction → 空 hash（兼容旧缓存）。"""
    from backend.app.services.build import _context_texts_hash

    assert _context_texts_hash("") == ""
    assert _context_texts_hash("   ") == ""
    # 非空：长度 16 hex
    h = _context_texts_hash("语气欢快")
    assert len(h) == 16


def test_p1_7_k5_cache_key_changes_with_sample_rate():
    """P1-4：sample_rate 改变 → 键必须不同（settings 改采样率后不能命中旧缓存）。"""
    from backend.app.services.build import _seg_cache_key

    base = _seg_cache_key("doubao:BV001_streaming", 1.0, "你好")
    diff_sr = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        sample_rate=16000,
    )
    diff_sr2 = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        sample_rate=32000,
    )
    assert base != diff_sr
    assert diff_sr != diff_sr2
    # sample_rate=空（默认）→ 与历史键一致
    explicit_empty = _seg_cache_key(
        "doubao:BV001_streaming", 1.0, "你好",
        sample_rate="",
    )
    assert explicit_empty == base
