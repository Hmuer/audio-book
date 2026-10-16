"""拼接 MP3 时长与旧构建质检回填的回归测试。"""
import json

import pytest

from backend.app.core.mp3_util import make_silent_mp3, mp3_duration_ms


def _id3_tag(body_size: int) -> bytes:
    size = body_size
    syncsafe = bytes(((size >> 21) & 127, (size >> 14) & 127,
                      (size >> 7) & 127, size & 127))
    return b"ID3\x04\x00\x00" + syncsafe + b"\x00" * body_size


def test_mp3_duration_reads_all_segments_with_repeated_id3():
    # 真实 TTS 每段自带 ID3；旧解析器在累计 65 字节后停在第三段。
    frame_part = make_silent_mp3(12500, sample_rate=24000, kbps=64)
    audio = b"".join(_id3_tag(80) + frame_part for _ in range(36))
    assert 450_000 <= mp3_duration_ms(audio) <= 452_000


def test_mp3_duration_resync_limit_is_consecutive():
    frame_part = make_silent_mp3(1000, sample_rate=24000, kbps=64)
    audio = b"\x00".join([frame_part] * 80)
    assert 79_000 <= mp3_duration_ms(audio) <= 83_000


def test_chapter_preview_cuts_at_fifteen_seconds_across_id3():
    from backend.app.services.preview import _split_frames

    audio = (
        _id3_tag(80) + make_silent_mp3(12_500, sample_rate=24000, kbps=64)
        + _id3_tag(80) + make_silent_mp3(10_000, sample_rate=24000, kbps=128)
    )
    cut = _split_frames(audio, max_seconds=15)
    assert 15_000 <= mp3_duration_ms(cut) <= 15_024
    assert len(cut) < len(audio)


def test_legacy_duration_uses_local_mp3_when_sidecar_missing(tmp_path, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.models import BuildArtifact
    from backend.app.services.build import _legacy_artifact_duration_ms

    monkeypatch.setattr(settings, "AUDIO_DIR", str(tmp_path))
    audio = _id3_tag(80) + make_silent_mp3(1000, sample_rate=24000, kbps=64)
    (tmp_path / "chapter.mp3").write_bytes(audio)
    art = BuildArtifact(build_id="old", chapter_idx=0, status="done",
                        audio_filename="chapter.mp3", duration_ms=100)
    assert 1000 <= _legacy_artifact_duration_ms("old", art) <= 1100


@pytest.mark.asyncio
async def test_legacy_quality_is_recomputed_from_sidecar(tmp_path, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.services import build as build_mod
    from backend.app.services.chapter import Chapter

    monkeypatch.setattr(settings, "AUDIO_DIR", str(tmp_path))
    sidecar = tmp_path / build_mod._timings_filename("old", 0)
    sidecar.write_text(json.dumps({
        "version": 1, "estimated": False,
        "segs": [{"start_ms": 0, "dur_ms": 370_000},
                 {"start_ms": 370_000, "dur_ms": 370_000}],
    }), encoding="utf-8")
    b = Build(build_id="old", project_id="p", status="partial_success",
              total_duration_ms=13_503,
              progress_meta_json=json.dumps({"phase": "done", "quality": {
                  "checked_n": 1, "failed_n": 1, "suspicious": [{
                      "chapter_idx": 0, "chars": 3269,
                      "duration_ms": 12503, "chars_per_sec": 261.46,
                  }],
              }}))
    arts = [
        BuildArtifact(build_id="old", chapter_idx=0, status="done",
                      duration_ms=12503, audio_filename="old.mp3"),
        BuildArtifact(build_id="old", chapter_idx=1, status="failed",
                      duration_ms=1000),
    ]

    async def chapters(_session, _project_id):
        return [Chapter(idx=0, title="", text="字" * 3269)]

    monkeypatch.setattr(build_mod, "load_chapters", chapters)

    class Session:
        commits = 0

        async def commit(self):
            self.commits += 1

    session = Session()
    await build_mod._repair_legacy_build_durations(session, b, arts)
    assert arts[0].duration_ms == 740_000
    assert b.total_duration_ms == 741_000
    meta = json.loads(b.progress_meta_json)
    assert meta["quality"] == {"suspicious": [], "failed_n": 1, "checked_n": 1}
    assert meta["duration_parser_version"] == 2
    assert session.commits == 1

    await build_mod._repair_legacy_build_durations(session, b, arts)
    assert session.commits == 1
