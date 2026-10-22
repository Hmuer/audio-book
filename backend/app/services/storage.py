"""对象存储（G-1）：交付物归档 + 分发。

后端（由 `STORAGE_BACKEND` 选择）
- **local**（默认）：产物只落本地盘，行为与接入前**完全一致** → 可随时回滚。
- **s3**：腾讯云 COS，走 S3 兼容协议（boto3）。上传/回源/删除都丢到线程池执行，
  不阻塞事件循环；下载使用 COS 签名直链（配置了公开 CDN 域名时用该域名）。

设计前提：对象存储保存章节 MP3、LRC、整书 ZIP；合成期间仍需临时文件。
`STORAGE_CLEANUP_LOCAL` 开启时，章节上传后立即清除本地副本，打包时逐章回源，
ZIP 通过管道直接上传对象存储，本地仅暂存当前章节 MP3。

key 规划（决策 4：仅交付物）
    {prefix}/{project_id}/{build_id}/{filename}
`filename` 就是既有的产物名（`build_xxx_ch0001.mp3` / `build_xxx_ch0001-0050.zip` / `…ch0001.lrc`），
key 与本地文件名一一对应，排查时不需要额外映射表。

不归档（有意为之）：段级缓存（可重建）、timings sidecar（内部中间产物）、
章节预告片、导入的源 TXT（可能含隐私或版权内容，公有读桶风险高）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable
from urllib.parse import urlparse

from ..core.config import settings

logger = logging.getLogger(__name__)


def _add_cos_delete_objects_md5(request, **_kwargs) -> None:
    """COS 的 DeleteObjects 必须带请求 XML 的 Content-MD5。"""
    body = request.body or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hashlib.md5(body, usedforsecurity=False).digest()
    request.headers["Content-MD5"] = base64.b64encode(digest).decode("ascii")


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
    async def delete_prefix(self, prefix: str) -> int:
        """删除前缀下全部对象；失败时抛错，供删除项目/构建阻止丢失清理线索。"""

    @abstractmethod
    def url(self, key: str) -> str | None:
        """远端下载直链；local 后端返回 None（调用方回落到媒体 token）。"""


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

    async def delete_prefix(self, prefix: str) -> int:
        return 0

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
        self._bucket = (settings.S3_BUCKET or "").strip()
        endpoint = (settings.S3_ENDPOINT or "").strip().rstrip("/")
        parsed = urlparse(endpoint)
        # COS 控制台给出的访问域名通常已含桶名；boto3 的 virtual 模式会再加一次。
        # 统一为地域端点，供上传、回源和公网直链共用。
        bucket_prefix = f"{self._bucket}.cos."
        if self._bucket and (parsed.hostname or "").startswith(bucket_prefix):
            endpoint = parsed._replace(netloc=parsed.netloc[len(self._bucket) + 1:]).geturl()
        self._endpoint = endpoint
        self._region = (settings.S3_REGION or "").strip() or "ap-guangzhou"
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
                # botocore 新版本默认给 UploadPart 加可选校验和并改用 aws-chunked。
                # COS 会拒绝这种不带 Content-Length 的分片请求。
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        # botocore 可能仅发送 CRC32；COS 批量删除仍要求 Content-MD5。
        # 在签名前读取已序列化的 XML 请求体，确保 MD5 参与 SigV4 签名。
        self._client.meta.events.register(
            "before-sign.s3.DeleteObjects", _add_cos_delete_objects_md5,
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

    async def archive_stream(
        self, *, key: str, producer: Callable[[BinaryIO], Awaitable[Any]],
        content_type: str | None = None,
    ) -> tuple[Any, int]:
        """从 ZIP 生成器流式上传；大文件由 boto3 分片，本地不落整包。"""
        client = self._ensure_client()
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        writer = os.fdopen(write_fd, "wb", buffering=0)

        class CountingWriter:
            def __init__(self, stream: BinaryIO) -> None:
                self.stream = stream
                self.count = 0

            def write(self, data: bytes) -> int:
                written = self.stream.write(data)
                self.count += written
                return written

            def flush(self) -> None:
                self.stream.flush()

        sink = CountingWriter(writer)

        class _FullReadPipe:
            """让 boto3 的“大小探测”读到完整阈值。

            管道 read(n) 是短读：boto3 的 UploadNonSeekableInputManager 先
            read(multipart_threshold) 判断是否分片；如果只返回当前管道里的
            几 KB，它会误判为小文件，随后 read() 把整个 ZIP 读进内存
            （5GB 级整书 ZIP 会直接把进程 OOM）。
            这里循环读满 n 字节（EOF 才提前返回），保证 >=8MB 的 ZIP 走分片。
            """

            def __init__(self, stream: BinaryIO) -> None:
                self._stream = stream

            def read(self, size: int = -1) -> bytes:
                if size is None or size < 0:
                    return self._stream.read()
                chunks: list[bytes] = []
                remaining = size
                while remaining > 0:
                    chunk = self._stream.read(remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks)

        def upload() -> None:
            with reader:
                client.upload_fileobj(
                    _FullReadPipe(reader), self._bucket, key,
                    ExtraArgs={"ContentType": content_type} if content_type else {},
                )

        upload_task = asyncio.create_task(asyncio.to_thread(upload))
        # 先让 upload 线程跑起来，再开始写管道：producer 第一段写入是同步
        # os.write，如果此时读端还没启动，>=64KB 的写入会阻塞事件循环。
        await asyncio.sleep(0)
        try:
            result = await producer(sink)
        except BaseException as producer_error:
            writer.close()
            upload_error = None
            try:
                await upload_task
            except Exception as error:
                upload_error = error
            # 管道 EOF 也可能被上传器视为完整文件；失败时不能留下残缺 ZIP。
            await self.delete_key(key)
            if isinstance(producer_error, BrokenPipeError) and upload_error is not None:
                raise upload_error from producer_error
            raise
        else:
            writer.close()
            try:
                await upload_task
            except Exception:
                await self.delete_key(key)
                raise
            logger.info("[storage] 已流式归档 key=%s size=%d", key, sink.count)
            return result, sink.count

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

    async def peek_bytes(self, key: str, count: int = 1024) -> bytes:
        """仅读取对象开头，判断旧 MP3 是否带失效的段级 seek 索引。"""
        from contextlib import closing

        def _read() -> bytes:
            response = self._ensure_client().get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes=0-{count - 1}",
            )
            with closing(response["Body"]) as body:
                return body.read(count)

        return await asyncio.to_thread(_read)

    async def delete_key(self, key: str) -> None:
        try:
            client = self._ensure_client()
            await asyncio.to_thread(
                client.delete_object, Bucket=self._bucket, Key=key
            )
        except Exception as e:
            logger.warning(f"[storage] 删除失败 key={key}: {type(e).__name__}: {e}")

    async def delete_prefix(self, prefix: str) -> int:
        """清理整个构建或项目目录，包含数据库未记录的中途上传产物。"""
        if not prefix or not prefix.endswith("/"):
            raise ValueError("对象存储删除前缀必须是非空目录")
        client = self._ensure_client()

        def _delete() -> int:
            count = 0
            batch: list[dict[str, str]] = []

            def flush() -> None:
                nonlocal count
                if not batch:
                    return
                response = client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": batch.copy(), "Quiet": True},
                )
                if response.get("Errors"):
                    raise RuntimeError(f"对象存储批量删除失败: {response['Errors'][:3]}")
                count += len(batch)
                batch.clear()

            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    batch.append({"Key": obj["Key"]})
                    if len(batch) == 1000:
                        flush()
            flush()
            return count

        count = await asyncio.to_thread(_delete)
        logger.info("[storage] 已清理 prefix=%s objects=%d", prefix, count)
        return count

    # ---------- 远端下载链接 ----------
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
        if not key:
            return None
        if self._public_base:
            return f"{self._public_base}/{key.lstrip('/')}"
        # 默认使用私有桶签名链接，避免依赖公有读 ACL；签名在本地计算，无网络调用。
        return self._ensure_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key.lstrip("/")},
            ExpiresIn=3600,
        )


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
