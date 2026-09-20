"""P-icl-v1：验证 v1 DoubaoTTSProvider 遇到 ICL 复刻音色时的实际行为。

按官方文档：
  - 复刻音色（训练后得到的 speaker_id 形如 S_xxx / icl_xxx）合成时
  - v1 端点 `/api/v1/tts` 需要 cluster=volcano_icl 而非 volcano_tts
  - v3 端点 `/api/v3/tts/unidirectional` 通过 X-Api-Resource-Id=seed-icl-2.0 路由

覆盖：
  T-ICL-V1-1  v1 provider 遇到 S_/icl_ 前缀音色：cluster 必须为 volcano_icl
  T-ICL-V1-2  v1 provider 遇到普通 BV/zh_ 音色：cluster 仍为 volcano_tts
  T-ICL-V1-3  v1 provider 不会自动跳过 ICL 音色（需要显式提示 / 兜底）
  T-ICL-V1-4  v3 provider 遇到 S_/icl_ 音色：X-Api-Resource-Id = seed-icl-2.0
  T-ICL-V1-5  v3 provider 遇到普通音色：X-Api-Resource-Id = seed-tts-2.0
              （显式 1.0 入参仍如实映射，不静默改写）
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 构造一个有效 MP3 字节（128kbps, 1 帧，0.026s）
def _fake_mp3_bytes() -> bytes:
    # 1 帧 MPEG1 Layer3 128kbps 44100Hz ≈ 417 bytes
    # 头 4 字节 0xFFFB9000 + 413 字节 data
    return b"\xff\xfb\x90\x00" + (b"\x00" * 413)


# ---------------------------------------------------------------------
# T-ICL-V1-1：v1 复刻音色 cluster 必须是 volcano_icl
# ---------------------------------------------------------------------
def test_v1_icl_speaker_uses_volcano_icl_cluster():
    """v1 provider 收到 S_/icl_ 前缀 speaker，构造的 payload cluster 应为 volcano_icl。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    p = DoubaoTTSProvider()
    payload = p._build_payload("测试文本", "S_abc123def", emotion="calm", speed=1.0)
    cluster = payload.get("app", {}).get("cluster")
    assert cluster == "volcano_icl", (
        f"复刻音色 v1 合成应使用 cluster=volcano_icl，实际={cluster!r}。\n"
        f"若 cluster=volcano_tts，豆包官方会按标准 TTS 路由，speaker_id=S_xxx 不存在 → 业务码错误。"
    )


def test_v1_icl_prefix_speaker_uses_volcano_icl_cluster():
    """v1 provider 收到 icl_xxx 前缀 speaker（来自我们生成的 speaker_id）。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    p = DoubaoTTSProvider()
    payload = p._build_payload("测试", "icl_abc123def456", emotion="calm", speed=1.0)
    assert payload["app"]["cluster"] == "volcano_icl"


# ---------------------------------------------------------------------
# T-ICL-V1-2：v1 普通音色 cluster 仍为 volcano_tts
# ---------------------------------------------------------------------
def test_v1_normal_speaker_uses_volcano_tts_cluster():
    """v1 provider 收到普通 BVxxx_streaming 音色，cluster 应保持 volcano_tts。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    p = DoubaoTTSProvider()
    payload = p._build_payload("测试", "zh_female_vv_uranus_bigtts", emotion="calm", speed=1.0)
    assert payload["app"]["cluster"] == "volcano_tts"


