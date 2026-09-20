"""`req_params.model` 开关 RED —— 为什么「语音指令没生效」（2026-09-20）。

背景（用户反馈）：给对白下发了逐段语音指令（`context_texts`），但生成音频完全听不出
情绪差异，例如正文写着「一虚弱的声音」，合成出来却毫无虚弱感。

根因：官方《模型列表》写明豆包语音合成大模型 2.0 分两版 ——
  - `seed-tts-2.0-standard`（**接口默认**）：延时更优、表现稳定，
    **不支持语音指令 QA 和语音标签 CoT**；
  - `seed-tts-2.0-expressive`：支持语音指令 QA 和语音标签 CoT，
    但官方提示「生成效果稳定性存在波动」。
本项目此前**从不下发 `model`** → 一律落在 standard → 指令发出去也被上游**静默忽略**
（没有报错、没有 warning，听感与不加指令一样）。修复：下发
`settings.DOUBAO_TTS_MODEL`（默认 expressive）。

覆盖：
  TM-1 官方 2.0 音色 + 指令 → req_params 同时带 model=expressive 与 context_texts
  TM-2 无指令也要带 model（全章统一，避免同章音色/韵律跳变）
  TM-3 DOUBAO_TTS_MODEL="" → 不下发 model（等价旧行为，可一键回退）
  TM-4 复刻音色不下发 model（官方 HTTP 文档：model 与 context_texts 在复刻场景互斥）
  TM-5 切 tts_model 必须让段缓存键与 build config_digest 变化
       （否则改完设置重建会命中旧产物，用户听不出任何变化）
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.ai.providers.doubao import tts as T  # noqa: E402
from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3  # noqa: E402

_ENDPOINT = "https://example.test/v3/tts/unidirectional"
_AUDIO = b"\xff\xfb\x90\x64" + b"\x11" * 32


class _Capture:
    """把 httpx 流式 client 换成假的，同时抓下实际发出的请求体。"""

    def __init__(self):
        self.payload: dict | None = None
        self.headers: dict | None = None

    def install(self, monkeypatch):
        payload_ref = self
        line = json.dumps({
            "code": 20000000, "message": "OK",
            "data": base64.b64encode(_AUDIO).decode(),
        })

        class _Resp:
            status_code = 200
            headers = {"X-Tt-Logid": "log"}

            def raise_for_status(self):
                return None

            async def aread(self):
                return b""

            async def aiter_lines(self):
                yield line

        class _Stream:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        class _Client:
            def stream(self, method, url, **kw):
                payload_ref.payload = kw.get("json")
                payload_ref.headers = kw.get("headers")
                return _Stream()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())


def _provider(monkeypatch) -> DoubaoTTSProviderV3:
    # ⚠️ patch provider 模块自己 import 的那个 settings 对象：全量跑时
    # test_path_env_override_red 会 reload core.config，`core.config.settings`
    # 会变成另一个对象，只 patch 那里的话 provider 侧看不到（E-1 的同一个根因）。
    monkeypatch.setattr(T.settings, "DOUBAO_TTS_V3_BASE_URL", _ENDPOINT)
    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake-api-key"  # type: ignore[method-assign]
    return p


@pytest.mark.asyncio
async def test_tm1_official_voice_sends_model_and_context_texts(monkeypatch):
    monkeypatch.setattr(T.settings, "DOUBAO_TTS_MODEL", "seed-tts-2.0-expressive")
    cap = _Capture()
    cap.install(monkeypatch)
    p = _provider(monkeypatch)

    await p.synthesize_to_bytes(
        "还撑得住，燕儿，快到飘云谷了吗？",
        "doubao:zh_female_vv_uranus_bigtts",
        instruction_text="用虚弱沙哑、气息不足的语气说",
    )

    req = cap.payload["req_params"]
    assert req["model"] == "seed-tts-2.0-expressive", (
        "不下发 model 会落到 standard，而 standard 不支持语音指令 → 指令被静默忽略"
    )
    assert req["context_texts"] == ["用虚弱沙哑、气息不足的语气说"]


@pytest.mark.asyncio
async def test_tm2_model_sent_even_without_instruction(monkeypatch):
    """model 是「整章统一」的模型选择：没指令的段也要带，避免同章音色/韵律跳变。"""
    monkeypatch.setattr(T.settings, "DOUBAO_TTS_MODEL", "seed-tts-2.0-expressive")
    cap = _Capture()
    cap.install(monkeypatch)
    p = _provider(monkeypatch)

    await p.synthesize_to_bytes("旁白一句。", "doubao:zh_male_qingcang_uranus_bigtts")

    assert cap.payload["req_params"]["model"] == "seed-tts-2.0-expressive"
    assert "context_texts" not in cap.payload["req_params"]


@pytest.mark.asyncio
async def test_tm3_empty_setting_falls_back_to_legacy(monkeypatch):
    """DOUBAO_TTS_MODEL="" → 完全不下发该字段（一键回退到旧行为）。"""
    monkeypatch.setattr(T.settings, "DOUBAO_TTS_MODEL", "")
    cap = _Capture()
    cap.install(monkeypatch)
    p = _provider(monkeypatch)

    await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert "model" not in cap.payload["req_params"]


@pytest.mark.asyncio
async def test_tm4_clone_voice_does_not_send_model(monkeypatch):
    """复刻音色：官方文档写明「model 仅当 speaker 为复刻音色时需指定，且指定后
    不支持 context_texts」→ 复刻场景保住 context_texts，不下发 model。"""
    monkeypatch.setattr(T.settings, "DOUBAO_TTS_MODEL", "seed-tts-2.0-expressive")
    cap = _Capture()
    cap.install(monkeypatch)
    p = _provider(monkeypatch)

    await p.synthesize_to_bytes("嗯。", "icl:S_abc123")

    assert "model" not in cap.payload["req_params"]
    assert cap.headers["X-Api-Resource-Id"] == "seed-icl-2.0"


def test_tm5_cache_key_and_config_digest_track_tts_model(monkeypatch):
    from backend.app.services.build import (
        _calc_config_digest,
        _seg_cache_key,
    )

    k_std = _seg_cache_key("doubao:zh_female_vv_uranus_bigtts", 1.0, "你好",
                           tts_model="seed-tts-2.0-standard")
    k_exp = _seg_cache_key("doubao:zh_female_vv_uranus_bigtts", 1.0, "你好",
                           tts_model="seed-tts-2.0-expressive")
    assert k_std != k_exp, "切 model 必须让段缓存失效，否则重建还是旧的无情绪音频"

    d_std = _calc_config_digest("narr", 1.0, {}, tts_model="seed-tts-2.0-standard")
    d_exp = _calc_config_digest("narr", 1.0, {}, tts_model="seed-tts-2.0-expressive")
    d_none = _calc_config_digest("narr", 1.0, {})
    assert d_std != d_exp, "切 model 必须产生新 build，否则会复用历史成功 build 的旧产物"
    assert d_none not in (d_std, d_exp)


def test_tm6_default_setting_is_expressive():
    """默认必须是 expressive：standard 不支持语音指令，指令就等于白生成。"""
    from backend.app.core.config import settings

    assert settings.DOUBAO_TTS_MODEL == "seed-tts-2.0-expressive"
