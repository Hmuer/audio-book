"""T-MODEL-TYPE：ICL 训练 model_type 透传测试。

覆盖：
  T-MT-1  HTTP /api/icl/voices 接收 model_type Form 字段
  T-MT-2  start_icl_training 把 model_type 传给 worker → client.create_training
  T-MT-3  create_training 不传 model_type 时回退 ICL2.0
  T-MT-4  create_training 传非法 model_type 抛 ValueError
  T-MT-5  start_icl_training 早期校验：非法 model_type 在数据库写入前抛错
  T-MT-6  HTTP /api/icl/voices 传非法 model_type 返回 400
  T-MT-7  payload 里 model_type 与传入值一致（不是默认 ICL2.0）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAKE_MP3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 2048)


def _setup_icl_client(monkeypatch, api_key: str = "test-api-key-xyz"):
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_API_KEY", api_key)
    return DoubaoICLClient()


# ---------------------------------------------------------------------
# T-MT-3：create_training 不传 model_type 时回退 ICL2.0
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_training_defaults_to_icl2_0(monkeypatch):
    client = _setup_icl_client(monkeypatch)

    captured: dict = {}

    async def _fake_post(url, payload, **_kwargs):
        captured["payload"] = payload
        return {"code": 0, "speaker_id": payload["speaker_id"]}

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    await client.create_training("我的声线", FAKE_MP3)
    assert captured["payload"]["model_type"] == "ICL2.0"


# ---------------------------------------------------------------------
# T-MT-7：payload 里 model_type 与传入值一致
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_training_passes_through_model_type(monkeypatch):
    client = _setup_icl_client(monkeypatch)

    captured: dict = {}

    async def _fake_post(url, payload, **_kwargs):
        captured["payload"] = payload
        return {"code": 0, "speaker_id": payload["speaker_id"]}

    client._http_post_json = _fake_post  # type: ignore[method-assign]

    # ICL1.0
    await client.create_training("音色A", FAKE_MP3, model_type="ICL1.0")
    assert captured["payload"]["model_type"] == "ICL1.0"

    # DiT
    await client.create_training("音色B", FAKE_MP3, model_type="DiT")
    assert captured["payload"]["model_type"] == "DiT"

    # 显式 None 也走默认
    await client.create_training("音色C", FAKE_MP3, model_type=None)
    assert captured["payload"]["model_type"] == "ICL2.0"


# ---------------------------------------------------------------------
# T-MT-4：非法 model_type 抛 ValueError
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_training_rejects_invalid_model_type(monkeypatch):
    client = _setup_icl_client(monkeypatch)

    # 不应触发出站（早于网络）
    sent: list[bool] = []

    async def _should_not_post(*args, **kwargs):
        sent.append(True)
        return {"code": 0, "speaker_id": "icl_xxx"}

    client._http_post_json = _should_not_post  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="不支持的 model_type"):
        await client.create_training("测试", FAKE_MP3, model_type="icl2.0")  # 大小写错
    with pytest.raises(ValueError, match="不支持的 model_type"):
        await client.create_training("测试", FAKE_MP3, model_type="seed-icl-2.0")  # 合成 resource_id
    with pytest.raises(ValueError, match="不支持的 model_type"):
        await client.create_training("测试", FAKE_MP3, model_type="custom")

    assert sent == [], "非法 model_type 必须在出站前抛错"


# ---------------------------------------------------------------------
# T-MT-5：start_icl_training 早期校验：非法 model_type 在数据库写入前抛错
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_icl_training_early_validates_invalid_model_type(monkeypatch, tmp_path):
    """校验非法 model_type 在 DB 写入前抛 ValueError（不留垃圾任务）。"""
    from backend.app.services import icl as icl_svc
    from backend.app.db.session import init_db
    from backend.app.db.models import IclTrainingTask

    await init_db()

    with pytest.raises(ValueError, match="不支持的 model_type"):
        await icl_svc.start_icl_training(
            user_id=1,
            voice_name="测试",
            audio_bytes=FAKE_MP3,
            filename="test.mp3",
            model_type="seed-icl-2.0",  # 错误：这是合成 resource_id 不是训练 model_type
        )

    # 验证数据库里没有产生脏数据
    from backend.app.db.session import get_session_factory
    factory = get_session_factory()
    async with factory() as s:
        from sqlalchemy import select
        rows = (await s.execute(select(IclTrainingTask))).scalars().all()
    assert len(rows) == 0, f"非法 model_type 不应写入 DB，实际 {len(rows)} 条"


# ---------------------------------------------------------------------
# T-MT-2：start_icl_training 把 model_type 传给 worker → client
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_icl_training_forwards_model_type_to_worker(monkeypatch, tmp_path):
    """校验 service 层把 model_type 透传到 ICL client.create_training。

    策略：直接 monkeypatch `_icl_training_worker`，让它立即把 model_type
    记录到一个 list，然后返回。不再走 asyncio.create_task 调度。
    """
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_API_KEY", "test-key")

    captured_model_types: list = []

    from backend.app.services import icl as icl_svc

    async def _fake_worker(task_id: str, *, model_type=None, **_kwargs):
        captured_model_types.append(model_type)

    monkeypatch.setattr(icl_svc, "_icl_training_worker", _fake_worker)

    # 跑 start_icl_training
    from backend.app.db.session import init_db
    await init_db()

    # 显式 model_type=ICL1.0
    await icl_svc.start_icl_training(
        user_id=1,
        voice_name="测试音色",
        audio_bytes=FAKE_MP3,
        filename="test.mp3",
        model_type="ICL1.0",
    )
    # start_icl_training 末尾 `task = asyncio.create_task(_icl_training_worker(...))`；
    # worker 被替换后立即同步完成（不再做 DB 写、轮询），但 `asyncio.create_task` 仍会
    # 把 coroutine 投到后台；我们需要等 task 完成：
    for t in list(icl_svc._icl_bg_tasks):
        try:
            await t
        except Exception:
            pass

    assert "ICL1.0" in captured_model_types, f"应记录到 ICL1.0，实际 {captured_model_types}"

    # 默认（None）走 ICL2.0
    await icl_svc.start_icl_training(
        user_id=1,
        voice_name="测试音色2",
        audio_bytes=FAKE_MP3,
        filename="test.mp3",
    )
    for t in list(icl_svc._icl_bg_tasks):
        try:
            await t
        except Exception:
            pass

    assert "ICL2.0" in captured_model_types, f"应记录到 ICL2.0，实际 {captured_model_types}"


# ---------------------------------------------------------------------
# T-MT-1 / T-MT-6：HTTP /api/icl/voices 接收 model_type Form 字段
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_api_icl_voices_accepts_model_type_form(admin_token, monkeypatch, tmp_path):
    """HTTP 路径走通：传 model_type=DiT 应被透传到 client。

    策略：直接 monkeypatch service 的 `_icl_training_worker`，让它同步记录 model_type。
    """
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_API_KEY", "test-key")

    captured_model_types: list = []
    from backend.app.services import icl as icl_svc

    async def _fake_worker(task_id: str, *, model_type=None, **_kwargs):
        captured_model_types.append(model_type)

    monkeypatch.setattr(icl_svc, "_icl_training_worker", _fake_worker)

    from fastapi.testclient import TestClient
    from backend.app.main import app

    with TestClient(app) as client:
        # 1) 缺省 model_type → 走默认 ICL2.0
        r = client.post(
            "/api/icl/voices",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"voice_name": "音色1"},
            files={"file": ("ref.mp3", FAKE_MP3, "audio/mpeg")},
        )
    assert r.status_code == 200, f"缺省 model_type 应 200，实际 {r.status_code} body={r.text}"

    # 2) 显式 model_type=DiT
    with TestClient(app) as client:
        r = client.post(
            "/api/icl/voices",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"voice_name": "音色2", "model_type": "DiT"},
            files={"file": ("ref.mp3", FAKE_MP3, "audio/mpeg")},
        )
    assert r.status_code == 200, f"显式 DiT 应 200，实际 {r.status_code} body={r.text}"

    # 3) 非法 model_type → 400
    with TestClient(app) as client:
        r = client.post(
            "/api/icl/voices",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"voice_name": "音色3", "model_type": "BOGUS"},
            files={"file": ("ref.mp3", FAKE_MP3, "audio/mpeg")},
        )
    assert r.status_code == 400, f"非法 model_type 应 400，实际 {r.status_code} body={r.text}"
    assert "不支持的 model_type" in r.text

    # 校验透传：缺省 → ICL2.0；DiT → DiT
    # 等所有后台 task 跑完
    for t in list(icl_svc._icl_bg_tasks):
        try:
            await t
        except Exception:
            pass
    assert "ICL2.0" in captured_model_types, captured_model_types
    assert "DiT" in captured_model_types, captured_model_types