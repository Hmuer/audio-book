"""
P1 #9 速率限制回归测试：
- /api/auth/login：每 IP 5/min，超额 429；
- /api/projects/{id}/prepare：每 (user, project) 3/min；
- /api/projects/{id}/builds：每 (user, project) 3/min；
- 不同 user / 不同 IP 各自独立计数。
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    from backend.app.services import rate_limit
    rate_limit.reset_all()
    yield
    rate_limit.reset_all()


@pytest_asyncio.fixture
async def admin_token(_isolate_data_dir):
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    await init_db()
    await seed_admin_user()
    token, _ = create_access_token("admin")
    return token


@pytest_asyncio.fixture
async def project_id(_isolate_data_dir):
    from sqlalchemy import select
    from backend.app.db.models import Project, User
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.auth import seed_admin_user
    await init_db()
    await seed_admin_user()
    pid = uuid.uuid4().hex
    factory = get_session_factory()
    async with factory() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(project_id=pid, name="P", status="imported", owner_user_id=admin.id))
        await s.commit()
    return pid


@pytest.mark.asyncio
async def test_login_rate_limit_returns_429_after_5_attempts():
    """同一 IP 第 6 次登录尝试必须 429。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user

    await init_db()
    await seed_admin_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 前 5 次都失败（密码错），但不抛 429
        for i in range(5):
            r = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
            )
            assert r.status_code in (401, 403), (i, r.status_code, r.text)
        # 第 6 次必须 429
        r6 = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert r6.status_code == 429, r6.text
        assert "Retry-After" in r6.headers


@pytest.mark.asyncio
async def test_login_rate_limit_different_ips_independent():
    """不同 IP（用 X-Forwarded-For 模拟）的登录限流独立。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user

    await init_db()
    await seed_admin_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # IP A 连刷 5 次 + 第 6 次 429
        for _ in range(5):
            r = await client.post(
                "/api/auth/login",
                headers={"X-Forwarded-For": "10.0.0.1"},
                json={"username": "admin", "password": "wrong"},
            )
            assert r.status_code in (401, 403), r.text
        r = await client.post(
            "/api/auth/login",
            headers={"X-Forwarded-For": "10.0.0.1"},
            json={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 429, r.text
        # IP B 第一次仍然正常（不是 429）
        r2 = await client.post(
            "/api/auth/login",
            headers={"X-Forwarded-For": "10.0.0.2"},
            json={"username": "admin", "password": "wrong"},
        )
        assert r2.status_code in (401, 403), r2.text


@pytest.mark.asyncio
async def test_prepare_rate_limit_returns_429_after_3_attempts(admin_token, project_id):
    """同一 (user, project) 第 4 次 prepare 触发必须 429。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 前 3 次：要么 202，要么 4xx 业务错（500 因 source_file 缺失 等），但绝不能 429
        for i in range(3):
            r = await client.post(
                f"/api/projects/{project_id}/prepare",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert r.status_code != 429, (i, r.status_code, r.text)
        # 第 4 次必须 429
        r4 = await client.post(
            f"/api/projects/{project_id}/prepare",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r4.status_code == 429, r4.text
        assert "Retry-After" in r4.headers


@pytest.mark.asyncio
async def test_build_rate_limit_returns_429_after_3_attempts(admin_token, project_id):
    """同一 (user, project) 第 4 次 start_build 必须 429。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(3):
            r = await client.post(
                f"/api/projects/{project_id}/builds",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "narrator_voice_id": "minimax:male-qn-jingying",
                    "speed": 1.0,
                    "voice_assignments": {},
                },
            )
            assert r.status_code != 429, (i, r.status_code, r.text)
        r4 = await client.post(
            f"/api/projects/{project_id}/builds",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "narrator_voice_id": "minimax:male-qn-jingying",
                "speed": 1.0,
                "voice_assignments": {},
            },
        )
        assert r4.status_code == 429, r4.text


@pytest.mark.asyncio
async def test_prepare_and_build_have_separate_buckets(admin_token, project_id):
    """prepare 限流桶和 build 限流桶互不影响。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 用尽 prepare 桶（3 次 + 第 4 次 429）
        for _ in range(3):
            await client.post(
                f"/api/projects/{project_id}/prepare",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        r_prep = await client.post(
            f"/api/projects/{project_id}/prepare",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_prep.status_code == 429
        # build 桶仍然是空的 → 第一次不 429
        r_build = await client.post(
            f"/api/projects/{project_id}/builds",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "narrator_voice_id": "minimax:male-qn-jingying",
                "speed": 1.0,
                "voice_assignments": {},
            },
        )
        assert r_build.status_code != 429, r_build.text


@pytest.mark.asyncio
async def test_rate_limit_does_not_affect_other_users(project_id):
    """不同用户对同一项目的 prepare 触发各自计数。"""
    from sqlalchemy import select
    from backend.app.db.models import User
    from backend.app.db.session import get_session_factory
    from backend.app.services.auth import (
        seed_admin_user, create_access_token, hash_password,
    )
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    # 建 bob 用户 + 把项目给 bob
    async with factory() as s:
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
        # 改 owner
        from backend.app.db.models import Project
        proj = await s.get(Project, project_id)
        proj.owner_user_id = bob_id
        await s.commit()

    bob_token, _ = create_access_token("bob")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # bob 触发 3 次 prepare
        for _ in range(3):
            await client.post(
                f"/api/projects/{project_id}/prepare",
                headers={"Authorization": f"Bearer {bob_token}"},
            )
        # 第 4 次 429
        r_bob = await client.post(
            f"/api/projects/{project_id}/prepare",
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert r_bob.status_code == 429
        # admin 不受影响
        admin_token, _ = create_access_token("admin")
        r_admin = await client.post(
            f"/api/projects/{project_id}/prepare",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        # admin 不是 owner → 进入业务层；由于项目没源文件会 400。
        # 关键断言：admin 的桶是独立的，没被 bob 用尽 → 不应出现 429
        assert r_admin.status_code != 429, r_admin.text
        # admin 走的路径不是 rate-limit 拒绝；这里接受业务错误码
        assert r_admin.status_code in (400, 403, 404, 422), r_admin.text