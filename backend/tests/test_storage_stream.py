"""整书 ZIP 通过上传管道写入 COS，无需在本地生成整包。"""
from __future__ import annotations

import asyncio
from io import BytesIO
import zipfile

import pytest

from backend.app.services.storage import S3Storage


class _StreamClient:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_upload = fail_upload

    def upload_fileobj(self, source, bucket, key, ExtraArgs=None) -> None:
        chunks = []
        while chunk := source.read(4096):
            chunks.append(chunk)
        self.objects[key] = b"".join(chunks)
        if self.fail_upload:
            raise RuntimeError("上传失败")

    def delete_object(self, *, Bucket, Key) -> None:
        self.objects.pop(Key, None)


def _storage(client: _StreamClient) -> S3Storage:
    storage = S3Storage()
    storage._client = client
    storage._bucket = "test-bucket"
    return storage


@pytest.mark.asyncio
async def test_archive_stream_uploads_readable_zip_and_counts_bytes():
    client = _StreamClient()
    storage = _storage(client)
    payload = bytes(range(256)) * 1024  # 超过管道容量，检查生产和上传并发推进。

    async def produce(sink):
        def write_zip():
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("book/chapter.mp3", payload)
            return "done"

        return await asyncio.to_thread(write_zip)

    result, size = await asyncio.wait_for(
        storage.archive_stream(key="book.zip", producer=produce, content_type="application/zip"),
        timeout=10,
    )
    assert result == "done"
    assert size == len(client.objects["book.zip"])
    with zipfile.ZipFile(BytesIO(client.objects["book.zip"])) as archive:
        assert archive.read("book/chapter.mp3") == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_upload", [False, True])
async def test_archive_stream_removes_partial_object_after_failure(fail_upload):
    client = _StreamClient(fail_upload=fail_upload)
    storage = _storage(client)

    async def produce(sink):
        await asyncio.to_thread(sink.write, b"partial zip")
        if not fail_upload:
            raise RuntimeError("打包失败")

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(storage.archive_stream(key="book.zip", producer=produce), timeout=10)
    assert "book.zip" not in client.objects


class _ThresholdClient:
    """模拟 boto3 的非 seekable 大小探测：第一次 read(threshold) 必须是满读。"""

    def __init__(self) -> None:
        self.first_read = 0
        self.total = 0

    def upload_fileobj(self, source, bucket, key, ExtraArgs=None) -> None:
        self.first_read = len(source.read(8 * 1024 * 1024))
        self.total = self.first_read
        while chunk := source.read(1024 * 1024):
            self.total += len(chunk)

    def delete_object(self, *, Bucket, Key) -> None:
        pass


@pytest.mark.asyncio
async def test_archive_stream_fills_boto3_threshold_read():
    """管道短读会让 boto3 误判为小文件并把整包读进内存（OOM）。"""
    client = _ThresholdClient()
    storage = S3Storage()
    storage._client = client
    storage._bucket = "test-bucket"
    payload = b"x" * (9 * 1024 * 1024)

    async def produce(sink):
        await asyncio.to_thread(sink.write, payload)
        return "done"

    result, size = await asyncio.wait_for(
        storage.archive_stream(key="book.zip", producer=produce), timeout=10,
    )
    assert result == "done"
    assert client.first_read == 8 * 1024 * 1024
    assert size == client.total == len(payload)
