"""AI 前沿日报 HTTP API 测试。

覆盖：列表/详情/配置、人工审核、音频 Range、RSS（?key= 鉴权）、生成触发
（用 monkeypatch 替换 pipeline.start_run，不真的跑 TTS/网络）。
"""
from __future__ import annotations

import json

import pytest

pytest_plugins = ("pytest_asyncio",)

DATE = "2025-11-24"


def _seed(daily_dir):
    from backend.app.core.mp3_util import make_silent_mp3

    base = daily_dir / DATE
    base.mkdir(parents=True, exist_ok=True)
    meta = {
        "date": DATE,
        "title": "AI 前沿日报：测试期",
        "created_at": "2025-11-24T08:00:00",
        "duration_ms": 800,
        "item_count": 3,
        "topics": [{
            "title": "OpenAI 发布新模型", "url": "https://example.com/a",
            "source": "机器之心", "summary": "摘要", "score": 0.8,
            "dims": {"relevance": 1.0}, "reasons": ["标题命中主题词"],
        }],
        "sections": {},
        "stats": {"candidates": 10, "clusters": 6, "scored": 4, "kept": 1},
        "review": {"decision": None, "note": "", "reviewed_at": None, "reviewed_by": None},
        "show": {"title": "AI 前沿日报"},
    }
    (base / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (base / "script.md").write_text("这是口播稿。", encoding="utf-8")
    (base / "shownotes.md").write_text("# 本期简介\n\n- 信源", encoding="utf-8")
    (base / "audio.mp3").write_bytes(make_silent_mp3(800))


@pytest.fixture
def daily_env(monkeypatch, tmp_path):
    from backend.app.core import config as cfgmod

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(cfgmod.settings, "DAILY_OUTPUT_DIR", daily_dir)
    monkeypatch.setattr(cfgmod.settings, "DAILY_FEED_KEY", "test-feed-key")
    _seed(daily_dir)
    return daily_dir


def _client(token):
    from httpx import ASGITransport, AsyncClient
    from backend.app.main import app

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_daily_list_detail_config(_isolate_data_dir, daily_env, admin_token):
    async with _client(admin_token) as c:
        r = await c.get("/api/daily/config")
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["sources"], "默认信源不应为空"
        assert cfg["feed_key_set"] is True
        assert cfg["show"]["title"]

        r = await c.get("/api/daily/issues")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["date"] == DATE
        assert items[0]["status"] == "draft"
        assert items[0]["has_audio"] is True

        r = await c.get(f"/api/daily/issues/{DATE}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["shownotes_md"].startswith("# 本期简介")
        assert detail["meta"]["topics"][0]["title"] == "OpenAI 发布新模型"
        assert detail["audio_path"].endswith("/audio")

        r = await c.get("/api/daily/issues/2099-01-01")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_daily_review_flow(_isolate_data_dir, daily_env, admin_token):
    async with _client(admin_token) as c:
        r = await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "approve", "note": "可以发"})
        assert r.status_code == 200
        issue = r.json()["issue"]
        assert issue["status"] == "approved"
        assert issue["review_note"] == "可以发"
        assert issue["reviewed_at"]

        r = await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "reject", "note": "标题要改"})
        assert r.status_code == 200
        assert r.json()["issue"]["status"] == "rejected"

        r = await c.post("/api/daily/issues/2099-01-01/review", json={"decision": "approve"})
        assert r.status_code == 404

        # 非法 decision 由 pydantic 拦截
        r = await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "ok"})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_daily_audio_range(_isolate_data_dir, daily_env, admin_token):
    async with _client(admin_token) as c:
        r = await c.get(f"/api/daily/issues/{DATE}/audio")
        assert r.status_code == 200
        assert r.headers["accept-ranges"] == "bytes"
        assert len(r.content) > 100

        r = await c.get(
            f"/api/daily/issues/{DATE}/audio",
            headers={"Range": "bytes=0-99"},
        )
        assert r.status_code == 206
        assert len(r.content) == 100
        assert r.headers["content-range"].startswith("bytes 0-99/")

        r = await c.get("/api/daily/issues/2099-01-01/audio")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_daily_feed_requires_key(_isolate_data_dir, daily_env, admin_token):
    async with _client(admin_token) as c:
        r = await c.get("/api/daily/feed.xml")
        assert r.status_code == 401

        r = await c.get("/api/daily/feed.xml", params={"key": "wrong"})
        assert r.status_code == 401

        r = await c.get("/api/daily/feed.xml", params={"key": "test-feed-key"})
        assert r.status_code == 200
        assert "application/rss+xml" in r.headers["content-type"]
        body = r.text
        assert "<rss" in body and "<enclosure" in body
        assert f"/api/daily/issues/{DATE}/audio?key=test-feed-key" in body
        assert "<itunes:explicit>false</itunes:explicit>" in body


@pytest.mark.asyncio
async def test_daily_run_trigger_and_status(
    _isolate_data_dir, daily_env, admin_token, monkeypatch,
):
    from backend.app.api import daily_routes
    from backend.app.services.daily.store import DailyError

    async with _client(admin_token) as c:
        r = await c.get("/api/daily/run/status")
        assert r.status_code == 200
        assert r.json()["run"]["status"] in ("idle", "success", "failed", "running")

        monkeypatch.setattr(
            daily_routes.pipeline, "start_run",
            lambda **kw: {"run_id": "run_test", "started_at": "2025-11-24T08:00:00", "date": DATE},
        )
        r = await c.post("/api/daily/run", json={})
        assert r.status_code == 202
        assert r.json()["run_id"] == "run_test"

        def _busy(**kw):
            raise DailyError("已有生成任务在运行")

        monkeypatch.setattr(daily_routes.pipeline, "start_run", _busy)
        r = await c.post("/api/daily/run", json={})
        assert r.status_code == 409
