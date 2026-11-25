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
        "title": "科技前沿日报：测试标题",
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
        assert detail["audio_path"].endswith("/audio.mp3")

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

        # 未审核（draft）的期数不得进入 RSS
        r = await c.get("/api/daily/feed.xml", params={"key": "test-feed-key"})
        assert r.status_code == 200
        assert "application/rss+xml" in r.headers["content-type"]
        assert "<rss" in r.text and "<enclosure" not in r.text

        # 人工审核通过后才进入 RSS
        r = await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "approve"})
        assert r.status_code == 200
        r = await c.get("/api/daily/feed.xml", params={"key": "test-feed-key"})
        body = r.text
        assert "<enclosure" in body
        assert f"/api/daily/issues/{DATE}/audio.mp3?key=test-feed-key" in body
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


@pytest.mark.asyncio
async def test_daily_config_switch_voice_and_sources(_isolate_data_dir, daily_env, admin_token):
    async with _client(admin_token) as c:
        r = await c.get("/api/daily/config")
        cfg = r.json()
        assert cfg["voices"], "mock TTS 应返回音色列表"
        assert cfg["voice_id"]
        first_voice = cfg["voices"][0]["id"]
        custom_url = "https://custom.example.com/feed.xml"

        r = await c.put("/api/daily/config", json={
            "voice_id": first_voice,
            "sources": [
                {"name": "示例源", "url": "https://example.com/rss", "enabled": True},
                {"name": "自定义", "url": custom_url, "enabled": False},
            ],
        })
        assert r.status_code == 200
        updated = r.json()
        assert updated["voice_id"] == first_voice
        # 信源采用"合并"语义：内置源（跟随版本）+ 保存的启停 + 自定义源
        by_url = {s["url"]: s for s in updated["sources"]}
        assert by_url[custom_url]["custom"] is True
        assert by_url[custom_url]["enabled"] is False
        assert by_url["https://example.com/rss"]["enabled"] is True
        assert len(updated["sources"]) >= 20, "内置 26 个源应完整出现"

        # 持久化（重新 GET）
        r = await c.get("/api/daily/config")
        assert r.json()["voice_id"] == first_voice

        # 恢复自动音色
        r = await c.put("/api/daily/config", json={"voice_id": ""})
        assert r.status_code == 200
        assert r.json()["voice_id"]  # 自动挑一个非空


@pytest.mark.asyncio
async def test_daily_config_validation_errors(_isolate_data_dir, daily_env, admin_token):
    async with _client(admin_token) as c:
        r = await c.put("/api/daily/config", json={"voice_id": "not-a-voice"})
        assert r.status_code == 400

        r = await c.put("/api/daily/config", json={
            "sources": [{"name": "bad", "url": "ftp://example.com/x", "enabled": True}],
        })
        assert r.status_code == 400

        r = await c.put("/api/daily/config", json={
            "sources": [{"name": "off", "url": "https://example.com/rss", "enabled": False}],
        })
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_daily_circuit_breaker_reset(_isolate_data_dir, daily_env, admin_token, monkeypatch):
    from backend.app.services.daily import store

    store.write_run_status(status="failed", consecutive_failures=2, circuit_open=True)

    async with _client(admin_token) as c:
        r = await c.get("/api/daily/run/status")
        assert r.json()["run"]["circuit_open"] is True
        assert r.json()["run"]["consecutive_failures"] == 2

        r = await c.get("/api/daily/config")
        assert r.json()["circuit"]["open"] is True

        r = await c.post("/api/daily/reset-circuit")
        assert r.status_code == 200
        assert r.json()["run"]["circuit_open"] is False
        assert r.json()["run"]["consecutive_failures"] == 0

        # 重置后允许触发生成（避免真的跑流水线，桩掉 start_run）
        from backend.app.api import daily_routes
        monkeypatch.setattr(
            daily_routes.pipeline, "start_run",
            lambda **kw: {"run_id": "run_after_reset", "started_at": "t", "date": DATE},
        )
        r = await c.post("/api/daily/run", json={})
        assert r.status_code == 202


