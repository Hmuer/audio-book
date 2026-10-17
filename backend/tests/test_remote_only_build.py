"""对象存储省盘模式：段缓存不落盘，成品逐章上传并回源打包。"""
from __future__ import annotations

import json
import asyncio
from io import BytesIO
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select


class _MemoryObjectStorage:
    archives_remotely = True

    def __init__(self, fail_mp3: bool = False, fail_zip: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_mp3 = fail_mp3
        self.fail_zip = fail_zip

    async def archive(self, *, key: str, local_path: Path, content_type: str | None = None) -> bool:
        if self.fail_mp3 and key.endswith(".mp3"):
            return False
        if self.fail_zip and key.endswith(".zip"):
            return False
        self.objects[key] = local_path.read_bytes()
        return True

    async def put_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> bool:
        self.objects[key] = data
        return True

    async def fetch_to(self, *, key: str, dest: Path) -> bool:
        data = self.objects.get(key)
        if data is None:
            return False
        dest.write_bytes(data)
        return True


async def _seed_build(chapters: int = 2) -> tuple[str, str]:
    from backend.app.db.models import Build, BuildArtifact, Project, ProjectChapter
    from backend.app.db.session import get_session_factory, init_db

    await init_db()
    project_id, build_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with get_session_factory()() as session:
        session.add(Project(project_id=project_id, name="对象存储构建", status="ready"))
        for idx in range(chapters):
            session.add(ProjectChapter(
                project_id=project_id, idx=idx, title=f"第{idx+1}章",
                text=f"这是第{idx+1}章。", text_len=6,
            ))
            session.add(BuildArtifact(
                build_id=build_id, chapter_idx=idx, title=f"第{idx+1}章", status="pending",
            ))
        session.add(Build(
            build_id=build_id, project_id=project_id, status="queued",
            total_chapters=chapters, completed_chapters=0,
            narrator_voice_id="female-tianmei", speed=1.0,
            mode="classic", tts_provider="doubao",
        ))
        await session.commit()
    return project_id, build_id


@pytest.mark.asyncio
async def test_remote_build_keeps_only_sidecars_locally(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.db.session import get_session_factory
    from backend.app.services import build

    remote = _MemoryObjectStorage()
    monkeypatch.setattr(build, "get_storage", lambda: remote)
    monkeypatch.setattr(settings, "STORAGE_CLEANUP_LOCAL", True)

    async def synth(*_args, **_kwargs):
        return make_silent_mp3(180, sample_rate=24000), 180

    monkeypatch.setattr(build, "get_tts", lambda _provider: SimpleNamespace(
        synthesize_to_bytes=synth,
    ))
    project_id, build_id = await _seed_build()
    await build._run_build_inner(build_id, project_id, {}, "female-tianmei")

    async with get_session_factory()() as session:
        row = await session.get(Build, build_id)
        arts = (await session.execute(select(BuildArtifact).where(
            BuildArtifact.build_id == build_id,
        ).order_by(BuildArtifact.chapter_idx))).scalars().all()
        assert row.status == "success"
        assert [art.status for art in arts] == ["done", "done"]
        assert json.loads(row.zip_filenames_json)
        assert row.total_size_bytes > 0
    assert len([key for key in remote.objects if key.endswith(".mp3")]) == 2
    zip_data = next(data for key, data in remote.objects.items() if key.endswith(".zip"))
    with zipfile.ZipFile(BytesIO(zip_data)) as archive:
        mp3_names = [name for name in archive.namelist() if name.endswith(".mp3")]
        assert len(mp3_names) == 2
        assert all(archive.read(name).startswith(b"\xff") for name in mp3_names)
    assert not list(Path(settings.AUDIO_DIR).glob(f"build_{build_id}_*.mp3"))
    assert not list(Path(settings.AUDIO_DIR).glob(f"build_{build_id}_*.zip"))
    assert not list(Path(settings.AUDIO_DIR).glob(".zip_ch_*.mp3"))
    assert not list((Path(settings.AUDIO_DIR) / "_seg_cache").glob("*.mp3"))


@pytest.mark.asyncio
async def test_remote_mode_never_recreates_disk_segment_cache(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.services import build

    remote = _MemoryObjectStorage()
    monkeypatch.setattr(build, "get_storage", lambda: remote)
    monkeypatch.setattr(settings, "STORAGE_CLEANUP_LOCAL", True)
    monkeypatch.setattr(settings, "TTS_SEGMENT_CACHE_MAX_ENTRIES", 2)
    build._tts_seg_mem_cache.clear()
    disk = Path(settings.AUDIO_DIR) / "_seg_cache"
    disk.mkdir(parents=True)
    (disk / "old.mp3").write_bytes(b"old")
    build._run_seg_cache_gc_if_needed(force=True)
    assert not disk.exists()

    await build.tts_segment_cache_put("doubao:test", 1.0, "你好", b"mp3", 100)
    assert await build.tts_segment_cache_get("doubao:test", 1.0, "你好") == (b"mp3", 100)
    await build.tts_segment_cache_put("doubao:test", 1.0, "再见", b"mp3b", 100)
    await build.tts_segment_cache_put("doubao:test", 1.0, "晚安", b"mp3c", 100)
    assert await build.tts_segment_cache_get("doubao:test", 1.0, "你好") is None
    assert await build.tts_segment_cache_get("doubao:test", 1.0, "晚安") == (b"mp3c", 100)
    assert not disk.exists()


@pytest.mark.asyncio
async def test_remote_mp3_upload_failure_stops_without_fake_success(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.db.models import BuildArtifact
    from backend.app.db.session import get_session_factory
    from backend.app.services import build

    remote = _MemoryObjectStorage(fail_mp3=True)
    monkeypatch.setattr(build, "get_storage", lambda: remote)
    monkeypatch.setattr(settings, "STORAGE_CLEANUP_LOCAL", True)

    async def synth(*_args, **_kwargs):
        return make_silent_mp3(180, sample_rate=24000), 180

    monkeypatch.setattr(build, "get_tts", lambda _provider: SimpleNamespace(
        synthesize_to_bytes=synth,
    ))
    project_id, build_id = await _seed_build()
    with pytest.raises(build._RemoteArchiveError):
        await build._run_build_inner(build_id, project_id, {}, "female-tianmei")
    async with get_session_factory()() as session:
        arts = (await session.execute(select(BuildArtifact).where(
            BuildArtifact.build_id == build_id,
        ).order_by(BuildArtifact.chapter_idx))).scalars().all()
        assert [art.status for art in arts] == ["failed", "pending"]
        assert arts[0].audio_filename is None


@pytest.mark.asyncio
async def test_remote_retry_reuses_source_without_retaining_local_mp3(_isolate_data_dir, monkeypatch):
    from backend.app.core import config
    from backend.app.core.config import settings
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.db.session import get_session_factory
    from backend.app.services import build

    remote = _MemoryObjectStorage()
    monkeypatch.setattr(build, "get_storage", lambda: remote)
    monkeypatch.setattr(settings, "STORAGE_CLEANUP_LOCAL", True)
    monkeypatch.setattr(config, "get_active", lambda _kind: ("doubao", "volcano_tts", {}))

    async def synth(*_args, **_kwargs):
        return make_silent_mp3(180, sample_rate=24000), 180

    async def voices(*_args):
        return "female-tianmei", "male-qingnian", "female-tianmei-2", {
            "female-tianmei", "male-qingnian", "female-tianmei-2",
        }

    monkeypatch.setattr(build, "get_tts", lambda _provider: SimpleNamespace(
        synthesize_to_bytes=synth,
    ))
    monkeypatch.setattr(build, "_resolve_build_voice_defaults", voices)
    project_id, source_id = await _seed_build()
    await build._run_build_inner(source_id, project_id, {}, "female-tianmei")
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(Build, source_id)
        row.status = "partial_success"
        row.failed_chapters_json = "[1]"
        art = (await session.execute(select(BuildArtifact).where(
            BuildArtifact.build_id == source_id,
            BuildArtifact.chapter_idx == 1,
        ))).scalar_one()
        art.status = "failed"
        await session.commit()

    retry = await build.retry_failed_build(source_id)
    for _ in range(100):
        async with factory() as session:
            row = await session.get(Build, retry.build_id)
            if row.status in ("success", "partial_success", "failed"):
                break
        await asyncio.sleep(0.05)
    assert row.status == "success", row.progress_msg
    assert not list(Path(settings.AUDIO_DIR).glob(f"build_{retry.build_id}_*.mp3"))
    assert not list(Path(settings.AUDIO_DIR).glob(f"build_{retry.build_id}_*.zip"))
    assert any(key.startswith(f"{project_id}/{retry.build_id}/") and key.endswith(".zip")
               for key in remote.objects)


@pytest.mark.asyncio
async def test_remote_zip_failure_can_retry_packaging_without_new_tts(_isolate_data_dir, monkeypatch):
    from backend.app.core import config
    from backend.app.core.config import settings
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.db.models import Build
    from backend.app.db.session import get_session_factory
    from backend.app.services import build

    remote = _MemoryObjectStorage(fail_zip=True)
    monkeypatch.setattr(build, "get_storage", lambda: remote)
    monkeypatch.setattr(settings, "STORAGE_CLEANUP_LOCAL", True)
    monkeypatch.setattr(config, "get_active", lambda _kind: ("doubao", "volcano_tts", {}))
    calls = 0

    async def synth(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return make_silent_mp3(180, sample_rate=24000), 180

    async def voices(*_args):
        return "female-tianmei", "male-qingnian", "female-tianmei-2", {
            "female-tianmei", "male-qingnian", "female-tianmei-2",
        }

    monkeypatch.setattr(build, "get_tts", lambda _provider: SimpleNamespace(
        synthesize_to_bytes=synth,
    ))
    monkeypatch.setattr(build, "_resolve_build_voice_defaults", voices)
    project_id, source_id = await _seed_build()
    with pytest.raises(build._RemoteArchiveError):
        await build._run_build_inner(source_id, project_id, {}, "female-tianmei")
    initial_calls = calls
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(Build, source_id)
        row.status = "failed"  # 生产 worker 的异常终态写入
        await session.commit()
    remote.fail_zip = False

    retry = await build.retry_failed_build(source_id)
    for _ in range(100):
        async with factory() as session:
            row = await session.get(Build, retry.build_id)
            if row.status in ("success", "partial_success", "failed"):
                break
        await asyncio.sleep(0.05)
    assert row.status == "success", row.progress_msg
    assert calls == initial_calls
    assert any(key.startswith(f"{project_id}/{retry.build_id}/") and key.endswith(".zip")
               for key in remote.objects)

