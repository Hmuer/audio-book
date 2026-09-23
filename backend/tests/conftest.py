"""pytest 共享 fixtures."""
from __future__ import annotations
import asyncio
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
os.environ.setdefault("LLM_API_KEY", "test")
# 测试固定用 admin/admin，方便 fixture 里登录拿 token
os.environ.setdefault("SEED_ADMIN_USER", "admin")
os.environ.setdefault("SEED_ADMIN_PASS", "admin")

# E-1：记录「权威 settings 对象」，供用例结束后还原（见 _restore_config_settings）。
# 背景：`importlib.reload(backend.app.core.config)` 会重新执行模块体，把
# `cfgmod.settings` 换成一个**新对象**；而 routes / build / project / session 等模块
# 在更早 import 时已用 `from ..core.config import settings` 绑定了**旧对象**。
# reload 后若不还原，后续用例 monkeypatch 改的是新对象，旧对象纹丝不动 →
# 模块继续读默认值（真实 ./data），造成「单跑通过、全量失败」的跨用例污染。
from backend.app.core import config as _cfgmod  # noqa: E402

_CANONICAL_SETTINGS = _cfgmod.settings


@pytest.fixture(autouse=True)
def _restore_config_settings():
    """E-1：若某用例 reload 了 config 导致 settings 对象分裂，用例结束后还原。"""
    yield
    if _cfgmod.settings is not _CANONICAL_SETTINGS:
        _cfgmod.settings = _CANONICAL_SETTINGS


@pytest.fixture(autouse=True)
def _snapshot_providers_config():
    """E-1：快照/还原 settings.PROVIDERS_CONFIG。

    `PROVIDERS_CONFIG` 是挂在 settings 上的**全局可变态**，某些用例会
    `save_providers_config({"providers": [单个厂商]})` 覆盖它却只还原单个
    字段、不还原厂商列表。E-1 统一导入路径后所有用例共享同一个 settings
    对象，这类泄漏会直接污染后续用例（如 GET /api/providers 默认形状断言）。
    """
    original = _cfgmod.settings.PROVIDERS_CONFIG
    yield
    _cfgmod.settings.PROVIDERS_CONFIG = original


def _reset_global_singletons() -> None:
    """E-1：每个用例前重置「模块级可变单例」。

    为什么必须做：pytest-asyncio 每个用例一个事件循环，而下列对象都是**进程级**的：
    - 跨用例残留会让后一个用例看到前一个的缓存/锁（「段缓存串味」就是典型症状）；
    - `asyncio.Semaphore` / `Lock` 一旦在旧循环里被 await 过，在新循环里使用会直接报错。

    注意：`factory._llm_instance` / `factory._tts_instance` 由本 fixture 用 mock
    monkeypatch，**不要在这里清掉**，否则会把 mock 也清没。
    """
    import sys

    def _get(mod_name: str):
        # 只处理**已经被导入**的模块，避免为了重置而提前 import 触发副作用
        return sys.modules.get(mod_name)

    b = _get("backend.app.services.build")
    if b is not None:
        b._ACTIVE_BUILDS = {}
        b._START_LOCKS = {}
        # TTS 段级内存缓存：跨用例串味会让「首次调用」类断言失效
        b._tts_seg_mem_cache = {}

    fd = _get("backend.app.ai.factory")
    if fd is not None:
        fd._tts_instances = {}
        fd._tts_default_instance = None
        fd._tts_sem = None

    m = _get("backend.app.ai.providers.minimax.llm")
    if m is not None:
        m._llm_sem = None

    p = _get("backend.app.services.project")
    if p is not None:
        p._prepare_running_tasks = {}

    s = _get("backend.app.services.storage")
    if s is not None:
        s._storage = None
        s._storage_fingerprint = None


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """每个测试独立 data 目录和 DB。"""
    # P1 #9：先重置 rate_limit（不抛异常），让任何重置异常暴露
    # 注意：conftest 用 backend.app... 而 test_providers_api_red.py 用 app...，
    # sys.modules 中两个 key 各自指向不同的模块对象，所以必须两边都重置。
    # E-1 后已统一为 backend.app.*，下面第二次重置保留只为兼容极老的调用点。
    from collections import defaultdict
    import sys
    for mod_name in (
        "backend.app.services.rate_limit",
        "app.services.rate_limit",
    ):
        mod_obj = sys.modules.get(mod_name)
        if mod_obj is None:
            continue
        mod_obj._recent_events = defaultdict(mod_obj._recent_events_factory)
        mod_obj._events_lock = asyncio.Lock()

    # E-1：统一重置其它模块级单例（锁/缓存/并发信号量）
    _reset_global_singletons()

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

    # P1 #9：每个测试也清空 rate_limit 计数（避免一个测试跑了 5 次 login 把后续测试挡掉）

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
