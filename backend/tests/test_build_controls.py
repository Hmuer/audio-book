from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from backend.app.ai import factory as ai_factory
from backend.app.core.config import settings
from backend.app.db.models import (
    Build,
    BuildArtifact,
    JobTask,
    Project,
    ProjectCharacter,
)
from backend.app.db.session import get_session_factory, init_db
from backend.app.services import build as builds
from backend.app.services.job_tasks import _recover_build_orphan, register_task
from backend.tests.mock_providers import MockTTSProvider


async def seed_build(status="queued", total=3, legacy=False):
    await init_db()
    pid, bid = "controls-project", "controls-build"
    async with get_session_factory()() as session:
        session.add(Project(
            project_id=pid, name="控制测试", status="ready",
            chapters_json=json.dumps([
                {"idx": i, "title": f"第{i + 1}章", "text": f"独特的章节内容{i}。"}
                for i in range(total)
            ], ensure_ascii=False),
        ))
        await session.commit()
        b = Build(
            build_id=bid, project_id=pid, status=status, total_chapters=total,
            narrator_voice_id="doubao:zh_female_qingxin", speed=1.0,
            mode="classic", tts_provider="doubao", voice_assignments_json="{}",
            narrator_emotion="", narrator_instruction="",
        )
        if not legacy:
            b.resume_digest = await builds._calc_resume_digest(session, b)
        session.add(b)
        for i in range(total):
            session.add(BuildArtifact(build_id=bid, chapter_idx=i, title=f"第{i + 1}章", status="pending"))
        await session.commit()
    return pid, bid


async def row(bid):
    async with get_session_factory()() as session:
        return await session.get(Build, bid)


async def wait_status(bid, status):
    async with asyncio.timeout(5):
        while (await row(bid)).status != status:
            await asyncio.sleep(0.01)
    return await row(bid)


class GatedTTS(MockTTSProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def synthesize_to_bytes(self, text, voice_id, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return await super().synthesize_to_bytes(text, voice_id, **kwargs)


@pytest.mark.asyncio
async def test_pause_before_start_and_resume_reuses_identity(monkeypatch):
    pid, bid = await seed_build()
    await builds._register_active_build(bid, pid)
    response = await builds.pause_build(pid, bid)
    assert response.status == "pausing"
    await builds._run_build_runner(bid, pid, {}, "doubao:zh_female_qingxin", 1.0)
    assert (await row(bid)).status == "paused"
    assert bid not in builds._ACTIVE_BUILDS
    assert (await builds.pause_build(pid, bid)).status == "paused"
    async with get_session_factory()() as session:
        jobs = (await session.execute(select(JobTask))).scalars().all()
        assert [j.status for j in jobs] == ["paused"]
    responses = await asyncio.gather(*(builds.resume_build(pid, bid) for _ in range(4)))
    assert {r.build_id for r in responses} == {bid}
    await wait_status(bid, "success")
    async with get_session_factory()() as session:
        jobs = (await session.execute(select(JobTask))).scalars().all()
        assert len(jobs) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_control_waits_for_chapter_and_usage_accumulates(monkeypatch, stop):
    pid, bid = await seed_build()
    provider = GatedTTS()
    monkeypatch.setattr(ai_factory, "_tts_instance", provider)
    await builds._register_active_build(bid, pid)
    task = asyncio.create_task(builds._run_build_runner(bid, pid, {}, "doubao:zh_female_qingxin", 1.0))
    await asyncio.wait_for(provider.entered.wait(), 3)
    assert (await builds.pause_build(pid, bid)).status == "pausing"
    assert bid in builds._ACTIVE_BUILDS
    assert not task.done()
    if stop:
        assert (await builds.stop_build(pid, bid)).status == "stopping"
        assert (await builds.stop_build(pid, bid)).status == "stopping"
    calls_before = provider.calls
    provider.release.set()
    await task
    b = await row(bid)
    assert b.status == ("cancelled" if stop else "paused")
    assert b.completed_chapters == 1
    assert provider.calls == calls_before
    assert b.tts_calls == calls_before
    assert b.tts_chars > 0
    assert bid not in builds._ACTIVE_BUILDS
    async with get_session_factory()() as session:
        artifacts = (await session.execute(select(BuildArtifact).order_by(BuildArtifact.chapter_idx))).scalars().all()
        assert [a.status for a in artifacts] == ["done", "pending", "pending"]
        first = settings.AUDIO_DIR / artifacts[0].audio_filename
        first_bytes = first.read_bytes()
    if stop:
        with pytest.raises(builds.BuildControlConflict):
            await builds.resume_build(pid, bid)
        assert (await builds.stop_build(pid, bid)).status == "cancelled"
    else:
        old_calls, old_chars = b.tts_calls, b.tts_chars
        await builds.resume_build(pid, bid)
        done = await wait_status(bid, "success")
        assert done.tts_calls > old_calls
        assert done.tts_calls == provider.calls
        assert done.tts_chars > old_chars
    assert first.read_bytes() == first_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "gender", "model", "voice", "speed", "style", "default"])
