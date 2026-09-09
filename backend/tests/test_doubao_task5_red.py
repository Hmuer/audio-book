"""Task 5 RED — ICL 声音复刻后端：训练任务 + 轮询 worker + icl: 合成路由 + /api/voices 聚合。

T-IC1  DoubaoICLClient.create_training 构造正确 payload 并返回豆包任务 id。
T-IC2  DoubaoICLClient.query_training 解析豆包状态（status/progress/voice_id）。
T-IC3  start_icl_training：落库 status=0、参考音频存盘、后台 worker 完成后 status=4 且
       cloned_voice_id 写入（模拟豆包训练成功）。
T-IC4  worker 轮询到 status=3（失败）→ 任务 status=3 + error_msg。
T-IC5  list_icl_tasks / get_icl_task / delete_icl_task。
T-IC6  /api/voices 聚合包含当前用户可用的 ICL 音色（icl:<clone_id>，provider=icl）。
T-IC7  合成路由：icl: 前缀音色 → DoubaoTTSProvider（strips icl: 前缀传 clone id）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAKE_MP3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 2048)


# ---------------------------------------------------------------------
# T-IC1 / T-IC2: DoubaoICLClient
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_icl_client_create_training_payload_and_taskid():
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    client = DoubaoICLClient()
    captured: dict = {}

    async def _fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"code": 0, "data": {"task_id": "doubao-icl-task-88"}}

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    task_id = await client.create_training("我的声线", FAKE_MP3, audio_format="mp3")
    assert task_id == "doubao-icl-task-88"
    assert "voice_clone" in captured["url"] or "icl" in captured["url"].lower()
    p = captured["payload"]
    assert p["voice_name"] == "我的声线"
    # 音频必须以某种形式上传（base64 或 multipart 字段）
    assert "audio_b64" in p or "audio" in p


@pytest.mark.asyncio
async def test_icl_client_query_training_parses_status():
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    client = DoubaoICLClient()

    async def _fake_post(url, payload):
        return {
            "code": 0,
            "data": {"status": 4, "progress": 100, "voice_id": "clone_xyz"},
        }

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    r = await client.query_training("doubao-icl-task-88")
    assert r["status"] == 4
    assert r["progress"] == 100
    assert r["cloned_voice_id"] == "clone_xyz"


# ---------------------------------------------------------------------
# 测试用 mock ICL client（服务层 worker 轮询注入）
# ---------------------------------------------------------------------
class _MockICLClient:
    def __init__(self, final_status: int = 4):
        self.final_status = final_status
        self.created: list[tuple[str, bytes]] = []
        self.query_count = 0

    async def create_training(self, voice_name: str, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        self.created.append((voice_name, audio_bytes))
        return "dtid-mock-1"

    async def query_training(self, doubao_task_id: str) -> dict:
        self.query_count += 1
        if self.query_count >= 2:
            return {
                "status": self.final_status,
                "progress": 100 if self.final_status in (2, 4) else 0,
                "cloned_voice_id": "clone_abc" if self.final_status in (2, 4) else None,
                "error": "训练失败：参考音频质量不足" if self.final_status == 3 else None,
            }
        return {"status": 1, "progress": 30, "cloned_voice_id": None, "error": None}


async def _wait_task_done(task_id: str, timeout_s: float = 15.0) -> None:
    """轮询等待训练 worker 结束（status 离开 0/1）。"""
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import IclTrainingTask

    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            t = await s.get(IclTrainingTask, task_id)
            if t and t.status not in (0, 1):
                return
        await asyncio.sleep(0.05)
    pytest.fail("ICL 训练 worker 超时未结束")


# ---------------------------------------------------------------------
# T-IC3: start_icl_training 全链路（mock 豆包）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_icl_training_success_flow(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import User, IclTrainingTask
    from backend.app.services import icl as icl_svc
    from backend.app.core import config as cfgmod

    await init_db()
    # 建 user
    factory = get_session_factory()
    async with factory() as s:
        u = User(username=f"u{uuid.uuid4().hex[:6]}", password_hash="x",
                 is_active=True, must_change_password=False)
        s.add(u)
        await s.commit()
        uid = u.id

    mock = _MockICLClient(final_status=4)
    prev = icl_svc._icl_client
    prev_interval = cfgmod.settings.DOUBAO_ICL_POLL_INTERVAL_SECS
    icl_svc._icl_client = mock
    cfgmod.settings.DOUBAO_ICL_POLL_INTERVAL_SECS = 0.05
    try:
        resp = await icl_svc.start_icl_training(
            user_id=uid, voice_name="测试声线", audio_bytes=FAKE_MP3,
            filename="ref.mp3",
        )
        task_id = resp["task_id"]
        assert resp["status"] == 0
        assert resp["voice_name"] == "测试声线"

        # 参考音频必须存盘
        async with factory() as s:
            row = await s.get(IclTrainingTask, task_id)
            assert row is not None
            assert row.reference_audio_path
            assert Path(row.reference_audio_path).is_file()
            assert row.reference_audio_size_bytes == len(FAKE_MP3)

        # worker 完成后 status=4 且 clone id 写入
        await _wait_task_done(task_id)
        async with factory() as s:
            row = await s.get(IclTrainingTask, task_id)
            assert row.status == 4, f"期望 4，实际 {row.status}"
            assert row.cloned_voice_id == "clone_abc"
            assert row.error_msg is None
        assert len(mock.created) == 1
    finally:
        icl_svc._icl_client = prev
        cfgmod.settings.DOUBAO_ICL_POLL_INTERVAL_SECS = prev_interval


# ---------------------------------------------------------------------
# T-IC4: 训练失败路径
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_icl_training_failure_flow(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import User
    from backend.app.services import icl as icl_svc
    from backend.app.core import config as cfgmod

    await init_db()
    factory = get_session_factory()
    async with factory() as s:
        u = User(username=f"u{uuid.uuid4().hex[:6]}", password_hash="x",
                 is_active=True, must_change_password=False)
        s.add(u)
        await s.commit()
        uid = u.id

    mock = _MockICLClient(final_status=3)
    prev = icl_svc._icl_client
    prev_interval = cfgmod.settings.DOUBAO_ICL_POLL_INTERVAL_SECS
    icl_svc._icl_client = mock
    cfgmod.settings.DOUBAO_ICL_POLL_INTERVAL_SECS = 0.05
    try:
        resp = await icl_svc.start_icl_training(
            user_id=uid, voice_name="失败声线", audio_bytes=FAKE_MP3,
            filename="ref.mp3",
        )
        await _wait_task_done(resp["task_id"])
        async with factory() as s:
            from backend.app.db.models import IclTrainingTask
            row = await s.get(IclTrainingTask, resp["task_id"])
            assert row.status == 3
            assert row.error_msg and "失败" in row.error_msg
            assert row.cloned_voice_id is None
    finally:
        icl_svc._icl_client = prev
        cfgmod.settings.DOUBAO_ICL_POLL_INTERVAL_SECS = prev_interval


# ---------------------------------------------------------------------
# T-IC5: 列表 / 详情 / 删除
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_icl_task_list_and_delete(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import User, IclTrainingTask
    from backend.app.services import icl as icl_svc

    await init_db()
    factory = get_session_factory()
    async with factory() as s:
        u = User(username=f"u{uuid.uuid4().hex[:6]}", password_hash="x",
                 is_active=True, must_change_password=False)
        u2 = User(username=f"u{uuid.uuid4().hex[:6]}", password_hash="x",
                  is_active=True, must_change_password=False)
        s.add_all([u, u2])
        await s.commit()
        uid, uid2 = u.id, u2.id

    async with factory() as s:
        s.add_all([
            IclTrainingTask(task_id="icl-a", user_id=uid, voice_name="声线A",
                            reference_audio_path="/tmp/a.mp3", reference_audio_size_bytes=100,
                            status=4, cloned_voice_id="clone_a"),
            IclTrainingTask(task_id="icl-b", user_id=uid, voice_name="声线B",
                            reference_audio_path="/tmp/b.mp3", reference_audio_size_bytes=100,
                            status=1),
            IclTrainingTask(task_id="icl-c", user_id=uid2, voice_name="他人声线",
                            reference_audio_path="/tmp/c.mp3", reference_audio_size_bytes=100,
                            status=4, cloned_voice_id="clone_c"),
        ])
        await s.commit()

    # 列表按 user 过滤
    lst = await icl_svc.list_icl_tasks(uid)
    assert {t["task_id"] for t in lst} == {"icl-a", "icl-b"}

    # 详情
    detail = await icl_svc.get_icl_task("icl-a")
    assert detail is not None
    assert detail["task_id"] == "icl-a"
    assert detail["cloned_voice_id"] == "clone_a"

    # 删除：他人任务不可删
    assert await icl_svc.delete_icl_task(uid2, "icl-a") is False
    # 删除：本人
    assert await icl_svc.delete_icl_task(uid, "icl-a") is True
    async with factory() as s:
        assert await s.get(IclTrainingTask, "icl-a") is None


# ---------------------------------------------------------------------
# T-IC6: /api/voices 聚合 ICL 音色（当前用户可用）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_voices_includes_icl_for_user(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import User, IclTrainingTask
    from backend.app.api.routes import list_voices

    await init_db()
    factory = get_session_factory()
    async with factory() as s:
        u = User(username=f"u{uuid.uuid4().hex[:6]}", password_hash="x",
                 is_active=True, must_change_password=False)
        s.add(u)
        await s.commit()
        uid = u.id
        s.add_all([
            IclTrainingTask(task_id="icl-x", user_id=uid, voice_name="我的克隆声",
                            reference_audio_path="/tmp/x.mp3", reference_audio_size_bytes=100,
                            status=4, cloned_voice_id="clone_x"),
            IclTrainingTask(task_id="icl-y", user_id=uid, voice_name="训练中的声",
                            reference_audio_path="/tmp/y.mp3", reference_audio_size_bytes=100,
                            status=1),
        ])
        await s.commit()

    result = await list_voices(icl_user_id=uid)
    ids = {v["id"] for v in result["voices"]}
    assert "icl:clone_x" in ids, f"聚合结果应包含可用 ICL 音色，实际 ids={sorted(ids)[:5]}..."
    assert "icl:clone_y" not in ids, "训练中/失败的 ICL 音色不应出现"
    icl_v = next(v for v in result["voices"] if v["id"] == "icl:clone_x")
    assert icl_v["provider"] == "icl"
    assert icl_v["name"] == "我的克隆声"


# ---------------------------------------------------------------------
# T-IC7: icl: 前缀音色路由到 Doubao provider 且剥离前缀传 clone id
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_synth_routes_icl_prefix_to_doubao(monkeypatch):
    from backend.app.ai.factory import get_tts_by_voice_id, get_tts
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")

    inst = get_tts_by_voice_id("icl:clone_x")
    assert isinstance(inst, DoubaoTTSProvider), (
        f"icl: 前缀必须路由到 DoubaoTTSProvider，实际 {type(inst).__name__}"
    )
    # 同一 provider 实例缓存
    assert inst is get_tts("doubao")

    captured: dict = {}

    async def _fake_post_json(url, headers, payload):
        captured["payload"] = payload
        # 走 v1 协议：返回业务码 3000 + base64 MP3
        import base64 as _b64
        return (
            {"code": 3000, "message": "Success", "data": _b64.b64encode(FAKE_MP3).decode("ascii")},
            "logid-test",
        )

    monkeypatch.setattr(
        "backend.app.ai.providers.doubao.tts._post_json_for_v1", _fake_post_json
    )
    data, dur = await inst.synthesize_to_bytes("你好世界", "icl:clone_x")
    assert len(data) > 0
    p = captured["payload"]
    # speaker 传 clone id（剥离 icl: 前缀）
    assert p["voice_id"] == "clone_x"
    assert p["speaker"] == "clone_x"
    assert p["audio"]["voice_type"] == "clone_x"
