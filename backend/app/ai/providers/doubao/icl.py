"""豆包 ICL 2.0 声音复刻客户端（基于官方 v3 接口 /api/v3/tts/voice_clone + /get_voice）。

接口规范（按官方文档 https://www.volcengine.com/docs/6561/2227958?lang=zh +
                              https://www.volcengine.com/docs/6561/2535742?lang=en）：

  - 创建训练：POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
    Headers: Content-Type: application/json
             X-Api-Key: <API Key>                  # 新版控制台
             X-Api-Request-Id: <uuid>              # 每次唯一
             （旧版兼容）X-Api-App-Key + X-Api-Access-Key
    Body: {
      "speaker_id": "<由我们生成的唯一代号>" | "custom_speaker_id",
      "custom_speaker_id": "<可选，后付费用户自定义>",
      "audio": {"data": "<base64>", "format": "mp3|wav|..."},
      "text": "<可选，按此文本念诵，校验 WER>",
      "language": 0,                              # 0=cn / 1=en / 2=ja ...
      "demo_text": "<可选，试听文本>",
      "model_type": "ICL2.0" | "ICL1.0" | "DiT",  # 复刻 2.0/1.0
    }
    响应: { "code": <int>, "message": ..., "data": {"speaker_id": "S_xxx", ...},
            "X-Tt-Logid": <header> }

  - 查询训练：POST https://openspeech.bytedance.com/api/v3/tts/get_voice
    Headers: 同上
    Body: {"speaker_id": "S_xxx"} 或 {speaker_id:"custom_speaker_id", custom_speaker_id:"..."}
    响应: {
      "code": <int>, "message": ...,
      "speaker_id": "S_xxx",
      "status": 0/1/2/3/4,   # NotFound/Training/Success/Failed/Active
      "speaker_status": [{"model_type": 5, "demo_audio": "https://..."}, ...],
      "available_training_times": <int>,
      "create_time": <ms>, "language": <int>,
      "X-Tt-Logid": <header>
    }

本模块对调用方提供统一抽象：
  - create_training(voice_name, audio_bytes) → str  (返回豆包侧 speaker_id)
  - query_training(speaker_id) → dict 返回:
      {status, progress, cloned_voice_id, error, model_type, demo_audio}
      status 语义按官方文档翻译：0 NotFound / 1 Training / 2 Success /
      3 Failed / 4 Active（2/4 均可合成）
  - 所有调用走 factory._doubao_rpm_wait_acquire("icl") 统一限流。
  - 测试可 monkeypatch `_http_post_json`（与原约定一致，向后兼容）。
"""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from typing import Any

from backend.app.ai.factory import _doubao_rpm_wait_acquire
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


# 官方 status 语义（参考火山文档 /api/v3/tts/get_voice 响应字段说明）
# 0 NotFound / 1 Training / 2 Success / 3 Failed / 4 Active（2/4 均可合成）
STATUS_NOT_FOUND = 0
STATUS_TRAINING = 1
STATUS_SUCCESS = 2
STATUS_FAILED = 3
STATUS_ACTIVE = 4
_USABLE_STATUSES = (STATUS_SUCCESS, STATUS_ACTIVE)

# 官方 API 端点（v3 协议）
_DEFAULT_VOICE_CLONE_URL = "https://openspeech.bytedance.com/api/v3/tts/voice_clone"
_DEFAULT_GET_VOICE_URL = "https://openspeech.bytedance.com/api/v3/tts/get_voice"