async def test_resume_fingerprint_rejects_changes(monkeypatch, change):
    pid, bid = await seed_build(status="paused")
    async with get_session_factory()() as session:
        b = await session.get(Build, bid)
        if change == "content":
            p = await session.get(Project, pid)
            p.chapters_json = p.chapters_json.replace("独特", "变更")
        elif change == "gender":
            session.add(ProjectCharacter(project_id=pid, name="小明", gender="male"))
        elif change == "model":
            monkeypatch.setattr(settings, "DOUBAO_TTS_MODEL", "changed-model")
        elif change == "voice":
            b.voice_assignments_json = '{"小明":"doubao:another"}'
        elif change == "speed":
            b.speed = 1.2
        elif change == "style":
            b.narrator_instruction = "不同风格"
        else:
            b.default_male_voice_id = "doubao:another"
        await session.commit()
    with pytest.raises(builds.BuildControlConflict, match="变化"):
        await builds.resume_build(pid, bid)
    assert (await row(bid)).status == "paused"
    assert bid not in builds._ACTIVE_BUILDS


@pytest.mark.asyncio
async def test_paused_protects_content_start_retry_delete():
    pid, bid = await seed_build(status="paused")
    from backend.app.services.project import (
        import_file,
        trigger_prepare_project,
        update_dialogue_speaker,
    )
    from backend.app.services.pronunciation import (
        PronunciationRuleInput,
        create_pronunciation_rule,
    )
    actions = [
        lambda: builds.start_build(pid, {}, "doubao:zh_female_qingxin"),
        lambda: builds.retry_failed_build(bid),
        lambda: builds.delete_build(pid, bid),
        lambda: import_file(pid, b"changed", "book.txt"),
        lambda: update_dialogue_speaker(pid, 123, "新角色"),
        lambda: trigger_prepare_project(pid),
        lambda: create_pronunciation_rule(pid, PronunciationRuleInput(pattern="a", replacement="b")),
    ]
    for action in actions:
        with pytest.raises(builds.BuildControlConflict):
            await action()
    from backend.app.services.project import get_project, list_projects
    assert (await get_project(pid)).status == "paused"
    assert (await list_projects(admin_view=True))[0].status == "paused"
    assert (await builds.stop_build(pid, bid)).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,target", [("pausing", "paused"), ("stopping", "cancelled"), ("paused", "paused")])
async def test_watchdog_confirms_control_without_restart(status, target):
    _pid, bid = await seed_build(status=status)
    job = await register_task("build", bid)
    assert not await _recover_build_orphan(job)
    assert (await row(bid)).status == target
    assert bid not in builds._ACTIVE_BUILDS
    async with get_session_factory()() as session:
        assert (await session.get(JobTask, job.task_id)).status == target


@pytest.mark.asyncio
async def test_legacy_fingerprint_is_saved_on_first_pause():
    pid, bid = await seed_build(legacy=True)
    assert (await row(bid)).resume_digest is None
    assert (await builds.pause_build(pid, bid)).status == "paused"
    assert (await row(bid)).resume_digest


