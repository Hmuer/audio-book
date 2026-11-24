"""DSH Agent 桥接 API（/api/agent/*）测试。

覆盖：
- 服务 token 鉴权（未配置 503 / token 错误 401 / 正确 200）
- 书库检索（内存索引，不碰对象存储）
- 创作任务创建幂等（同一 idempotency_key 返回同一 job）
- 模型调用流水上报 + 预算熔断（超预算 → job.status=blocked）
- 项目 / 构建状态查询

对象存储相关的"试读"路径（Range 读取）在这里只验证降级返回，
真实 COS 读取由 test_object_storage_red.py 覆盖。
"""
from __future__ import annotations

import pytest

pytest_plugins = ("pytest_asyncio",)

_TOKEN = "test-agent-token"


def _enable_agent(monkeypatch) -> None:
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DSH_SERVICE_TOKEN", _TOKEN)


def _client(token: str | None = _TOKEN, **kwargs):
    from httpx import ASGITransport, AsyncClient
    from backend.app.main import app

    headers = {"X-DSH-Token": token} if token else {}
    headers.update(kwargs.pop("headers", {}))
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers, **kwargs
    )


@pytest.mark.asyncio
async def test_agent_fails_closed_without_token(monkeypatch, _isolate_data_dir):
    """未配置 DSH_SERVICE_TOKEN 时整个命名空间 503，不做任何鉴权放行。"""
    from backend.app.db.session import init_db
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DSH_SERVICE_TOKEN", "")
    await init_db()

    async with _client(token=None) as c:
        r = await c.get("/api/agent/health")
        assert r.status_code == 503
        assert "DSH_SERVICE_TOKEN" in r.json()["detail"]


@pytest.mark.asyncio
async def test_agent_rejects_wrong_token(monkeypatch, _isolate_data_dir):
    from backend.app.db.session import init_db

    _enable_agent(monkeypatch)
    await init_db()

    async with _client(token="wrong-token") as c:
        r = await c.get("/api/agent/health")
        assert r.status_code == 401

    async with _client() as c:
        r = await c.get("/api/agent/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["agent_api_version"] == 1


@pytest.mark.asyncio
async def test_agent_library_search_uses_memory_index(monkeypatch, _isolate_data_dir):
    """书库检索命中进程内索引；local 存储下试读降级为 ok=False。"""
    from backend.app.db.session import init_db
    from backend.app.core import config as cfgmod
    from backend.app.services import library as lib

    _enable_agent(monkeypatch)
    monkeypatch.setattr(cfgmod.settings, "LIBRARY_INDEX_TTL_SECONDS", 1800)
    monkeypatch.setattr(lib, "_catalog", lib.parse_index({
        "library": {"name": "测试书库", "prefix": "library"},
        "generated_at": "2026-11-24T00:00:00+00:00",
        "totals": {"novels": 2},
        "categories": [{"name": "玄幻奇幻", "slug": "xuanhuan", "count": 2}],
        "novels": [
            {
                "uid": "t:1", "title": "小书", "author": "甲",
                "category": "玄幻奇幻", "category_slug": "xuanhuan",
                "chapter_count": 12, "word_count": 3000, "size_bytes": 9000,
                "status": "已完结", "txt": "library/books/小书.txt",
            },
            {
                "uid": "t:2", "title": "大书", "author": "乙",
                "category": "玄幻奇幻", "category_slug": "xuanhuan",
                "chapter_count": 900, "word_count": 9000, "size_bytes": 30000,
                "status": "已完结", "txt": "library/books/大书.txt",
            },
        ],
    }))
    await init_db()

    async with _client() as c:
        r = await c.get("/api/agent/library/search", params={"category": "玄幻奇幻"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["categories"][0]["name"] == "玄幻奇幻"
        # 默认按字数倒序
        assert [b["title"] for b in body["items"]] == ["大书", "小书"]

        r = await c.get("/api/agent/library/search", params={"q": "小书"})
        assert r.json()["total"] == 1

        r = await c.get("/api/agent/library/books/t:1", params={"excerpt_chars": 100})
        assert r.status_code == 200
        detail = r.json()
        assert detail["book"]["title"] == "小书"
        assert detail["excerpt"]["ok"] is False  # local 后端没有远端原文

        r = await c.get("/api/agent/library/books/not-exist")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_agent_job_idempotent_and_run_budget(monkeypatch, _isolate_data_dir):
    """同一 idempotency_key 只建一个 job；上报成本超预算后 job 置 blocked。"""
    from backend.app.db.session import init_db

    _enable_agent(monkeypatch)
    await init_db()

    async with _client() as c:
        payload = {
            "content_type": "audiobook",
            "title": "幂等测试",
            "idempotency_key": "workflow-abc-001",
            "budget_cny": 0.01,
        }
        r1 = await c.post("/api/agent/jobs", json=payload)
        assert r1.status_code == 200
        first = r1.json()
        assert first["created"] is True
        job_id = first["job"]["job_id"]

        r2 = await c.post("/api/agent/jobs", json=payload)
        assert r2.status_code == 200
        second = r2.json()
        assert second["created"] is False
        assert second["job"]["job_id"] == job_id

        # 记录一次 0.02 元的调用 → 超过 0.01 预算熔断
        r = await c.post("/api/agent/runs", json={
            "job_id": job_id,
            "role": "copy",
            "model": "MiniMax-M3",
            "prompt_version": "v1",
            "tokens_in": 1200,
            "tokens_out": 300,
            "cost_cny": 0.02,
            "output": {"selling_points": "八个字卖点"},
        })
        assert r.status_code == 200
        assert r.json()["recorded"] is True
        assert r.json()["job_blocked"] is True

        r = await c.get(f"/api/agent/jobs/{job_id}")
        body = r.json()
        assert body["job"]["status"] == "blocked"
        assert body["job"]["spent_cny"] == pytest.approx(0.02)
        assert len(body["runs"]) == 1
        assert body["runs"][0]["role"] == "copy"
        assert body["runs"][0]["output"] == {"selling_points": "八个字卖点"}

        # 任务列表
        r = await c.get("/api/agent/jobs", params={"status": "blocked"})
        body = r.json()
        assert body["count"] == 1
        assert body["total"] == 1

        # 未知 job 上报 → 404
        r = await c.post("/api/agent/runs", json={"job_id": "job_missing", "cost_cny": 0})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_agent_project_and_build_status(monkeypatch, _isolate_data_dir):
    """项目状态返回最近构建；未知项目 404。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build
    from backend.app.services.project import create_project

    _enable_agent(monkeypatch)
    await init_db()

    proj = await create_project("状态测试", owner_user_id=None)
    factory = get_session_factory()
    async with factory() as s:
        s.add(Build(
            build_id="build_test_1",
            project_id=proj.project_id,
            status="running",
            total_chapters=10,
            completed_chapters=3,
            failed_chapters_json="[2]",
        ))
        await s.commit()

    async with _client() as c:
        r = await c.get(f"/api/agent/projects/{proj.project_id}/status")
        assert r.status_code == 200
        body = r.json()
        assert body["project_id"] == proj.project_id
        assert body["latest_build"]["build_id"] == "build_test_1"
        assert body["latest_build"]["status"] == "running"
        assert body["latest_build"]["failed_chapters"] == [2]

        r = await c.get("/api/agent/builds/build_test_1")
        assert r.status_code == 200
        assert r.json()["completed_chapters"] == 3

        r = await c.get("/api/agent/projects/not-exist/status")
        assert r.status_code == 404

        r = await c.get("/api/agent/pricing/quote", params={"chars": 10000})
        assert r.status_code == 200
        assert r.json()["chars"] == 10000