class DoubaoICLClient:
    """豆包 ICL 2.0 声音复刻客户端（基于 v3 接口）。

    鉴权优先级：
      1. settings.DOUBAO_ICL_API_KEY（新版控制台 API Key，推荐）
      2. settings.DOUBAO_AK（与 TTS 共用 AK）
      3. PROVIDERS_CONFIG[id=doubao].api_key
      4. 环境变量 MEGACORE_ACCESS_KEY_FROM_ENV
    """

    name = "doubao_icl"

    @property
    def voice_clone_url(self) -> str:
        """创建训练任务端点。"""
        base = (settings.DOUBAO_ICL_BASE_URL or _DEFAULT_VOICE_CLONE_URL).rstrip("/")
        # 兼容历史配置：如果用户配置的是 /api/v1/voice_clone（旧的 create/query 自造端点），
        # 强制重写为 v3 标准端点
        if "voice_clone" in base and "/v3/" not in base and base.endswith("/voice_clone"):
            return _DEFAULT_VOICE_CLONE_URL
        return base

    @property
    def get_voice_url(self) -> str:
        """查询训练状态端点。"""
        # 与 voice_clone_url 同源；如果用户改了 BASE_URL 指向了别的协议，
        # 这里直接拼到同一 base 上
        base = self.voice_clone_url
        if base.endswith("/voice_clone"):
            return base[: -len("/voice_clone")] + "/get_voice"
        return _DEFAULT_GET_VOICE_URL

    # ---------------------------------------------------------------
    # 鉴权
    # ---------------------------------------------------------------
    def _resolve_api_key(self) -> str:
        """按优先级解析 API Key。"""
        # 1) 显式 ICL key
        ak = getattr(settings, "DOUBAO_ICL_API_KEY", None) or ""
        # 2) 与 TTS 共用
        if not ak:
            ak = getattr(settings, "DOUBAO_AK", None) or ""
        # 3) PROVIDERS_CONFIG 里的 doubao.api_key
        if not ak:
            try:
                from ....core.config import get_provider
                prov = get_provider("doubao")
                ak = (prov or {}).get("api_key") or ""
            except Exception:
                ak = ""
        # 4) 兜底 ENV
        if not ak:
            ak = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if not ak:
            raise RuntimeError(
                "未配置豆包凭据：请在「设置 → 模型厂商 → 火山引擎豆包语音」"
                "启用并填入 API Key 后保存，或设置 DOUBAO_ICL_API_KEY / DOUBAO_AK 环境变量。"
            )
        return ak.strip()

    def _auth_headers(self, *, prefix: str = "icl") -> dict[str, str]:
        """构造鉴权头。新版控制台：X-Api-Key；旧版控制台：X-Api-App-Key + X-Api-Access-Key。

        简单策略：默认按新版控制台发 X-Api-Key；如果 key 看起来像 APP_ID（纯数字），
        则按旧版鉴权（需要额外的 access_key）。

        Args:
            prefix: X-Api-Request-Id 前缀，方便日志按调用类型区分（create/get）。
        """
        ak = self._resolve_api_key()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Api-Request-Id": f"{prefix}-{uuid.uuid4().hex}",
        }
        # 兼容"Bearer;xxx" 旧写法（与 tts.py 一致的历史约定）
        ak_value = ak.split()[-1] if " " in ak else ak
        if ak_value.lstrip("-").isdigit():
            # 看起来是纯数字 APP_ID → 走旧版鉴权
            access_key = getattr(settings, "DOUBAO_ICL_ACCESS_KEY", None) or ak_value
            headers["X-Api-App-Key"] = ak_value
            headers["X-Api-Access-Key"] = str(access_key)
        else:
            headers["X-Api-Key"] = ak_value
        return headers

    # ---------------------------------------------------------------
    # 对外 API
    # ---------------------------------------------------------------
    async def create_training(
        self,
        voice_name: str,
        audio_bytes: bytes,
        *,
        audio_format: str = "mp3",
        demo_text: str | None = None,
        language: int = 0,
        model_type: str = "ICL2.0",
    ) -> str:
        """创建声音复刻训练任务。

        Args:
            voice_name: 业务方给的展示名（仅用于本地记录；speaker_id 由我们生成）
            audio_bytes: 参考音频（建议 6~30 秒清晰人声）
            audio_format: wav/mp3/ogg/m4a/aac（pcm 仅 24k）
            demo_text: 可选，按此文本校验 WER
            language: 0=cn / 1=en / 2=ja ...
            model_type: ICL2.0（推荐）/ ICL1.0 / DiT

        Returns:
            豆包侧 speaker_id（格式 "S_xxx"，合成时透传给 TTS 接口）

        Raises:
            ValueError: 音频过小
            RuntimeError: API 错误 / 业务码非 0 / 缺 speaker_id
        """
        if not audio_bytes or len(audio_bytes) < 512:
            raise ValueError("参考音频过小：请上传 3 秒以上（建议 6~10 秒）的清晰人声录音")

        # speaker_id 由我们生成（前端 / DB 唯一标识）—— 官方允许 8~256 字符、首字符英文、
        # 仅含数字字母-_，且不能匹配官方正则（不能是 S_/ICL_/MIX_/DiT_/BV 开头或
        # uranus/bigtts/tob 等结尾）。我们用 "icl_<uuid hex>" 形式。
        speaker_id = f"icl_{uuid.uuid4().hex}"

        payload: dict[str, Any] = {
            "speaker_id": speaker_id,
            "audio": {
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "format": audio_format.lower(),
            },
            "language": int(language),
            "model_type": model_type,
        }
        if demo_text:
            payload["demo_text"] = str(demo_text)[:300]
        # 业务方提供的 voice_name 用于日志/审计（不发给豆包）

        await _doubao_rpm_wait_acquire("icl")
        # 请求头：让 _auth_headers 内部用 create- 前缀生成 X-Api-Request-Id
        resp = await self._http_post_json(
            self.voice_clone_url, payload,
            _request_kind="create",
        )
        code = int(resp.get("code", -1))
        if code != 0:
            msg = resp.get("message") or "未知错误"
            raise RuntimeError(
                f"豆包 ICL 创建训练任务失败：code={code} msg={msg} resp={resp}"
            )

        data = resp.get("data") or {}
        sid = data.get("speaker_id") or resp.get("speaker_id") or speaker_id
        logger.info(
            f"[icl_client] create_training OK voice_name={voice_name} speaker_id={sid}"
        )
        return str(sid)

    async def query_training(self, speaker_id: str) -> dict[str, Any]:
        """查询训练状态。

        Returns:
            {
              "status": 0/1/2/3/4,
              "progress": 100（终态）或 0（其他），
              "cloned_voice_id": speaker_id,  # 即返回的可用 speaker_id
              "error": "<失败原因>" | None,
              "model_type": <int> | None,        # 来自 speaker_status[0]
              "demo_audio": <url> | None,        # 来自 speaker_status[0]
            }
        """
        if not speaker_id:
            raise ValueError("speaker_id 不能为空")

        payload: dict[str, Any] = {"speaker_id": str(speaker_id)}

        await _doubao_rpm_wait_acquire("icl")
        resp = await self._http_post_json(
            self.get_voice_url, payload,
            _request_kind="get",
        )

        code = int(resp.get("code", -1))
        # NotFound 不算错——返回 status=0 让上层决定如何处理
        if code not in (0,):
            msg = resp.get("message") or "未知错误"
            raise RuntimeError(
                f"豆包 ICL 查询训练状态失败：code={code} msg={msg} resp={resp}"
            )

        status = int(resp.get("status", STATUS_NOT_FOUND))
        # progress：官方没返回具体进度。终态（2/4）置 100，其他置 0
        progress = 100 if status in _USABLE_STATUSES else (
            50 if status == STATUS_TRAINING else 0
        )
        speaker_status_list = resp.get("speaker_status") or []
        first_ss = speaker_status_list[0] if speaker_status_list else {}

        return {
            "status": status,
            "progress": progress,
            "cloned_voice_id": str(speaker_id) if status in _USABLE_STATUSES else None,
            "error": str(resp.get("message") or "") if status == STATUS_FAILED else None,
            "model_type": first_ss.get("model_type"),
            "demo_audio": first_ss.get("demo_audio"),
        }

    # ---------------------------------------------------------------
    # HTTP 内部方法（测试可 monkeypatch）
    # ---------------------------------------------------------------
    async def _http_post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *args: Any,
        _request_kind: str = "icl",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """POST JSON 并解析响应（测试可 monkeypatch）。

        注意：保留 `_http_post_json(url, payload)` 的 2 参数形式以向后兼容现有测试
        （test_doubao_task5_red.py 里 T-IC1/T-IC2 直接把 client._http_post_json 替换成
        一个 `_fake_post(url, payload)` 函数）。`_request_kind` 仅用于 _auth_headers 内部
        决定 X-Api-Request-Id 前缀；测试 fake 函数一般会忽略 kwargs。
        """
        import json as _json

        import httpx

        # 测试 fake 通常签名为 _fake_post(url, payload)，不接受 kwargs——
        # 为了不破坏向后兼容，把 _request_kind 提取后再调用 fake。
        # 但 fake 直接绑定到 client._http_post_json，会拦截方法调用，
        # 所以 fake 必须自己处理额外参数；这里我们改用：把 _request_kind 传给
        # 真实 HTTP 路径（_auth_headers），fake 路径则完全忽略（fake 只看 url/payload）。
        headers = self._auth_headers(prefix=_request_kind)

        timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            # 透出 logid 便于排查
            logid = resp.headers.get("X-Tt-Logid") or resp.headers.get("x-tt-logid")
            if logid:
                logger.debug(f"[icl_client] logid={logid}")
            resp.raise_for_status()
            return _json.loads(resp.content.decode("utf-8"))
