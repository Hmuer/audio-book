"""P2-3：章节预告片（preview）测试。

覆盖：
  T-PV1  _split_frames 按比特率截取（mock mutagen）
  T-PV2  _split_frames mutagen 失败 → 退化到 24kbps 估算（不抛错）
  T-PV3  generate_chapter_preview 写盘 + 文件大小合理
  T-PV4  generate_chapter_preview 章节 MP3 缺失 → FileNotFoundError
  T-PV5  preview_path / _preview_filename 命名规则
  T-PV6  get_or_generate_preview 磁盘有预告片 → 复用，不重新生成
  T-PV7  get_or_generate_preview 章节 MP3 缺失 → 返回 None
  T-PV8  路由：GET /preview 返回 mp3 + Content-Type audio/mpeg
  T-PV9  路由：章节未完成（artifact.status != done）→ 404
  T-PV10 路由：无 token → 401
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------
# T-PV5 命名规则
# ---------------------------------------------------------------------
def test_p2_3_pv5_preview_filename_and_path():
    """preview 文件命名规则：build_<id>_ch<NN>_preview.mp3。"""
    from backend.app.services.preview import _preview_filename, preview_path

    assert _preview_filename("b1234", 0) == "build_b1234_ch0000_preview.mp3"
    assert _preview_filename("b1234", 5) == "build_b1234_ch0005_preview.mp3"
    assert preview_path("/tmp/audio", "b1234", 0).name == "build_b1234_ch0000_preview.mp3"


# ---------------------------------------------------------------------
# T-PV1 _split_frames 按比特率截取
# ---------------------------------------------------------------------
def test_p2_3_pv1_split_frames_uses_bitrate(monkeypatch):
    """128kbps 比特率 × 5s ≈ 80000 bytes（±10%）；截取后大小接近。"""
    from backend.app.services import preview as mod

    class _FakeInfo:
        bitrate = 128_000  # 128 kbps

    class _FakeAudio:
        info = _FakeInfo()

    class _FakeMP3:
        def __init__(self, _bio): pass
        # 直接挂 info 属性
    # mock MP3 类
    monkeypatch.setattr("mutagen.mp3.MP3", lambda bio: type("A", (), {"info": _FakeInfo()})())

    # 1MB 原始 mp3 bytes（截取 5s 应该得 ~80KB）
    raw = b"\xff\xfb\x90\x64" + (b"\x00" * (1024 * 1024 - 4))
    cut = mod._split_frames(raw, max_seconds=5)
    # 80KB ± 20KB 容差（mutagen bitrate 字段偶有偏差）
    assert 50_000 < len(cut) < 120_000


def test_p2_3_pv1_split_frames_fallback_when_no_bitrate(monkeypatch):
    """无 bitrate → 兜底按 3000 bytes/s（24kbps）。"""
    from backend.app.services import preview as mod

    class _FakeInfo:
        bitrate = None  # 或 0

    monkeypatch.setattr("mutagen.mp3.MP3", lambda bio: type("A", (), {"info": _FakeInfo()})())

    raw = b"\xff\xfb\x90\x64" + (b"\x00" * (100 * 1024))
    cut = mod._split_frames(raw, max_seconds=10)
    # 3000 bytes/s × 10s = 30000 bytes（±一些）
    assert 20_000 < len(cut) <= 100 * 1024


# ---------------------------------------------------------------------
# T-PV2 mutagen 失败 → 退化
# ---------------------------------------------------------------------
def test_p2_3_pv2_split_frames_swallow_mutagen_errors(monkeypatch):
    """mutagen 抛错（坏 frame header）→ 按 3000 bytes/s 截取，不抛错。"""
    from backend.app.services import preview as mod

    def _raise(_bio):
        raise RuntimeError("not a valid mp3")
    monkeypatch.setattr("mutagen.mp3.MP3", _raise)

    raw = b"\x00" * 60_000
    cut = mod._split_frames(raw, max_seconds=15)
    # 3000 × 15 = 45000
    assert 40_000 < len(cut) <= 60_000


# ---------------------------------------------------------------------
# T-PV3 generate_chapter_preview
# ---------------------------------------------------------------------
def test_p2_3_pv3_generate_writes_preview(tmp_path):
    """章节 MP3 存在 → 生成 preview，输出文件大小合理。"""
    from backend.app.services.preview import generate_chapter_preview

    chapter_mp3 = tmp_path / "build_x_ch0000.mp3"
    # 200KB fake mp3
    chapter_mp3.write_bytes(b"\xff\xfb\x90\x64" + (b"\x00" * (200 * 1024 - 4)))
    out = tmp_path / "preview.mp3"

    p = generate_chapter_preview(chapter_mp3, out, max_seconds=15)
    assert p == out
    assert out.is_file()
    # 截取后 < 200KB
    assert out.stat().st_size < 200 * 1024
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------
# T-PV4 generate_chapter_preview 章节缺失
# ---------------------------------------------------------------------
def test_p2_3_pv4_generate_raises_when_chapter_missing(tmp_path):
    """章节 MP3 不存在 → FileNotFoundError。"""
    from backend.app.services.preview import generate_chapter_preview

    with pytest.raises(FileNotFoundError, match="章节音频不存在"):
        generate_chapter_preview(tmp_path / "nope.mp3", tmp_path / "out.mp3")


# ---------------------------------------------------------------------
# T-PV6 get_or_generate 复用缓存
# ---------------------------------------------------------------------
def test_p2_3_pv6_reuses_existing_preview(tmp_path, monkeypatch):
    """磁盘已有 preview → 复用，不重新生成（不调 generate_chapter_preview）。"""
    from backend.app.services import preview as mod

    chapter_mp3 = tmp_path / "build_b1_ch0000.mp3"
    chapter_mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 1000)
    pv = tmp_path / "build_b1_ch0000_preview.mp3"
    pv.write_bytes(b"cached-preview")

    called = {"gen": 0}
    def _fake_gen(*a, **kw):
        called["gen"] += 1
        return pv
    monkeypatch.setattr(mod, "generate_chapter_preview", _fake_gen)

    p = mod.get_or_generate_preview("b1", 0, audio_dir=tmp_path)
    assert p == pv
    assert called["gen"] == 0


def test_p2_3_pv6_force_regenerate_overwrites(tmp_path, monkeypatch):
    """force_regenerate=True → 重新生成。"""
    from backend.app.services import preview as mod

    chapter_mp3 = tmp_path / "build_b1_ch0000.mp3"
    chapter_mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 1000)
    pv = tmp_path / "build_b1_ch0000_preview.mp3"
    pv.write_bytes(b"old-cache")

    called = {"gen": 0}
    def _fake_gen(src, dst, **kw):
        called["gen"] += 1
        Path(dst).write_bytes(b"new-content")
        return Path(dst)
    monkeypatch.setattr(mod, "generate_chapter_preview", _fake_gen)

    p = mod.get_or_generate_preview("b1", 0, audio_dir=tmp_path, force_regenerate=True)
    assert called["gen"] == 1
    assert pv.read_bytes() == b"new-content"


# ---------------------------------------------------------------------
# T-PV7 章节 MP3 缺失
# ---------------------------------------------------------------------
def test_p2_3_pv7_returns_none_when_chapter_missing(tmp_path):
    """章节 MP3 不存在 → 返回 None（不抛错）。"""
    from backend.app.services.preview import get_or_generate_preview

    assert get_or_generate_preview("missing_build", 0, audio_dir=tmp_path) is None


# ---------------------------------------------------------------------
# T-PV8/9/10 路由端到端
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_3_pv8_route_returns_preview_mp3(_isolate_data_dir, tmp_path, monkeypatch):
    """写章节 MP3 + mock artifact.status=done → GET /preview 返回 audio/mpeg。"""
    from httpx import ASGITransport, AsyncClient

    from backend.app.db.session import init_db
    from backend.app.db.models import Build, BuildArtifact, Project, User
    from backend.app.db.session import get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.core import config as cfgmod
    from sqlalchemy import select as _select
    from backend.app.main import app as _app

    # audio_dir 切到 tmp_path，章节 MP3 已写
    monkeypatch.setattr(cfgmod.settings, "AUDIO_DIR", tmp_path)
    chapter_mp3 = tmp_path / "build_pv0001_ch0000.mp3"
    # 构造一个合法 MPEG frame header (128kbps/44.1kHz/mono → 417 bytes/frame)
    # 简化：直接写足够大的 fake mp3 bytes
    chapter_mp3.write_bytes(b"\xff\xfb\x90\x64" + (b"\x00" * (64 * 1024)))

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    async with factory() as s:
        admin_row = (await s.execute(
            _select(User).where(User.username == "admin")
        )).scalar_one()
        uid = admin_row.id
        s.add(Project(project_id="pv_p1", name="test", owner_user_id=uid))
        s.add(Build(
            build_id="pv0001", project_id="pv_p1",
            status="running", total_chapters=1, completed_chapters=0,
            narrator_voice_id="doubao:BV001_streaming", speed=1.0,
            mode="classic", tts_provider="doubao",
            is_retry=False,
        ))
        await s.flush()
        s.add(BuildArtifact(
            build_id="pv0001", chapter_idx=0, title="预告测试章",
            status="done", audio_filename="build_pv0001_ch0000.mp3",
            audio_url="/media/build_pv0001_ch0000.mp3",
            duration_ms=15000,
        ))
        await s.commit()

    token, _ = create_access_token("admin")
    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/api/projects/pv_p1/builds/pv0001/chapters/0/preview",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert len(r.content) > 0
    assert len(r.content) < 64 * 1024  # 比原始小
    # 预告片文件已落盘（第二次访问会复用）
    pv = tmp_path / "build_pv0001_ch0000_preview.mp3"
    assert pv.is_file()


@pytest.mark.asyncio
async def test_p2_3_pv9_route_404_when_chapter_not_done(_isolate_data_dir, tmp_path, monkeypatch):
    """artifact.status != done → 404。"""
    from httpx import ASGITransport, AsyncClient

    from backend.app.db.session import init_db
    from backend.app.db.models import Build, BuildArtifact, Project, User
    from backend.app.db.session import get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.core import config as cfgmod
    from sqlalchemy import select as _select
    from backend.app.main import app as _app

    monkeypatch.setattr(cfgmod.settings, "AUDIO_DIR", tmp_path)

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    async with factory() as s:
        admin_row = (await s.execute(
            _select(User).where(User.username == "admin")
        )).scalar_one()
        s.add(Project(project_id="pv_p2", name="t", owner_user_id=admin_row.id))
        s.add(Build(
            build_id="pv0002", project_id="pv_p2",
            status="running", total_chapters=1, completed_chapters=0,
            narrator_voice_id="doubao:BV001_streaming", speed=1.0,
            mode="classic", tts_provider="doubao", is_retry=False,
        ))
        await s.flush()
        s.add(BuildArtifact(
            build_id="pv0002", chapter_idx=0, title="x",
            status="running",  # ← 未完成
            audio_filename="build_pv0002_ch0000.mp3",
            audio_url="/media/build_pv0002_ch0000.mp3",
            duration_ms=0,
        ))
        await s.commit()

    token, _ = create_access_token("admin")
    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/api/projects/pv_p2/builds/pv0002/chapters/0/preview",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 404
    assert "尚未完成" in r.text


@pytest.mark.asyncio
async def test_p2_3_pv10_route_401_without_token(_isolate_data_dir, monkeypatch):
    """无 token → 401。"""
    from httpx import ASGITransport, AsyncClient

    from backend.app.main import app as _app

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/api/projects/any/builds/any/chapters/0/preview",
        )
    assert r.status_code == 401
