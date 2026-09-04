"""
P1 #6 — 媒体签名 URL（一次性 + 资源绑定 + 短时）回归测试。

覆盖：
- /api/media/sign 签发一次性 token 并校验资源归属；
- token 消费一次后立刻失效；
- token TTL 过期后无法使用；
- 跨用户签发 → 403；
- /api/media/stream 不接受完整登录 JWT（sub_kind=access 会被拒）。
"""
from __future__ import annotations

import asyncio
import time as _time
import uuid

import jwt
import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


@pytest_asyncio.fixture
async def project_with_build(_isolate_data_dir):
    """创建一个 admin 拥有的项目，并写一个 build + 一个 done artifact。
    返回 (project_id, build_id, chapter_idx=0, admin_token)。"""
    from sqlalchemy import select
    from backend.app.db.models import (
        Project, Build, BuildArtifact, User,
    )
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    async with factory() as s:
        pid = uuid.uuid4().hex
        bid = uuid.uuid4().hex
        admin = (
            await s.execute(select(User).where(User.username == "admin"))
        ).scalar_one()
        s.add(Project(project_id=pid, name="Test", status="imported", owner_user_id=admin.id))
        s.add(Build(
            build_id=bid,
            project_id=pid,
            status="success",
            total_chapters=1,
            completed_chapters=1,
            narrator_voice_id="",
            speed=1.0,
            mode="classic",
            tts_provider="minimax",
            zip_filename=f"build_{bid}_all.zip",
        ))
        s.add(BuildArtifact(
            build_id=bid,
            chapter_idx=0,
            title="ch0",
            status="done",
            audio_filename=f"build_{bid}_ch0000.mp3",
        ))
        await s.commit()
        # 写一个真实的 MP3 文件占位，让 /api/media/stream 找到它
        from backend.app.core import config as cfg
        cfg.settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        (cfg.settings.AUDIO_DIR / f"build_{bid}_ch0000.mp3").write_bytes(b"ID3" + b"\x00" * 100)
        (cfg.settings.AUDIO_DIR / f"build_{bid}_all.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    admin_token, _ = create_access_token("admin")
    return pid, bid, 0, admin_token


@pytest.mark.asyncio
async def test_sign_returns_url_and_consume_works(project_with_build):
    """签发一次性 token，stream 接口能消费并返回音频。"""
    from backend.app.main import app
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.db.session import init_db

    pid, bid, idx = project_with_build[:3]
    tok = project_with_build[3]
    await init_db()
    await seed_admin_user()

    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 签发
        r = await client.get(
            f"/api/media/sign?build_id={bid}&kind=chapter_mp3&idx={idx}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"]
        assert body["url"].startswith("/api/media/stream?token=")
        assert body["expires_at"]

        # stream
        r2 = await client.get(body["url"])
        assert r2.status_code == 200, r2.text
        assert r2.headers["content-type"].startswith("audio/mpeg")


@pytest.mark.asyncio
async def test_token_is_single_use(project_with_build):
    """一次性 token：消费一次后立刻失效。"""
    from backend.app.main import app
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    pid, bid, idx = project_with_build[:3]
    tok = project_with_build[3]
    await init_db()
    await seed_admin_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            f"/api/media/sign?build_id={bid}&kind=chapter_mp3&idx={idx}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        url = r.json()["url"]
        r2 = await client.get(url)
        assert r2.status_code == 200
        # 第二次必须 401
        r3 = await client.get(url)
        assert r3.status_code == 401


@pytest.mark.asyncio
async def test_stream_rejects_full_login_jwt(project_with_build):
    """/api/media/stream 不接受 sub_kind=access 的完整登录 JWT（必须有 sub_kind=media）。"""
    from backend.app.main import app
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    pid, bid, idx = project_with_build[:3]
    await init_db()
    await seed_admin_user()
    full_jwt, _ = create_access_token("admin")  # sub 不为 media
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/api/media/stream?token={full_jwt}")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_sign_for_other_users_build_returns_403(project_with_build):
    """Bob 不能签发 alice 拥有的 build 的媒体 URL。"""
    from sqlalchemy import select
    from backend.app.db.models import User, Project
    from backend.app.db.session import get_session_factory
    from backend.app.main import app
    from backend.app.services.auth import (
        seed_admin_user, create_access_token, hash_password,
    )
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    pid, bid, idx = project_with_build[:3]
    await init_db()
    await seed_admin_user()

    # 把 project 所有者从 admin 改成 alice；并建 bob 用户（让 get_current_user 能解析）
    factory = get_session_factory()
    async with factory() as s:
        alice_id = None
        bob_id = None
        existing_alice = (
            await s.execute(select(User).where(User.username == "alice"))
        ).scalar_one_or_none()
        if not existing_alice:
            u = User(
                username="alice",
                password_hash=hash_password("alice_pw"),
                is_active=True,
            )
            s.add(u)
            await s.flush()
            alice_id = u.id
        else:
            alice_id = existing_alice.id
        existing_bob = (
            await s.execute(select(User).where(User.username == "bob"))
        ).scalar_one_or_none()
        if not existing_bob:
            u = User(
                username="bob",
                password_hash=hash_password("bob_pw"),
                is_active=True,
            )
            s.add(u)
            await s.flush()
            bob_id = u.id
        else:
            bob_id = existing_bob.id
        p = await s.get(Project, pid)
        p.owner_user_id = alice_id
        await s.commit()

    bob_tok, _ = create_access_token("bob")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            f"/api/media/sign?build_id={bid}&kind=chapter_mp3&idx={idx}",
            headers={"Authorization": f"Bearer {bob_tok}"},
        )
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_token_expires():
    """手动签发一个 ttl=1 的 token，sleep 2 秒后 stream 必须 401。"""
    from backend.app.services.media_sign import (
        issue_media_token, consume_media_token,
    )
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.db.session import get_session_factory
    from sqlalchemy import select
    from backend.app.db.models import User, Project, Build

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    async with factory() as s:
        admin = (
            await s.execute(select(User).where(User.username == "admin"))
        ).scalar_one()
        pid = uuid.uuid4().hex
        bid = uuid.uuid4().hex
        s.add(Project(project_id=pid, name="x", status="imported", owner_user_id=admin.id))
        s.add(Build(
            build_id=bid, project_id=pid, status="success",
            total_chapters=1, completed_chapters=1,
            narrator_voice_id="", speed=1.0, mode="classic", tts_provider="minimax",
        ))
        await s.commit()
        info = await issue_media_token(
            s, build_id=bid, kind="chapter_mp3", chapter_idx=0,
            user_id=admin.id, ttl_seconds=1,
        )
    await asyncio.sleep(2)
    payload = await consume_media_token(info["token"])
    assert payload is None