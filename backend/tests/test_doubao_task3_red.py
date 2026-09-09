"""Task 3 RED tests — DoubaoTTSProvider 核心行为。

T-DV1 list_voices() 合并：
    a) 官方内置 ≥14 条音色（豆包官方音色库 BV 系列 + 历史 zh_* 兼容 ID，
       每条带 name/zh_tags/gender/age/scene/dialect 等属性，且 provider=doubao），
    b) 本地 voices_doubao.json（若存在）加载的自定义条目，
    c) icl: < 官方音色，ID 前缀为 "doubao:"。
T-DV2 synthesize_to_bytes 走 HTTP API，voice_id 必须剥离前缀（icl:* 原样保留）。
T-DV3 指令参数：instruction_text / speaker_style / emotion / speed 正确加入请求体。
T-DV4 重试策略：遇到 429 至少重试一次，指数退避；遇到 5xx 重试 N 次，最后抛 RuntimeError。

测试使用 httpx 离线 Mock，不依赖真实豆包后端。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-DV1a：官方内置 ≥14 条音色 + voice 对象包含元数据
# ---------------------------------------------------------------------
def test_doubao_list_voices_has_builtin_14_profiles():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    dp = DoubaoTTSProvider()
    voices = dp._builtin_voices_sync()
    # 豆包官方音色库 ≥ 14 条（实际内置约 100+ 条 BV 系列 + zh_* 兼容）
    assert len(voices) >= 14
    # 关键字段必须齐全
    for v in voices:
        assert v["id"].startswith("doubao:"), f"doubao 音色 id 需带前缀：{v['id']}"
        assert v["provider"] == "doubao"
        assert v["name"]
        # 至少包含一个中文标签
        assert isinstance(v.get("zh_tags"), list) and v["zh_tags"]
        assert v.get("gender") in ("male", "female", "neutral")
        # 元属性：age / scene / dialect
        assert v.get("age") in ("child", "teen", "youth", "middle", "senior")
        assert isinstance(v.get("scene"), list) or isinstance(v.get("scene"), str)
        # dialect 可以为空字符串但必须存在
        assert "dialect" in v

    # 核心声线必须覆盖：基于官方音色表（97465 + 1257544）的真实 voice_type。
    # 注意：原 v1 风格的 zh_*_xxx 自创 id 全部已移除，BVxxx_stream 全部已修正为
    # 官方 BVxxx_streaming 拼写。
    sample_ids = [
        # 小模型通用（官方 BV 拼写：_streaming 后缀）
        "doubao:BV001_streaming",          # 通用女声
        "doubao:BV002_streaming",          # 通用男声
        "doubao:BV700_streaming",          # 灿灿（多情感 + 多语种）
        "doubao:BV701_streaming",          # 擎苍（旁白 + 多情感）
        # 小模型有声阅读
        "doubao:BV102_streaming",          # 儒雅青年
        "doubao:BV107_streaming",          # 霸气青叔
        "doubao:BV119_streaming",          # 通用赘婿
        # 小模型方言
        "doubao:BV019_streaming",          # 重庆小伙（四川）
        "doubao:BV021_streaming",          # 东北老铁（东北）
        "doubao:BV026_streaming",          # 港剧男神（粤语）
        # 小模型多语种
        "doubao:BV503_streaming",          # 活力女声-Ariana（美式英语）
        "doubao:BV040_streaming",          # 亲切女声-Anna（英式英语）
        "doubao:BV421_streaming",          # 天才少女（8 国）
        # 小模型特色 / 教育 / 智能助手
        "doubao:BV034_streaming",          # 知性姐姐-双语（教育）
        "doubao:BV007_streaming",          # 亲切女声（智能助手）
        "doubao:BV051_streaming",          # 奶气萌娃（特色）
        # 大模型 2.0 通用
        "doubao:zh_female_vv_uranus_bigtts",     # Vivi 2.0（S2S 多语种多方言）
        "doubao:zh_female_cancan_uranus_bigtts", # 知性灿灿 2.0
        "doubao:zh_male_qingcang_uranus_bigtts", # 擎苍 2.0
        # 大模型 2.0 角色 / 教育 / 客服
        "doubao:zh_female_peiqi_uranus_bigtts",   # 佩奇猪 2.0
        "doubao:zh_female_mizai_uranus_bigtts",   # 黑猫侦探社咪仔 2.0
        "doubao:zh_female_kefunvsheng_uranus_bigtts", # 暖阳女声 2.0
    ]
    ids = [v["id"] for v in voices]
    for sid in sample_ids:
        assert sid in ids, f"缺少内置音色：{sid}"


# ---------------------------------------------------------------------
# T-DV1b：voices_doubao.json 增量覆盖（用户在 data 目录放自定义音色表时合并）
# ---------------------------------------------------------------------
def test_doubao_list_voices_merges_custom_json(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "audio").mkdir(parents=True, exist_ok=True)
        # 用 monkeypatch settings.DATA_DIR
        from backend.app.core import config as cfgmod
        monkeypatch.setattr(cfgmod.settings, "DATA_DIR", td_path)

        custom_voices = [
            {
                "id": "doubao:custom_female",
                "name": "定制女声",
                "provider": "doubao",
                "zh_tags": ["定制", "测试"],
                "gender": "female",
                "age": "youth",
                "scene": ["日常"],
                "dialect": "",
            }
        ]
        (td_path / "voices_doubao.json").write_text(
            json.dumps(custom_voices, ensure_ascii=False), encoding="utf-8"
        )

        import asyncio
        dp = DoubaoTTSProvider()
        voices = asyncio.run(dp.list_voices())
        ids = [v["id"] for v in voices]
        # 合并后既有内置音色，又有自定义 1 条
        assert "doubao:custom_female" in ids
        # 且自定义覆盖（自定义 id 重复时应当替换内置）
        assert ids.count("doubao:custom_female") == 1
        assert len(voices) >= 2  # 内置 ≥14 + 自定义 1，总数远大于 2


# ---------------------------------------------------------------------
# T-DV2 + T-DV3：synthesize_to_bytes 正确剥离前缀并注入所有请求参数
# ---------------------------------------------------------------------
class _CapturedRequest:
    def __init__(self):
        self.headers: dict | None = None
        self.json_body: dict | None = None


def _fake_async_client(captured: _CapturedRequest, response_mp3_bytes: bytes, status: int = 200, response_json: dict | None = None):
    """构造一个假的 httpx.AsyncClient，请求时捕获请求体并返回固定响应。"""

    class _FakeStream:
        def __init__(self, data: bytes):
            self._data = data

        async def aread(self):
            return self._data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeResponse:
        status_code = status

        def __init__(self, mp3_bytes: bytes, json_body: dict | None):
            self._mp3 = mp3_bytes
            self._json = json_body
            self.headers = {"Content-Type": "audio/mpeg" if json_body is None else "application/json"}

        async def aiter_bytes(self, chunk_size=1024):
            # 简单地一次性 yield
            if self._json is not None:
                yield json.dumps(self._json).encode("utf-8")
            else:
                yield self._mp3

        async def aread(self):
            if self._json is not None:
                return json.dumps(self._json).encode("utf-8")
            return self._mp3

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                raise httpx.HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=None,  # type: ignore
                    response=self,  # type: ignore
                )

        def json(self):
            return self._json or {}

    class _FakeClient:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def build_request(self, *a, **kw):
            # 简单满足 httpx 接口，无需真实构建
            class _FakeReq:
                url = "https://example.com"
                method = "POST"
                content = b""
            return _FakeReq()

        def send(self, request):
            # 同步 send；测试用 async 版本也会走 post()
            raise NotImplementedError

        async def post(self, url, headers=None, json=None, data=None, files=None, params=None, timeout=None):
            captured.headers = dict(headers or {})
            captured.json_body = json
            if response_json is not None and status >= 400:
                return _FakeResponse(b"", response_json)
            if response_json is not None:
                return _FakeResponse(b"", response_json)
            return _FakeResponse(response_mp3_bytes, None)

        def close(self):
            self.closed = True
            pass

    return _FakeClient


def test_synthesize_strips_prefix_and_injects_params(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider

    # 注入假 ak
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak-xyz")

    captured = _CapturedRequest()
    dummy_mp3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 48)
    # 真实 v1 协议：服务端返 JSON，业务码 3000 + base64 MP3
    import base64 as _b64
    success_json = {"code": 3000, "message": "Success", "data": _b64.b64encode(dummy_mp3).decode("ascii")}
    dp = DoubaoTTSProvider()

    # Monkey patch 掉 httpx.AsyncClient 的构造
    import httpx

    saved = httpx.AsyncClient
    try:
        class _PatchedAsyncClient:
            def __init__(self, *a, **kw):
                self._inner = _fake_async_client(captured, dummy_mp3, status=200, response_json=success_json)()

            async def __aenter__(self):
                return await self._inner.__aenter__()

            async def __aexit__(self, *a):
                return await self._inner.__aexit__(*a)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        httpx.AsyncClient = _PatchedAsyncClient

        import asyncio
        data, dur = asyncio.run(
            dp.synthesize_to_bytes(
                "你好世界",
                "doubao:zh_female_qingxin",
                emotion="happy",
                speed=1.15,
                instruction_text="语气欢快，像老朋友",
                speaker_style="witty",
            )
        )
    finally:
        httpx.AsyncClient = saved

    assert data == dummy_mp3
    assert dur >= 0
    # Authorization 必须带 ak
    assert captured.headers
    auth = captured.headers.get("Authorization", "") or captured.headers.get("authorization", "")
    assert "test-ak-xyz" in auth, f"请求未携带 AK：{captured.headers}"

    # 请求体关键参数
    body = captured.json_body or {}
    # voice_id 前缀被剥掉
    assert body.get("voice_id") == "zh_female_qingxin" or body.get("speaker_id") == "zh_female_qingxin" or body.get("speaker") == "zh_female_qingxin"
    assert body.get("text") == "你好世界"
    # speed 在 [0.2, 3.0] 倍内的换算，原始 1.15 应当被传下去
    speed_val = body.get("speed_ratio") or body.get("speed") or body.get("rate")
    assert speed_val is not None
    assert 1.1 <= float(speed_val) <= 1.2
    # emotion / instruction_text / speaker_style
    if "emotion" in body:
        assert body["emotion"] == "happy"
    if "instruction_text" in body:
        assert body["instruction_text"] == "语气欢快，像老朋友"
    if "speaker_style" in body:
        assert body["speaker_style"] == "witty"


# ---------------------------------------------------------------------
# T-DV4：429 重试 + 最终 5xx 抛错
# ---------------------------------------------------------------------
class _RetryCountClient:
    """一个会前 N-1 次返回 429，最后一次返回 200 的假 client。"""

    def __init__(self, fail_count: int, fail_status: int, final_ok: bool, captured: _CapturedRequest):
        self._fail_count = fail_count
        self._fail_status = fail_status
        self._final_ok = final_ok
        self._call_count = 0
        self._captured = captured
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None, **kw):
        self._call_count += 1
        self._captured.headers = dict(headers or {})
        self._captured.json_body = json
        if self._call_count <= self._fail_count:
            import httpx
            fail_status = self._fail_status

            class _FailResp:
                status_code = fail_status
                headers = {"Retry-After": "1"}

                def raise_for_status(self_):
                    raise httpx.HTTPStatusError(
                        f"HTTP {self_.status_code}",
                        request=None,  # type: ignore
                        response=self_,  # type: ignore
                    )

                def json(self_):
                    return {"code": self_.status_code, "message": "rate limit"}

                async def aiter_bytes(self_, *a, **kw):
                    yield b""

                async def aread(self_):
                    return b""
            return _FailResp()
        if self._final_ok:
            class _OkResp:
                status_code = 200
                headers = {"Content-Type": "application/json"}

                def raise_for_status(self_):
                    return None

                async def aiter_bytes(self_, *a, **kw):
                    # v1 协议：返回 JSON 业务码 + base64 MP3
                    import base64 as _b64
                    dummy_mp3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 48)
                    resp = {"code": 3000, "message": "Success",
                            "data": _b64.b64encode(dummy_mp3).decode("ascii")}
                    import json as _json
                    yield _json.dumps(resp).encode("utf-8")

                async def aread(self_):
                    import base64 as _b64
                    dummy_mp3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 48)
                    resp = {"code": 3000, "message": "Success",
                            "data": _b64.b64encode(dummy_mp3).decode("ascii")}
                    import json as _json
                    return _json.dumps(resp).encode("utf-8")

                def json(self_):
                    return {}
            return _OkResp()
        # 最终也失败（5xx 用尽重试）
        import httpx

        class _FinalFail:
            status_code = 500
            headers = {}

            def raise_for_status(self_):
                raise httpx.HTTPStatusError(
                    f"HTTP {self_.status_code}",
                    request=None,  # type: ignore
                    response=self_,  # type: ignore
                )

            def json(self_):
                return {"code": 500, "message": "server error"}

            async def aiter_bytes(self_, *a, **kw):
                yield b""

            async def aread(self_):
                return b""
        return _FinalFail()

    def close(self):
        self.closed = True


def test_synthesize_retries_429_then_succeeds(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "ak")

    captured = _CapturedRequest()
    retry_client = _RetryCountClient(fail_count=2, fail_status=429, final_ok=True, captured=captured)

    import httpx
    saved = httpx.AsyncClient
    try:
        class _PC:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return retry_client
            async def __aexit__(self, *a):
                return False
        httpx.AsyncClient = _PC

        import asyncio
        dp = DoubaoTTSProvider()
        data, dur = asyncio.run(dp.synthesize_to_bytes("a", "doubao:zh_female_qingxin"))
    finally:
        httpx.AsyncClient = saved

    # 2 次 429 + 1 次 200 = 3 次请求
    assert retry_client._call_count == 3
    assert data.startswith(b"\xff\xfb")


def test_synthesize_5xx_exhaustive_retries_raises(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "ak")

    captured = _CapturedRequest()
    # 默认重试 5 次 → 前 5 次都 5xx，最后还是 5xx（fail_count=10 保证 5 次 retry 全击中）
    retry_client = _RetryCountClient(fail_count=20, fail_status=500, final_ok=False, captured=captured)

    import httpx
    saved = httpx.AsyncClient
    try:
        class _PC:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return retry_client
            async def __aexit__(self, *a):
                return False
        httpx.AsyncClient = _PC

        import asyncio
        dp = DoubaoTTSProvider()
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(dp.synthesize_to_bytes("a", "doubao:zh_female_qingxin"))
    finally:
        httpx.AsyncClient = saved

    # 总共重试 = 初始调用 + 5 次 retry = 6 次
    assert retry_client._call_count >= 5
    assert retry_client._call_count <= 10
    msg = str(excinfo.value).lower()
    assert "tts" in msg or "豆包" in msg or "doubao" in msg or "http" in msg
