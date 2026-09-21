"""从豆包控制台同步已有复刻音色 RED（2026-09-21）。

需求：用户在豆包控制台/页面里做的复刻音色不会自动出现在本平台 ——
`icl_voices_for_user()` 只读本地 `icl_training_tasks` 表。补一个「同步」能力：
调**音色管理 HTTP** `BatchListMegaTTSTrainStatus`（文档 6561/2235883，
控制台文档里说的「批量查询接口」）把已购买的音色槽位拉进来。

设计要点（本文件锁定的契约）：
  1. **只读**：只调查询接口，不在豆包侧创建/删除/改名任何音色；
  2. **幂等**：按 `cloned_voice_id`(=SpeakerID) upsert，重复同步不产生重复行；
  3. **不覆盖**：已存在的行只更新 状态/别名，不动参考音频等本地信息；
  4. **落成普通任务行**：于是它会自然出现在「声音复刻」列表、`/api/voices` 音色库、
     角色推荐候选池里；合成时 `icl:S_xxx` 走 seed-icl-2.0；
  5. 官方 `State` 枚举 → 本地 status 有明确映射（含 Expired/Reclaimed 的失败原因）。

覆盖：
  CS-1  State → status 映射 + 同步后能被 icl_voices_for_user 看见
  CS-2  幂等：第二次同步 created=0、行数不增
  CS-3  不覆盖已有训练任务的本地信息（参考音频、别名反向更新）
  CS-4  缺 APP_ID / 缺 SK → ValueError（提示去设置页补）
  CS-5  控制面客户端：分页（本页不足一页即停）+ 请求体/鉴权头正确
  CS-6  控制面错误：HTTP 401 与 ResponseMetadata.Error 都要带出原因
  CS-7  HTTP 路由 POST /api/icl/sync：200 结构 / 配置缺失 → 400
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# 假的控制面客户端 / httpx
# ---------------------------------------------------------------------
class _FakeICLClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    async def batch_list_train_status(self, app_id, **_kw):
        self.calls += 1
        return list(self._rows)


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, headers: dict | None = None):
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}


class _FakeAsyncClient:
    """按顺序吐出预设响应；记录每次请求的 body/headers。"""

    def __init__(self, responses, sink: list):
        self._responses = list(responses)
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None, **kwargs):
        self._sink.append({"url": url, "content": content, "headers": headers or {}})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _ok_page(speakers: list[dict]) -> bytes:
    import json as _json
    return _json.dumps({"Result": {"Statuses": speakers}}).encode("utf-8")


def _patch_httpx(monkeypatch, responses, sink):
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(responses, sink)
    )


def _patch_cfg(monkeypatch, *, app_id="1234567890", ak="ak-test", sk="sk-test"):
    from backend.app.core import config as cfgmod

    fields = {"app_id": app_id, "api_key": ak, "secret": sk}
    monkeypatch.setattr(cfgmod, "doubao_field", lambda name: fields.get(name, ""))


# ---------------------------------------------------------------------
# CS-1：State → status 映射 + 出现在可用音色里
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cs1_state_mapping_and_visible_as_voice(monkeypatch):
    from backend.app.db.session import init_db
    from backend.app.services import icl as icl_svc

    await init_db()
    rows = [
        {"SpeakerID": "S_ok1", "State": "Success", "Alias": "张祥祥"},
        {"SpeakerID": "S_ok2", "State": "Active", "Alias": ""},
        {"SpeakerID": "S_training", "State": "Training"},
        {"SpeakerID": "S_expired", "State": "Expired"},
        {"SpeakerID": "S_reclaimed", "State": "Reclaimed"},
        {"SpeakerID": "S_unknown", "State": "Unknown"},
    ]
    monkeypatch.setattr(icl_svc, "get_icl_client", lambda: _FakeICLClient(rows))

    r = await icl_svc.sync_icl_voices_from_console(user_id=1)

    assert r["total"] == 6
    assert r["created"] == 6 and r["updated"] == 0
    assert r["usable"] == 2
    by_id = {i["speaker_id"]: i for i in r["items"]}
    assert by_id["S_ok1"]["status"] == 4
    assert by_id["S_ok2"]["status"] == 4
    assert by_id["S_training"]["status"] == 1
    assert by_id["S_expired"]["status"] == 3
    assert by_id["S_reclaimed"]["status"] == 3
    assert by_id["S_unknown"]["status"] == 0
    # 别名优先作展示名；没别名就用 speaker_id
    assert by_id["S_ok1"]["name"] == "张祥祥"
    assert by_id["S_ok2"]["name"] == "S_ok2"

    # 只有可用的两个会出现在音色库里，且 id 形如 icl:S_xxx（合成时路由到 seed-icl-2.0）
    voices = await icl_svc.icl_voices_for_user(1)
    ids = sorted(v["id"] for v in voices)
    assert ids == ["icl:S_ok1", "icl:S_ok2"]
    assert {v["name"] for v in voices} == {"张祥祥", "S_ok2"}

    # Expired/Reclaimed 要有可读原因（前端「我的复刻音色」直接展示 error_msg）
    tasks = {t["cloned_voice_id"]: t for t in await icl_svc.list_icl_tasks(1)}
    assert "过期" in (tasks["S_expired"]["error_msg"] or "")
    assert "回收" in (tasks["S_reclaimed"]["error_msg"] or "")


# ---------------------------------------------------------------------
# CS-2 / CS-3：幂等 + 不覆盖本地信息
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cs2_idempotent_and_keeps_local_fields(monkeypatch):
    from backend.app.db.session import init_db
    from backend.app.services import icl as icl_svc

    await init_db()
    rows = [{"SpeakerID": "S_ok1", "State": "Success", "Alias": "张祥祥"}]
    fake = _FakeICLClient(rows)
    monkeypatch.setattr(icl_svc, "get_icl_client", lambda: fake)

    first = await icl_svc.sync_icl_voices_from_console(user_id=1)
    assert (first["created"], first["updated"]) == (1, 0)

    # 模拟「这条行原本是平台内训练出来的」，本地有参考音频
    from backend.app.db.session import get_session_factory
    factory = get_session_factory()
    async with factory() as s:
        from sqlalchemy import select
        from backend.app.db.models import IclTrainingTask
        row = (await s.execute(
            select(IclTrainingTask).where(IclTrainingTask.cloned_voice_id == "S_ok1")
        )).scalar_one()
        row.reference_audio_path = "/data/icl/ref.mp3"
        await s.commit()

    # 第二次同步：同名音色改了别名 + 变成过期
    rows[0] = {"SpeakerID": "S_ok1", "State": "Expired", "Alias": "张祥祥-旧"}
    second = await icl_svc.sync_icl_voices_from_console(user_id=1)

    assert (second["created"], second["updated"]) == (0, 1), "重复同步不应再新增行"
    async with factory() as s:
        from sqlalchemy import select
        from backend.app.db.models import IclTrainingTask
        all_rows = (await s.execute(select(IclTrainingTask))).scalars().all()
    assert len(all_rows) == 1, f"幂等失败，出现重复行：{len(all_rows)}"
    assert all_rows[0].voice_name == "张祥祥-旧"
    assert all_rows[0].status == 3
    assert all_rows[0].reference_audio_path == "/data/icl/ref.mp3", "不得覆盖本地参考音频"


# ---------------------------------------------------------------------
# CS-4 / CS-5 / CS-6：控制面客户端契约
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cs4_missing_app_id_or_sk_raises_with_guidance(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    _patch_cfg(monkeypatch, app_id="", ak="ak", sk="sk")
    with pytest.raises(ValueError, match="APP_ID"):
        await DoubaoICLClient().batch_list_train_status("")

    _patch_cfg(monkeypatch, app_id="123", ak="ak", sk="")
    with pytest.raises(ValueError, match="SK"):
        await DoubaoICLClient().batch_list_train_status("123")


@pytest.mark.asyncio
async def test_cs5_pagination_and_request_shape(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    _patch_cfg(monkeypatch)
    sink: list = []

    page1 = [{"SpeakerID": f"S_p1_{i}", "State": "Success"} for i in range(100)]
    page2 = [{"SpeakerID": "S_p2_0", "State": "Success"}]
    _patch_httpx(
        monkeypatch,
        [_FakeResponse(200, _ok_page(page1)), _FakeResponse(200, _ok_page(page2))],
        sink,
    )

    rows = await DoubaoICLClient().batch_list_train_status("1234567890", page_size=100)

    assert len(rows) == 101, "首页满 100 条应继续翻页，第二页不满即停"
    assert len(sink) == 2
    # 请求形状：Action/Version 在 query，AppID 在 body，AK/SK 签名在 header
    assert "Action=BatchListMegaTTSTrainStatus" in sink[0]["url"]
    assert "Version=2023-11-07" in sink[0]["url"]
    assert '"AppID":"1234567890"' in sink[0]["content"]
    assert '"PageNumber":1' in sink[0]["content"]
    assert '"PageNumber":2' in sink[1]["content"]
    assert sink[0]["headers"]["Authorization"].startswith("HMAC-SHA256 Credential=ak-test/")


@pytest.mark.asyncio
async def test_cs6_control_plane_errors_are_explained(monkeypatch):
    from backend.app.ai.providers.doubao.icl import (
        DoubaoICLClient,
        DoubaoICLHTTPError,
    )

    _patch_cfg(monkeypatch)
    sink: list = []

    # (a) 鉴权失败：401 + ResponseMetadata.Error（AK/SK 不是合成的 API Key）
    _patch_httpx(
        monkeypatch,
        [_FakeResponse(401, b'{"ResponseMetadata":{"Error":'
                            b'{"Code":"AccessDenied","Message":"signature mismatch"}}}')],
        sink,
    )
    with pytest.raises(DoubaoICLHTTPError) as ei:
        await DoubaoICLClient().batch_list_train_status("123")
    msg = str(ei.value)
    assert "AccessDenied" in msg and "signature mismatch" in msg
    assert "AK/SK" in msg, "应提示这是 AK/SK 签名鉴权问题"

    # (b) HTTP 200 但 body 里带业务错误
    sink.clear()
    _patch_httpx(
        monkeypatch,
        [_FakeResponse(200, b'{"ResponseMetadata":{"Error":'
                            b'{"Code":"OperationDenied.InvalidParameter","Message":"bad"}}}')],
        sink,
    )
    with pytest.raises(DoubaoICLHTTPError, match="InvalidParameter"):
        await DoubaoICLClient().batch_list_train_status("123")


# ---------------------------------------------------------------------
# CS-7：HTTP 路由
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cs7_api_endpoint(monkeypatch, admin_token):
    from backend.app.db.session import init_db
    from backend.app.services import icl as icl_svc

    await init_db()
    monkeypatch.setattr(
        icl_svc,
        "get_icl_client",
        lambda: _FakeICLClient([{"SpeakerID": "S_api1", "State": "Success", "Alias": "小雅"}]),
    )

    from fastapi.testclient import TestClient
    from backend.app.main import app

    with TestClient(app) as client:
        r = client.post("/api/icl/sync", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1 and body["usable"] == 1
    assert body["items"][0]["speaker_id"] == "S_api1"

    # 配置缺失（APP_ID 空）→ 400，且带可执行提示
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod, "doubao_field", lambda name: "")
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient
    monkeypatch.setattr(
        icl_svc, "get_icl_client", lambda: DoubaoICLClient()
    )
    with TestClient(app) as client:
        r2 = client.post("/api/icl/sync", headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.status_code == 400, r2.text
    assert "APP_ID" in r2.text
