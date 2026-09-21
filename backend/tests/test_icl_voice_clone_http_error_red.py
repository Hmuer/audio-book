"""声音复刻（ICL）握手修复 RED（2026-09-21）。

用户反馈的真机日志：
    POST /api/icl/voices … 200（任务已入库）
    [icl_worker] FAIL HTTPStatusError: Server error '500 Internal Server Error'
                 for url 'https://openspeech.bytedance.com/api/v3/tts/voice_clone'

两个问题一起暴露：

1. **报错信息什么也没说** —— `_http_post_json` 直接 `resp.raise_for_status()`，
   而上游的错误码/错误信息全在 **body** 里（官方文档写明「训练失败时 HTTP 状态码
   不为 200」，body 带 `code`/`message`），raise_for_status 把 body 丢了，日志里
   只剩一句 `500 Internal Server Error`。

2. **请求体本来就不可能成功** —— 按官方 V3 文档（6561/2534906 + 2227958）逐字段核对：
   - `model_type` 是 **V1** 训练接口（/api/v1/mega_tts/audio/upload）的整型字段，
     V3 请求参数表里没有它（历史实现把它当 body 字段下发）；
   - `speaker_id` 必须要么是控制台购买音色槽位得到的 `S_xxx`（预付费），要么是固定
     字面值 `"custom_speaker_id"`（后付费，真实代号写在 `custom_speaker_id`）；
     历史实现塞的是自己生成的 `"icl_xxx"`；
   - 而且 `icl_xxx` 本身就是**非法代号** —— 官方防冲突正则里 `(?i:ICL_)` 是保留前缀；
   - `demo_text` 属于 `extra_params`，不是顶层字段。

覆盖：
  IH-1  HTTP 500 + 顶层 {code,message} → 异常带状态码 / 业务码 / 原文 / 排查提示
  IH-2  网关级 4xx + {header:{code,message}} 也能解析出 code
  IH-3  logid（X-Tt-Logid）必须进异常，便于找技术支持
  IH-4  查音色定位参数：预付费 S_xxx / 后付费成对
  IH-5  生成的音色代号必须躲开官方防冲突正则（随机 300 次）
  IH-6  create_training 撞上 HTTP 500 时，抛出的异常里能看到业务码（不再只剩 500）
  IH-7  45000030「resource not granted」要给出控制台开通清单 + 本次实际鉴权方式
        （这个报错跟请求体无关，最容易误判成代码 bug）
  IH-8  鉴权方式描述能区分新版 X-Api-Key / 旧版 X-Api-App-Key，且不泄露完整密钥
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAKE_MP3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 2048)


def _setup_client(monkeypatch, api_key: str = "test-api-key-xyz"):
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_API_KEY", api_key)
    return DoubaoICLClient()


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, headers: dict | None = None):
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}


class _FakeAsyncClient:
    """替掉 httpx.AsyncClient：不回网络，直接返回预设响应。"""

    def __init__(self, response: _FakeResponse, **_kwargs):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return self._response


def _patch_httpx(monkeypatch, response: _FakeResponse):
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response, **kw)
    )


# ---------------------------------------------------------------------
# IH-1：HTTP 500 + 顶层 code/message
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ih1_http_error_surfaces_body_code_and_hint(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLHTTPError

    client = _setup_client(monkeypatch)
    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            500,
            b'{"code":45001107,"message":"speaker_id not found"}',
            {"X-Tt-Logid": "logid-abc-123"},
        ),
    )

    with pytest.raises(DoubaoICLHTTPError) as ei:
        await client.create_training("张祥祥", FAKE_MP3, audio_format="m4a")

    msg = str(ei.value)
    assert "HTTP 500" in msg
    assert "45001107" in msg, "body 里的业务码必须出现在错误信息里"
    assert "speaker_id not found" in msg
    assert "custom_speaker_id" in msg, "45001107 应附带可执行的排查提示"
    assert ei.value.status_code == 500
    assert ei.value.code == 45001107
    assert ei.value.logid == "logid-abc-123"


# ---------------------------------------------------------------------
# IH-2：网关级 4xx，code/message 藏在 header 里
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ih2_gateway_4xx_parses_header_code(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLHTTPError

    client = _setup_client(monkeypatch)
    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            403,
            b'{"header":{"reqid":"r1","code":45000030,'
            b'"message":"[resource_id=x] requested resource not granted"}}',
        ),
    )

    with pytest.raises(DoubaoICLHTTPError) as ei:
        await client.query_training("S_abc123")

    assert ei.value.code == 45000030
    assert "未开通" in str(ei.value)


# ---------------------------------------------------------------------
# IH-3：拿不到结构化 body 时，原始 body 片段要带出来
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ih3_raw_body_included_when_not_json(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLHTTPError

    client = _setup_client(monkeypatch)
    _patch_httpx(monkeypatch, _FakeResponse(502, b"Bad Gateway"))

    with pytest.raises(DoubaoICLHTTPError) as ei:
        await client.query_training("S_abc123")

    assert "HTTP 502" in str(ei.value)
    assert "Bad Gateway" in str(ei.value)


# ---------------------------------------------------------------------
# IH-4：查音色的定位参数
# ---------------------------------------------------------------------
def test_ih4_speaker_lookup_payload_forms():
    from backend.app.ai.providers.doubao.icl import _speaker_lookup_payload

    # 预付费音色：控制台槽位 id，直接用 speaker_id 定位
    assert _speaker_lookup_payload("S_abc123") == {"speaker_id": "S_abc123"}
    # 后付费：必须成对
    assert _speaker_lookup_payload("iclvoiceabc") == {
        "speaker_id": "custom_speaker_id",
        "custom_speaker_id": "iclvoiceabc",
    }


# ---------------------------------------------------------------------
# IH-5：生成的音色代号必须合法
# ---------------------------------------------------------------------
def test_ih5_generated_speaker_id_is_legal():
    from backend.app.ai.providers.doubao.icl import (
        _OFFICIAL_SPEAKER_ID_FORBIDDEN_RE,
        _generate_custom_speaker_id,
    )

    for _ in range(300):
        sid = _generate_custom_speaker_id()
        assert 8 <= len(sid) <= 256
        assert sid[0].isalpha()
        assert not _OFFICIAL_SPEAKER_ID_FORBIDDEN_RE.search(sid), (
            f"{sid!r} 命中官方防冲突正则，上游会直接拒"
        )

    # 历史实现用的 "icl_xxx" 必须被判定为非法（防回归）
    assert _OFFICIAL_SPEAKER_ID_FORBIDDEN_RE.search("icl_2f1a4b6c8d")
    assert _OFFICIAL_SPEAKER_ID_FORBIDDEN_RE.search("ICL_abc123")


# ---------------------------------------------------------------------
# IH-6：create_training 撞 HTTP 500，错误信息里能看到业务码
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ih6_create_training_500_reports_business_code(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLHTTPError

    client = _setup_client(monkeypatch)
    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            500,
            b'{"code":45001001,"message":"[Invalid argument] invalid param"}',
            {"x-tt-logid": "logid-lower"},
        ),
    )

    with pytest.raises(DoubaoICLHTTPError) as ei:
        await client.create_training("张祥祥", FAKE_MP3, audio_format="m4a")

    msg = str(ei.value)
    assert "45001001" in msg and "invalid param" in msg
    assert "logid-lower" in msg


# ---------------------------------------------------------------------
# IH-7：45000030 → 控制台开通清单（真机 403 的处置指引）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ih7_resource_not_granted_hint_is_actionable(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLHTTPError

    client = _setup_client(monkeypatch)
    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            403,
            b'{"code":45000030,"message":"[resource_id=volc.megatts.timbre] '
            b'requested resource not granted"}',
            {"X-Tt-Logid": "logid-real-1"},
        ),
    )

    with pytest.raises(DoubaoICLHTTPError) as ei:
        await client.create_training("张祥祥", FAKE_MP3, audio_format="m4a")

    msg = str(ei.value)
    # 网关归一化出来的 resource_id 要带出来（用户拿它能直接找技术支持）
    assert "volc.megatts.timbre" in msg
    # 必须点明「后付费音色服务」是单独一项（用户「已开通声音复刻2.0」仍会踩的坑）
    assert "后付费音色服务" in msg
    # 项目隔离
    assert "项目" in msg
    # 本次实际鉴权方式
    assert "鉴权=" in msg
    assert ei.value.code == 45000030
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------
# IH-8：鉴权方式描述
# ---------------------------------------------------------------------
def test_ih8_auth_mode_desc_distinguishes_console_versions(monkeypatch):
    monkeypatch.setenv("MEGACORE_ACCESS_KEY_FROM_ENV", "")
    # 新版控制台：非纯数字 key → X-Api-Key
    new_client = _setup_client(monkeypatch, api_key="abcdefg-1234-5678")
    desc = new_client._auth_mode_desc()
    assert "X-Api-Key" in desc and "旧版" not in desc
    assert "5678" in desc, "应保留末 4 位便于比对"
    assert "abcdefg" not in desc, "不能把密钥明文写进日志/报错"

    # 旧版控制台：纯数字 AppID → X-Api-App-Key
    old_client = _setup_client(monkeypatch, api_key="1234567890")
    old_desc = old_client._auth_mode_desc()
    assert "X-Api-App-Key" in old_desc and "旧版" in old_desc
    assert "1234567890" not in old_desc
