"""拼接 MP3 时长与旧构建质检回填的回归测试。"""
import json

import pytest

from backend.app.core.mp3_util import (
    concat_mp3_files, has_mp3_seek_header, make_silent_mp3, mp3_channel_modes,
    mp3_duration_ms, neutralize_mp3_seek_headers,
)


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


def test_chapter_mp3_keeps_mono_channel_mode_and_strips_midstream_id3():
    # MiniMax 请求 channel=1；旧静音帧却写成 stereo(0)，浏览器会报 midstream change。
    voice = _id3_tag(80) + make_silent_mp3(1000, sample_rate=32000)
    pause = make_silent_mp3(250, sample_rate=32000)
    chapter = concat_mp3_files(voice, pause, voice)
    assert mp3_channel_modes(chapter) == {3}
    assert b"ID3" not in chapter
    assert 2200 <= mp3_duration_ms(chapter) <= 2350


def test_chapter_concat_removes_stale_segment_seek_headers():
    # 模拟 MiniMax 每段首帧的 Info 索引；拼接后索引仍指向短段，会让播放器跳转到错误位置。
    segment = bytearray(make_silent_mp3(1000, sample_rate=32000))
    segment[21:25] = b"Info"  # MPEG1 单声道 side info 之后
    segment[25:29] = (15).to_bytes(4, "big")
    segment[29:33] = (3).to_bytes(4, "big")
    source = bytes(segment)
    assert has_mp3_seek_header(source)

    old_chapter = source + source
    fixed, count = neutralize_mp3_seek_headers(old_chapter)
    assert count == 2
    assert not has_mp3_seek_header(fixed)
    assert len(fixed) == len(old_chapter)
    assert mp3_duration_ms(fixed) == mp3_duration_ms(old_chapter)
    assert fixed == concat_mp3_files(source, source)
    # 误碰普通语音负载里的字符串不会清除有效字节。
    plain = bytearray(make_silent_mp3(1000, sample_rate=32000))
    plain[100:104] = b"Info"
    assert neutralize_mp3_seek_headers(bytes(plain)) == (bytes(plain), 0)


@pytest.mark.asyncio
async def test_old_remote_chapter_repairs_once_without_tts(tmp_path, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.services import audio_seek

    segment = bytearray(make_silent_mp3(1000, sample_rate=32000))
    segment[21:25] = b"Info"

    class Remote:
        archives_remotely = True

        def __init__(self):
            self.data = bytes(segment) * 2
            self.fetches = 0
            self.uploads = 0

        async def peek_bytes(self, _key, count):
            return self.data[:count]

        async def fetch_to(self, *, key, dest):
            self.fetches += 1
            dest.write_bytes(self.data)
            return True

        async def archive(self, *, key, local_path, content_type):
            self.uploads += 1
            self.data = local_path.read_bytes()
            return True

    remote = Remote()
    monkeypatch.setattr(settings, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(audio_seek, "get_storage", lambda: remote)
    monkeypatch.setattr(audio_seek, "cleanup_local_enabled", lambda: True)

    assert await audio_seek.ensure_chapter_seekable("p", "b", "chapter.mp3") is None
    assert (remote.fetches, remote.uploads) == (1, 1)
    assert not has_mp3_seek_header(remote.data)
    assert len(remote.data) == 2 * len(segment)
    assert not (tmp_path / "chapter.mp3").exists()
    assert await audio_seek.ensure_chapter_seekable("p", "b", "chapter.mp3") is None
    assert (remote.fetches, remote.uploads) == (1, 1)


def test_channel_modes_detect_old_stereo_silence_for_retry():
    mono = make_silent_mp3(100, sample_rate=32000)
    stereo = bytearray(mono)
    # 模拟旧版 32kHz 双声道静音帧（第 4 字节 mode 位为 0）。
    frame_len = 576
    for pos in range(0, len(stereo), frame_len):
        stereo[pos + 3] &= 0x3F
    assert mp3_channel_modes(mono + stereo + mono) == {0, 3}


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
