"""批次 7 F-5（批量签发章节音频 URL）回归测试。

覆盖：
  T-BS1  /chapter-signs?start&count 一次返回一段章节的签名 URL（避免逐章往返）
  T-BS2  返回的 URL 可直接喂给 /media/stream 取到对应章节的字节
  T-BS3  count 超上限被夹到 200（防止一次签发过多）
  T-BS4  没有音频产物的章节不出现在 items 里
  T-BS5  非归属项目 → 403（沿用既有读权限校验）
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


@pytest_asyncio.fixture
async def build_with_chapters(_isolate_data_dir, monkeypatch):
    """项目 + build + 3 个 done artifact（其中 idx=1 无 audio_filename）。"""
    from sqlalchemy import select
    from backend.app.db.models import Project, Build, BuildArtifact, User
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.core import config as cfg
    from backend.app.api import routes as _routes

    # ⚠️ patch 路由模块自己绑定的 settings（E-1：test_path_env_override_red 会 reload config）
    audio_dir = cfg.settings.AUDIO_DIR
    monkeypatch.setattr(_routes.settings, "AUDIO_DIR", audio_dir)

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    bid = uuid.uuid4().hex
    async with factory() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(project_id=pid, name="Test", status="done", owner_user_id=admin.id))
        s.add(Build(
            build_id=bid, project_id=pid, status="success",
            total_chapters=3, completed_chapters=2,
            narrator_voice_id="", speed=1.0, mode="classic", tts_provider="doubao",
        ))
        for idx in (0, 1, 2):
            s.add(BuildArtifact(
                build_id=bid, chapter_idx=idx, title=f"第{idx+1}章",
                status="done" if idx != 1 else "pending",
                # idx=1 故意没有音频产物
                audio_filename=None if idx == 1 else f"build_{bid}_ch{idx:04d}.mp3",
            ))
        await s.commit()

    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / f"build_{bid}_ch0000.mp3").write_bytes(b"ID3CH0")
    (audio_dir / f"build_{bid}_ch0002.mp3").write_bytes(b"ID3CH2")

    token, _ = create_access_token("admin")
    return pid, bid, token


async def _client():
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_bs1_signs_a_range_in_one_request(build_with_chapters):
    pid, bid, token = build_with_chapters
    async with await _client() as client:
        r = await client.get(
            f"/api/projects/{pid}/builds/{bid}/chapter-signs",
            params={"start": 0, "count": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["start"] == 0 and body["count"] == 3
        # idx=1 无音频 → 只回 0 与 2
        assert [it["chapter_idx"] for it in body["items"]] == [0, 2]
        for it in body["items"]:
            assert it["url"].startswith("/api/media/stream?token=")


@pytest.mark.asyncio
async def test_bs2_returned_url_is_usable(build_with_chapters):
    pid, bid, token = build_with_chapters
    async with await _client() as client:
        r = await client.get(
            f"/api/projects/{pid}/builds/{bid}/chapter-signs",
            params={"start": 2, "count": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert [it["chapter_idx"] for it in items] == [2]
        # 直接消费该 URL，应拿到第 2 章的字节
        r2 = await client.get(items[0]["url"])
        assert r2.status_code == 200, r2.text
        assert r2.content == b"ID3CH2"


@pytest.mark.asyncio
async def test_bs3_count_is_clamped(build_with_chapters):
    pid, bid, token = build_with_chapters
    async with await _client() as client:
        r = await client.get(
            f"/api/projects/{pid}/builds/{bid}/chapter-signs",
            params={"start": 0, "count": 100000},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        # 请求的 count 被夹到 200（响应里回显夹取后的值）
        assert r.json()["count"] == 200


@pytest.mark.asyncio
async def test_bs4_chapters_without_audio_are_omitted(build_with_chapters):
    pid, bid, token = build_with_chapters
    async with await _client() as client:
        r = await client.get(
            f"/api/projects/{pid}/builds/{bid}/chapter-signs",
            params={"start": 1, "count": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["items"] == [], "无音频产物的章节不应出现在 items 里"


@pytest.mark.asyncio
async def test_bs5_requires_project_ownership(build_with_chapters):
    """换个用户（非 owner 且非 admin）→ 拒绝。"""
    from backend.app.db.models import User
    from backend.app.db.session import get_session_factory
    from backend.app.services.auth import create_access_token

    pid, bid, _ = build_with_chapters
    factory = get_session_factory()
    async with factory() as s:
        other = User(username=f"u_{uuid.uuid4().hex[:8]}", password_hash="x", is_active=True)
        s.add(other)
        await s.commit()
        await s.refresh(other)
        other_username = other.username

    tok, _ = create_access_token(other_username)

    async with await _client() as client:
        r = await client.get(
            f"/api/projects/{pid}/builds/{bid}/chapter-signs",
            params={"start": 0, "count": 3},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code in (403, 404), r.text