@pytest.mark.asyncio
async def test_daily_service_token_auth(_isolate_data_dir, daily_env, monkeypatch):
    """定时任务/DSH 插件用 X-DSH-Token 也能触发（不需要 JWT）。"""
    from backend.app.core import config as cfgmod
    from httpx import ASGITransport, AsyncClient
    from backend.app.main import app

    monkeypatch.setattr(cfgmod.settings, "DSH_SERVICE_TOKEN", "cron-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-DSH-Token": "cron-token"},
    ) as c:
        r = await c.get("/api/daily/issues")
        assert r.status_code == 200

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-DSH-Token": "wrong"},
    ) as c:
        r = await c.get("/api/daily/issues")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_daily_audio_via_feed_key_requires_approval(_isolate_data_dir, daily_env, admin_token):
    """播客客户端只能用 ?key= 拉音频：未审核 403，审核通过后可 Range 拉取。"""
    from httpx import ASGITransport, AsyncClient
    from backend.app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/api/daily/issues/{DATE}/audio", params={"key": "test-feed-key"})
        assert r.status_code == 403

        r = await c.get(f"/api/daily/issues/{DATE}/audio", params={"key": "wrong"})
        assert r.status_code == 401

    async with _client(admin_token) as c:
        await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "approve"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/api/daily/issues/{DATE}/audio",
            params={"key": "test-feed-key"},
            headers={"Range": "bytes=0-99"},
        )
        assert r.status_code == 206
        assert len(r.content) == 100


@pytest.mark.asyncio
async def test_daily_config_survives_no_voices(_isolate_data_dir, daily_env, admin_token, monkeypatch):
    """TTS 厂商暂不可用时，设置页仍可打开（voices=[]，voice_id=""）。"""
    from backend.app.api import daily_routes

    async def _no_voices(*, force: bool = False):
        return []

    monkeypatch.setattr(daily_routes.prefs, "available_voices", _no_voices)
    async with _client(admin_token) as c:
        r = await c.get("/api/daily/config")
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["voices"] == []
        assert cfg["voice_id"] == ""
        assert cfg["sources"], "信源仍应可见"


@pytest.mark.asyncio
async def test_daily_audio_mp3_alias_and_download_name(_isolate_data_dir, daily_env, admin_token):
    """公开 URL 用 .mp3（播客客户端按末段取名），下载名统一为 AI前沿日报_日期.mp3。"""
    async with _client(admin_token) as c:
        r = await c.get(
            f"/api/daily/issues/{DATE}/audio.mp3",
            headers={"Range": "bytes=0-99"},
        )
        assert r.status_code == 206
        cd = r.headers.get("content-disposition", "")
        assert "Tech-Daily-2025-11-24.mp3" in cd
        assert "%E7%A7%91%E6%8A%80%E5%89%8D%E6%B2%BF%E6%97%A5%E6%8A%A5_2025-11-24.mp3" in cd

        r = await c.get(f"/api/daily/issues/{DATE}/audio")
        assert r.status_code == 200
        assert "Tech-Daily-2025-11-24.mp3" in r.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_daily_export_package(_isolate_data_dir, daily_env, admin_token):
    """上架包：仅审核通过可导出；ZIP 内含音频/标题/标签/说明。"""
    import io
    import zipfile

    async with _client(admin_token) as c:
        r = await c.get(f"/api/daily/issues/{DATE}/export")
        assert r.status_code == 409

        await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "approve"})
        r = await c.get(f"/api/daily/issues/{DATE}/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")
        assert "Tech-Daily-2025-11-24-package.zip" in r.headers["content-disposition"]

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        assert "科技前沿日报：测试标题.mp3" in names
        assert any(n.endswith(".srt") for n in names), "上架包应包含字幕"
        for required in ("标题.txt", "简介.txt", "标签.txt", "元数据.json", "上传说明.txt"):
            assert required in names
        assert "AI 辅助" in zf.read("上传说明.txt").decode("utf-8")


@pytest.mark.asyncio
async def test_daily_subtitles_endpoint(_isolate_data_dir, daily_env, admin_token):
    """字幕：JWT 可取（老期数按字数比例兜底）；feed key 仅 approved 可用。"""
    from httpx import ASGITransport, AsyncClient
    from backend.app.main import app

    async with _client(admin_token) as c:
        r = await c.get(f"/api/daily/issues/{DATE}/subtitles.srt")
        assert r.status_code == 200
        assert "-->" in r.text
        assert "text/plain" in r.headers["content-type"] or "x-subrip" in r.headers["content-type"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/api/daily/issues/{DATE}/subtitles.srt", params={"key": "test-feed-key"})
        assert r.status_code == 403

    async with _client(admin_token) as c:
        await c.post(f"/api/daily/issues/{DATE}/review", json={"decision": "approve"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/api/daily/issues/{DATE}/subtitles.srt", params={"key": "test-feed-key"})
        assert r.status_code == 200 and "-->" in r.text
