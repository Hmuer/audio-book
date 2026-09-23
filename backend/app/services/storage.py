"""对象存储（G-1）：交付物归档 + 分发。

后端（由 `STORAGE_BACKEND` 选择）
- **local**（默认）：产物只落本地盘，行为与接入前**完全一致** → 可随时回滚。
- **s3**：腾讯云 COS，走 S3 兼容协议（boto3）。上传/回源/删除都丢到线程池执行，
  不阻塞事件循环；**下载走公有读直链**（不经过后端转发，省服务器带宽）。

设计前提（很关键）：对象存储在这里是「产物归档 + 分发」，**不是**实时随机读写后端。
合成期的 MP3 拼接（`concat_mp3_files`）、时长探测、预告片截取都需要**本地可读**文件，
所以本地暂存会一直保留到该产物上传成功之后（见 `STORAGE_CLEANUP_LOCAL`）。

key 规划（决策 4：仅交付物）
    {prefix}/{project_id}/{build_id}/{filename}
`filename` 就是既有的产物名（`build_xxx_ch0001.mp3` / `build_xxx_ch0001-0050.zip` / `…ch0001.lrc`），
key 与本地文件名一一对应，排查时不需要额外映射表。

不归档（有意为之）：段级缓存（可重建）、timings sidecar（内部中间产物）、
章节预告片、导入的源 TXT、ICL 参考音频（后两者含隐私/版权内容，公有读桶风险高）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

from ..core.config import settings

logger = logging.getLogger(__name__)


def media_key(project_id: str, build_id: str, filename: str) -> str:
    """交付物在对象存储里的 key。

    前缀为空时不留前导 `/`；filename 里不含路径分隔符（产物名由后端生成）。
    """
    prefix = (settings.S3_PREFIX or "").strip().strip("/")
    parts = [p for p in (prefix, project_id, build_id, filename) if p]
    return "/".join(parts)


class StorageBackend(ABC):
    """存储后端接口。`archive` 返回是否**真的归档成功**。

    调用方约定：只有 `archive` 返回 True 时才允许删除本地副本 ——
    归档失败必须保留本地文件，否则交付物就丢了。
    """

    name: str = "base"
    #: 是否会把本地产物镜像到远端（local 为 False，用于跳过清理逻辑）
    archives_remotely: bool = False

    @abstractmethod
    async def archive(self, *, key: str, local_path: Path, content_type: str | None = None) -> bool:
        """把本地产物归档到远端。local 后端直接返回 False（无需归档）。"""

    @abstractmethod
    async def put_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> bool:
        """直接写入字节（用于没有本地文件的产物，如按章 LRC）。"""

    @abstractmethod
    async def fetch_to(self, *, key: str, dest: Path) -> bool:
        """回源下载到 dest（G-5：本地已清理时重建/复用交付物）。"""

    @abstractmethod
    async def delete_key(self, key: str) -> None:
        """删除远端对象。不存在/失败都只告警，不抛。"""

    @abstractmethod
    def url(self, key: str) -> str | None:
        """公有读直链；local 后端返回 None（调用方回落到签名 URL）。"""


class LocalStorage(StorageBackend):
    """默认后端：产物就在 `settings.AUDIO_DIR`，无需任何归档动作。"""

    name = "local"
    archives_remotely = False

    async def archive(self, *, key: str, local_path: Path, content_type: str | None = None) -> bool:
        return False

    async def put_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> bool:
        return False

    async def fetch_to(self, *, key: str, dest: Path) -> bool:
        return False

    async def delete_key(self, key: str) -> None:
        return None

    def url(self, key: str) -> str | None:
        return None


class S3Storage(StorageBackend):
    """腾讯云 COS（S3 兼容协议）。

    为什么用 boto3 而不是手写 SigV4：签名/重试/分片上传都是成熟实现，
    自研密码学代码在没有真实环境验证的情况下风险太高。
    boto3 是同步的 → 全部调用包在 `asyncio.to_thread` 里，避免阻塞事件循环。
    """

    name = "s3"
    archives_remotely = True

    def __init__(self) -> None:
        self._endpoint = (settings.S3_ENDPOINT or "").strip()
        self._region = (settings.S3_REGION or "").strip() or "ap-guangzhou"
        self._bucket = (settings.S3_BUCKET or "").strip()
        self._access_key = (settings.S3_ACCESS_KEY or "").strip()
        self._secret_key = (settings.S3_SECRET_KEY or "").strip()
        self._public_base = (settings.S3_PUBLIC_BASE_URL or "").strip().rstrip("/")
        self._path_style = bool(settings.S3_PATH_STYLE)
        self._client = None  # 惰性创建

    # ---------- 配置校验 ----------
    def missing_config(self) -> list[str]:
        missing = []
        if not self._endpoint:
            missing.append("S3_ENDPOINT")
        if not self._bucket:
            missing.append("S3_BUCKET")
        if not self._access_key:
            missing.append("S3_ACCESS_KEY")
        if not self._secret_key:
            missing.append("S3_SECRET_KEY")
        return missing

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        missing = self.missing_config()
        if missing:
            raise RuntimeError(
                f"对象存储配置不完整，缺少：{', '.join(missing)}（请在设置页「对象存储」补齐）"
            )
        import boto3  # 延迟 import：local 后端 + 未装 boto3 的环境仍可正常启动
        from botocore.config import Config as _BotoConfig

        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=_BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path" if self._path_style else "virtual"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return self._client

    # ---------- 归档 / 回源 / 删除 ----------
    async def archive(self, *, key: str, local_path: Path, content_type: str | None = None) -> bool:
        if not local_path.is_file():
            logger.warning(f"[storage] 归档跳过，本地文件不存在: {local_path}")
            return False
        try:
            client = self._ensure_client()
            extra = {"ContentType": content_type} if content_type else None
            await asyncio.to_thread(
                client.upload_file, str(local_path), self._bucket, key,
                ExtraArgs=extra or {},
            )
            logger.info(
                f"[storage] 已归档 key={key} size={os.path.getsize(local_path)}"
            )
            return True
        except Exception as e:
            logger.warning(f"[storage] 归档失败 key={key}: {type(e).__name__}: {e}")
            return False

    async def put_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> bool:
        try:
            client = self._ensure_client()
            kw = {"ContentType": content_type} if content_type else {}
            await asyncio.to_thread(
                client.put_object, Bucket=self._bucket, Key=key, Body=data, **kw
            )
            return True
        except Exception as e:
            logger.warning(f"[storage] 写入失败 key={key}: {type(e).__name__}: {e}")
            return False

    async def fetch_to(self, *, key: str, dest: Path) -> bool:
        try:
            client = self._ensure_client()
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(client.download_file, self._bucket, key, str(dest))
            return dest.is_file()
        except Exception as e:
            logger.warning(f"[storage] 回源失败 key={key}: {type(e).__name__}: {e}")
            return False

    async def delete_key(self, key: str) -> None:
        try:
            client = self._ensure_client()
            await asyncio.to_thread(
                client.delete_object, Bucket=self._bucket, Key=key
            )
        except Exception as e:
            logger.warning(f"[storage] 删除失败 key={key}: {type(e).__name__}: {e}")

    # ---------- 公有读直链 ----------
    def public_base(self) -> str:
        """公网访问前缀。未显式配置时按 endpoint 推导（COS 虚拟主机风格）。"""
        if self._public_base:
            return self._public_base
        parsed = urlparse(self._endpoint)
        host = parsed.netloc
        if not host:
            return ""
        scheme = parsed.scheme or "https"
        if self._path_style:
            return f"{scheme}://{host}/{self._bucket}"
        return f"{scheme}://{self._bucket}.{host}"

    def url(self, key: str) -> str | None:
        base = self.public_base()
        if not base or not key:
            return None
        return f"{base}/{key.lstrip('/')}"


# ---------------------------------------------------------------------
# 全局实例：按配置指纹缓存（运行中改设置后自动重建，参照 factory.py 的 TTS 缓存做法）
# ---------------------------------------------------------------------
_storage: StorageBackend | None = None
_storage_fingerprint: str | None = None


def _fingerprint() -> str:
    backend = (settings.STORAGE_BACKEND or "local").strip().lower()
    if backend != "s3":
        return "local"
    return "|".join([
        "s3",
        settings.S3_ENDPOINT or "",
        settings.S3_REGION or "",
        settings.S3_BUCKET or "",
        settings.S3_ACCESS_KEY or "",
        settings.S3_SECRET_KEY or "",
        settings.S3_PREFIX or "",
        settings.S3_PUBLIC_BASE_URL or "",
        str(bool(settings.S3_PATH_STYLE)),
    ])


def get_storage() -> StorageBackend:
    """取当前存储后端（按配置指纹缓存；改设置后自动重建）。"""
    global _storage, _storage_fingerprint
    fp = _fingerprint()
    if _storage is None or _storage_fingerprint != fp:
        _storage = S3Storage() if fp != "local" else LocalStorage()
        _storage_fingerprint = fp
    return _storage


def storage_enabled() -> bool:
    """是否启用远端归档（local 时为 False）。"""
    return get_storage().archives_remotely


def cleanup_local_enabled() -> bool:
    """归档成功后是否删除本地副本（仅远端归档时才有意义）。"""
    return bool(getattr(settings, "STORAGE_CLEANUP_LOCAL", True))
