"""
MP3 帧级工具：精确时长解析 + 采样率匹配的静音帧生成。

背景（为什么不用"文件大小 ÷ 固定码率"估算）：
- 旧实现 _estimate_mp3_duration_ms 按 96kbps 估算，而 MiniMax 实际输出 128kbps，
  所有章节时长被系统性高估 +33%——M4B 章节标记、SRT 章间偏移、进度条全部漂移。
- 旧 make_silent_mp3 硬编码 44.1kHz 静音帧，而 MiniMax 输出 32kHz——
  每个段间停顿都是一个采样率不一致的拼接点，部分播放器会产生杂音。

实现：解析 MPEG1/2/2.5 Layer III 帧头，逐帧累加 (samples_per_frame / sample_rate)。
CBR/VBR 均精确；对损坏数据做有限重同步，失败时回退大小估算。
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

# MPEG 版本 → 采样率表（索引 0/1/2；3 = 保留）
_SAMPLERATES: dict[int, tuple[int, int, int]] = {
    3: (44100, 48000, 32000),   # MPEG1
    2: (22050, 24000, 16000),   # MPEG2
    0: (11025, 12000, 8000),    # MPEG2.5
}
# Layer III 码率表（kbps；索引 0 与 15 = 无效）
_BITRATES_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BITRATES_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


def _parse_frame_header(data: bytes, pos: int) -> tuple[int, int, int] | None:
    """解析 pos 处的 MP3 帧头，返回 (frame_len_bytes, sample_rate, samples_per_frame)。"""
    if pos + 4 > len(data):
        return None
    b1, b2 = data[pos + 1], data[pos + 2]
    # 帧同步已在调用方保证 data[pos] == 0xFF 且 (b1 & 0xE0) == 0xE0
    version_bits = (b1 >> 3) & 0x03    # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=保留
    layer_bits = (b1 >> 1) & 0x03      # 01=Layer III
    if version_bits == 1 or layer_bits != 0x01:
        return None
    bitrate_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if bitrate_idx in (0, 15) or sr_idx == 3:
        return None
    table = _SAMPLERATES.get(version_bits)
    if table is None:
        return None
    sample_rate = table[sr_idx]
    if version_bits == 3:  # MPEG1 L3
        kbps = _BITRATES_V1_L3[bitrate_idx]
        samples = 1152
        frame_len = (144 * kbps * 1000) // sample_rate + padding
    else:  # MPEG2 / MPEG2.5 L3
        kbps = _BITRATES_V2_L3[bitrate_idx]
        samples = 576
        frame_len = (72 * kbps * 1000) // sample_rate + padding
    if frame_len < 4 or kbps == 0:
        return None
    return frame_len, sample_rate, samples


def _skip_id3v2(data: bytes) -> int:
    """返回 ID3v2 tag 之后的偏移（无 tag 返回 0）。"""
    if len(data) < 10 or data[0:3] != b"ID3":
        return 0
    size = (
        ((data[6] & 0x7F) << 21)
        | ((data[7] & 0x7F) << 14)
        | ((data[8] & 0x7F) << 7)
        | (data[9] & 0x7F)
    )
    return 10 + size


def mp3_duration_ms(mp3_bytes: bytes) -> int:
    """逐帧累加时长（毫秒）。解析失败回退"大小 ÷ 128kbps"估算。"""
    if not mp3_bytes or len(mp3_bytes) < 4:
        return 0
    data = mp3_bytes
    pos = _skip_id3v2(data)
    total_sec = 0.0
    parsed_frames = 0
    resync_failures = 0
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            # 垃圾数据：向前重同步（最多连续 64 次后放弃）
            resync_failures += 1
            if resync_failures > 64:
                break
            pos += 1
            continue
        parsed = _parse_frame_header(data, pos)
        if parsed is None:
            resync_failures += 1
            if resync_failures > 64:
                break
            pos += 1
            continue
        frame_len, sample_rate, samples = parsed
        if pos + frame_len > n:
            break  # 末尾截断帧：忽略
        total_sec += samples / sample_rate
        parsed_frames += 1
        pos += frame_len
    if parsed_frames > 0:
        return int(total_sec * 1000)
    # 回退：假设 128kbps（MiniMax/主流 TTS 输出码率）
    return int(len(mp3_bytes) * 8 / 128000 * 1000)


def mp3_sample_rate(mp3_bytes: bytes) -> int | None:
    """解析首个有效帧的采样率；失败返回 None。"""
    if not mp3_bytes or len(mp3_bytes) < 4:
        return None
    pos = _skip_id3v2(mp3_bytes)
    n = len(mp3_bytes)
    attempts = 0
    while pos + 4 <= n and attempts < 512:
        attempts += 1
        if mp3_bytes[pos] != 0xFF or (mp3_bytes[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        parsed = _parse_frame_header(mp3_bytes, pos)
        if parsed is None:
            pos += 1
            continue
        return parsed[1]
    return None


def _frame_bytes(sample_rate: int, kbps: int) -> tuple[int, int, int, bytes]:
    """构造 (header_version, bitrate_idx, sr_idx, 4字节帧头) + 帧长。"""
    # 找采样率所属版本与索引
    for ver, table in _SAMPLERATES.items():
        if sample_rate in table:
            sr_idx = table.index(sample_rate)
            break
    else:
        raise ValueError(f"不支持的采样率: {sample_rate}")
    if ver == 3:
        bitrates = _BITRATES_V1_L3
        samples = 1152
    else:
        bitrates = _BITRATES_V2_L3
        samples = 576
    if kbps not in bitrates:
        raise ValueError(f"{sample_rate}Hz 下不支持的码率: {kbps}")
    bitrate_idx = bitrates.index(kbps)
    if ver == 3:
        frame_len = (144 * kbps * 1000) // sample_rate
        version_bits = 3
    else:
        frame_len = (72 * kbps * 1000) // sample_rate
        version_bits = 2
    b1 = 0xFF
    # byte1 = [sync 3bit=111][version 2bit][layer 2bit=01(L3)][protection 1bit=1(无CRC)]
    b2 = 0xE0 | (version_bits << 3) | 0x03
    b3 = (bitrate_idx << 4) | (sr_idx << 2)  # padding=0
    header = struct.pack("BBB", b1, b2, b3) + b"\x00"
    return frame_len, samples, sample_rate, header


def make_silent_mp3(duration_ms: int, sample_rate: int = 32000, kbps: int = 128) -> bytes:
    """生成指定采样率的静音 MP3（MPEG L3, 128kbps, 单声道语义）。

    与真实 TTS 音频同采样率拼接，消除旧实现 44.1kHz 假帧造成的
    段间采样率不一致（杂音/卡顿隐患）。默认 32kHz = MiniMax 输出。
    """
    duration_ms = max(int(duration_ms), 1)
    try:
        frame_len, samples, sr, header = _frame_bytes(int(sample_rate), int(kbps))
    except ValueError:
        # 兜底：老 44.1kHz 帧不至于崩
        frame_len, samples, sr, header = _frame_bytes(44100, 128)
    frame_ms = samples / sr * 1000.0
    frame_body = b"\x00" * (frame_len - len(header))
    frame = header + frame_body
    frames_needed = max(1, int(duration_ms / frame_ms) + 1)
    return frame * frames_needed
