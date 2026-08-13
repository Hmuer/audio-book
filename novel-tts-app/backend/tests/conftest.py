"""pytest 共享 fixtures."""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path
import pytest

# 确保 backend 包在 sys.path 中（支持从 root 或 backend 目录跑 pytest）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# monkeypatch env 必须在导入 backend 之前
os.environ.setdefault("TTS_API_KEY", "test")
os.environ.setdefault("LLM_API_KEY", "test")
# 测试固定用 admin/admin，方便 fixture 里登录拿 token
os.environ.setdefault("SEED_ADMIN_USER", "admin")
os.environ.setdefault("SEED_ADMIN_PASS", "admin")


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """每个测试独立 data 目录和 DB。"""
    data_dir = tmp_path / "data"
    audio_dir = data_dir / "audio"
    data_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # monkeypatch settings (通过重设环境变量，settings 是模块级初始化的)
    # 因此这里直接 patch settings 实例的属性
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod.settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(cfgmod.settings, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(
        cfgmod.settings,
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{data_dir}/app.db",
    )
    # 测试期间默认启用鉴权（真实链路）；个别用例可 monkeypatch 关掉
    monkeypatch.setattr(cfgmod.settings, "DISABLE_AUTH", False)

    # 重置 DB engine / session_factory 全局缓存，确保每个测试用独立 DB
    # （session.py 的 _engine / _session_factory 是模块级缓存，
    #  不重置的话会复用第一个测试创建的 engine，导致跨测试数据泄漏）
    from backend.app.db import session as sessmod
    monkeypatch.setattr(sessmod, "_engine", None)
    monkeypatch.setattr(sessmod, "_session_factory", None)

    # monkeypatch factory 返回 mock
    from backend.app.ai import factory as _aifactory_mod  # noqa: F401
    import backend.app.ai as ai_pkg
    if not hasattr(ai_pkg, "factory"):
        from backend.app.ai import factory
        ai_pkg.factory = factory

    from backend.tests.mock_providers import MockLLMProvider, MockTTSProvider

    mock_llm = MockLLMProvider()
    mock_tts = MockTTSProvider()
    monkeypatch.setattr(ai_pkg.factory, "_llm_instance", mock_llm)
    monkeypatch.setattr(ai_pkg.factory, "_tts_instance", mock_tts)

    # 返回 mock_llm 供测试用例记录调用
    yield {"llm": mock_llm, "tts": mock_tts}


@pytest.fixture
async def db_session():
    from backend.app.db.session import init_db, get_session_factory
    await init_db()
    factory = get_session_factory()
    async with factory() as s:
        yield s


@pytest.fixture
async def admin_token(_isolate_data_dir):
    """
    返回已登录 admin 的 JWT token。
    - 自动 init_db + seed admin（admin/admin）
    - 调用 /api/auth/login 拿 token
    测试里用 client.headers['Authorization'] = f'Bearer {token}' 携带。
    """
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    await init_db()
    await seed_admin_user()
    token, _ = create_access_token("admin")
    return token
