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

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model_pro: str | None = None, model_fast: str | None = None,
                 extra_headers: dict[str, str] | None = None):
        # 优先取多厂商配置；不再需要 fast/pro 双模型区分（用户要求合并为一个激活模型）
        from ....core.config import get_active_llm_provider
        prov = get_active_llm_provider()
        if prov and prov.get("id") == "minimax" and prov.get("api_key"):
            self.api_key = api_key or prov["api_key"]
            self.base_url = (base_url or prov.get("base_url") or settings.LLM_BASE_URL).rstrip("/")
            # 单一激活模型：fast/pro 共用
            single = settings.ACTIVE_LLM_MODEL or "MiniMax-M3"
            self.model_pro = single
            self.model_fast = single
            self.extra_headers = dict(prov.get("extra_headers") or {})
        else:
            self.api_key = api_key or settings.LLM_API_KEY
            self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
            # 兜底：.env LLM_MODEL_PRO / LLM_MODEL_FAST；用户已确认 fast/pro 合并
            single = settings.ACTIVE_LLM_MODEL or settings.LLM_MODEL_PRO or "MiniMax-M3"
            self.model_pro = single
            self.model_fast = single
            self.extra_headers = dict(extra_headers or {})
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
        # schema 是否期望"对象"顶层：决定 JSON 兜底提取时优先选 { } 而不是 [ ]
        expect_object = schema_dict.get("type") == "object"
        required_keys = tuple(schema_dict.get("required") or ())
        prompt_chars = len(prompt) + len(sys_prompt)

        total_start = _time.perf_counter()
        logger.info(
            f"[LLM] start model={model} schema={schema_name} "
            f"prompt_chars={prompt_chars} max_tokens={max_tokens} retries={max_retries} "
            f"concurrency={settings.LLM_MAX_CONCURRENCY}"
        )

        last_err: Optional[Exception] = None
        # thinking 循环检测：M2.x 的 thinking 可能陷入重复循环耗尽 max_tokens，
        # 导致 content 为空。检测到后重试时切换到 M3（可真正关闭 thinking）。
        thinking_loop_detected = False
        # 重试时的"格式纠正"提示：schema 校验失败 ≠ 模型不会做题，而是输出形状错了
        # （实测 M3 会把内层数组当整个答案返回）。只在原 prompt 后追加说明，不改动原 prompt。
        attempt_hint = ""
        for attempt in range(1, max_retries + 1):
            t0 = _time.perf_counter()
            req_id: Optional[str] = None
            http_status: Optional[int] = None
            is_rate_limited = False
            # 失败诊断用（FAIL 日志需要看到"响应是否被截断/token 用量/原始片段"）
            finish_reason: Optional[str] = None
            completion_tokens: Optional[int] = None
            resp_chars = 0
            fail_preview = ""
            # 检测到 thinking 循环时，重试切换到 M3（可真正关闭 thinking，不会循环）
            use_model = self.model_pro if thinking_loop_detected else model
            use_reasoning_split = not thinking_loop_detected
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
                                "model": use_model,
                                "temperature": min(temperature + (attempt - 1) * 0.1, 1.0),
                                "max_tokens": max_tokens,
                                "messages": [
                                    {"role": "system", "content": sys_prompt},
                                    {"role": "user", "content": prompt + attempt_hint},
                                ],
                                "response_format": {
                                    "type": "json_schema",
                                    "json_schema": {
                                        "name": schema_name,
                                        "schema": schema_dict,
                                        "strict": False,
                                    },
                                },
                                # thinking 控制（参考官方文档 thinking 控制章节）：
                                # - M3: thinking 默认 adaptive 开启，传 disabled 可关闭
                                # - M2.x: 官方明确说明 thinking 无法关闭，disabled 不生效
                                # 对 M2.x 用 reasoning_split=true：把 thinking 拆分到
                                # reasoning_content，让 content 保持干净 JSON。
                                # 检测到 thinking 循环时切换到 M3 + 关闭 reasoning_split，
                                # M3 可真正关 thinking，不会循环。
                                "thinking": {"type": "disabled"},
                                "reasoning_split": use_reasoning_split,
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
                    finish_reason = data.get("choices", [{}])[0].get("finish_reason")
                    raw_content = msg_obj.get("content", "") or ""
                    content = raw_content
                    resp_chars = len(content)
                    fail_preview = content[:200]
                    if not content.strip():
                        # thinking 循环检测：finish_reason='length' + content 空 +
                        # reasoning_content 有内容 → M2.x thinking 陷入重复循环耗尽 token
                        # 标记后重试切换到 M3（可真正关闭 thinking）
                        reasoning_present = bool(msg_obj.get("reasoning_content"))
                        if finish_reason == "length" and reasoning_present:
                            thinking_loop_detected = True
                            raise ValueError(
                                f"LLM thinking 陷入循环耗尽 token "
                                f"(finish_reason={finish_reason} reasoning_chars={len(msg_obj.get('reasoning_content', ''))}): "
                                f"重试将切换到 {self.model_pro}"
                            )
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
                        # 诊断盲点：打印原始 content（剥离前），便于看 LLM 实际返回了什么
                        raise ValueError(
                            f"LLM 响应仅含 thinking 无有效内容 "
                            f"(resp_chars={resp_chars} reasoning_present={bool(msg_obj.get('reasoning_content'))}): "
                            f"{raw_content[:500]}"
                        )

                    # 3) 去掉 markdown 代码块包裹
                    if stripped.startswith("```"):
                        stripped = stripped.strip("`")
                        if stripped.lower().startswith("json"):
                            stripped = stripped[4:]
                        stripped = stripped.strip()

                    def _extract_json_blob(
                        s: str,
                        *,
                        prefer_object: bool = False,
                        required_keys: tuple[str, ...] = (),
                    ) -> str | None:
                        r"""用平衡花括号/方括号扫描，找第一个**合法可解析**的 JSON 对象或数组。
                        比起 r'\{.*\}' 这种贪婪匹配，能避免 thinking 残留里包含
                        单个 { 或 "xxx": "{" 这种导致的误匹配；同时会在多个平衡候选中
                        逐个尝试 json.loads，跳过那些括号平衡但内容非法（缺逗号、引号）的片段。

                        prefer_object=True（schema 期望 dict）时改变候选优先级：
                          1) 含全部 required_keys 的对象
                          2) 任意对象
                          3) 兜底：数组 / 其它
                        动机：模型偶尔把某个**内层数组**当成整个答案返回；或响应不完整时
                        外层对象括号不平衡、扫描只能捞到内层数组。此时按"起点最早"返回会把
                        数组交给 model_validate，报出误导性的 Pydantic 校验错（看起来像代码问题，
                        实际是响应形状问题）。优先取对象可直接救回这类响应。
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
                        # 逐个尝试 json.loads，保留"可解析"的候选（顺序不变）
                        parsed_cands: list[tuple[int, int, str, Any]] = []
                        for start, end, _ in candidates:
                            blob = s[start : end + 1]
                            try:
                                obj = json.loads(blob)  # 仅验证可解析性
                            except (json.JSONDecodeError, ValueError):
                                continue
                            parsed_cands.append((start, end, blob, obj))
                        if not parsed_cands:
                            return None
                        if prefer_object:
                            objs = [c for c in parsed_cands if isinstance(c[3], dict)]
                            if required_keys:
                                full = [
                                    c for c in objs
                                    if all(k in c[3] for k in required_keys)
                                ]
                                if full:
                                    return full[0][2]
                            if objs:
                                return objs[0][2]
                        return parsed_cands[0][2]

                    # 4) 尝试解析 JSON
                    parsed: Any = None
                    parse_err: Exception | None = None
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError as _je:
                        parse_err = _je

                    # 4b) schema 期望对象、但拿到的是数组/标量（模型把内层数组当答案，
                    #     或响应不完整后被兜底捞到内层数组）→ 重新扫描，优先取"含全部
                    #     必填字段的对象"，避免把形状错误拖到 model_validate 才暴露。
                    if expect_object and not isinstance(parsed, dict):
                        _blob = _extract_json_blob(
                            stripped,
                            prefer_object=True,
                            required_keys=required_keys,
                        )
                        if _blob is not None:
                            try:
                                _cand = json.loads(_blob)
                            except json.JSONDecodeError:
                                _cand = None
                            if isinstance(_cand, dict):
                                parsed = _cand
                                parse_err = None

                    # 5) fallback：用平衡括号扫描提取真实 JSON 块
                    if parsed is None:
                        blob = _extract_json_blob(stripped)
                        if blob is None:
                            logger.error(
                                f"[LLM] JSON parse failed, raw content (first 1000 chars): {raw_content[:1000]}"
                            )
                            if parse_err is not None:
                                raise parse_err
                            raise ValueError(
                                f"LLM 响应中未找到可解析的 JSON: {stripped[:300]}"
                            )
                        try:
                            parsed = json.loads(blob)
                        except json.JSONDecodeError:
                            logger.error(
                                f"[LLM] JSON parse failed after blob extraction, "
                                f"raw content (first 1000 chars): {raw_content[:1000]}"
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
                        f"[LLM] ok model={use_model} schema={schema_name} attempt={attempt}/{max_retries} "
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
                    f"[LLM] FAIL model={use_model} schema={schema_name} attempt={attempt}/{max_retries} "
                    f"req_id={req_id} status={http_status} this_ms={int(elapsed*1000)} "
                    f"finish_reason={finish_reason} tok_c={completion_tokens} resp_chars={resp_chars} "
                    f"{type(e).__name__}: {e}"
                )
                logger.log(lvl, msg, exc_info=is_last)
                if attempt < max_retries:
                    # 形状类失败（不是模型不会做题，而是输出形状不对）→ 下次重试追加纠偏说明，
                    # 而不是只把 temperature +0.1 后原样重发（那是碰运气）。
                    if isinstance(e, ValidationError):
                        attempt_hint = (
                            "\n\n【格式纠正】上一次的返回不符合要求：它必须是**一个**以 { 开始、"
                            f"以 }} 结束的完整 JSON 对象（schema={schema_name}），必须包含字段："
                            f"{', '.join(required_keys) or '（见 schema）'}。"
                            "不要返回数组，不要只返回某个字段的内容，"
                            "不要输出解释文字、markdown 或代码块。"
                        )
                    elif isinstance(e, json.JSONDecodeError):
                        attempt_hint = (
                            "\n\n【格式纠正】上一次的返回不是合法 JSON。请只输出**一个**完整 JSON "
                            f"对象（以 {{ 开始、以 }} 结束），必须包含字段："
                            f"{', '.join(required_keys) or '（见 schema）'}，"
                            "不要输出解释文字、markdown 或代码块。"
                        )
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
            f"[LLM] all {max_retries} attempts exhausted model={use_model} schema={schema_name} "
            f"prompt_chars={prompt_chars} total_ms={int(total_elapsed*1000)}"
        )
        assert last_err is not None
        raise last_err
