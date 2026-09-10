"""P2-2：豆包长文本异步 TTS 客户端。

对接官方 `/api/v3/tts/submit` + `/api/v3/tts/query` 接口（v3 HTTP 大模型版本）。
适用场景：单次文本 >3 万字符（同步流式 v3 unidirectional 在长文本下会 60s 超时）。

核心 API：
  submit(text, voice_id, *, audio_params, ...) -> task_id
  query(task_id) -> TaskStatus + audio_url (status==Success 时)

鉴权（双模式，与其他 v3 接口一致）：
  - 新版控制台：X-Api-Key + X-Api-Resource-Id
  - 旧版控制台：X-Api-App-Id + X-Api-Access-Key（纯数字 APP_ID 触发）
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from ....core.config import settings


logger = logging.getLogger(__name__)


# 官方接口常量（https://www.volcengine.com/docs/6561/1829010?lang=zh）
_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/tts/submit"
_QUERY_URL = "https://openspeech.bytedance.com/api/v3/tts/query"

# 长文本阈值：超过此长度走 async，否则同步 v3 unidirectional
LONG_TEXT_THRESHOLD_CHARS = 30000

# 默认轮询间隔 + 上限
_DEFAULT_POLL_INTERVAL_S = 5.0
_DEFAULT_POLL_MAX_TIMES = 360  # 360 * 5s = 30 分钟


class AsyncTaskStatus(str, Enum):
    """官方 query 返回的 task_status 枚举值。"""

    PROCESSING = "Processing"   # 0 合成中
    SUCCESS = "Success"         # 1 合成成功
    FAILED = "Failed"           # 2 合成失败


@dataclass
class AsyncTaskResult:
    """query 一次的返回结果。"""

    task_id: str
    status: AsyncTaskStatus
    audio_url: str | None = None
    audio_bytes: int | None = None  # 服务端返回的音频字节数（可作为健康度提示）
    error_code: int | None = None
    error_message: str | None = None


class DoubaoAsyncTTSClient:
    """豆包长文本异步 TTS 客户端（无状态 + 协程安全）。"""

    def __init__(
        self,
        api_key: str | None = None,
        access_key: str | None = None,
        app_id: str | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Args:
            api_key: 新版 X-Api-Key（与 settings.DOUBAO_AK 共用）
            access_key: 旧版 X-Api-Access-Key
            app_id: 旧版 X-Api-App-Id
            timeout_s: 单次 HTTP 超时（submit/query 都是短请求）
        """
        self.api_key = api_key or settings.DOUBAO_AK
        self.access_key = access_key or settings.DOUBAO_ICL_ACCESS_KEY or settings.DOUBAO_AK
        self.app_id = app_id or settings.DOUBAO_APP_ID
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------
    # 鉴权头
    # ------------------------------------------------------------------
    def _is_legacy(self) -> bool:
        """纯数字 APP_ID 即旧版控制台。"""
        return bool(self.app_id) and self.app_id.isdigit()

    def _headers(self, resource_id: str, req_id: str) -> dict[str, str]:
        """根据新版/旧版控制台返回对应的鉴权头。"""
        if self._is_legacy():
            return {
                "X-Api-App-Id": self.app_id,
                "X-Api-Access-Key": self.access_key,
                "X-Api-Resource-Id": resource_id,
                "X-Api-Request-Id": req_id,
                "Content-Type": "application/json",
            }
        # 新版
        return {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": req_id,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # submit
    # ------------------------------------------------------------------
    async def submit(
        self,
        text: str,
        voice_id: str,
        *,
        resource_id: str = "seed-tts-2.0",
        sample_rate: int = 24000,
        speech_rate: int = 0,
        loudness_rate: int = 0,
        emotion: str | None = None,
        context_texts: list[str] | None = None,
        enable_subtitle: bool = False,
    ) -> str:
        """提交长文本任务，返回 task_id。

        Args:
            text: 待合成文本（最长 10万字符）
            voice_id: 音色 ID（不含 "doubao:" 前缀）
            resource_id: 模型版本（默认 seed-tts-2.0；ICL 复刻请传 seed-icl-2.0）
            sample_rate: 8000 / 16000 / 24000 / ...
            speech_rate: -50~100
            loudness_rate: -50~100
            emotion: happy/sad/...（可选；非空 + 音色 supports_emotion 时才下发）
            context_texts: 指令文本（context_texts，v3 长文本同步接口也支持）
            enable_subtitle: 是否返回字级别时间戳（仅中英文）

        Returns:
            task_id（用于 query）

        Raises:
            RuntimeError: 鉴权/网络/业务错
        """
        if not text or not text.strip():
            raise RuntimeError("submit: text 不能为空")
        if len(text) > 100_000:
            raise RuntimeError(f"submit: 文本超过 10万字符上限（{len(text)} 字符）")

        # audio_params
        audio_params: dict[str, Any] = {
            "format": "mp3",
            "sample_rate": int(sample_rate),
            "speech_rate": int(speech_rate),
            "loudness_rate": int(loudness_rate),
            "enable_subtitle": bool(enable_subtitle),
        }
        if emotion:
            audio_params["emotion"] = emotion

        # req_params
        req_params: dict[str, Any] = {
            "text": text,
            "speaker": voice_id,
            "audio_params": audio_params,
        }
        if context_texts:
            req_params["context_texts"] = list(context_texts)

        # 嵌套结构 + user
        payload = {
            "user": {"uid": f"local-{os.getpid() % 10000:04d}"},
            "req_params": req_params,
        }

        req_id = f"async-submit-{int(time.time() * 1000)}"
        headers = self._headers(resource_id, req_id)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(_SUBMIT_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # 响应结构示例（成功）：
        #   { "task_id": "bd0c2171-...", "task_status": 0, "text_length": 12345 }
        # 错误：
        #   { "reqid": "...", "code": 40000, "message": "请求参数错误：..." }
        if "task_id" not in data:
            raise RuntimeError(
                f"submit: 响应缺 task_id → code={data.get('code')} message={data.get('message')}"
            )
        task_id = str(data["task_id"])
        logger.info(
            f"[DoubaoAsyncTTS] submit OK task_id={task_id} text_len={len(text)} "
            f"voice={voice_id} resource={resource_id}"
        )
        return task_id

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------
    async def query(self, task_id: str, *, resource_id: str = "seed-tts-2.0") -> AsyncTaskResult:
        """查询任务状态（单次）。返回结构化结果。"""
        if not task_id:
            raise RuntimeError("query: task_id 不能为空")

        req_id = f"async-query-{int(time.time() * 1000)}"
        headers = self._headers(resource_id, req_id)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                _QUERY_URL,
                json={"task_id": task_id},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        # 响应示例：
        #   { "task_id": "...", "task_status": 1, "audio_url": "https://..." }
        #   { "task_id": "...", "task_status": 2, "error_code": 50000, "error_message": "..." }
        status_code = int(data.get("task_status", -1))
        # 0=processing, 1=success, 2=failed
        if status_code == 1:
            return AsyncTaskResult(
                task_id=task_id,
                status=AsyncTaskStatus.SUCCESS,
                audio_url=data.get("audio_url"),
                audio_bytes=int(data["audio_bytes"]) if data.get("audio_bytes") else None,
            )
        if status_code == 2:
            return AsyncTaskResult(
                task_id=task_id,
                status=AsyncTaskStatus.FAILED,
                error_code=int(data.get("error_code", 0)) or None,
                error_message=data.get("error_message") or data.get("message"),
            )
        return AsyncTaskResult(
            task_id=task_id,
            status=AsyncTaskStatus.PROCESSING,
        )

    # ------------------------------------------------------------------
    # 高层：submit + 轮询 query + 下载 audio_url
    # ------------------------------------------------------------------
    async def wait_for_result(
        self,
        task_id: str,
        *,
        resource_id: str = "seed-tts-2.0",
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        poll_max_times: int = _DEFAULT_POLL_MAX_TIMES,
        on_progress: Any | None = None,  # 回调：async def on_progress(elapsed_s: float) -> None
    ) -> AsyncTaskResult:
        """轮询 query 直到任务完成（success / failed / 超时）。

        Args:
            task_id: submit 返回的 id
            resource_id: 与 submit 一致
            poll_interval_s: 两次 query 间隔
            poll_max_times: 最大轮询次数（默认 360 * 5s = 30 min）
            on_progress: 可选进度回调（async fn）

        Returns:
            AsyncTaskResult（status 必为 SUCCESS 或 FAILED，否则超时）

        Raises:
            asyncio.TimeoutError: 超过 poll_max_times
        """
        for i in range(poll_max_times):
            r = await self.query(task_id, resource_id=resource_id)
            if r.status != AsyncTaskStatus.PROCESSING:
                logger.info(
                    f"[DoubaoAsyncTTS] query 终态 task_id={task_id} status={r.status.value}"
                )
                return r
            await asyncio.sleep(poll_interval_s)
            if on_progress is not None:
                elapsed = (i + 1) * poll_interval_s
                try:
                    await on_progress(elapsed)
                except Exception:  # 进度回调不应阻塞
                    logger.exception("on_progress 回调异常")

        raise asyncio.TimeoutError(
            f"async TTS 任务 {task_id} 在 {poll_max_times * poll_interval_s:.0f}s 内未完成"
        )

    async def download_audio(self, url: str) -> bytes:
        """下载官方 audio_url 的音频 bytes（合成音频 7 天有效，URL 1 小时有效）。"""
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    # ------------------------------------------------------------------
    # 顶层便捷：submit + wait + download
    # ------------------------------------------------------------------
    async def synthesize_long_text(
        self,
        text: str,
        voice_id: str,
        **kwargs: Any,
    ) -> tuple[bytes, str]:
        """submit + 轮询 + 下载一次搞定。返回 (audio_bytes, audio_url)。"""
        # 透传 kwargs（resource_id / sample_rate / emotion / ...）
        resource_id = kwargs.pop("resource_id", "seed-tts-2.0")
        task_id = await self.submit(text, voice_id, resource_id=resource_id, **kwargs)
        result = await self.wait_for_result(task_id, resource_id=resource_id)
        if result.status != AsyncTaskStatus.SUCCESS or not result.audio_url:
            raise RuntimeError(
                f"async TTS 任务失败 task_id={task_id} "
                f"code={result.error_code} message={result.error_message}"
            )
        audio_bytes = await self.download_audio(result.audio_url)
        return audio_bytes, result.audio_url


# ----------------------------------------------------------------------
# 顶层 helper（单例 + 同步 v3 自动切换）
# ----------------------------------------------------------------------
_default_client: DoubaoAsyncTTSClient | None = None


def get_async_tts_client() -> DoubaoAsyncTTSClient:
    """获取默认异步 TTS 客户端（懒加载单例）。"""
    global _default_client
    if _default_client is None:
        _default_client = DoubaoAsyncTTSClient()
    return _default_client


def should_use_async_tts(text: str) -> bool:
    """P2-2：是否需要切到 async TTS？默认阈值 3 万字符。"""
    return len(text or "") >= LONG_TEXT_THRESHOLD_CHARS
