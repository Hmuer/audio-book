"""Review-Fix R2：/media 鉴权回归。

T-MA1 匿名访问 /media/<任意文件> → 401（无 token）。
T-MA2 错误/过期 token → 401。
T-MA3 有效 token（Authorization: Bearer）→ 200。
T-MA4 有效 token（?token=…）→ 200（供 <audio src> 形态使用）。
T-MA5 DISABLE_AUTH=True 时即便无 token 也放行（调试用）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def client_with_file(_isolate_data_dir):
    """挂一个真实的 mp3 文件到 audio 目录，验证 /media 鉴权链路。"""
    from backend.app.main import app
    from backend.app.core import config as cfgmod
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    import asyncio as _aio

    # 准备文件
    audio_path = Path(cfgmod.settings.AUDIO_DIR) / "build_auth_test.mp3"
    audio_path.write_bytes(b"\xff\xfb\x90\x64\x00" + (b"\x00" * 256))

    _aio.run(init_db())
    _aio.run(seed_admin_user())
    token, _ = create_access_token("admin")

    client = TestClient(app)
    yield {"client": client, "token": token, "cfg": cfgmod}


def test_media_no_token_401(client_with_file):
    c = client_with_file["client"]
    r = c.get("/media/build_auth_test.mp3")
    assert r.status_code == 401, f"无 token 必须 401，实际 { r.status_code }"
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert "Missing token" in r.text


def test_media_invalid_token_401(client_with_file):
    c = client_with_file["client"]
    r = c.get("/media/build_auth_test.mp3", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert "Invalid token" in r.text


def test_media_valid_bearer_header_ok(client_with_file):
    c = client_with_file["client"]
    tok = client_with_file["token"]
    r = c.get(
        "/media/build_auth_test.mp3",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, f"有效 Bearer 应 200，实际 { r.status_code } { r.text }"
    assert r.headers["content-type"] == "audio/mpeg"
    assert len(r.content) > 0


def test_media_valid_query_token_ok(client_with_file):
    c = client_with_file["client"]
    tok = client_with_file["token"]
    r = c.get(f"/media/build_auth_test.mp3?token={tok}")
    assert r.status_code == 200, f"有效 ?token= 应 200，实际 { r.status_code }"
    assert r.headers["content-type"] == "audio/mpeg"


def test_media_disable_auth_allows_anonymous(client_with_file, monkeypatch):
    """DISABLE_AUTH=True 时即便无 token 也放行（调试模式）。"""
    cfgmod = client_with_file["cfg"]
    from backend.app.api.routes import _decode_token_for_static
    monkeypatch.setattr(cfgmod.settings, "DISABLE_AUTH", True)
    # 验证 _decode_token_for_static 读到的 DISABLE_AUTH 也是 True
    _decode_token_for_static("garbage", cfgmod.settings)  # 不应抛错
    c = client_with_file["client"]
    r = c.get("/media/build_auth_test.mp3")
    assert r.status_code == 200, f"DISABLE_AUTH=True 应放行，实际 { r.status_code }"