@pytest.mark.asyncio
async def test_pause_during_zip_finishes_only_current_shard(monkeypatch):
    pid, bid = await seed_build(total=3)
    monkeypatch.setattr(settings, "ZIP_SHARD_CHAPTERS", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = builds._archive_file
    archives = []

    async def archive(key, path, content_type):
        if content_type == "application/zip":
            archives.append(path.name)
            entered.set()
            await release.wait()
        return await original(key, path, content_type)

    monkeypatch.setattr(builds, "_archive_file", archive)
    await builds._register_active_build(bid, pid)
    task = asyncio.create_task(builds._run_build_runner(bid, pid, {}, "doubao:zh_female_qingxin", 1.0))
    await asyncio.wait_for(entered.wait(), 3)
    await builds.pause_build(pid, bid)
    release.set()
    await task
    b = await row(bid)
    assert b.status == "paused"
    assert b.completed_chapters == 3
    assert len(archives) == 1
    assert len(json.loads(b.zip_filenames_json)) == 1
    calls = b.tts_calls
    first_shard = settings.AUDIO_DIR / archives[0]
    first_bytes = first_shard.read_bytes()
    packed = []
    original_pack = builds._build_book_zip

    def pack(path, **kwargs):
        packed.append(path)
        return original_pack(path, **kwargs)

    monkeypatch.setattr(builds, "_build_book_zip", pack)
    await builds.resume_build(pid, bid)
    done = await wait_status(bid, "success")
    assert done.tts_calls == calls
    assert len(json.loads(done.zip_filenames_json)) == 3
    assert len(packed) == 2
    assert str(first_shard) not in packed
    assert first_shard.read_bytes() == first_bytes


@pytest.mark.asyncio
async def test_failed_chapter_keeps_failure_on_resume(monkeypatch):
    pid, bid = await seed_build(status="paused")
    failed_file = settings.AUDIO_DIR / "failed-chapter.mp3"
    failed_file.write_bytes(b"failure-product")
    async with get_session_factory()() as session:
        await session.execute(update(BuildArtifact).where(BuildArtifact.chapter_idx == 0).values(
            status="failed", audio_filename=failed_file.name, error_msg="original failure", duration_ms=1000,
        ))
        await session.commit()
    await builds.resume_build(pid, bid)
    await wait_status(bid, "partial_success")
    async with get_session_factory()() as session:
        art = await session.scalar(select(BuildArtifact).where(BuildArtifact.chapter_idx == 0))
        assert art.status == "failed"
        assert art.error_msg == "original failure"
    assert failed_file.read_bytes() == b"failure-product"


@pytest.mark.asyncio
async def test_http_control_not_found_conflict_and_permissions(admin_token):
    pid, bid = await seed_build(status="paused")
    from backend.app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {admin_token}"
        for action in ("pause", "resume", "stop"):
            assert (await client.post(f"/api/projects/unknown/builds/{bid}/{action}")).status_code == 404
            assert (await client.post(f"/api/projects/{pid}/builds/unknown/{action}")).status_code == 404
        assert (await client.post(f"/api/projects/{pid}/builds/{bid}/pause")).status_code == 200
        assert (await client.post(f"/api/projects/{pid}/builds/{bid}/stop")).status_code == 200
        assert (await client.post(f"/api/projects/{pid}/builds/{bid}/resume")).status_code == 409
        assert (await client.post(f"/api/projects/{pid}/builds/{bid}/pause")).status_code == 409
        client.headers.clear()
        assert (await client.post(f"/api/projects/{pid}/builds/{bid}/stop")).status_code == 401


@pytest.mark.asyncio
async def test_retry_persists_explicit_target_and_source(monkeypatch):
    _pid, bid = await seed_build(status="failed")
    async with get_session_factory()() as session:
        b = await session.get(Build, bid)
        b.failed_chapters_json = "[0]"
        await session.commit()

    launched = []

    async def record_runner(*args, **kwargs):
        launched.append((args, kwargs))

    monkeypatch.setattr(builds, "_run_build_runner", record_runner)
    result = await builds.retry_failed_build(bid, chapters=[2])
    await asyncio.sleep(0)
    async with get_session_factory()() as session:
        retry = await session.get(Build, result.build_id)
        assert retry.is_retry
        assert json.loads(retry.retry_chapters_json) == [0, 2]
        assert retry.retry_source_build_id == bid
    assert launched[0][1]["only_chapter_idxs"] == [0, 2]


@pytest.mark.asyncio
async def test_retry_recovery_keeps_target_chapters(monkeypatch):
    _pid, bid = await seed_build(status="running")
    async with get_session_factory()() as session:
        b = await session.get(Build, bid)
        b.is_retry = True
        b.retry_chapters_json = "[1]"
        b.retry_source_build_id = "source-build"
        await session.commit()

    captured = {}

    async def record_inner(**kwargs):
        captured.update(kwargs)
        async with get_session_factory()() as session:
            retry = await session.get(Build, kwargs["build_id"])
            captured["retry_chapters"] = json.loads(retry.retry_chapters_json)
            captured["source_build"] = retry.retry_source_build_id
        raise RuntimeError("stop before synthesis")

    monkeypatch.setattr(builds, "_run_build_inner", record_inner)
    job = await register_task("build", bid)
    assert await _recover_build_orphan(job)
    await wait_status(bid, "failed")
    assert captured["build_id"] == bid
    assert captured["retry_chapters"] == [1]
    assert captured["source_build"] == "source-build"


@pytest.mark.asyncio
async def test_retry_missing_snapshot_fails_closed(monkeypatch):
    pid, bid = await seed_build(status="queued")
    async with get_session_factory()() as session:
        b = await session.get(Build, bid)
        b.is_retry = True
        await session.commit()
    with pytest.raises(RuntimeError, match="重试 Build"):
        await builds._run_build_inner(bid, pid, {}, "doubao:zh_female_qingxin")


@pytest.mark.asyncio
async def test_cache_key_distinguishes_precise_speeds():
    assert builds._seg_cache_key("voice", 1.001, "text") != builds._seg_cache_key("voice", 1.004, "text")
    assert builds._seg_cache_key("voice", 1.001, "text") == builds._seg_cache_key("voice", 1.001, "text")


@pytest.mark.asyncio
async def test_resume_digest_legacy_migration():
    import sqlite3

    path = settings.DATABASE_URL.split("///")[-1]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE builds (build_id VARCHAR(64) PRIMARY KEY)")
        conn.execute("INSERT INTO builds VALUES ('legacy')")
    await init_db()
    await init_db()
    with sqlite3.connect(path) as conn:
        column = next(r for r in conn.execute("PRAGMA table_info(builds)") if r[1] == "resume_digest")
        assert column[3] == 0
        assert column[4] is None
        assert conn.execute("SELECT resume_digest FROM builds").fetchone() == (None,)


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
async def test_done_missing_file_restores_before_synthesis(monkeypatch, restored):
    pid, bid = await seed_build(status="paused", total=1)
    provider = GatedTTS()
    provider.release.set()
    monkeypatch.setattr(ai_factory, "_tts_instance", provider)
    async with get_session_factory()() as session:
        await session.execute(update(BuildArtifact).values(
            status="done", audio_filename="missing.mp3", duration_ms=1000,
        ))
        await session.commit()
    requests = []

    async def restore(project_id, build_id, filename, target):
        requests.append((project_id, build_id, filename))
        if restored:
            target.write_bytes(builds.make_silent_mp3(1000))
        return restored

    monkeypatch.setattr(builds, "_restore_from_object_storage", restore)
    await builds.resume_build(pid, bid)
    await wait_status(bid, "success")
    assert requests == [(pid, bid, "missing.mp3")]
    assert bool(provider.calls) is not restored


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["retry", "recover"])
async def test_retry_and_recover_pause_resume_usage(monkeypatch, path):
    pid, bid = await seed_build(status="failed" if path == "retry" else "running")
    async with get_session_factory()() as session:
        b = await session.get(Build, bid)
        b.tts_calls = 5
        b.tts_chars = 50
        if path == "retry":
            b.failed_chapters_json = "[0,1,2]"
        await session.commit()
    provider = GatedTTS()
    monkeypatch.setattr(ai_factory, "_tts_instance", provider)
    if path == "retry":
        bid = (await builds.retry_failed_build(bid)).build_id
    else:
        job = await register_task("build", bid)
        assert await _recover_build_orphan(job)
    await asyncio.wait_for(provider.entered.wait(), 3)
    await builds.pause_build(pid, bid)
    provider.release.set()
    paused = await wait_status(bid, "paused")
    assert bid not in builds._ACTIVE_BUILDS
    base = 5 if path == "recover" else 0
    assert paused.tts_calls == base + provider.calls
    await builds.resume_build(pid, bid)
    done = await wait_status(bid, "success")
    assert done.tts_calls == base + provider.calls
    assert done.tts_chars > paused.tts_chars


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pausing", "stopping", "cancelled"])
async def test_late_worker_error_does_not_override_control(monkeypatch, status):
    pid, bid = await seed_build(status=status)
    await builds._register_active_build(bid, pid)

    async def fail(*args, **kwargs):
        raise ValueError("late failure")

    monkeypatch.setattr(builds, "_run_build_inner", fail)
    await builds._run_build_runner(bid, pid, {}, "doubao:zh_female_qingxin", 1.0)
    expected = "paused" if status == "pausing" else "cancelled"
    assert (await row(bid)).status == expected
    assert bid not in builds._ACTIVE_BUILDS


