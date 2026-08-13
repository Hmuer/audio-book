"""
鉴权端到端测试：覆盖登录、token 校验、修改密码、登出等完整链路。

覆盖：
- POST /api/auth/login 成功 / 失败 / 422
- GET  /api/auth/me  token 有效 / 缺失 / 无效
- 受保护接口 /api/projects 在无 token / 错 token 时 401
- /api/health 公开（不需要 token）
- POST /api/auth/change-password 改密码后旧密码失败、新密码可用
- POST /api/auth/logout 占位返回 ok
"""
from __future__ import annotations

import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.mark.asyncio
async def test_health_is_public(_isolate_data_dir):
    """/api/health 是公开接口，无 token 也能访问。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_login_success_returns_token(_isolate_data_dir, admin_token):
    """正确账号密码登录 → 200 + token + user 信息。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert r.status_code == 200, f"登录失败: {r.text}"
        body = r.json()
        assert body["token"]
        assert body["token_type"] == "Bearer"
        assert body["expires_at"]
        assert body["user"]["username"] == "admin"
        assert body["user"]["is_active"] is True


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(_isolate_data_dir):
    """密码错误 → 401。"""
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "WRONG"},
        )
        assert r.status_code == 401
        # 登录失败不返回 WWW-Authenticate（该 header 仅用于 token 校验 401）
        assert "用户名或密码错误" in r.json()["detail"]


@pytest.mark.asyncio
async def test_login_unknown_user_returns_401(_isolate_data_dir):
    """不存在的用户 → 401。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "whatever"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_validation_empty_username_422(_isolate_data_dir):
    """空用户名 → 422（pydantic 校验）。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login",
            json={"username": "", "password": "x"},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_auth_me_with_valid_token(_isolate_data_dir, admin_token):
    """带有效 token 访问 /api/auth/me → 200 + 当前用户。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        r = await client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["username"] == "admin"


@pytest.mark.asyncio
async def test_auth_me_without_token_401(_isolate_data_dir):
    """无 token 访问 /api/auth/me → 401。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/auth/me")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers


@pytest.mark.asyncio
async def test_auth_me_with_invalid_token_401(_isolate_data_dir):
    """无效 token 访问 /api/auth/me → 401。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer not.a.real.token"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        r = await client.get("/api/auth/me")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_without_token_401(_isolate_data_dir):
    """受保护接口 /api/projects 无 token → 401。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/projects")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_with_invalid_token_401(_isolate_data_dir):
    """受保护接口带错误 token → 401。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer fake.token.here"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        r = await client.get("/api/projects")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_change_password_then_old_fails_new_works(_isolate_data_dir):
    """
    修改密码完整链路：
    1. 登录 admin/admin 拿 token
    2. 改密码为 newpass123
    3. 旧密码登录 → 401
    4. 新密码登录 → 200
    """
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 登录拿 token
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert r.status_code == 200
        token = r.json()["token"]

        # 改密码
        r = await client.post(
            "/api/auth/change-password",
            json={"old_password": "admin", "new_password": "newpass123"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, f"改密码失败: {r.text}"
        assert r.json()["ok"] is True

        # 旧密码登录 → 401
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert r.status_code == 401

        # 新密码登录 → 200
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "newpass123"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_old_400(_isolate_data_dir, admin_token):
    """原密码错误 → 400。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        r = await client.post(
            "/api/auth/change-password",
            json={"old_password": "WRONG", "new_password": "newpass123"},
        )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_change_password_same_as_old_400(_isolate_data_dir):
    """新密码 == 原密码 → 400（需先用一个 >=6 字符的密码才能触发该业务校验）。"""
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 先用 admin/admin 登录拿 token，然后改成 >=6 字符的密码
        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert r.status_code == 200
        token = r.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/api/auth/change-password",
            json={"old_password": "admin", "new_password": "newpass123"},
            headers=headers,
        )
        assert r.status_code == 200

        # 再用同样的新旧密码（均 >=6 字符）→ 命中"新密码不能与原密码相同"业务校验 → 400
        r = await client.post(
            "/api/auth/change-password",
            json={"old_password": "newpass123", "new_password": "newpass123"},
            headers=headers,
        )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_change_password_too_short_422(_isolate_data_dir, admin_token):
    """新密码 < 6 字符 → 422（pydantic 校验）。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        r = await client.post(
            "/api/auth/change-password",
            json={"old_password": "admin", "new_password": "123"},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_logout_returns_ok(_isolate_data_dir, admin_token):
    """登出接口返回 ok（无状态 JWT，服务端不存黑名单）。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        r = await client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json()["ok"] is True