def test_v1_big_model_speaker_uses_volcano_tts_cluster():
    """v1 provider 收到大模型 2.0 音色（zh_xxx_uranus_bigtts），cluster 仍为 volcano_tts
    （因为 TTS 2.0 大模型与 ICL 复刻不同 endpoint，但 cluster 都是 volcano_tts；model=2 走 /v1/tts 即可）。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    p = DoubaoTTSProvider()
    payload = p._build_payload("测试", "zh_female_uranus_bigtts", emotion="calm", speed=1.0)
    assert payload["app"]["cluster"] == "volcano_tts"


# ---------------------------------------------------------------------
# T-ICL-V1-3：完整 mock HTTP — v1 合成复刻音色时 cluster 必须为 volcano_icl
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v1_icl_synthesize_sends_volcano_icl_cluster(monkeypatch):
    """mock httpx，验证 v1 走 S_xxx 音色时 payload 真正包含 cluster=volcano_icl。"""
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfg

    captured: dict = {}

    async def _fake_post_json_for_v1(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        # 返回成功响应：code=3000 + base64 编码的假 MP3
        b64 = base64.b64encode(_fake_mp3_bytes()).decode("ascii")
        return {"code": 3000, "message": "ok", "data": b64}, "fake-logid-1"

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake_post_json_for_v1)
    # 跳过 RPM 等待
    async def _no_wait(_key): return None
    monkeypatch.setattr(tts_mod, "_doubao_rpm_wait_acquire", _no_wait)

    saved_ak = cfg.settings.DOUBAO_AK
    cfg.settings.DOUBAO_AK = "test-key"
    try:
        p = DoubaoTTSProvider()
        mp3_b, dur = await p.synthesize_to_bytes("测试文本", "S_test_clone_001")

        assert captured["payload"]["app"]["cluster"] == "volcano_icl", (
            f"v1 ICL 合成应使用 cluster=volcano_icl，实际={captured['payload']['app']['cluster']!r}"
        )
        assert captured["payload"]["voice_id"] == "S_test_clone_001"
        assert captured["payload"]["audio"]["voice_type"] == "S_test_clone_001"
    finally:
        cfg.settings.DOUBAO_AK = saved_ak


# ---------------------------------------------------------------------
# T-ICL-V1-4：v3 复刻音色 → X-Api-Resource-Id = seed-icl-2.0
# ---------------------------------------------------------------------
def test_v3_icl_speaker_uses_seed_icl_2_resource_id():
    """v3 provider 收到 S_/icl_ 音色，应自动设置 X-Api-Resource-Id=seed-icl-2.0。"""
    from backend.app.ai.providers.doubao.tts import (
        _resolve_resource_id_for_v3,
        _strip_voice_id_for_api_v3,
    )
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    v3 = DoubaoTTSProviderV3()
    for raw in ("S_abc123", "icl_abc123def", "icl:icl_abc123def", "doubao:S_abc123"):
        bare = _strip_voice_id_for_api_v3(raw)
        model = v3._resolve_model_for_speaker(bare)
        rid = _resolve_resource_id_for_v3(model)
        assert rid == "seed-icl-2.0", (
            f"v3 ICL 复刻音色应 resource_id=seed-icl-2.0；raw={raw!r} bare={bare!r} model={model!r} rid={rid!r}"
        )


# ---------------------------------------------------------------------
# T-ICL-V1-5：v3 普通音色 → X-Api-Resource-Id = seed-tts-2.0
# ---------------------------------------------------------------------
def test_v3_normal_speaker_uses_seed_tts_2_resource_id():
    """v3 provider 收到 2.0 音色，resource_id 为 seed-tts-2.0。"""
    from backend.app.ai.providers.doubao.tts import _resolve_resource_id_for_v3
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    v3 = DoubaoTTSProviderV3()
    # 内置 2.0 音色 → seed-tts-2.0
    model = v3._resolve_model_for_speaker("zh_female_vv_uranus_bigtts")
    rid = _resolve_resource_id_for_v3(model)
    assert rid == "seed-tts-2.0"

    # 未收录 speaker（如已删除的 1.0 BV 音色）→ 兜底 seed-tts-2.0
    model = v3._resolve_model_for_speaker("BV001_streaming")
    rid = _resolve_resource_id_for_v3(model)
    assert rid == "seed-tts-2.0"

    # 显式声明 1.0 的入参（远程/自定义音色）仍如实映射，交由上层过滤
    assert _resolve_resource_id_for_v3("seed-tts-1.0") == "seed-tts-1.0"


# ---------------------------------------------------------------------
# T-ICL-V1-6：v3 合成复刻音色（mock HTTP）→ 真的发 seed-icl-2.0
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v3_icl_synthesize_sends_seed_icl_2_header(monkeypatch):
    """mock httpx，验证 v3 走 S_xxx 音色时 X-Api-Resource-Id 头是 seed-icl-2.0。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3
    from backend.app.core import config as cfg

    captured: dict = {}

    async def _fake_post_stream_v3(self, url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        b64 = base64.b64encode(_fake_mp3_bytes()).decode("ascii")
        # 假 chunked 响应：单 chunk JSON
        return [(b'{"code": 0, "data": "%s", "audio_info": {"duration": 1000}}' % b64.encode())], "fake-logid-3"

    # 用 monkeypatch 把实例方法替换
    monkeypatch.setattr(DoubaoTTSProviderV3, "_post_stream_v3", _fake_post_stream_v3)

    saved = cfg.settings.DOUBAO_AK
    cfg.settings.DOUBAO_AK = "test-key"
    try:
        v3 = DoubaoTTSProviderV3()
        mp3_b, dur = await v3.synthesize_to_bytes("测试文本", "S_test_v3_clone_001")

        assert captured["headers"]["X-Api-Resource-Id"] == "seed-icl-2.0", (
            f"v3 ICL 应发 X-Api-Resource-Id=seed-icl-2.0；实际={captured['headers'].get('X-Api-Resource-Id')!r}"
        )
        body_str = str(captured["body"])
        assert "S_test_v3_clone_001" in body_str
    finally:
        cfg.settings.DOUBAO_AK = saved
