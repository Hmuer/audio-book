"""Task 7 RED — Seed-Audio 1.0 多播剧 Provider + 构建管线接入。

T-MC1  build_prompt：把 segment 列表转多角色 prompt（含角色名+台词），跳过静音段。
T-MC2  synthesize_chapter_to_file：payload 携带 prompt/roles(剥离前缀的 voice)/speed/
       instruction_text；调 Seed-Audio 端点（seed_audio RPM 桶）；原子写文件。
T-MC3  factory.get_multicast_tts() 单例 + 测试注入。
T-MC4  _validate_tts_namespace：mode=multicast 必须配 doubao provider。
T-MC5  构建管线：mode=multicast + tts_provider=doubao 时章节走 multicast provider
       （mock 注入），不走逐段 TTS；build 成功产出 MP3。
T-MC6  多播剧失败（strict 默认开）→ Build failed，无占位 MP3（已由 Task 8 覆盖，
       此处验证 multicast provider 抛错路径即抛异常不降级）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAKE_MP3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 4096)


def _mk_segments() -> list[dict]:
    return [
        {"kind": "title", "speaker": None, "text": "第一章 初见", "voice_id": "doubao:zh_female_qingxin"},
        {"kind": "silence", "speaker": None, "text": "", "voice_id": None, "silence_ms": 1500},
        {"kind": "narrator", "speaker": None, "text": "清晨的山间小路上，", "voice_id": "doubao:zh_female_qingxin"},
        {"kind": "dialogue", "speaker": "林轩", "text": "你好，请问怎么走？", "voice_id": "doubao:zh_male_qingnianqingche"},
        {"kind": "dialogue", "speaker": "苏瑶", "text": "跟我来吧。", "voice_id": "icl:clone_x"},
    ]


# ---------------------------------------------------------------------
# T-MC1: build_prompt
# ---------------------------------------------------------------------
def test_multicast_build_prompt():
    from backend.app.ai.providers.doubao.multicast import DoubaoMulticastProvider

    p = DoubaoMulticastProvider()
    prompt = p.build_prompt(_mk_segments(), chapter_title="第一章 初见")
    assert "林轩" in prompt and "你好，请问怎么走？" in prompt
    assert "苏瑶" in prompt and "跟我来吧。" in prompt
    assert "旁白" in prompt or "narrator" in prompt.lower()
    # 静音段不进 prompt
    assert "silence" not in prompt


# ---------------------------------------------------------------------
# T-MC2: synthesize_chapter_to_file
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multicast_synthesize_chapter_to_file(tmp_path, monkeypatch):
    from backend.app.ai.providers.doubao.multicast import DoubaoMulticastProvider
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")
    p = DoubaoMulticastProvider()
    captured: dict = {}

    async def _fake_http(url, headers, payload):
        captured["url"] = url
        captured["payload"] = payload
        return FAKE_MP3

    p._http_post_bytes = _fake_http  # type: ignore[method-assign]
    out = str(tmp_path / "ch0000.mp3")
    path, dur = await p.synthesize_chapter_to_file(
        _mk_segments(), out, speed=1.2, chapter_title="第一章 初见",
        instruction_text="语气生动，带一点清晨的空灵感",
    )
    assert path == out
    assert Path(out).is_file() and Path(out).read_bytes() == FAKE_MP3
    assert dur > 0

    assert "seed_audio" in captured["url"].lower()
    pl = captured["payload"]
    assert pl["prompt"] and "林轩" in pl["prompt"]
    # roles：剥离前缀
    roles = {r["name"]: r["voice"] for r in pl["roles"]}
    assert roles.get("林轩") == "zh_male_qingnianqingche"
    assert roles.get("苏瑶") == "clone_x"  # icl: 剥离
    assert pl["speed_ratio"] == 1.2
    assert "清晨" in pl["instruction_text"]


# ---------------------------------------------------------------------
# T-MC3: factory.get_multicast_tts 单例 + 注入
# ---------------------------------------------------------------------
def test_factory_get_multicast_tts_singleton_and_injection(monkeypatch):
    from backend.app.ai import factory as aifact
    from backend.app.ai.providers.doubao.multicast import DoubaoMulticastProvider

    inst1 = aifact.get_multicast_tts()
    inst2 = aifact.get_multicast_tts()
    assert isinstance(inst1, DoubaoMulticastProvider)
    assert inst1 is inst2

    class _FakeMC:
        pass

    fake = _FakeMC()
    monkeypatch.setattr(aifact, "_multicast_instance", fake)
    assert aifact.get_multicast_tts() is fake


# ---------------------------------------------------------------------
# T-MC4: multicast 必须 doubao
# ---------------------------------------------------------------------
def test_multicast_mode_requires_doubao_provider():
    from backend.app.services.build import _validate_tts_namespace

    with pytest.raises(RuntimeError) as ei:
        _validate_tts_namespace(
            tts_provider="minimax",
            mode="multicast",
            narrator_voice_id="minimax:male-qn-jingying",
            voice_assignments={},
        )
    assert "doubao" in str(ei.value).lower() or "多播剧" in str(ei.value)

    # doubao + multicast → OK
    _validate_tts_namespace(
        tts_provider="doubao",
        mode="multicast",
        narrator_voice_id="doubao:zh_female_qingxin",
        voice_assignments={"林轩": "doubao:zh_male_qingnianqingche"},
    )


# ---------------------------------------------------------------------
# T-MC5: 构建管线走 multicast provider（mock 注入）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_build_pipeline_uses_multicast_provider(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, get_build_status, _RUNNING_BUILDS, _RUNNING_LOCK
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider
    from backend.app.core import config as cfgmod

    await init_db()

    class _RecordingMC:
        provider = "doubao"
        def __init__(self):
            self.calls: list[dict] = []

        async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                             chapter_title="", instruction_text=None):
            self.calls.append({
                "segments": list(segments), "output_path": output_path,
                "speed": speed, "chapter_title": chapter_title,
                "instruction_text": instruction_text,
            })
            Path(output_path).write_bytes(FAKE_MP3)
            return output_path, len(FAKE_MP3) // 16

    mock_mc = _RecordingMC()
    prev_mc = aifact._multicast_instance
    prev_tts = aifact._tts_instance
    prev_llm = aifact._llm_instance
    aifact._multicast_instance = mock_mc
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    try:
        pid = (await create_project("多播剧测试")).project_id
        book = "第一章 初见\n李明说：「你好，请问怎么走？」\n林若雪说：「跟我来吧。」\n第二章 启程\n他们出发了。"
        await import_file(pid, book.encode("utf-8"), "book.txt")
        await prepare_project(pid)
        resp = await start_build(
            project_id=pid,
            voice_assignments={"李明": "doubao:zh_male_qingnianqingche"},
            narrator_voice_id="doubao:zh_female_qingxin",
            tts_provider="doubao", mode="multicast",
        )
        bid = resp.build_id
        for _ in range(120):
            s = await get_build_status(bid)
            if s.status in ("success", "partial_success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.25)
        else:
            async with _RUNNING_LOCK:
                _RUNNING_BUILDS.discard(pid)
            pytest.fail("build worker 超时未结束")

        assert s.status == "success", f"期望 success，实际 {s.status} msg={s.progress_msg!r}"
        # 每章走一次 multicast provider
        assert len(mock_mc.calls) == 2, f"期望 2 章 × 1 次 multicast 调用，实际 {len(mock_mc.calls)}"
        first = mock_mc.calls[0]
        assert first["chapter_title"] == "第一章 初见"
        # segments 传入的是 dict 列表且带 voice_id
        segs = first["segments"]
        assert any(seg.get("voice_id") == "doubao:zh_male_qingnianqingche" for seg in segs)

        # 章节 MP3 落盘且为 multicast 产物（FAKE_MP3）
        factory = get_session_factory()
        async with factory() as sess:
            arts = list((await sess.execute(
                __import__("sqlalchemy").select(BuildArtifact).where(
                    BuildArtifact.build_id == bid
                )
            )).scalars().all())
            assert len(arts) == 2
            for a in arts:
                assert a.status == "done"
                assert a.audio_filename
                f = Path(cfgmod.settings.AUDIO_DIR) / a.audio_filename
                assert f.is_file()
                assert f.read_bytes() == FAKE_MP3
    finally:
        aifact._multicast_instance = prev_mc
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        async with _RUNNING_LOCK:
            _RUNNING_BUILDS.discard(pid)
