"""批次 8（对象存储 / 腾讯云 COS）回归测试。

覆盖：
  T-OS1  media_key 组装（前缀为空 / 带前缀 / 前缀两端斜杠）
  T-OS2  LocalStorage：不归档、无可删、url 为 None（默认行为与接入前一致）
  T-OS3  S3Storage 公网前缀推导（显式 base 优先 / virtual-hosted / path-style）
  T-OS4  S3Storage 配置缺失提示（列出缺哪些键）
  T-OS5  配置指纹变化 → get_storage() 重建实例
  T-OS6  归档失败不抛异常（对象存储不可用时不能让整次合成失败）
  T-OS7  /media/sign 在对象存储（公有读）下返回**直链**，不落 token
  T-OS8  delete_build 在「没有 ZIP 的 build」上不得抛异常（回归 NameError）
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


# =====================================================================
# T-OS1 key 组装
# =====================================================================
def test_os1_media_key(monkeypatch):
    from backend.app.services import storage as st

    monkeypatch.setattr(st.settings, "S3_PREFIX", "")
    assert st.media_key("p1", "b1", "f.mp3") == "p1/b1/f.mp3"

    monkeypatch.setattr(st.settings, "S3_PREFIX", "novel-tts")
    assert st.media_key("p1", "b1", "f.mp3") == "novel-tts/p1/b1/f.mp3"

    # 前缀两端多余的斜杠要归一，避免出现 // 或前导 /
    monkeypatch.setattr(st.settings, "S3_PREFIX", "/novel-tts/")
    assert st.media_key("p1", "b1", "f.mp3") == "novel-tts/p1/b1/f.mp3"


# =====================================================================
# T-OS2 local 后端：一切照旧
# =====================================================================
async def test_os2_local_backend_is_noop(monkeypatch, tmp_path):
    from backend.app.services import storage as st

    monkeypatch.setattr(st.settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(st, "_storage", None)
    be = st.get_storage()

    assert be.name == "local"
    assert be.archives_remotely is False
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    # 全部返回 False / None：调用方据此跳过「删本地」与「直链」分支
    assert await be.archive(key="k", local_path=f) is False
    assert await be.put_bytes(key="k", data=b"x") is False
    assert await be.fetch_to(key="k", dest=tmp_path / "b") is False
    assert be.url("k") is None


# =====================================================================
# T-OS3 公网前缀推导
# =====================================================================
def test_os3_public_base_derivation(monkeypatch):
    from backend.app.services import storage as st

    monkeypatch.setattr(st.settings, "S3_ENDPOINT", "https://cos.ap-guangzhou.myqcloud.com")
    monkeypatch.setattr(st.settings, "S3_BUCKET", "mybucket")
    monkeypatch.setattr(st.settings, "S3_PATH_STYLE", False)

    # 未配置 public base → 按 COS 虚拟主机风格推导
    monkeypatch.setattr(st.settings, "S3_PUBLIC_BASE_URL", "")
    be = st.S3Storage()
    assert be.public_base() == "https://mybucket.cos.ap-guangzhou.myqcloud.com"
    assert be.url("novel-tts/p1/b1/ch0001.mp3") == (
        "https://mybucket.cos.ap-guangzhou.myqcloud.com/novel-tts/p1/b1/ch0001.mp3"
    )

    # path-style（自建 MinIO 场景）
    monkeypatch.setattr(st.settings, "S3_PATH_STYLE", True)
    be2 = st.S3Storage()
    assert be2.public_base() == "https://cos.ap-guangzhou.myqcloud.com/mybucket"

    # 显式配置的 CDN/自定义域名优先
    monkeypatch.setattr(st.settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com/")
    be3 = st.S3Storage()
    assert be3.public_base() == "https://cdn.example.com"
    assert be3.url("a/b.mp3") == "https://cdn.example.com/a/b.mp3"


# =====================================================================
# T-OS4 配置缺失
# =====================================================================
def test_os4_missing_config_lists_absent_keys(monkeypatch):
    from backend.app.services import storage as st

    for k in ("S3_ENDPOINT", "S3_REGION", "S3_BUCKET",
              "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_PUBLIC_BASE_URL"):
        monkeypatch.setattr(st.settings, k, "")
    be = st.S3Storage()
    assert be.missing_config() == [
        "S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"
    ]

    monkeypatch.setattr(st.settings, "S3_ENDPOINT", "https://cos.ap-guangzhou.myqcloud.com")
    monkeypatch.setattr(st.settings, "S3_BUCKET", "b")
    monkeypatch.setattr(st.settings, "S3_ACCESS_KEY", "ak")
    monkeypatch.setattr(st.settings, "S3_SECRET_KEY", "sk")
    assert st.S3Storage().missing_config() == []


# =====================================================================
# T-OS5 配置指纹 → 实例重建
# =====================================================================
def test_os5_get_storage_rebuilds_on_config_change(monkeypatch):
    from backend.app.services import storage as st

    monkeypatch.setattr(st, "_storage", None)
    monkeypatch.setattr(st, "_storage_fingerprint", None)

    monkeypatch.setattr(st.settings, "STORAGE_BACKEND", "local")
    a = st.get_storage()
    assert a.name == "local"
    assert st.get_storage() is a, "同配置应复用同一实例"

    monkeypatch.setattr(st.settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(st.settings, "S3_ENDPOINT", "https://cos.ap-guangzhou.myqcloud.com")
    b = st.get_storage()
    assert b.name == "s3" and b is not a, "改设置后必须重建"

    # 改 bucket 也要重建（否则会往旧桶写）
    monkeypatch.setattr(st.settings, "S3_BUCKET", "bucket-a")
    c1 = st.get_storage()
    monkeypatch.setattr(st.settings, "S3_BUCKET", "bucket-b")
    c2 = st.get_storage()
    assert c1 is not c2


# =====================================================================
# T-OS6 归档失败不抛
# =====================================================================
async def test_os6_archive_failure_returns_false_not_raise(monkeypatch, tmp_path):
    from backend.app.services import storage as st

    monkeypatch.setattr(st.settings, "S3_ENDPOINT", "https://cos.ap-guangzhou.myqcloud.com")
    monkeypatch.setattr(st.settings, "S3_BUCKET", "b")
    monkeypatch.setattr(st.settings, "S3_ACCESS_KEY", "ak")
    monkeypatch.setattr(st.settings, "S3_SECRET_KEY", "sk")
    be = st.S3Storage()

    class _BoomClient:
        def upload_file(self, *a, **kw):
            raise RuntimeError("network down")

        def put_object(self, *a, **kw):
            raise RuntimeError("network down")

        def download_file(self, *a, **kw):
            raise RuntimeError("network down")

        def delete_object(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(be, "_ensure_client", lambda: _BoomClient())
    f = tmp_path / "a.zip"
    f.write_bytes(b"PK")
    assert await be.archive(key="k", local_path=f) is False
    assert await be.put_bytes(key="k", data=b"x") is False
    assert await be.fetch_to(key="k", dest=tmp_path / "d") is False
    # 删除失败也不抛
    await be.delete_key("k")

    # 本地文件不存在时直接跳过（不调 client）
    assert await be.archive(key="k", local_path=tmp_path / "nope.zip") is False


# =====================================================================
# T-OS7 /media/sign 返回直链
# =====================================================================
@pytest_asyncio.fixture
async def build_for_sign(_isolate_data_dir, monkeypatch):
    from sqlalchemy import select
    from backend.app.db.models import Project, Build, BuildArtifact, User
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.auth import seed_admin_user, create_access_token

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    bid = uuid.uuid4().hex
    async with factory() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(project_id=pid, name="T", status="done", owner_user_id=admin.id))
        s.add(Build(
            build_id=bid, project_id=pid, status="success",
            total_chapters=1, completed_chapters=1,
            narrator_voice_id="", speed=1.0, mode="classic", tts_provider="doubao",
            zip_filename=f"build_{bid}_all.zip",
        ))
        s.add(BuildArtifact(
            build_id=bid, chapter_idx=0, title="ch", status="done",
            audio_filename=f"build_{bid}_ch0000.mp3",
        ))
        await s.commit()
    token, _ = create_access_token("admin")
    return pid, bid, token


@pytest.mark.asyncio
async def test_os7_sign_returns_direct_url_when_remote(build_for_sign, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app
    from backend.app.api import routes as _routes

    pid, bid, token = build_for_sign

    class _FakeRemote:
        archives_remotely = True

        def url(self, key: str) -> str:
            return f"https://cdn.example.com/{key}"

    monkeypatch.setattr(_routes, "get_storage", lambda: _FakeRemote())
    monkeypatch.setattr(_routes.settings, "S3_PREFIX", "")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        # 单章
        r = await client.get(
            "/api/media/sign",
            params={"build_id": bid, "kind": "chapter_mp3", "idx": 0},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["direct"] is True
        assert body["token"] is None, "公有读直链不需要签发 token"
        assert body["url"] == f"https://cdn.example.com/{pid}/{bid}/build_{bid}_ch0000.mp3"

        # 分片 ZIP（不传 idx → 第 0 片）
        r2 = await client.get(
            "/api/media/sign",
            params={"build_id": bid, "kind": "all_zip"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["url"] == f"https://cdn.example.com/{pid}/{bid}/build_{bid}_all.zip"


@pytest.mark.asyncio
async def test_os7_local_backend_still_returns_token(build_for_sign, monkeypatch):
    """local 后端必须保持原行为（返回 token，不是直链）。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app
    from backend.app.api import routes as _routes

    _pid, bid, token = build_for_sign
    monkeypatch.setattr(_routes.settings, "STORAGE_BACKEND", "local")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/api/media/sign",
            params={"build_id": bid, "kind": "chapter_mp3", "idx": 0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("direct") is not True
        assert body["url"].startswith("/api/media/stream?token=")


# =====================================================================
# T-OS8 delete_build 回归（无 ZIP 时不得抛 NameError）
# =====================================================================
@pytest.mark.asyncio
async def test_os8_delete_build_without_zip_does_not_raise(
    build_for_sign, monkeypatch, tmp_path
):
    from backend.app.services import build as build_mod

    pid, bid, _ = build_for_sign
    # 造一个「没有任何产物」的 build：_parse_zip_shards 返回空
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Build

    factory = get_session_factory()
    empty_bid = uuid.uuid4().hex
    async with factory() as s:
        s.add(Build(
            build_id=empty_bid, project_id=pid, status="failed",
            total_chapters=1, completed_chapters=0,
            narrator_voice_id="", speed=1.0, mode="classic", tts_provider="doubao",
        ))
        await s.commit()

    monkeypatch.setattr(build_mod.settings, "AUDIO_DIR", tmp_path)
    # 关键：不得抛异常（历史实现引用了循环变量 zip_fname，空列表时是 NameError）
    await build_mod.delete_build(pid, empty_bid)
