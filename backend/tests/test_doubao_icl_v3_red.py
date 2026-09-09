"""P0-4 RED 测试：豆包 ICL v3 接口协议（voice_clone + get_voice）。

数据来源：
  - https://www.volcengine.com/docs/6561/2227958?lang=zh （声音复刻 API-V3 文档）
  - https://www.volcengine.com/docs/6561/2535742?lang=en （音色查询 HTTP 文档）

本测试覆盖 8 个关键约束：
  T-IC3-V1  默认端点是 v3 协议（voice_clone_url + get_voice_url 都含 /v3/）
  T-IC3-V2  _auth_headers 用 X-Api-Key（新版控制台），非纯数字 key 不走旧版鉴权
  T-IC3-V3  _auth_headers 收到纯数字 key 时走 X-Api-App-Key + X-Api-Access-Key（旧版）
  T-IC3-V4  create_training 的 payload 严格匹配官方 schema（speaker_id / audio.data base64 /
             audio.format / language / model_type）
  T-IC3-V5  create_training 业务码非 0 抛 RuntimeError
  T-IC3-V6  query_training 状态 0/1/2/3/4 全部映射到 progress / cloned_voice_id / error
  T-IC3-V7  query_training NotFound (status=0) 不抛错
  T-IC3-V8  speaker_status[0] 的 model_type 和 demo_audio 被透出
  T-IC3-V9  audio_bytes 过小（< 512）抛 ValueError
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAKE_MP3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 2048)


def _setup_client(monkeypatch, api_key: str = "test-api-key-xyz"):
    """构造一个配置好 api_key 的 client，绕开 _resolve_api_key 真实查找链。"""
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_API_KEY", api_key)
    return DoubaoICLClient()


# ---------------------------------------------------------------------
# T-IC3-V1：默认端点是 v3 协议
# ---------------------------------------------------------------------
def test_default_endpoints_are_v3(monkeypatch):
    client = _setup_client(monkeypatch)
    # voice_clone_url 必须含 /api/v3/tts/voice_clone
    assert "/api/v3/tts/voice_clone" in client.voice_clone_url
    assert client.voice_clone_url == (
        "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
    )
    # get_voice_url 必须含 /api/v3/tts/get_voice
    assert "/api/v3/tts/get_voice" in client.get_voice_url
    assert client.get_voice_url == (
        "https://openspeech.bytedance.com/api/v3/tts/get_voice"
    )


def test_legacy_v1_base_url_is_rewritten_to_v3(monkeypatch):
    """如果用户环境变量还配着 v1 自造端点，必须自动重写到 v3。"""
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_BASE_URL",
                        "https://openspeech.bytedance.com/api/v1/voice_clone")
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_ICL_API_KEY", "test-ak")
    client = DoubaoICLClient()
    assert client.voice_clone_url == (
        "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
    )


# ---------------------------------------------------------------------
# T-IC3-V2：X-Api-Key（新版控制台）
# ---------------------------------------------------------------------
def test_auth_headers_use_x_api_key_for_new_console(monkeypatch):
    client = _setup_client(monkeypatch, api_key="abc-new-console-key")
    headers = client._auth_headers()
    assert headers["X-Api-Key"] == "abc-new-console-key"
    assert "X-Api-App-Key" not in headers
    assert "X-Api-Access-Key" not in headers
    assert "X-Api-Request-Id" in headers
    assert headers["X-Api-Request-Id"].startswith("icl-")


# ---------------------------------------------------------------------
# T-IC3-V3：X-Api-App-Key + X-Api-Access-Key（旧版控制台：纯数字 APP_ID）
# ---------------------------------------------------------------------
def test_auth_headers_use_legacy_keys_for_pure_digit_app_id(monkeypatch):
    client = _setup_client(monkeypatch, api_key="1234567890")
    headers = client._auth_headers()
    assert headers["X-Api-App-Key"] == "1234567890"
    assert "X-Api-Access-Key" in headers
    assert "X-Api-Key" not in headers


# ---------------------------------------------------------------------
# T-IC3-V4：create_training payload 严格匹配官方 schema
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_training_payload_matches_v3_schema(monkeypatch):
    client = _setup_client(monkeypatch)
    captured: dict = {}

    async def _fake_post(url, payload, **_kwargs):
        captured["url"] = url
        captured["payload"] = payload
        return {"code": 0, "data": {"speaker_id": payload["speaker_id"]}}

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    sid = await client.create_training("我的声线", FAKE_MP3, audio_format="mp3")

    assert sid == captured["payload"]["speaker_id"]
    p = captured["payload"]

    # 官方文档要求的必填字段
    assert p["speaker_id"].startswith("icl_")
    assert isinstance(p["audio"], dict)
    assert p["audio"]["format"] == "mp3"
    # audio.data 是 base64 字符串
    decoded = base64.b64decode(p["audio"]["data"], validate=False)
    assert len(decoded) == len(FAKE_MP3)
    # language 必传（建议设置）
    assert "language" in p
    # model_type 必传（区分 ICL 1.0 / 2.0 / DiT）
    assert p["model_type"] in ("ICL1.0", "ICL2.0", "DiT")

    # voice_name 仅业务方本地记录，不应下发到豆包
    assert "voice_name" not in p
    # 历史字段 audio_b64 / reqid 等不应再出现
    assert "audio_b64" not in p
    assert "reqid" not in p


# ---------------------------------------------------------------------
# T-IC3-V5：业务码非 0 抛 RuntimeError
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_training_raises_on_business_error(monkeypatch):
    client = _setup_client(monkeypatch)

    async def _fake_post(url, payload, **_kwargs):
        return {"code": 45001109, "message": "WER过高"}

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="code=45001109"):
        await client.create_training("我的声线", FAKE_MP3, audio_format="mp3")


# ---------------------------------------------------------------------
# T-IC3-V6：query_training 状态映射
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_training_maps_all_status_values(monkeypatch):
    client = _setup_client(monkeypatch)

    async def _make_fake_post(status_value):
        async def _fake_post(url, payload, **_kwargs):
            return {
                "code": 0,
                "speaker_id": payload["speaker_id"],
                "status": status_value,
                "speaker_status": [],
            }
        return _fake_post

    # status=1 Training → progress=50, cloned_voice_id=None
    client._http_post_json = await _make_fake_post(1)  # type: ignore[method-assign]
    r = await client.query_training("icl_abc")
    assert r["status"] == 1
    assert r["progress"] == 50
    assert r["cloned_voice_id"] is None
    assert r["error"] is None

    # status=2 Success → progress=100, cloned_voice_id=input
    client._http_post_json = await _make_fake_post(2)  # type: ignore[method-assign]
    r = await client.query_training("icl_abc")
    assert r["status"] == 2
    assert r["progress"] == 100
    assert r["cloned_voice_id"] == "icl_abc"

    # status=4 Active → 同 Success
    client._http_post_json = await _make_fake_post(4)  # type: ignore[method-assign]
    r = await client.query_training("icl_abc")
    assert r["status"] == 4
    assert r["progress"] == 100
    assert r["cloned_voice_id"] == "icl_abc"

    # status=3 Failed → error 字段透出
    async def _fake_fail(url, payload, **_kwargs):
        return {
            "code": 0, "speaker_id": payload["speaker_id"],
            "status": 3, "message": "音频质量不足",
        }
    client._http_post_json = _fake_fail  # type: ignore[method-assign]
    r = await client.query_training("icl_abc")
    assert r["status"] == 3
    assert "音频质量不足" in r["error"]
    assert r["cloned_voice_id"] is None


# ---------------------------------------------------------------------
# T-IC3-V7：query_training NotFound (status=0) 不抛错
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_training_not_found_does_not_raise(monkeypatch):
    client = _setup_client(monkeypatch)

    async def _fake_post(url, payload, **_kwargs):
        return {
            "code": 0,
            "speaker_id": payload["speaker_id"],
            "status": 0,  # NotFound
        }

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    r = await client.query_training("icl_不存在")
    assert r["status"] == 0
    assert r["cloned_voice_id"] is None
    assert r["error"] is None


# ---------------------------------------------------------------------
# T-IC3-V8：speaker_status[0] 透出 model_type 和 demo_audio
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_training_passes_through_speaker_status(monkeypatch):
    client = _setup_client(monkeypatch)

    async def _fake_post(url, payload, **_kwargs):
        return {
            "code": 0,
            "speaker_id": payload["speaker_id"],
            "status": 2,
            "speaker_status": [
                {"model_type": 5, "demo_audio": "https://x/icl_abc.mp3"},
                {"model_type": 4, "demo_audio": "https://x/icl_abc_v1.mp3"},
            ],
        }

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    r = await client.query_training("icl_abc")
    assert r["model_type"] == 5
    assert r["demo_audio"] == "https://x/icl_abc.mp3"


# ---------------------------------------------------------------------
# T-IC3-V9：audio_bytes 过小抛 ValueError
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_training_rejects_tiny_audio(monkeypatch):
    client = _setup_client(monkeypatch)
    with pytest.raises(ValueError, match="参考音频过小"):
        await client.create_training("测试", b"\x00" * 100, audio_format="mp3")


# ---------------------------------------------------------------------
# T-IC3-V10：RPM 限流调用次数正确（create + query 各自 1 次）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rpm_limiter_invoked_once_per_call(monkeypatch):
    client = _setup_client(monkeypatch)

    # 把 _doubao_rpm_wait_acquire 替换成计数 spy（icl.py 里直接 import 到了本地名空间）
    from backend.app.ai.providers.doubao import icl as icl_mod
    wait_calls: list[str] = []

    async def _spy_wait(kind: str) -> None:
        wait_calls.append(kind)

    monkeypatch.setattr(icl_mod, "_doubao_rpm_wait_acquire", _spy_wait)

    async def _fake_post(url, payload, **_kwargs):
        return {
            "code": 0,
            "data": {"speaker_id": payload["speaker_id"]},
        }

    client._http_post_json = _fake_post  # type: ignore[method-assign]
    await client.create_training("x", FAKE_MP3, audio_format="mp3")
    await client.query_training("icl_abc")

    # create 走 "icl"，query 也走 "icl"（worker 共享同一桶）
    assert wait_calls == ["icl", "icl"]
