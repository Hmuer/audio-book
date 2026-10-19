"""书籍分类旁白与封面输入的离线回归测试。"""
import pytest
import zlib
import json
import time
import base64
import httpx
from fastapi.testclient import TestClient

from backend.app.services import build, cover


@pytest.mark.asyncio
async def test_category_selects_narrator_without_overriding_manual_voice(monkeypatch):
    voices = [
        {"id": "demo:m1", "provider": "demo", "gender": "male"},
        {"id": "demo:m2", "provider": "demo", "gender": "male"},
        {"id": "demo:f1", "provider": "demo", "gender": "female"},
        {"id": "demo:f2", "provider": "demo", "gender": "female"},
    ]

    class FakeTTS:
        async def list_voices(self):
            return voices

    monkeypatch.setattr(build, "get_tts", lambda _: FakeTTS())
    female = await build._resolve_build_voice_defaults("demo", "", book_category="女频")
    assert female[:3] == ("demo:f1", "demo:m1", "demo:f2")
    manual = await build._resolve_build_voice_defaults("demo", "demo:m1", book_category="女频")
    assert manual[0] == "demo:m1"


def test_cover_rejects_non_image_and_oversized_payload():
    with pytest.raises(ValueError):
        cover.image_type(b"<svg onload='alert(1)'></svg>")
    with pytest.raises(ValueError):
        cover.image_type(b"\x89PNG\r\n\x1a\n" + b"x" * cover.MAX_IMAGE_BYTES)
    def chunk(kind, data):
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    tiny_png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    assert cover.image_type(tiny_png) == ("png", "image/png")


def test_cover_upload_read_and_delete_use_object_storage(monkeypatch):
    class FakeStorage:
        archives_remotely = True

        def __init__(self):
            self.objects = {}

        async def put_bytes(self, *, key, data, content_type=None):
            self.objects[key] = data
            return True

        async def fetch_to(self, *, key, dest):
            if key not in self.objects:
                return False
            dest.write_bytes(self.objects[key])
            return True

        async def delete_key(self, key):
            self.objects.pop(key, None)

    fake = FakeStorage()
    monkeypatch.setattr(cover, "get_storage", lambda: fake)

    def chunk(kind, data):
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    from backend.app.main import app
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project_id = client.post("/api/projects", json={"name": "测试封面"}, headers=headers).json()["project_id"]
        path = f"/api/projects/{project_id}/cover"
        uploaded = client.post(path + "/upload", files={"file": ("cover.png", png, "image/png")}, headers=headers)
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["has_cover"] is True
        assert len(fake.objects) == 1
        assert client.get(path, headers=headers).content == png
        assert client.delete(path, headers=headers).json()["has_cover"] is False
        assert fake.objects == {}


@pytest.mark.parametrize("provider_id, model", [
    ("image_provider", "cover-model"),
    ("minimax", "image-01"),
])
def test_ai_cover_uses_book_metadata_and_archives_result(monkeypatch, provider_id, model):
    class FakeStorage:
        archives_remotely = True
        objects = {}

        async def put_bytes(self, *, key, data, content_type=None):
            self.objects[key] = data
            return True

        async def delete_key(self, key):
            self.objects.pop(key, None)

    fake = FakeStorage()
    monkeypatch.setattr(cover, "get_storage", lambda: fake)
    monkeypatch.setattr(cover, "get_active", lambda kind: (
        provider_id, model, {
            "enabled": True, "api_key": "test-key", "base_url": "https://images.example/v1",
        }
    ))
    def chunk(kind, data):
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if provider_id == "minimax":
            assert str(request.url) == "https://api.minimax.cn/v1/image_generation"
            return httpx.Response(200, json={
                "data": {"image_base64": [base64.b64encode(png).decode()]},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            })
        assert request.url.path == "/v1/images/generations"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(cover.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))

    from backend.app.main import app
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project_id = client.post("/api/projects", json={"name": "海边故事"}, headers=headers).json()["project_id"]
        assert client.patch(f"/api/projects/{project_id}", json={
            "description": "一段关于大海的旅程", "book_category": "女频", "selling_points": "温暖治愈",
        }, headers=headers).status_code == 200
        started = client.post(f"/api/projects/{project_id}/cover/generate", headers=headers)
        assert started.status_code == 200, started.text
        for _ in range(40):
            status = client.get(f"/api/projects/{project_id}/cover/status", headers=headers).json()
            if status["status"] == "done":
                break
            time.sleep(0.05)
        assert status["status"] == "done", status
        assert len(fake.objects) == 1
        assert requests[0]["model"] == model
        assert "海边故事" in requests[0]["prompt"]
        assert "温暖治愈" in requests[0]["prompt"]
        if provider_id == "minimax":
            assert requests[0]["aspect_ratio"] == "2:3"
            assert requests[0]["response_format"] == "base64"
            assert "size" not in requests[0]
            assert len(requests[0]["prompt"]) <= 1500
        else:
            assert requests[0]["size"] == "1024x1536"


@pytest.mark.asyncio
async def test_minimax_cover_surfaces_provider_error(monkeypatch):
    progress = []

    async def set_progress(*args):
        progress.append(args)

    async def save_cover(*args):
        pytest.fail("失败响应不应保存封面")

    monkeypatch.setattr(cover, "_set_progress", set_progress)
    monkeypatch.setattr(cover, "_save_cover", save_cover)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"},
        "data": {},
    }))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(cover.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))

    await cover._generate(
        "book-id", "minimax", "image-01", {"api_key": "test-key", "base_url": "https://api.minimaxi.com/v1"},
        "书名", "简介", "文学", "标签", "卖点",
    )
    assert progress[-1][1:3] == ("failed", 0)
    assert "1008" in progress[-1][3]
    assert "insufficient balance" in progress[-1][3]
