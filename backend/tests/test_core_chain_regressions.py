"""导入状态与媒体归属的主链路回归测试。"""
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


@pytest.mark.asyncio
async def test_reimport_invalidates_prepared_content_and_preserves_history(db_session):
    from backend.app.db.models import (
        Build, Project, ProjectChapter, ProjectCharacter, ProjectDialogue,
    )
    from backend.app.services.build import start_build
    from backend.app.services.project import create_project, get_project, import_file, import_text

    pid = (await create_project("导入替换")).project_id
    await import_file(pid, "第一章 旧内容".encode(), "old.txt")
    p = await db_session.get(Project, pid)
    old_path = Path(p.source_file_path)
    p.status = "ready"
    p.chapter_count = 1
    p.chapters_json = json.dumps([{"idx": 0, "title": "旧", "text": "旧内容"}])
    p.progress_json = json.dumps({"stage": "done", "char_extract_raw_list": [{"name": "旧角色"}]})
    db_session.add_all([
        ProjectChapter(project_id=pid, idx=0, title="旧", text="旧内容", text_len=3),
        ProjectCharacter(project_id=pid, name="旧角色"),
        ProjectDialogue(project_id=pid, chapter_idx=0, speaker="旧角色", text="旧对白"),
        Build(build_id="history", project_id=pid, status="success"),
    ])
    await db_session.commit()

    result = await import_text(pid, "第二章 新内容", "new.txt")
    assert result.status == "imported" and result.chapter_count == 0
    await db_session.refresh(p)
    assert p.chapters_json is None and p.progress_json is None
    assert Path(p.source_file_path).read_text() == "第二章 新内容"
    assert not old_path.exists()
    detail = await get_project(pid)
    assert detail.chapters == [] and detail.characters == []
    for model in (ProjectChapter, ProjectCharacter, ProjectDialogue):
        rows = (await db_session.execute(select(model).where(model.project_id == pid))).scalars().all()
        assert rows == []
    assert await db_session.get(Build, "history") is not None
    with pytest.raises(RuntimeError, match="尚未完成识别"):
        await start_build(pid, {}, "doubao:voice")


@pytest.mark.asyncio
async def test_import_and_prepare_reject_active_work(db_session):
    from backend.app.db.models import Build, Project
    from backend.app.services.project import (
        create_project, import_file, import_text, trigger_prepare_project,
    )

    pid = (await create_project("冲突保护")).project_id
    await import_file(pid, b"old content", "old.txt")
    p = await db_session.get(Project, pid)
    old_path = p.source_file_path
    p.status = "preparing"
    await db_session.commit()
    with pytest.raises(RuntimeError, match="正在识别"):
        await import_text(pid, "new content")
    assert Path(old_path).read_bytes() == b"old content"

    p.status = "ready"
    db_session.add(Build(build_id="busy", project_id=pid, status="queued"))
    await db_session.commit()
    with pytest.raises(RuntimeError, match="正在合成"):
        await import_file(pid, b"new content", "new.txt")
    with pytest.raises(RuntimeError, match="正在合成"):
        await trigger_prepare_project(pid)
    assert p.source_file_path == old_path
    assert Path(old_path).read_bytes() == b"old content"


@pytest.mark.asyncio
async def test_shards_sidecars_and_previews_require_their_owner(db_session):
    from backend.app.core.config import settings
    from backend.app.db.models import Build, Project, User
    from backend.app.main import app
    from backend.app.services.auth import create_access_token, hash_password

    alice = User(username="media_alice", password_hash=hash_password("password"))
    bob = User(username="media_bob", password_hash=hash_password("password"))
    db_session.add_all([alice, bob])
    await db_session.flush()
    db_session.add(Project(project_id="media_project", name="私有书", status="done", owner_user_id=alice.id))
    db_session.add(Build(build_id="media123", project_id="media_project", status="success"))
    await db_session.commit()

    names = [
        "build_media123_ch0001-0050.zip",
        "build_media123_ch0000_timings.json",
        "build_media123_ch0000_preview.mp3",
        f"preview_u{alice.id}_{'a' * 32}.mp3",
        "preview_deadbeef.mp3",
    ]
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (audio_dir / name).write_bytes(b"private")

    alice_token, _ = create_access_token(alice.username)
    bob_token, _ = create_access_token(bob.username)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for name in names:
            response = await client.get(f"/media/{name}", headers={"Authorization": f"Bearer {bob_token}"})
            assert response.status_code == 403, (name, response.text)
        for name in names[:-1]:
            response = await client.get(f"/media/{name}", headers={"Authorization": f"Bearer {alice_token}"})
            assert response.status_code == 200, (name, response.text)


@pytest.mark.asyncio
async def test_tts_preview_requires_login(db_session):
    from backend.app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/tts/preview",
            json={"text": "试听", "voice_id": "doubao:test", "speed": 1.0},
        )
    assert response.status_code == 401
