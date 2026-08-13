"""
FastAPI HTTP 层端到端测试：通过 TestClient 走完整 HTTP 路由。

覆盖：
- POST /api/projects 创建
- POST /api/projects/{id}/import 上传
- POST /api/projects/{id}/prepare 识别
- GET /api/projects/{id} 详情
- PATCH /api/projects/{id}/characters/{char_id} 改音色
- POST /api/projects/{id}/builds 启动 build
- GET /api/projects/{id}/builds/{build_id} 详情
- GET /api/projects/{id}/builds/{build_id}/chapters/{idx}/download 单章下载
- GET /api/projects/{id}/builds/{build_id}/download-all ZIP 下载
- DELETE /api/projects/{id}/builds/{build_id} 删 build
- DELETE /api/projects/{id} 删项目
"""
from __future__ import annotations
import asyncio
import io
from pathlib import Path

import pytest

pytest_plugins = ("pytest_asyncio",)


_BOOK_TXT = """\
第一章 初遇

林若雪低着头，缓慢走在街道一侧。
「怎么了？」李明拍了拍她的肩膀。
「没什么。」林若雪小声说。

第二章 告别

夜里下起了小雨。
「明天见。」李明轻声说道。
「嗯。」林若雪点点头，转身走入雨幕。
"""


@pytest.mark.asyncio
async def test_http_project_full_flow(_isolate_data_dir):
    """HTTP 层完整流程：建项目→导入→识别→改音色→启动build→完成→下载→删。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 健康检查
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        # 2. 创建项目
        r = await client.post("/api/projects", json={"name": "HTTP测试-小城"})
        assert r.status_code == 200, f"创建失败: {r.text}"
        pid = r.json()["project_id"]
        assert r.json()["status"] == "draft"

        # 3. 上传文件（multipart）
        r = await client.post(
            f"/api/projects/{pid}/import",
            files={"file": ("小城.txt", _BOOK_TXT.encode("utf-8"), "text/plain")},
        )
        assert r.status_code == 200, f"导入失败: {r.text}"
        assert r.json()["status"] == "imported"
        assert r.json()["source_filename"] == "小城.txt"

        # 4. 触发 prepare
        r = await client.post(f"/api/projects/{pid}/prepare")
        assert r.status_code == 200, f"prepare 失败: {r.text}"
        prep = r.json()
        assert prep["total_chapters"] == 2
        assert len(prep["characters"]) >= 3

        # 5. 详情
        r = await client.get(f"/api/projects/{pid}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["status"] == "ready"
        assert detail["chapter_count"] == 2
        # 至少一个角色有 assigned_voice_id（来自 Mock 推荐）
        assert any(c["assigned_voice_id"] for c in detail["characters"])
        # last_build 还没起，应为 null
        assert detail["last_build"] is None

        # 6. 改一个角色的音色
        target_char = next(c for c in detail["characters"] if c["name"] == "李明")
        r = await client.patch(
            f"/api/projects/{pid}/characters/{target_char['id']}",
            json={"voice_id": "male-qn-badao"},
        )
        assert r.status_code == 200, f"改音色失败: {r.text}"
        assert r.json()["assigned_voice_id"] == "male-qn-badao"

        # 7. 启动 build
        chars_after = (await client.get(f"/api/projects/{pid}/characters")).json()
        assignments = {
            c["name"]: (c["assigned_voice_id"] or "male-qn-jingying")
            for c in chars_after
        }
        r = await client.post(
            f"/api/projects/{pid}/builds",
            json={
                "voice_assignments": assignments,
                "narrator_voice_id": "male-qn-jingying",
                "speed": 1.0,
            },
        )
        assert r.status_code == 200, f"启动 build 失败: {r.text}"
        build_id = r.json()["build_id"]
        assert r.json()["total_chapters"] == 2

        # 8. 轮询直到完成
        final_status = None
        for _ in range(60):
            r = await client.get(f"/api/projects/{pid}/builds/{build_id}/status")
            assert r.status_code == 200
            st = r.json()
            final_status = st["status"]
            if st["status"] in ("success", "failed"):
                break
            await asyncio.sleep(0.5)
        assert final_status == "success", (
            f"build 应成功，实际 {final_status}, msg={st.get('progress_msg')}"
        )
        # 章节产出
        done_arts = [a for a in st["artifacts"] if a["status"] == "done"]
        assert len(done_arts) == 2

        # 9. build 详情（含 zip_url）
        r = await client.get(f"/api/projects/{pid}/builds/{build_id}")
        assert r.status_code == 200
        bd = r.json()
        assert bd["zip_url"] is not None
        assert bd["total_size_kb"] > 0

        # 10. 详情里 last_build 已写入
        r = await client.get(f"/api/projects/{pid}")
        assert r.json()["last_build"]["build_id"] == build_id

        # 11. 单章下载：HTTP 200 + audio/mpeg
        r = await client.get(f"/api/projects/{pid}/builds/{build_id}/chapters/0/download")
        assert r.status_code == 200, f"单章下载失败: {r.text}"
        assert r.headers["content-type"] == "audio/mpeg"
        assert len(r.content) > 0
        assert "attachment" in r.headers["content-disposition"].lower()

        # 12. ZIP 全部下载
        r = await client.get(f"/api/projects/{pid}/builds/{build_id}/download-all")
        assert r.status_code == 200, f"ZIP 下载失败: {r.text}"
        assert r.headers["content-type"] == "application/zip"
        # ZIP 文件应以 PK 头开始
        assert r.content[:4] == b"PK\x03\x04", "ZIP 文件头不对"

        # 13. build 列表
        r = await client.get(f"/api/projects/{pid}/builds")
        assert r.status_code == 200
        assert any(b["build_id"] == build_id for b in r.json())

        # 14. 删 build
        r = await client.delete(f"/api/projects/{pid}/builds/{build_id}")
        assert r.status_code == 200
        # 列表已无
        r = await client.get(f"/api/projects/{pid}/builds")
        assert all(b["build_id"] != build_id for b in r.json())

        # 15. 删项目
        r = await client.delete(f"/api/projects/{pid}")
        assert r.status_code == 200
        # 详情 404
        r = await client.get(f"/api/projects/{pid}")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_http_project_validation_errors(_isolate_data_dir):
    """HTTP 层错误处理：404/400/422 等。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 不存在的 project
        r = await client.get("/api/projects/notexist")
        assert r.status_code == 404

        # 创建 name 空 → 422
        r = await client.post("/api/projects", json={"name": ""})
        assert r.status_code == 422

        # 给不存在的项目 prepare
        r = await client.post("/api/projects/notexist/prepare")
        assert r.status_code == 404

        # 创建项目但不 import 就 prepare → 400
        r = await client.post("/api/projects", json={"name": "x"})
        pid = r.json()["project_id"]
        r = await client.post(f"/api/projects/{pid}/prepare")
        assert r.status_code == 400
        # 项目状态置 failed
        r = await client.get(f"/api/projects/{pid}")
        assert r.json()["status"] == "failed"

        # 启动 build 但没 prepare → 400 (chapters_json 为空)
        r = await client.post("/api/projects", json={"name": "y"})
        pid2 = r.json()["project_id"]
        r = await client.post(
            f"/api/projects/{pid2}/builds",
            json={"voice_assignments": {}, "narrator_voice_id": "male-qn-jingying"},
        )
        # 项目没 prepare → chapters_json 为空 → RuntimeError → 400
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_http_voices_endpoint(_isolate_data_dir):
    """/api/voices 返回音色库（Mock TTS 加载的 voices.json）。"""
    from backend.app.db.session import init_db
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/voices")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] > 0
        assert len(data["voices"]) == data["count"]
        # 每个音色有 id
        assert all("id" in v for v in data["voices"])
