"""旧章节 MP3 的按需 seek 索引修复，不重新调用 TTS。"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

from ..core.config import settings
from ..core.mp3_util import has_mp3_seek_header, neutralize_mp3_seek_headers
from .storage import get_storage, media_key, cleanup_local_enabled

logger = logging.getLogger(__name__)
_locks: dict[str, asyncio.Lock] = {}


def _repair_file(path: Path) -> int:
    original = path.read_bytes()
    fixed, count = neutralize_mp3_seek_headers(original)
    if count:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.seek-tmp")
        try:
            temporary.write_bytes(fixed)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return count


async def ensure_chapter_seekable(project_id: str, build_id: str, filename: str) -> Path | None:
    """修复旧 MP3 的段级索引；远端归档被清理时先小范围探测，再按需回源重传。"""
    if Path(filename).name != filename:
        raise ValueError("章节音频文件名无效")
    path = Path(settings.AUDIO_DIR) / filename
    key = media_key(project_id, build_id, filename)
    lock = _locks.setdefault(str(path), asyncio.Lock())
    async with lock:
        storage = get_storage()
        if path.is_file():
            # 正常新产物首帧无 Info，Range 请求走这条快速路径。
            with path.open("rb") as source:
                prefix = source.read(1024)
            if has_mp3_seek_header(prefix):
                count = await asyncio.to_thread(_repair_file, path)
                if count:
                    logger.info("[audio_seek] 本地旧章节已修复 %s：%d 个段级索引", filename, count)
                    if storage.archives_remotely:
                        # 归档失败时保留修复后的本地副本供播放，后续仍可补传。
                        if not await storage.archive(key=key, local_path=path, content_type="audio/mpeg"):
                            logger.warning("[audio_seek] 远端修复版上传失败，保留本地 %s", filename)
            return path

        if not storage.archives_remotely:
            return None
        peek = getattr(storage, "peek_bytes", None)
        if peek is None:
            return None
        try:
            prefix = await peek(key, 1024)
        except Exception as exc:
            logger.warning("[audio_seek] 远端旧章节探测失败 %s: %s", filename, exc)
            return None
        if not has_mp3_seek_header(prefix):
            return None

        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.seek-fetch")
        try:
            if not await storage.fetch_to(key=key, dest=temporary):
                raise RuntimeError("旧章节音频回源失败，无法修复播放跳转")
            count = await asyncio.to_thread(_repair_file, temporary)
            if not count:
                raise RuntimeError("旧章节音频索引修复失败")
            archived = await storage.archive(
                key=key, local_path=temporary, content_type="audio/mpeg",
            )
            logger.info("[audio_seek] 远端旧章节已修复 %s：%d 个段级索引", filename, count)
            if archived and cleanup_local_enabled():
                return None
            if not archived:
                logger.warning("[audio_seek] 远端修复版上传失败，保留本地 %s", filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, path)
            return path
        finally:
            temporary.unlink(missing_ok=True)
