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
        import time as _time
        model = self.model_fast if use_fast_model else self.model_pro
        sys_prompt = (
            system_prompt
            or "你是一个专业的中文 AI 助手，严格按照 JSON Schema 返回结构化结果，只输出 JSON，不要任何额外文字或 markdown。"
        )
        schema_name = output_schema.__name__
        schema_dict = output_schema.model_json_schema()
        prompt_chars = len(prompt) + len(sys_prompt)

        total_start = _time.perf_counter()
        logger.info(
            f"[LLM] start model={model} schema={schema_name} "
            f"prompt_chars={prompt_chars} max_tokens={max_tokens} retries={max_retries}"
        )

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            t0 = _time.perf_counter()
            req_id: Optional[str] = None
            http_status: Optional[int] = None
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
                                    "name": schema_name,
                                    "schema": schema_dict,
                                    "strict": False,
                                },
                            },
                        },
                    )
                    http_status = resp.status_code
                    req_id = (
                        resp.headers.get("x-request-id")
                        or resp.headers.get("X-Request-Id")
                        or None
                    )
                    body_preview = resp.text[:500]
                    if resp.status_code >= 400:
                        # 尝试从 body 里提取供应商 request_id
                        try:
                            err_data = resp.json()
                            req_id = err_data.get("request_id") or req_id
                            err_msg = err_data.get("error", {}).get("message") or body_preview
                        except Exception:
                            err_msg = body_preview
                        raise RuntimeError(
                            f"LLM HTTP {resp.status_code}: {err_msg}"
                        )
                    data = resp.json()
                    # 同样从响应体提取 request_id（优先级更高）
                    req_id = data.get("request_id") or req_id
                    usage = data.get("usage") or {}
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")
                    total_tokens = usage.get("total_tokens")
                    msg_obj = data.get("choices", [{}])[0].get("message", {})
                    # MiniMax 默认在 content 中嵌入 <think>...</think> 思考内容，需要剥离
                    content = msg_obj.get("content", "") or ""
                    resp_chars = len(content)
                    if not content.strip():
                        raise ValueError(f"Empty LLM response: {data}")

                    # 1) 剥离 <think> 标签（MiniMax/M3/M2.x 系列默认 thinking）
                    import re as _re
                    stripped = _re.sub(
                        r"<think>.*?</think>",
                        "",
                        content,
                        flags=_re.DOTALL,
                    ).strip()
                    # 若 reasoning_content 字段存在且 content 为纯 thinking 时，回退
                    if not stripped:
                        reasoning = msg_obj.get("reasoning_content")
                        if reasoning:
                            stripped = reasoning.strip()

                    if not stripped:
                        raise ValueError(f"LLM 响应仅含 thinking 无有效内容: {content[:500]}")

                    # 2) 有时 LLM 额外包裹 ```json ... ```，去掉
                    if stripped.startswith("```"):
                        stripped = stripped.strip("`")
                        if stripped.lower().startswith("json"):
                            stripped = stripped[4:]
                        stripped = stripped.strip()

                    parsed = json.loads(stripped)
                    validated = output_schema.model_validate(parsed)
                    elapsed = _time.perf_counter() - t0
                    total_elapsed = _time.perf_counter() - total_start
                    tok_str = (
                        f"tok_p={prompt_tokens} tok_c={completion_tokens} tok_t={total_tokens}"
                        if total_tokens is not None else "tok=N/A"
                    )
                    logger.info(
                        f"[LLM] ok model={model} schema={schema_name} attempt={attempt}/{max_retries} "
                        f"req_id={req_id} status={http_status} {tok_str} "
                        f"resp_chars={resp_chars} this_ms={int(elapsed*1000)} total_ms={int(total_elapsed*1000)}"
                    )
                    return validated

            except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError, httpx.HTTPError) as e:
                last_err = e
                elapsed = _time.perf_counter() - t0
                is_last = attempt == max_retries
                lvl = logging.ERROR if is_last else logging.WARNING
                msg = (
                    f"[LLM] FAIL model={model} schema={schema_name} attempt={attempt}/{max_retries} "
                    f"req_id={req_id} status={http_status} this_ms={int(elapsed*1000)} "
                    f"{type(e).__name__}: {e}"
                )
                logger.log(lvl, msg, exc_info=is_last)
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * attempt)
        total_elapsed = _time.perf_counter() - total_start
        logger.error(
            f"[LLM] all {max_retries} attempts exhausted model={model} schema={schema_name} "
            f"prompt_chars={prompt_chars} total_ms={int(total_elapsed*1000)}"
        )
        assert last_err is not None
        raise last_err
