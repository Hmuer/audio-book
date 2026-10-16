"""P2-3：章节预告片生成。

完全独立于 Build 流水线 —— 不重新跑 TTS，只截取章节 MP3 的前 N 秒拼接成预览片段。
适用于详情页"试听"按钮，避免用户下载完整章节（百兆级 MP3）才能听效果。

策略：
  1. 逐帧累加时长，跨段跳过 ID3 标签，达到 max_seconds 时停止
  2. 写入 build_<id>_ch<NN>_preview_v2.mp3（与章节同目录）
  3. 第二次调用复用磁盘缓存（源章节改变时重新生成）
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from ..core.config import settings
from ..core.mp3_util import mp3_prefix_for_duration


logger = logging.getLogger(__name__)


DEFAULT_PREVIEW_SECONDS = 15


def _preview_filename(build_id: str, ch_idx: int) -> str:
    """章节预告片文件名（与章节 MP3 同目录）。"""
    return f"build_{build_id}_ch{ch_idx:04d}_preview_v2.mp3"


def preview_path(audio_dir: str | Path, build_id: str, ch_idx: int) -> Path:
    """预告片磁盘路径。"""
    return Path(audio_dir) / _preview_filename(build_id, ch_idx)


def _split_frames(mp3_bytes: bytes, max_seconds: int) -> bytes:
    """按 MPEG frame header 截取前 max_seconds 秒的 bytes。

    Returns:
        截取后的 MP3 bytes（含所有完整 frame；最后一帧不会半截）
    """
    precise = mp3_prefix_for_duration(mp3_bytes, max_seconds * 1000)
    if precise is not None:
        return precise
    # 损坏或非标准音频：保留旧的码率估算兜底。
    try:
        from mutagen.mp3 import MP3  # type: ignore
        bio = io.BytesIO(mp3_bytes)
        audio = MP3(bio)
        # 平均比特率 → 总时长
        if audio.info.bitrate:
            avg_bytes_per_sec = audio.info.bitrate // 8
            cut = min(len(mp3_bytes), max(1, int(max_seconds * avg_bytes_per_sec)))
        else:
            cut = min(len(mp3_bytes), max(1, max_seconds * 3000))  # 兜底 24kbps
        return mp3_bytes[:cut]
    except Exception as e:
        # mutagen 解析失败（罕见，比如坏 frame header）→ 退化到 24kbps 估算
        logger.warning(f"[preview] mutagen 解析失败，按 24kbps 估算截取: {type(e).__name__}: {e}")
        cut = min(len(mp3_bytes), max(1, max_seconds * 3000))
        return mp3_bytes[:cut]


def generate_chapter_preview(
    chapter_audio_path: str | Path,
    output_path: str | Path,
    *,
    max_seconds: int = DEFAULT_PREVIEW_SECONDS,
) -> Path:
    """从章节 MP3 截取前 max_seconds 秒，写到 output_path。

    Args:
        chapter_audio_path: 章节 MP3 完整路径（必存在）
        output_path: 输出 .mp3 文件路径（目录会自动创建）
        max_seconds: 截取时长上限（秒），默认 15

    Returns:
        output_path

    Raises:
        FileNotFoundError: 章节 MP3 不存在
    """
    src = Path(chapter_audio_path)
    if not src.is_file():
        raise FileNotFoundError(f"章节音频不存在: {src}")

    dst = Path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    mp3_bytes = src.read_bytes()
    truncated = _split_frames(mp3_bytes, max_seconds)
    dst.write_bytes(truncated)
    logger.info(
        f"[preview] 生成预告片 src={src.name} dst={dst.name} "
        f"orig_size={len(mp3_bytes)} cut_size={len(truncated)} max_s={max_seconds}"
    )
    return dst


def get_or_generate_preview(
    build_id: str,
    ch_idx: int,
    *,
    audio_dir: str | Path | None = None,
    max_seconds: int = DEFAULT_PREVIEW_SECONDS,
    force_regenerate: bool = False,
) -> Path | None:
    """获取/生成预告片。返回预告片路径；章节 MP3 缺失返回 None（不抛错）。

    Args:
        build_id: build 标识（不含 "build_" 前缀）
        ch_idx: 章节号（0 起）
        audio_dir: 默认 settings.AUDIO_DIR
        max_seconds: 截取时长上限
        force_regenerate: True 强制重新生成（覆盖已有预告片）

    Returns:
        预告片 Path；章节 MP3 缺失返回 None
    """
    adir = Path(audio_dir or settings.AUDIO_DIR)
    # 章节 MP3 路径（按 build.py _audio_filename 规则）
    chapter_mp3 = adir / f"build_{build_id}_ch{ch_idx:04d}.mp3"
    if not chapter_mp3.is_file():
        return None

    pv = preview_path(adir, build_id, ch_idx)
    if pv.is_file() and not force_regenerate and pv.stat().st_mtime >= chapter_mp3.stat().st_mtime:
        return pv
    result = generate_chapter_preview(chapter_mp3, pv, max_seconds=max_seconds)
    legacy = adir / f"build_{build_id}_ch{ch_idx:04d}_preview.mp3"
    try:
        legacy.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("[preview] 清理旧预告片失败 %s: %s", legacy.name, e)
    return result
