"""导入失败后草稿保持可用，用户可在同一项目重试。"""

import pytest


@pytest.mark.asyncio
async def test_invalid_upload_keeps_draft_and_valid_retry_works(_isolate_data_dir, admin_token):
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import ASGITransport, AsyncClient

    await init_db()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        created = await client.post("/api/projects", json={"name": "导入重试"})
        assert created.status_code == 200
        project_id = created.json()["project_id"]

        for filename, content in (
            ("bad.epub", b"not a zip"),
            ("empty.txt", b"  \n  "),
            ("binary.txt", b"\x00\x01\x02"),
            ("book.pdf", b"some text"),
        ):
            result = await client.post(
                f"/api/projects/{project_id}/import",
                files={"file": (filename, content)},
            )
            assert result.status_code == 400, (filename, result.text)
            detail = await client.get(f"/api/projects/{project_id}")
            assert detail.json()["status"] == "draft"
            assert detail.json()["source_filename"] is None

        result = await client.post(
            f"/api/projects/{project_id}/import",
            files={"file": ("book.txt", "第一章 开始\n正文内容".encode("utf-8"))},
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "imported"
