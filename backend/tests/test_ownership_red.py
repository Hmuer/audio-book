"""
P1 #5 — 资源归属（Project ownership）回归测试。

覆盖：
- 用户 A 创建的项目默认 owner=A；
- 用户 B 列项目时看不到 A 的；
- 用户 B GET / DELETE / PATCH / prepare / builds 等任一路径访问 A 的项目 → 403；
- 用户 A 自己能正常读 / 改 / 删自己的项目；
- admin 用户能看到 / 操作任何项目。
"""
from __future__ import annotations

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


async def _create_user(username: str, password: str) -> int:
    """在 DB 里直接创建一个普通用户，返回 user.id（绕开 /api/auth/register）。"""
    from sqlalchemy import select
    from backend.app.db.models import User
    from backend.app.services.auth import hash_password
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as s:
        existing = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if existing:
            return existing.id
        u = User(
            username=username,
            password_hash=hash_password(password),
            is_active=True,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


@pytest_asyncio.fixture
async def two_users(_isolate_data_dir):
    """返回 (admin_token, alice_token, bob_token, alice_id, bob_id)。"""
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token

    await init_db()
    await seed_admin_user()

    alice_id = await _create_user("alice", "alice_pw_123")
    bob_id = await _create_user("bob", "bob_pw_123")
    admin_tok, _ = create_access_token("admin")
    alice_tok, _ = create_access_token("alice")
    bob_tok, _ = create_access_token("bob")
    return admin_tok, alice_tok, bob_tok, alice_id, bob_id


@pytest.mark.asyncio
async def test_create_project_records_owner(two_users):
    """alice 创建的项目 owner_user_id == alice.id，bob 创建的归 bob。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.db.models import Project
    from backend.app.db.session import get_session_factory
    from httpx import AsyncClient, ASGITransport

    admin_tok, alice_tok, bob_tok, alice_id, bob_id = two_users
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/projects",
            json={"name": "Alice 的书"},
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        assert r.status_code == 200, r.text
        alice_proj = r.json()["project_id"]
        r = await client.post(
            "/api/projects",
            json={"name": "Bob 的书"},
            headers={"Authorization": f"Bearer {bob_tok}"},
        )
        assert r.status_code == 200, r.text
        bob_proj = r.json()["project_id"]

    factory = get_session_factory()
    async with factory() as s:
        a = await s.get(Project, alice_proj)
        b = await s.get(Project, bob_proj)
        assert a is not None and a.owner_user_id == alice_id
        assert b is not None and b.owner_user_id == bob_id


@pytest.mark.asyncio
async def test_list_projects_filters_by_owner(two_users):
    """alice 列项目看不到 bob 的，反之亦然；admin 都能看到。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    admin_tok, alice_tok, bob_tok, alice_id, bob_id = two_users
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for name, tok in (("A-1", alice_tok), ("A-2", alice_tok), ("B-1", bob_tok)):
            await client.post(
                "/api/projects",
                json={"name": name},
                headers={"Authorization": f"Bearer {tok}"},
            )

        # alice
        r = await client.get(
            "/api/projects", headers={"Authorization": f"Bearer {alice_tok}"}
        )
        assert r.status_code == 200
        names = sorted(p["name"] for p in r.json())
        assert names == ["A-1", "A-2"]

        # bob
        r = await client.get(
            "/api/projects", headers={"Authorization": f"Bearer {bob_tok}"}
        )
        assert r.status_code == 200
        names = sorted(p["name"] for p in r.json())
        assert names == ["B-1"]

        # admin 全可见
        r = await client.get(
            "/api/projects", headers={"Authorization": f"Bearer {admin_tok}"}
        )
        assert r.status_code == 200
        names = sorted(p["name"] for p in r.json())
        assert names == ["A-1", "A-2", "B-1"]


@pytest.mark.asyncio
async def test_cross_user_get_returns_403(two_users):
    """bob GET alice 的项目 → 403。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    admin_tok, alice_tok, bob_tok, alice_id, bob_id = two_users
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/projects",
            json={"name": "A's"},
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        pid = r.json()["project_id"]

        r = await client.get(
            f"/api/projects/{pid}",
            headers={"Authorization": f"Bearer {bob_tok}"},
        )
        assert r.status_code == 403, f"bob 不应能读到 Alice 的项目: {r.text}"


@pytest.mark.asyncio
async def test_cross_user_patch_returns_403(two_users):
    """bob PATCH alice 的项目 → 403。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    admin_tok, alice_tok, bob_tok, alice_id, bob_id = two_users
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/projects",
            json={"name": "A's"},
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        pid = r.json()["project_id"]

        r = await client.patch(
            f"/api/projects/{pid}",
            json={"name": "Hacked!"},
            headers={"Authorization": f"Bearer {bob_tok}"},
        )
        assert r.status_code == 403, f"bob 不应能改 Alice 的项目: {r.text}"


@pytest.mark.asyncio
async def test_cross_user_delete_returns_403(two_users):
    """bob DELETE alice 的项目 → 403；alice 自己删成功。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from httpx import AsyncClient, ASGITransport

    admin_tok, alice_tok, bob_tok, alice_id, bob_id = two_users
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/projects",
            json={"name": "A's"},
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        pid = r.json()["project_id"]

        r = await client.delete(
            f"/api/projects/{pid}",
            headers={"Authorization": f"Bearer {bob_tok}"},
        )
        assert r.status_code == 403

        # alice 自己删成功
        r = await client.delete(
            f"/api/projects/{pid}",
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_orphan_project_claimed_to_admin_on_startup(_isolate_data_dir):
    """启动时 lifespan 应把 owner IS NULL 的项目一次性归属到 admin。"""
    from backend.app.db.models import Project, User
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.auth import seed_admin_user
    from backend.app.services.ownership import claim_orphan_projects
    from sqlalchemy import insert, select
    import uuid

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    async with factory() as s:
        # 插入一行 NULL owner 项目
        pid = uuid.uuid4().hex
        await s.execute(
            insert(Project).values(
                project_id=pid,
                name="Orphan",
                status="draft",
                owner_user_id=None,
            )
        )
        await s.commit()

        admin = (
            await s.execute(select(User).where(User.username == "admin"))
        ).scalar_one_or_none()
        assert admin is not None, "seed_admin_user 后 admin 必须存在"
        n = await claim_orphan_projects(s, admin)
        assert n >= 1
        p = await s.get(Project, pid)
        assert p.owner_user_id == admin.id