@pytest.mark.asyncio
async def test_edits_share_project_lock_with_control():
    from backend.app.services.project import import_text, update_project

    pid, bid = await seed_build(status="paused")
    lock = builds._START_LOCKS.setdefault(pid, asyncio.Lock())
    async with lock:
        tasks = [asyncio.create_task(update_project(pid, name="changed")),
                 asyncio.create_task(import_text(pid, "changed"))]
        await asyncio.sleep(0.02)
        assert all(not task.done() for task in tasks)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, builds.BuildControlConflict) for result in results)
    assert (await row(bid)).status == "paused"


@pytest.mark.asyncio
async def test_cross_project_and_non_owner_controls(admin_token):
    from backend.app.db.models import User
    from backend.app.main import app
    from backend.app.services.auth import create_access_token, hash_password

    pid, bid = await seed_build(status="paused")
    async with get_session_factory()() as session:
        session.add(Project(project_id="other-project", name="other", status="ready"))
        session.add(User(username="controls-user", password_hash=hash_password("test-password"), is_active=True))
        await session.commit()
    token, _ = create_access_token("controls-user")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for action in ("pause", "resume", "stop"):
            response = await client.post(f"/api/projects/other-project/builds/{bid}/{action}",
                                         headers={"Authorization": f"Bearer {admin_token}"})
            assert response.status_code == 404
            response = await client.post(f"/api/projects/{pid}/builds/{bid}/{action}",
                                         headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 403
    assert (await row(bid)).status == "paused"


@pytest.mark.asyncio
async def test_pause_inside_zip_thread(monkeypatch):
    import threading

    pid, bid = await seed_build(total=2)
    monkeypatch.setattr(settings, "ZIP_SHARD_CHAPTERS", 1)
    entered, release = threading.Event(), threading.Event()
    original = builds._build_book_zip
    calls = []

    def pack(*args, **kwargs):
        calls.append(args[0])
        entered.set()
        if not release.wait(5):
            raise TimeoutError("zip gate")
        return original(*args, **kwargs)

    monkeypatch.setattr(builds, "_build_book_zip", pack)
    await builds._register_active_build(bid, pid)
    task = asyncio.create_task(builds._run_build_runner(bid, pid, {}, "doubao:zh_female_qingxin", 1.0))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert (await builds.pause_build(pid, bid)).status == "pausing"
        assert not task.done()
    finally:
        release.set()
        await task
    assert (await row(bid)).status == "paused"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_pause_at_lrc_boundary_skips_packaging(monkeypatch):
    from backend.app.services import subtitles

    pid, bid = await seed_build(total=1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = subtitles.generate_chapters_lrc

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(subtitles, "generate_chapters_lrc", generate)
    await builds._register_active_build(bid, pid)
    task = asyncio.create_task(builds._run_build_runner(bid, pid, {}, "doubao:zh_female_qingxin", 1.0))
    await asyncio.wait_for(entered.wait(), 3)
    await builds.pause_build(pid, bid)
    release.set()
    await task
    paused = await row(bid)
    assert paused.status == "paused"
    assert paused.completed_chapters == 1
    assert not paused.zip_filename


@pytest.mark.parametrize("failure", ["write", "close", "replace", None])
def test_zip_replacement_preserves_old_until_success(monkeypatch, tmp_path, failure):
    path = tmp_path / "existing.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("old.mp3", b"old audio")
    old_bytes = path.read_bytes()
    original_write = zipfile.ZipFile.writestr
    original_close = zipfile.ZipFile.close
    original_replace = builds.os.replace

    def write(zf, *args, **kwargs):
        assert path.read_bytes() == old_bytes
        original_write(zf, *args, **kwargs)
        if failure == "write":
            raise OSError("write failure")

    def close(zf):
        writing = zf.mode == "w" and zf.fp is not None
        original_close(zf)
        if writing and failure == "close":
            raise OSError("close failure")

    def replace(src, dest):
        assert path.read_bytes() == old_bytes
        assert Path(src).parent == path.parent
        with zipfile.ZipFile(src) as zf:
            assert zf.testzip() is None
        if failure == "replace":
            raise OSError("replace failure")
        return original_replace(src, dest)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", write)
    monkeypatch.setattr(zipfile.ZipFile, "close", close)
    monkeypatch.setattr(builds.os, "replace", replace)
    kwargs = {"job_id": "book", "job_title": "book", "chapter_outputs": [(None, 100)], "chapter_titles": ["chapter"]}
    if failure:
        with pytest.raises(OSError, match=f"{failure} failure"):
            builds._build_book_zip(str(path), **kwargs)
        assert path.read_bytes() == old_bytes
    else:
        builds._build_book_zip(str(path), **kwargs)
        assert path.read_bytes() != old_bytes
    assert [entry for entry in tmp_path.iterdir() if entry.is_file()] == [path]
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", ["valid", "missing", "corrupt", "wrong_size", "regenerated"])
async def test_resume_reuses_only_valid_unchanged_shards(monkeypatch, existing):
    pid, bid = await seed_build(status="paused", total=1)
    audio_path = settings.AUDIO_DIR / "completed.mp3"
    audio_path.write_bytes(builds.make_silent_mp3(1000))
    zip_path = settings.AUDIO_DIR / builds._zip_filename(bid)
    builds._build_book_zip(
        str(zip_path), job_id=bid, job_title=None,
        chapter_outputs=[(str(audio_path), 1000)], chapter_titles=["第1章"],
    )
    size = zip_path.stat().st_size
    if existing == "missing":
        zip_path.unlink()
    elif existing == "corrupt":
        zip_path.write_bytes(b"broken zip")
    elif existing == "regenerated":
        audio_path.unlink()
    async with get_session_factory()() as session:
        await session.execute(update(BuildArtifact).values(
            status="done", audio_filename=audio_path.name, duration_ms=1000,
        ))
        b = await session.get(Build, bid)
        b.zip_filename = zip_path.name
        b.zip_filenames_json = json.dumps([{
            "filename": zip_path.name, "start": 0, "end": 0,
            "size_bytes": size + 1 if existing == "wrong_size" else size,
        }])
        await session.commit()
    packed = []
    original = builds._build_book_zip

    def pack(path, **kwargs):
        packed.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(builds, "_build_book_zip", pack)
    await builds.resume_build(pid, bid)
    await wait_status(bid, "success")
    assert len(packed) == (0 if existing == "valid" else 1)
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None


@pytest.mark.asyncio
async def test_resume_packaging_failure_keeps_previous_zip(monkeypatch):
    pid, bid = await seed_build(status="paused", total=1)
    zip_path = settings.AUDIO_DIR / builds._zip_filename(bid)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("previous.mp3", b"previous audio")
    previous = zip_path.read_bytes()
    async with get_session_factory()() as session:
        b = await session.get(Build, bid)
        b.zip_filename = zip_path.name
        await session.commit()
    original = zipfile.ZipFile.write

    def write(zf, *args, **kwargs):
        original(zf, *args, **kwargs)
        raise OSError("packaging failure")

    monkeypatch.setattr(zipfile.ZipFile, "write", write)
    await builds.resume_build(pid, bid)
    failed = await wait_status(bid, "failed")
    assert failed.zip_filename == zip_path.name
    assert zip_path.read_bytes() == previous
    assert not list(settings.AUDIO_DIR.glob(".*.tmp"))
