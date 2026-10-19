"""项目封面：短暂在内存处理图片，正式文件只保存在对象存储。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import uuid
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from ..core.config import get_active
from ..db.models import Project
from ..db.session import get_session_factory
from .storage import get_storage, media_key

logger = logging.getLogger(__name__)
_tasks: dict[str, asyncio.Task] = {}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def image_type(data: bytes) -> tuple[str, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("封面图片必须小于 8 MB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, seen_idat = 8, False
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            end = offset + 12 + length
            if end > len(data):
                break
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            checksum = int.from_bytes(data[offset + 8 + length:end], "big")
            if zlib.crc32(kind + payload) != checksum:
                break
            if offset == 8 and (kind != b"IHDR" or length != 13 or
                                not 1 <= int.from_bytes(payload[:4], "big") <= 8192 or
                                not 1 <= int.from_bytes(payload[4:8], "big") <= 8192):
                break
            if kind == b"IDAT":
                seen_idat = True
            if kind == b"IEND" and length == 0 and seen_idat and end == len(data):
                return "png", "image/png"
            offset = end
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        offset, dimensions_ok = 2, False
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            marker = data[offset + 1]
            if marker == 0xDA:  # SOS 后是压缩数据，末尾 EOI 已检查
                if dimensions_ok:
                    return "jpg", "image/jpeg"
                break
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if length < 2 or offset + 2 + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3) and length >= 7:
                height = int.from_bytes(data[offset + 5:offset + 7], "big")
                width = int.from_bytes(data[offset + 7:offset + 9], "big")
                dimensions_ok = 1 <= width <= 8192 and 1 <= height <= 8192
            offset += 2 + length
    if (data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") + 8 == len(data)
            and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X")):
        return "webp", "image/webp"
    raise ValueError("仅支持 PNG、JPEG 或 WebP 图片")


def _storage():
    storage = get_storage()
    if not storage.archives_remotely:
        raise ValueError("请先配置对象存储，封面图片只保存到对象存储")
    if hasattr(storage, "missing_config"):
        missing = storage.missing_config()
        if missing:
            raise ValueError(f"对象存储配置不完整：{', '.join(missing)}")
    return storage


async def _set_progress(project_id: str, status: str, progress: int, error: str | None = None) -> None:
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        if project:
            project.cover_status = status
            project.cover_progress = progress
            project.cover_error = error
            await session.commit()


async def _save_cover(project_id: str, data: bytes) -> None:
    suffix, content_type = image_type(data)
    storage = _storage()
    key = media_key(project_id, "cover", f"{uuid.uuid4().hex}.{suffix}")
    if not await storage.put_bytes(key=key, data=data, content_type=content_type):
        raise RuntimeError("封面写入对象存储失败")
    old_key = None
    try:
        async with get_session_factory()() as session:
            project = await session.get(Project, project_id)
            if not project:
                raise ValueError("项目已删除")
            old_key = project.cover_key
            project.cover_key = key
            project.cover_status = "done"
            project.cover_progress = 100
            project.cover_error = None
            await session.commit()
    except Exception:
        await storage.delete_key(key)
        raise
    if old_key and old_key != key:
        await storage.delete_key(old_key)


async def upload_cover(project_id: str, data: bytes) -> None:
    if project_id in _tasks:
        raise ValueError("AI 封面生成中，请完成后再上传")
    await _save_cover(project_id, data)


async def remove_cover(project_id: str) -> None:
    if project_id in _tasks:
        raise ValueError("封面正在生成，请完成后再删除")
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError("项目不存在")
        old_key = project.cover_key
        storage = _storage() if old_key else None
        project.cover_key = None
        project.cover_status = "empty"
        project.cover_progress = 0
        project.cover_error = None
        await session.commit()
    if old_key and storage:
        await storage.delete_key(old_key)


async def read_cover(project_id: str) -> tuple[bytes, str] | None:
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        key = project.cover_key if project else None
    if not key:
        return None
    suffix = Path(key).suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
    if not mime:
        raise ValueError("封面格式无效")
    with TemporaryDirectory(prefix="audiobook-cover-") as folder:
        target = Path(folder) / f"cover{suffix}"
        if not await _storage().fetch_to(key=key, dest=target):
            raise FileNotFoundError("对象存储中的封面不存在")
        data = target.read_bytes()
    image_type(data)
    return data, mime


async def cover_status(project_id: str) -> dict:
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError("项目不存在")
        if project.cover_status == "generating" and project_id not in _tasks:
            project.cover_status = "failed"
            project.cover_error = "服务曾中断，请重新生成"
            await session.commit()
        return {
            "status": project.cover_status or "empty",
            "progress": project.cover_progress or 0,
            "error": project.cover_error,
            "has_cover": bool(project.cover_key),
        }


async def start_cover_generation(project_id: str) -> dict:
    if project_id in _tasks:
        raise ValueError("封面正在生成")
    _storage()
    pid, model, provider = get_active("image")
    if not pid or not model or not provider or not provider.get("enabled"):
        raise ValueError("请先在模型厂商设置中激活图片模型")
    if not provider.get("api_key") or not provider.get("base_url"):
        raise ValueError("图片模型厂商缺少 API Key 或 Base URL")
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError("项目不存在")
        title = project.book_title or project.name
        description = project.description or ""
        category = project.book_category or ""
        tags = project.tags or ""
        selling_points = project.selling_points or ""
        project.cover_status = "generating"
        project.cover_progress = 5
        project.cover_error = None
        await session.commit()
    task = asyncio.create_task(_generate(project_id, pid, model, dict(provider), title, description, category, tags, selling_points))
    _tasks[project_id] = task
    task.add_done_callback(lambda _: _tasks.pop(project_id, None))
    return {"status": "generating", "progress": 5}


async def _generate(project_id: str, provider_id: str, model: str, provider: dict, title: str,
                    description: str, category: str, tags: str, selling_points: str) -> None:
    try:
        await _set_progress(project_id, "generating", 20)
        is_minimax = provider_id == "minimax"
        # MiniMax 图片接口与其 LLM 共用厂商卡片，但官方图片 API 位于 cn 域名。
        base_url = "https://api.minimax.cn/v1" if is_minimax else str(provider["base_url"]).rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("图片模型 Base URL 必须使用 HTTPS")
        prompt = (
            "为中文有声书设计一张专业竖版封面。画面需要契合书名、类型和简介，视觉重点清晰，"
            "避免水印、二维码、作者姓名和多余文字；文字排版可能不准，标题将在页面另行展示。\n"
            f"书名：{title[:120]}\n分类：{category[:64]}\n标签：{tags[:256]}\n"
            f"卖点：{selling_points[:500]}\n介绍：{description[:1200]}"
        )
        if is_minimax:
            prompt = prompt[:1500]  # 官方文生图 prompt 上限。
        headers = {"Authorization": f"Bearer {provider['api_key']}"}
        headers.update({str(k): str(v) for k, v in (provider.get("extra_headers") or {}).items()})
        endpoint = f"{base_url}/image_generation" if is_minimax else f"{base_url}/images/generations"
        request_body = (
            {"model": model, "prompt": prompt, "aspect_ratio": "2:3", "response_format": "base64", "n": 1}
            if is_minimax else
            {"model": model, "prompt": prompt, "size": "1024x1536", "response_format": "b64_json", "n": 1}
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            async with client.stream(
                "POST", endpoint, headers=headers, json=request_body,
            ) as response:
                response.raise_for_status()
                parts, total = [], 0
                async for part in response.aiter_bytes():
                    total += len(part)
                    if total > MAX_IMAGE_BYTES * 2:
                        raise ValueError("图片模型返回内容超过大小限制")
                    parts.append(part)
        import json
        payload = json.loads(b"".join(parts))
        await _set_progress(project_id, "generating", 75)
        if is_minimax:
            base_resp = payload.get("base_resp") or {}
            if base_resp.get("status_code") != 0:
                raise RuntimeError(
                    f"MiniMax 图片生成失败（{base_resp.get('status_code', '未知状态')}）："
                    f"{base_resp.get('status_msg') or '请检查模型、额度及提示词'}"
                )
            images = (payload.get("data") or {}).get("image_base64") or []
            encoded = images[0] if images else None
        else:
            images = payload.get("data") or []
            encoded = images[0].get("b64_json") if images else None
        if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 2:
            raise ValueError("图片模型未返回有效的 Base64 图片")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片模型返回的图片编码无效") from exc
        image_type(data)
        await _set_progress(project_id, "generating", 90)
        await _save_cover(project_id, data)
    except Exception as exc:
        logger.exception("封面生成失败 project_id=%s", project_id)
        await _set_progress(project_id, "failed", 0, str(exc)[:500])
