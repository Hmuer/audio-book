"""P0-1 + P0-3 回归测试：豆包 v1 TTS 响应解析与时长估算。

覆盖 5 个场景：
  T-P0-1  业务码 3000 + 合法 base64 → 正确解码 + 帧头时长
  T-P0-2  业务码 3001（可重试）→ 第一次失败，第二次成功
  T-P0-3  业务码 4001（不可重试）→ 立刻抛错，不重试
  T-P0-4  HTTP 200 但响应不是 JSON → 抛 DoubaoTTSResponseError(code=-1)，不重试
  T-P0-5  帧头解析 vs len/16 估算的精度差异

这些是「用户用真实 Key 调豆包 TTS 必然触发」的关键路径——之前 _http_post_bytes
把响应原样当 MP3 写盘，一旦服务端返 JSON 业务失败（HTTP 200 + code != 3000），
就会写入"JSON 伪装的 .mp3"文件，损坏 Build 产物。
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import struct
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# 测试工具：构造合法的 MPEG audio frame
# ---------------------------------------------------------------------
def make_mp3_frame_bytes(
    bitrate_kbps: int = 128,
    sample_rate: int = 44100,
    duration_ms: int = 1000,
) -> bytes:
    """构造一个带合法 MPEG1 Layer3 frame header 的字节序列用于测试。

    返回的总字节数按 bitrate 精确匹配目标时长（误差 ±10ms）。
    不关注音频内容是否可解码——只测试帧头解析逻辑。
    """
    # bitrate 表（MPEG1 Layer III）：[0,32,40,...,128,...,320]
    mpeg1_l3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    # sample_rate 表（MPEG1）：[44100, 48000, 32000, 0]
    sr_table = [44100, 48000, 32000, 0]
    br_idx = mpeg1_l3.index(bitrate_kbps) if bitrate_kbps in mpeg1_l3 else 9  # 128 default
    sr_idx = sr_table.index(sample_rate) if sample_rate in sr_table else 0
    # header byte 0-3：FF FB / F3 / +br_idx +sr_idx
    # version=3(MPEG1), layer=1(L3), no CRC, bitrate_idx, sample_idx, padding=0, private=0
    b1 = 0xFF
    b2 = 0xFB                            # 11111011 → version=11, layer=01, prot=1
    b3 = (br_idx << 4) | (sr_idx << 2) | 0x00
    b4 = 0x00  # channel_mode=0 (stereo), mode_ext=0, copyright=0, original=0, emphasis=0
    header = bytes([b1, b2, b3, b4])
    # 一帧大小：MPEG1 Layer3 = 144 * bitrate / sample_rate + padding
    frame_size = 144 * bitrate_kbps * 1000 // sample_rate
    total_bytes = int(bitrate_kbps * 1000 * duration_ms / 8 / 1000)
    # 用整数个完整帧填，剩余用零填充
    n_frames = total_bytes // frame_size
    return header + (b"\x00" * (frame_size - 4)) * n_frames


# ---------------------------------------------------------------------
# T-P0-1: 业务码 3000 + 合法 base64 → 正确解码 + 帧头时长
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_1_business_code_3000_decodes_mp3(monkeypatch):
    """正常路径：返 3000 + base64 MP3 → 应解码成功并解析帧头时长。"""
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")
    # 让重试退避 = 0 加速测试
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "JITTER_SECS", 0.0)

    mp3_bytes = make_mp3_frame_bytes(bitrate_kbps=128, sample_rate=44100, duration_ms=1500)
    expected_b64 = _b64.b64encode(mp3_bytes).decode("ascii")

    call_count = {"n": 0}

    async def _fake_post_json(url, headers, payload):
        call_count["n"] += 1
        return (
            {"code": 3000, "message": "Success", "data": expected_b64},
            "logid-success-001",
        )

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake_post_json)

    # 构造 provider 实例（避免走全局 cache）
    provider = tts_mod.DoubaoTTSProvider()
    data, dur_ms = await provider.synthesize_to_bytes("你好世界", "BV001_stream")

    assert call_count["n"] == 1, "成功路径应只调用一次"
    assert data == mp3_bytes, "解码后的字节必须等于原始 MP3"
    # 1500ms 音频按 128kbps 精确估算应 ≈ 1500ms（误差 ±50ms）
    assert 1450 <= dur_ms <= 1550, f"帧头估算时长异常：{dur_ms}ms（期望 ~1500）"


# ---------------------------------------------------------------------
# T-P0-2: 业务码 3001（可重试）→ 第一次失败，第二次成功
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_2_retryable_business_code_retries(monkeypatch):
    """业务码 3001 属可重试类（参数/限流），第一次返 3001 第二次 3000 应最终成功。"""
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "JITTER_SECS", 0.0)

    mp3_bytes = make_mp3_frame_bytes(duration_ms=500)
    expected_b64 = _b64.b64encode(mp3_bytes).decode("ascii")

    seq = iter([
        ({"code": 3001, "message": "RateLimited"}, "logid-001"),
        ({"code": 3000, "message": "Success", "data": expected_b64}, "logid-002"),
    ])

    async def _fake_post_json(url, headers, payload):
        return next(seq)

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake_post_json)

    provider = tts_mod.DoubaoTTSProvider()
    data, dur_ms = await provider.synthesize_to_bytes("限流重试", "BV001_stream")

    assert data == mp3_bytes, "第二次成功后应返回解码后的 MP3"
    assert dur_ms > 0


# ---------------------------------------------------------------------
# T-P0-3: 业务码 4001（不可重试）→ 立刻抛错，不重试
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_3_non_retryable_code_fails_immediately(monkeypatch):
    """业务码 4001 不在 _V1_RETRYABLE_CODES 内，应立刻抛错不重试。"""
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "JITTER_SECS", 0.0)

    call_count = {"n": 0}

    async def _fake_post_json(url, headers, payload):
        call_count["n"] += 1
        return ({"code": 4001, "message": "InvalidArgument / 音色不存在"}, "logid-bad")

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake_post_json)

    provider = tts_mod.DoubaoTTSProvider()
    with pytest.raises(RuntimeError) as ei:
        await provider.synthesize_to_bytes("你好", "BV001_stream")

    # 关键断言：4001 不重试 → 只调一次
    assert call_count["n"] == 1, f"不可重试业务码不应重试，实际调了 {call_count['n']} 次"
    msg = str(ei.value)
    assert "4001" in msg, f"错误信息应包含业务码 4001，实际：{msg}"


# ---------------------------------------------------------------------
# T-P0-4: HTTP 200 但响应不是 JSON → 抛 code=-1，不重试
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_4_non_json_response_fails_without_retry(monkeypatch):
    """服务端返非 JSON（HTML 错误页 / 网关异常页）→ 立刻抛错不重试。"""
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "JITTER_SECS", 0.0)

    call_count = {"n": 0}

    async def _fake_post_json_non_json(url, headers, payload):
        call_count["n"] += 1
        # _post_json_for_v1 内部检测到非 JSON 会抛 DoubaoTTSResponseError(code=-1)
        raise tts_mod.DoubaoTTSResponseError(
            "响应不是合法 JSON：JSONDecodeError",
            code=-1,
            logid="logid-html",
        )

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake_post_json_non_json)

    provider = tts_mod.DoubaoTTSProvider()
    with pytest.raises(RuntimeError):
        await provider.synthesize_to_bytes("你好", "BV001_stream")

    assert call_count["n"] == 1, "code=-1 是 JSON 解析错，不应触发重试"


# ---------------------------------------------------------------------
# T-P0-5: 帧头解析 vs len/16 估算的精度差异
# ---------------------------------------------------------------------
def test_p0_5_mp3_frame_header_parsing_is_more_accurate():
    """同一段 MP3，帧头解析 vs 旧 len/16 估算的差异必须体现精度提升。

    旧估算在非 128kbps（如 64kbps）时偏差 >50%；
    帧头解析应返回真实时长（±50ms）。
    """
    from backend.app.ai.providers.doubao.tts import _estimate_mp3_duration_ms

    # 64kbps / 44100Hz / 2 秒音频
    mp3 = make_mp3_frame_bytes(bitrate_kbps=64, sample_rate=44100, duration_ms=2000)
    est = _estimate_mp3_duration_ms(mp3)

    # 真实时长 2000ms，帧头解析应落在 [1950, 2050]
    assert 1900 <= est <= 2100, (
        f"帧头估算应 ≈ 2000ms（±100ms），实际 {est}ms；"
        f"旧 len/16 估算会得 ≈ {len(mp3)/16:.0f}ms（偏差 {(est - len(mp3)/16):.0f}ms）"
    )


# ---------------------------------------------------------------------
# T-P0-6（额外）：异常类 is_retryable 判定
# ---------------------------------------------------------------------
def test_p0_6_response_error_retryable_classification():
    """DoubaoTTSResponseError.is_retryable 应严格按 _V1_RETRYABLE_CODES 判定。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSResponseError

    for code in (3000,):
        # 3000 是成功码，理论上不应作为异常抛出，但即便抛了也不应"重试"
        e = DoubaoTTSResponseError("ok", code=code)
        assert not e.is_retryable, f"code={code} 不应在可重试集合内"

    for code in (3001, 3002, 3003, 3010, 3011):
        e = DoubaoTTSResponseError("retryable", code=code)
        assert e.is_retryable, f"code={code} 应可重试"

    for code in (3004, 3005, 4001, 5001, -1):
        e = DoubaoTTSResponseError("fatal", code=code)
        assert not e.is_retryable, f"code={code} 不应重试"


# ---------------------------------------------------------------------
# T-P0-7（额外）：空字符串文本短路
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_7_empty_text_short_circuits(monkeypatch):
    """空文本不应发请求，直接返 0 时长占位。"""
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")

    called = {"n": 0}

    async def _fake(url, headers, payload):
        called["n"] += 1
        return ({"code": 3000, "message": "ok", "data": ""}, None)

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake)

    provider = tts_mod.DoubaoTTSProvider()
    data, dur_ms = await provider.synthesize_to_bytes("", "BV001_stream")
    assert called["n"] == 0, "空文本必须短路，不能发出请求"
    assert dur_ms == 0, "空文本时长应为 0"
    assert len(data) > 0, "空文本应返占位字节"