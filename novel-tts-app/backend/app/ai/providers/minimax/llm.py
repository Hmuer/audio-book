import json
import asyncio
import logging
from pathlib import Path
from typing import Type, TypeVar, Optional, Any
import httpx
from pydantic import BaseModel, ValidationError

from ....core.config import settings
from ...base import BaseLLMProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class MiniMaxLLMProvider(BaseLLMProvider):
    name = "minimax"

    def __init__(self):
        self.api_key = settings.LLM_API_KEY
        self.base_url = settings.LLM_BASE_URL.rstrip("/")
        self.model_pro = settings.LLM_MODEL_PRO
        self.model_fast = settings.LLM_MODEL_FAST
        self.timeout = httpx.Timeout(
            connect=settings.LLM_TIMEOUT,
            read=settings.LLM_TIMEOUT,
            write=settings.LLM_TIMEOUT,
            pool=settings.LLM_TIMEOUT,
        )

    async def chat_structured(
        self,
        prompt: str,
        output_schema: Type[T],
        *,
        system_prompt: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 8000,
        use_fast_model: bool = False,
        max_retries: int = 3,
    ) -> T:
        model = self.model_fast if use_fast_model else self.model_pro
        sys_prompt = (
            system_prompt
            or "你是一个专业的中文 AI 助手，严格按照 JSON Schema 返回结构化结果，只输出 JSON，不要任何额外文字或 markdown。"
        )

        # 构造 JSON mode 的 output schema（draft-07 简易版）
        schema_dict = output_schema.model_json_schema()

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "temperature": min(temperature + (attempt - 1) * 0.1, 1.0),
                            "max_tokens": max_tokens,
                            "messages": [
                                {"role": "system", "content": sys_prompt},
                                {"role": "user", "content": prompt},
                            ],
                            "response_format": {
                                "type": "json_schema",
                                "json_schema": {
                                    "schema": schema_dict,
                                    "strict": False,
                                },
                            },
                        },
                    )
                    if resp.status_code >= 400:
                        raise RuntimeError(
                            f"LLM HTTP {resp.status_code}: {resp.text[:500]}"
                        )
                    data = resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    if not content:
                        raise ValueError(f"Empty LLM response: {data}")

                    # 有时 LLM 返回的是 ```json ... ```，去掉
                    stripped = content.strip()
                    if stripped.startswith("```"):
                        stripped = stripped.strip("`")
                        if stripped.lower().startswith("json"):
                            stripped = stripped[4:]
                        stripped = stripped.strip()

                    parsed = json.loads(stripped)
                    return output_schema.model_validate(parsed)

            except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError, httpx.HTTPError) as e:
                last_err = e
                logger.warning(
                    f"[MiniMaxLLM] attempt {attempt}/{max_retries} failed: {type(e).__name__}: {e}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * attempt)
        assert last_err is not None
        raise last_err
