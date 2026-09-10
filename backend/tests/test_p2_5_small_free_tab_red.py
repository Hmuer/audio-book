"""P2-5：小模型免费音色 Tab — 后端 free_only 过滤 + 路由参数透传 测试。

覆盖：
  T-P25-1  list_voices(free_only=True) 只保留 doubao provider 且 free=True 且 model=seed-tts-1.0 的音色
  T-P25-2  list_voices(free_only=True) 排除 ICL 复刻音色（不属于官方免费列表）
  T-P25-3  list_voices(free_only=True) 排除 MiniMax 音色
  T-P25-4  list_voices(free_only=True) 排除豆包大模型 2.0 音色（model=seed-tts-2.0）
  T-P25-5  list_voices() 默认（free_only=False）行为不变（兼容旧调用方）
  T-P25-6  HTTP GET /api/voices?free_only=true 端到端路由
  T-P25-7  HTTP GET /api/voices?free_only=false 显式关闭
  T-P25-8  HTTP free_only=true + tts_provider=doubao 同时生效
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _restore_factory_get_tts():
    """所有 P2-5 测试结束后把 factory.get_tts 还原，避免污染后续测试模块。"""
    from backend.app.ai import factory as _factory_mod
    original = _factory_mod.get_tts
    yield
    _factory_mod.get_tts = original


# ---------------------------------------------------------------------
# T-P25-1：free_only=True 过滤逻辑
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_1_free_only_filters_to_doubao_small_free_only(monkeypatch):
    """free_only=True 时返回的全是 doubao + free=True + model=seed-tts-1.0。"""
    from backend.app.api import routes as mod

    # mock 三个 provider 的 list_voices 返回
    async def _fake_minimax():
        return [
            {"id": "minimax:nn", "name": "MiniMax女声", "provider": "minimax"},
            {"id": "minimax:nm", "name": "MiniMax男声", "provider": "minimax"},
        ]

    async def _fake_doubao():
        return [
            # 小模型免费（应保留）
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
            # 小模型付费（应排除）
            {"id": "doubao:BV002_streaming", "name": "付费女声",
             "provider": "doubao", "free": False, "model": "seed-tts-1.0"},
            # 大模型（应排除：model != seed-tts-1.0）
            {"id": "doubao:zh_male_uranus_bigtts", "name": "磁性男声",
             "provider": "doubao", "free": True, "model": "seed-tts-2.0"},
        ]

    async def _fetch_side_effect(p):
        return await {"minimax": _fake_minimax, "doubao": _fake_doubao}[p]()

    # patch _fetch 内部用的 _get_tts；用 monkeypatch 把 list_voices 在 provider 实例上的实现替掉
    class _Stub:
        async def list_voices(self_inner):
            return await _fake_doubao() if False else []  # 实际走 patch

    from backend.app.ai.factory import get_tts as real_get_tts

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await _fetch_side_effect(p)
        return _Inst()

    monkeypatch.setattr("backend.app.api.routes._get_tts_unused", lambda p: None, raising=False)
    # 直接 patch 模块内 _fetch 调用入口
    from backend.app.ai import factory as _factory_mod

    monkeypatch.setattr(_factory_mod, "get_tts", _stub_get_tts)

    out = await mod.list_voices(free_only=True, icl_user_id=None)
    ids = [v["id"] for v in out["voices"]]
    assert ids == ["doubao:BV001_streaming"], ids
    assert out["count"] == 1
    for v in out["voices"]:
        assert v["provider"] == "doubao"
        assert v["free"] is True
        assert v["model"] == "seed-tts-1.0"


# ---------------------------------------------------------------------
# T-P25-2：free_only=True 排除 ICL 复刻音色
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_2_free_only_excludes_icl_clones(monkeypatch):
    """ICL 复刻音色（provider=icl 或 provider=doubao 但 model=seed-icl-2.0）不在免费列表里。"""
    from backend.app.api import routes as mod
    from backend.app.ai import factory as _factory_mod

    async def _fake_doubao():
        return [
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await _fake_doubao()
        return _Inst()

    monkeypatch.setattr(_factory_mod, "get_tts", _stub_get_tts)

    # ICL 音色服务（即使被调用也不应出现在 free_only 结果里）
    async def _fake_icl_for_user(uid):
        return [
            {"id": "icl:abc123", "name": "我的复刻音色",
             "provider": "icl", "free": True, "model": "seed-icl-2.0"},
        ]

    monkeypatch.setattr(
        "backend.app.services.icl.icl_voices_for_user", _fake_icl_for_user, raising=False,
    )
    # 即便显式注入一个伪装 doubao + free=True 的 ICL 克隆也应被模型字段过滤掉
    out = await mod.list_voices(free_only=True, icl_user_id=42)
    ids = [v["id"] for v in out["voices"]]
    assert "icl:abc123" not in ids
    assert all(v.get("model") == "seed-tts-1.0" for v in out["voices"])


# ---------------------------------------------------------------------
# T-P25-3：free_only=True 排除 MiniMax
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_3_free_only_excludes_minimax(monkeypatch):
    """即使 MiniMax 数据里 free=True 也应被 provider 字段排除。"""
    from backend.app.api import routes as mod
    from backend.app.ai import factory as _factory_mod

    async def _fake_minimax():
        return [
            {"id": "minimax:nn", "name": "mm", "provider": "minimax",
             "free": True, "model": "seed-tts-1.0"},
        ]

    async def _fake_doubao():
        return [
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await {"minimax": _fake_minimax, "doubao": _fake_doubao}[p]()
        return _Inst()

    monkeypatch.setattr(_factory_mod, "get_tts", _stub_get_tts)
    out = await mod.list_voices(free_only=True, icl_user_id=None)
    ids = [v["id"] for v in out["voices"]]
    assert all(not i.startswith("minimax:") for i in ids)
    assert ids == ["doubao:BV001_streaming"]


# ---------------------------------------------------------------------
# T-P25-4：free_only=True 排除豆包大模型 2.0
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_4_free_only_excludes_big_model_v2(monkeypatch):
    """豆包大模型 2.0 即使 free=True 也要被 model 字段过滤掉。"""
    from backend.app.api import routes as mod
    from backend.app.ai import factory as _factory_mod

    async def _fake_doubao():
        return [
            {"id": "doubao:zh_male_uranus_bigtts", "name": "磁性男声",
             "provider": "doubao", "free": True, "model": "seed-tts-2.0"},
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await _fake_doubao()
        return _Inst()

    monkeypatch.setattr(_factory_mod, "get_tts", _stub_get_tts)
    out = await mod.list_voices(free_only=True, icl_user_id=None)
    ids = [v["id"] for v in out["voices"]]
    assert "doubao:zh_male_uranus_bigtts" not in ids
    assert ids == ["doubao:BV001_streaming"]


# ---------------------------------------------------------------------
# T-P25-5：默认 free_only=False 行为兼容
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_5_default_no_filter_keeps_all(monkeypatch):
    """free_only=False（默认）时返回所有厂商所有音色（不应用过滤）。"""
    from backend.app.api import routes as mod
    from backend.app.ai import factory as _factory_mod

    async def _fake_minimax():
        return [{"id": "minimax:nn", "name": "mm", "provider": "minimax",
                 "free": True, "model": "seed-tts-1.0"}]

    async def _fake_doubao():
        return [
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
            {"id": "doubao:zh_male_uranus_bigtts", "name": "磁性男声",
             "provider": "doubao", "free": False, "model": "seed-tts-2.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await {"minimax": _fake_minimax, "doubao": _fake_doubao}[p]()
        return _Inst()

    monkeypatch.setattr(_factory_mod, "get_tts", _stub_get_tts)
    out = await mod.list_voices(free_only=False, icl_user_id=None)
    ids = sorted(v["id"] for v in out["voices"])
    assert ids == ["doubao:BV001_streaming", "doubao:zh_male_uranus_bigtts", "minimax:nn"]
    assert out["count"] == 3


# ---------------------------------------------------------------------
# T-P25-6：HTTP GET /api/voices?free_only=true 端到端
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_6_http_route_free_only_true(_isolate_data_dir):
    """HTTP 路由 GET /api/voices?free_only=true 返回过滤后的音色列表。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.ai import factory as _factory_mod
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    admin_tok, _ = create_access_token("admin")

    async def _fake_minimax():
        return [{"id": "minimax:nn", "name": "mm", "provider": "minimax",
                 "free": True, "model": "seed-tts-1.0"}]

    async def _fake_doubao():
        return [
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
            {"id": "doubao:zh_male_uranus_bigtts", "name": "磁性男声",
             "provider": "doubao", "free": False, "model": "seed-tts-2.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await {"minimax": _fake_minimax, "doubao": _fake_doubao}[p]()
        return _Inst()

    _factory_mod.get_tts = _stub_get_tts

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            "/api/voices?free_only=true",
            headers={"Authorization": f"Bearer {admin_tok}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1
        assert body["voices"][0]["id"] == "doubao:BV001_streaming"


# ---------------------------------------------------------------------
# T-P25-7：HTTP free_only=false 显式关闭（兼容）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_7_http_route_free_only_false(_isolate_data_dir):
    """free_only=false 时返回所有厂商音色（不过滤）。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.ai import factory as _factory_mod
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    admin_tok, _ = create_access_token("admin")

    async def _fake_minimax():
        return [{"id": "minimax:nn", "name": "mm", "provider": "minimax"}]

    async def _fake_doubao():
        return [
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
            {"id": "doubao:zh_male_uranus_bigtts", "name": "磁性男声",
             "provider": "doubao", "free": False, "model": "seed-tts-2.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                return await {"minimax": _fake_minimax, "doubao": _fake_doubao}[p]()
        return _Inst()

    _factory_mod.get_tts = _stub_get_tts

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            "/api/voices?free_only=false",
            headers={"Authorization": f"Bearer {admin_tok}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 3


# ---------------------------------------------------------------------
# T-P25-8：free_only + tts_provider=doubao 同时生效
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_5_8_free_only_with_provider_doubao(_isolate_data_dir):
    """free_only + tts_provider=doubao 同时给：只走 doubao 通道并应用 free 过滤。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.ai import factory as _factory_mod
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    admin_tok, _ = create_access_token("admin")

    fetched_providers: list[str] = []

    async def _fake_doubao():
        fetched_providers.append("doubao")
        return [
            {"id": "doubao:BV001_streaming", "name": "通用女声",
             "provider": "doubao", "free": True, "model": "seed-tts-1.0"},
            {"id": "doubao:BV002_streaming", "name": "付费女声",
             "provider": "doubao", "free": False, "model": "seed-tts-1.0"},
        ]

    def _stub_get_tts(p):
        class _Inst:
            async def list_voices(self_inner):
                if p == "doubao":
                    return await _fake_doubao()
                return []
        return _Inst()

    _factory_mod.get_tts = _stub_get_tts

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            "/api/voices?tts_provider=doubao&free_only=true",
            headers={"Authorization": f"Bearer {admin_tok}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1
        assert body["voices"][0]["id"] == "doubao:BV001_streaming"
        assert fetched_providers == ["doubao"]


# ---------------------------------------------------------------------
# T-P25-9：真实内置表自洽 — free_only 子集 ⊆ 内置全集
# ---------------------------------------------------------------------
def test_p2_5_9_real_builtin_table_free_subset_is_valid():
    """内置音色表中：free_only 子集 ID 必须全是 doubao provider + free=True + model=seed-tts-1.0。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    voices = DoubaoTTSProvider()._builtin_voices_sync()
    free_small = [
        v for v in voices
        if v.get("provider") == "doubao"
        and v.get("free") is True
        and v.get("model") == "seed-tts-1.0"
    ]
    # 至少要有 21 条（FAQ 锁定）
    assert len(free_small) >= 21, f"免费小模型音色过少：{len(free_small)}"
    for v in free_small:
        assert v["id"].startswith("doubao:")
        assert v["free"] is True
        assert v["model"] == "seed-tts-1.0"
