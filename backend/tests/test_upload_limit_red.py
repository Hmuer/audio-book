"""
P1 #8 — 流式上传限制回归测试。

覆盖：
- _read_upload_with_limit 在超 max_bytes 时立即抛 413（不读全文件）；
- /api/icl/voices 上传超大参考音频 → 413；
- /api/projects/{pid}/import 上传超大文件 → 413；
- /api/projects/{pid}/import 上传正常大小文件 → 200。

注：HTTP/1.1 server 端会先收齐整个 multipart 才能解析（取决于框架），但
我们这里只验证 _read_upload_with_limit 的 helper 行为 + 路由最终状态码。
"""
from __future__ import annotations

import io

import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.mark.asyncio
async def test_helper_reads_under_limit():
    """_read_upload_with_limit 在限额内能读完正常数据。"""
    from backend.app.api.routes import _read_upload_with_limit
    from fastapi import UploadFile

    # 模拟 UploadFile：用 starlette 提供的 UploadFile 包一个 SpooledTemporaryFile
    from starlette.datastructures import UploadFile as StarletteUploadFile

    data = b"hello world" * 100
    spooled = io.BytesIO(data)
    uf = StarletteUploadFile(filename="x.txt", file=spooled)
    out = await _read_upload_with_limit(uf, max_bytes=10 * 1024 * 1024)
    assert out == data


@pytest.mark.asyncio
async def test_helper_413_when_exceeded():
    """_read_upload_with_limit 超过 max_bytes 时抛 413，且不再继续读。"""
    from backend.app.api.routes import _read_upload_with_limit
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile as StarletteUploadFile

    data = b"A" * (1024 * 1024)  # 1MB
    spooled = io.BytesIO(data)
    uf = StarletteUploadFile(filename="x.txt", file=spooled)

    # max_bytes=10KB 一定会超
    with pytest.raises(HTTPException) as ei:
        await _read_upload_with_limit(uf, max_bytes=10 * 1024, chunk_size=4 * 1024)
    assert ei.value.status_code == 413


@pytest.mark.asyncio
async def test_helper_chunk_size_smaller_than_limit():
    """_read_upload_with_limit 在 chunk 边界附近的限内仍能读完。"""
    from backend.app.api.routes import _read_upload_with_limit
    from starlette.datastructures import UploadFile as StarletteUploadFile

    data = b"B" * 8192
    spooled = io.BytesIO(data)
    uf = StarletteUploadFile(filename="x.txt", file=spooled)
    out = await _read_upload_with_limit(uf, max_bytes=8 * 1024, chunk_size=2 * 1024)
    assert out == data


@pytest.mark.asyncio
async def test_icl_route_rejects_oversize(_isolate_data_dir):
    """ICL /api/icl/voices 上传超限文件 → 413。"""
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.main import app
    from backend.app.core.config import settings
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    tok, _ = create_access_token("admin")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 构造刚好超过 ICL_MAX_AUDIO_BYTES 的 multipart
        big = b"X" * (settings.ICL_MAX_AUDIO_BYTES + 1024)
        files = {"file": ("ref.wav", big, "audio/wav")}
        r = await client.post(
            "/api/icl/voices",
            data={"voice_name": "too-big"},
            files=files,
            headers={"Authorization": f"Bearer {tok}"},
        )
        # 流式超限 → 413
        assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_projects_import_rejects_oversize(_isolate_data_dir):
    """POST /api/projects/{pid}/import 上传超 50MB 文件 → 413。"""
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    tok, _ = create_access_token("admin")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 先创建项目
        r = await client.post(
            "/api/projects",
            json={"name": "Big"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text
        pid = r.json()["project_id"]

        big = b"Y" * (50 * 1024 * 1024 + 1024)
        files = {"file": ("book.txt", big, "text/plain")}
        r = await client.post(
            f"/api/projects/{pid}/import",
            files=files,
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_projects_import_normal_size_works(_isolate_data_dir):
    """正常大小项目导入仍然成功。"""
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    tok, _ = create_access_token("admin")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/projects",
            json={"name": "Normal"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        pid = r.json()["project_id"]

        text = "第一章 测试章节\n内容\n第二章 测试章节\n更多内容\n"
        files = {"file": ("book.txt", text.encode("utf-8"), "text/plain")}
        r = await client.post(
            f"/api/projects/{pid}/import",
            files=files,
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text