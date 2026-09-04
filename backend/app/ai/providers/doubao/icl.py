"""豆包 ICL 2.0 声音复刻客户端（训练任务创建 + 状态查询）。

职责（可 mock：`_http_post_json` / `create_training` / `query_training`）：
  - create_training(voice_name, audio_bytes)：上传参考音频（base64）并创建训练任务，
    返回豆包侧 task_id。
  - query_training(doubao_task_id)：查询训练状态，返回
    {"status": int, "progress": int, "cloned_voice_id": str|None, "error": str|None}
    status 语义：0 排队 / 1 训练中 / 2 成功 / 3 失败 / 4 可用（2/4 均可合成）。
  - 所有调用走 factory._doubao_rpm_wait_acquire("icl") 统一限流。
"""
from __future__ import annotations

import base64
import logging
import os
import uuid
from typing import Any

from backend.app.ai.factory import _doubao_rpm_wait_acquire
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class DoubaoICLClient:
    name = "doubao_icl"

    @property
    def base_url(self) -> str:
        return (settings.DOUBAO_ICL_BASE_URL or "").rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        # 优先级：PROVIDERS_CONFIG[id=doubao].api_key → .env DOUBAO_AK → ENV 兜底
        ak = ""
        try:
            from ....core.config import get_provider
            prov = get_provider("doubao")
            ak = (prov or {}).get("api_key") or ""
        except Exception:
            ak = ""
        if not ak:
            ak = settings.DOUBAO_AK
        if not ak:
            ak = os.environ.get("MEGACORE_ACCESS_KEY_FROM_ENV") or ""
        if not ak:
            raise RuntimeError(
                "未配置豆包凭据：请在「设置 → 模型厂商 → 火山引擎豆包语音」"
                "启用并填入 API Key 后保存，或设置 DOUBAO_AK 环境变量。"
            )
        auth = ak if " " in ak else f"Bearer;{ak}"
        return {"Content-Type": "application/json", "Authorization": auth}

    async def create_training(
        self,
        voice_name: str,
        audio_bytes: bytes,
        *,
        audio_format: str = "mp3",
    ) -> str:
        """创建训练任务，返回豆包侧 task_id。"""
        if not audio_bytes or len(audio_bytes) < 512:
            raise ValueError("参考音频过小：请上传 3 秒以上（建议 6~10 秒）的清晰录音")
        payload: dict[str, Any] = {
            "voice_name": voice_name,
            "audio_format": audio_format.lower(),
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
            "language": "zh",
            "model": "iclv2",
            # reqid 必须每次唯一：单例 client 的 id(self) 恒定，
            # 若服务端按 reqid 幂等去重，第二次训练会拿到旧 task
            "reqid": f"icl-{uuid.uuid4().hex}",
        }
        await _doubao_rpm_wait_acquire("icl")
        resp = await self._http_post_json(f"{self.base_url}/create", payload)
        data = resp.get("data") or {}
        task_id = data.get("task_id") or resp.get("task_id")
        if not task_id:
            raise RuntimeError(f"豆包 ICL 创建训练任务失败：响应缺少 task_id：{resp}")
        return str(task_id)

    async def query_training(self, doubao_task_id: str) -> dict[str, Any]:
        """查询训练状态。返回统一结构（见类 docstring）。"""
        payload = {"task_id": doubao_task_id, "reqid": f"icl-q-{doubao_task_id}"}
        await _doubao_rpm_wait_acquire("icl")
        resp = await self._http_post_json(f"{self.base_url}/query", payload)
        data = resp.get("data") or resp
        status = int(data.get("status", 0))
        return {
            "status": status,
            "progress": int(data.get("progress", 0)),
            "cloned_voice_id": data.get("voice_id") or data.get("cloned_voice_id"),
            "error": data.get("error") or data.get("message"),
        }

    async def _http_post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON 并解析响应（测试可 monkeypatch）。"""
        import json as _json

        import httpx

        timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=self._auth_headers(), json=payload)
            resp.raise_for_status()
            return _json.loads(resp.content.decode("utf-8"))
