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


# 全局并发限流 semaphore：模块级单例，确保所有 LLM 调用（无论从哪发起）
# 都被强制串行/限流。默认 LLM_MAX_CONCURRENCY=1，避免按量套餐 RPM 触发 429。
def _build_llm_semaphore() -> asyncio.Semaphore:
    n = max(1, int(settings.LLM_MAX_CONCURRENCY))
    return asyncio.Semaphore(n)


_llm_sem: asyncio.Semaphore | None = None


def _get_llm_sem() -> asyncio.Semaphore:
    """惰性初始化 semaphore（在事件循环内创建，避免跨循环报错）。"""
    global _llm_sem
    if _llm_sem is None:
        _llm_sem = _build_llm_semaphore()
    return _llm_sem


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
            f"prompt_chars={prompt_chars} max_tokens={max_tokens} retries={max_retries} "
            f"concurrency={settings.LLM_MAX_CONCURRENCY}"
        )

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            t0 = _time.perf_counter()
            req_id: Optional[str] = None
            http_status: Optional[int] = None
            is_rate_limited = False
            try:
                # 关键：在 semaphore 内发请求，限流同时并发的 HTTP 调用数
                # 业务层 asyncio.gather 多个 LLM 调用时，这里会强制排队
                async with _get_llm_sem():
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
                                # 关闭 thinking：json_schema 模式下 thinking 会污染 content 导致 JSON 解析失败
                                "thinking": {"type": "disabled"},
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
                        # 429 速率限制：标记走指数退避路径
                        if resp.status_code == 429:
                            is_rate_limited = True
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
                    content = msg_obj.get("content", "") or ""
                    resp_chars = len(content)
                    if not content.strip():
                        raise ValueError(f"Empty LLM response: {data}")

                    import re as _re

                    # 1) 剥离 thinking / RichMediaReference 等非 JSON 内容块
                    #    MiniMax M2.x/M3 即便请求里关了 thinking，仍可能把"思考过程"
                    #    塞进 content：表现为 …、<thinking>…</thinking>
                    #    或 <RichMediaReference>…</RichMediaReference>（M2.7-highspeed
                    #    实测会把 prompt 复述包进 RichMediaReference），导致 JSON 解析失败。
                    #    统一成对剥离 + 残留孤立标签清理；支持大小写、跨多行、多次出现。
                    _LONE_TAG_RE = _re.compile(
                        r"</?(?:t(?:hink|hinking)|RichMediaReference)\b[^>]*>",
                        flags=_re.IGNORECASE,
                    )
                    # 先暴力把成对标签块整体去掉（含跨多行）
                    for _open, _close in (
                        (r"<think\b[^>]*>", r"</think\s*>"),
                        (r"<thinking\b[^>]*>", r"</thinking\s*>"),
                        (r"<RichMediaReference\b[^>]*>", r"</RichMediaReference\s*>"),
                    ):
                        _p = _re.compile(
                            f"{_open}.*?{_close}",
                            flags=_re.DOTALL | _re.IGNORECASE,
                        )
                        content = _p.sub("", content)
                    # 再清掉任何残留的孤立标签（比如缺右标签的脏响应）
                    content = _LONE_TAG_RE.sub("", content)
                    stripped = content.strip()

                    # 2) 若剥离后为空，尝试 reasoning_content 字段
                    if not stripped:
                        reasoning = msg_obj.get("reasoning_content")
                        if reasoning:
                            stripped = reasoning.strip()

                    if not stripped:
                        raise ValueError(f"LLM 响应仅含 thinking 无有效内容: {content[:500]}")

                    # 3) 去掉 markdown 代码块包裹
                    if stripped.startswith("```"):
                        stripped = stripped.strip("`")
                        if stripped.lower().startswith("json"):
                            stripped = stripped[4:]
                        stripped = stripped.strip()

                    def _extract_json_blob(s: str) -> str | None:
                        """用平衡花括号/方括号扫描，找第一个**合法可解析**的 JSON 对象或数组。
                        比起 r'\{.*\}' 这种贪婪匹配，能避免 thinking 残留里包含
                        单个 { 或 "xxx": "{" 这种导致的误匹配；同时会在多个平衡候选中
                        逐个尝试 json.loads，跳过那些括号平衡但内容非法（缺逗号、引号）的片段。
                        """
                        n = len(s)
                        candidates: list[tuple[int, int, str]] = []  # (start, end, first_char)
                        i = 0
                        while i < n:
                            ch = s[i]
                            if ch not in "[{":
                                i += 1
                                continue
                            open_ch = ch
                            close_ch = "]" if open_ch == "[" else "}"
                            depth = 0
                            in_str = False
                            escape_next = False
                            j = i
                            while j < n:
                                c = s[j]
                                if in_str:
                                    if escape_next:
                                        escape_next = False
                                    elif c == "\\":
                                        escape_next = True
                                    elif c == '"':
                                        in_str = False
                                else:
                                    if c == '"':
                                        in_str = True
                                    elif c == open_ch:
                                        depth += 1
                                    elif c == close_ch:
                                        depth -= 1
                                        if depth == 0:
                                            candidates.append((i, j, open_ch))
                                            break
                                j += 1
                            i += 1
                        if not candidates:
                            return None
                        # 起点升序，同起点按长度升序（越短越可能是完整 JSON）
                        candidates.sort(key=lambda t: (t[0], t[1] - t[0]))
                        # 逐个尝试，返回第一个真正能 parse 的 blob
                        for start, end, _ in candidates:
                            blob = s[start : end + 1]
                            try:
                                json.loads(blob)  # 仅验证可解析性
                                return blob
                            except (json.JSONDecodeError, ValueError):
                                continue
                        return None

                    # 4) 尝试解析 JSON
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        # 5) fallback：用平衡括号扫描提取真实 JSON 块
                        blob = _extract_json_blob(stripped)
                        if blob is None:
                            logger.error(
                                f"[LLM] JSON parse failed, raw content (first 1000 chars): {content[:1000]}"
                            )
                            raise
                        try:
                            parsed = json.loads(blob)
                        except json.JSONDecodeError:
                            logger.error(
                                f"[LLM] JSON parse failed after blob extraction, "
                                f"raw content (first 1000 chars): {content[:1000]}"
                            )
                            raise

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
                    if is_rate_limited:
                        # 429 速率限制：指数退避（2s / 4s / 8s ...）
                        # 比固定 1s 更稳，给供应商 RPM 窗口恢复时间
                        backoff = 2.0 * (2 ** (attempt - 1))
                        logger.warning(
                            f"[LLM] 429 rate-limited, backing off {backoff}s before retry "
                            f"(attempt={attempt}/{max_retries})"
                        )
                        await asyncio.sleep(backoff)
                    else:
                        # 其他错误：保持原有 1s/2s/3s 退避
                        await asyncio.sleep(1.0 * attempt)
        total_elapsed = _time.perf_counter() - total_start
        logger.error(
            f"[LLM] all {max_retries} attempts exhausted model={model} schema={schema_name} "
            f"prompt_chars={prompt_chars} total_ms={int(total_elapsed*1000)}"
        )
        assert last_err is not None
        raise last_err
