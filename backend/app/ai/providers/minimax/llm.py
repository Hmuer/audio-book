import json
import asyncio
import logging
import time
from pathlib import Path
from typing import Type, TypeVar, Optional, Any
import httpx
from pydantic import BaseModel, ValidationError

from ....core.config import settings
from ...base import BaseLLMProvider, LLMQuotaExhaustedError, LLMContentRejectedError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# 全局并发限流 semaphore：模块级单例，确保所有 LLM 调用（无论从哪发起）
# 都被强制串行/限流。默认 LLM_MAX_CONCURRENCY=1，避免按量套餐 RPM 触发 429。
def _build_llm_semaphore() -> asyncio.Semaphore:
    n = max(1, int(settings.LLM_MAX_CONCURRENCY))
    return asyncio.Semaphore(n)


_llm_sem: asyncio.Semaphore | None = None
# H-1：记录创建 sem 时的并发值；与当前 settings 不一致则重建（热更新）
_llm_sem_value: int = 0


# ---------------------------------------------------------------------
# H-22 402 配额熔断：MiniMax Token Plan 等按周期计费套餐额度用完时，
# 窗口内（约 5h）所有调用必然 402，重试只会刷日志。首个 402 打开熔断，
# _quota_blocked_until 之前的所有调用**不发 HTTP、瞬间失败、不打日志**
# （快速失败静默：熔断打开事件本身已记过一次 WARNING）；到期后放一次
# 真实调用探测，配额已重置则自动恢复，仍未重置则重新熔断（再记一行）。
# 业务层（prepare）捕获 LLMQuotaExhaustedError 后中止流水线并保住 checkpoint。
# ---------------------------------------------------------------------
_quota_blocked_until: float = 0.0


def reset_quota_breaker() -> None:
    """手动/测试用：清掉 402 熔断状态（下一次调用直接发真实 HTTP）。"""
    global _quota_blocked_until
    _quota_blocked_until = 0.0


def _quota_breaker_secs() -> int:
    return max(1, int(getattr(settings, "LLM_QUOTA_RETRY_SECS", 300) or 300))


def _open_quota_breaker() -> None:
    global _quota_blocked_until
    _quota_blocked_until = time.monotonic() + _quota_breaker_secs()


def _get_llm_sem() -> asyncio.Semaphore:
    """惰性初始化 semaphore（在事件循环内创建，避免跨循环报错）。

    H-1 热更新：settings.LLM_MAX_CONCURRENCY 变化时立即重建 —— 用户在设置页
    调高/调低并发**不需要重启后端**。旧 sem 由在飞请求的持有者自然释放（不中断
    在飞请求）；新请求立即按新并发度限流。
    """
    global _llm_sem, _llm_sem_value
    n = max(1, int(settings.LLM_MAX_CONCURRENCY))
    if _llm_sem is None or _llm_sem_value != n:
        _llm_sem = asyncio.Semaphore(n)
        _llm_sem_value = n
    return _llm_sem


# =====================================================================
# H-24 响应抢救（salvage）：partial > total loss
#
# 实测（app.log 2026-10-09，383 章剑来）：模型大批量结构化输出时
# 「整体校验失败 → 整批作废 → 全量重试 3×」是最大的 token/时间浪费源——
#   - 对白批 200k 字符 prompt × 3 服务重试 × 3 provider 重试，606~1188 条
#     校验错（部分 dialogues 缺 anchor）→ 14 章全丢；ReadTimeout 600s × 3
#   - 润色 diff 频繁返回裸数组/键名偏差（`data: Field required`）→ 30+ 次重试
#   - 语音指令批撞 8k token 上限被截断 → JSON 不可解析 → 整批丢
# 抢救三层（只对『唯一 list[BaseModel] 字段』的包裹 schema 生效，全部
# 主业务 schema 均是该形状——_ListWrapper / _Wrapper / PolishDiffResult /
# DialogueBatchResponse / VoiceInstructionBatchResponse / 音色推荐 _Wrapper）：
#   S1 形状归一化（零损失）：裸数组包 {field:[...]} / 键名不对重命名 / 
#      field 是 dict 且只含一个 list → 解包
#   S2 逐条丢弃：合法条目留下、非法条目丢弃（嵌套 list 字段递归，如
#      ChapterDialogueBatchResult.dialogues）；保留 ≥50% 才算成功（防把
#      纯垃圾响应当成功吞掉本该重试的机会）
#   S3 不可解析兜底：整体 JSON 解析失败（截断/夹杂垃圾）时，把响应里
#      所有括号平衡的片段逐个按 item 校验，能过的凑成响应（截断的尾巴
#      条目天然不是平衡片段、自动丢弃）
# 抢救成功一律打一行 WARNING（schema/kept/dropped），失败照旧走重试。
# =====================================================================

def _scan_balanced_blobs(s: str) -> list[tuple[int, int, str]]:
    """扫描 s 中所有「括号平衡」的 JSON 片段（对象或数组），返回 (start, end, blob)。

    与贪婪正则的本质区别：字符串里的转义引号 / 孤立花括号不会误配；
    截断响应的最后一个不完整条目因括号不平衡天然不会入选。
    """
    n = len(s)
    out: list[tuple[int, int, str]] = []
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
                        out.append((i, j, s[i : j + 1]))
                        break
            j += 1
        i += 1
    return out


def _extract_json_blob(
    s: str,
    *,
    prefer_object: bool = False,
    required_keys: tuple[str, ...] = (),
) -> str | None:
    """从噪声文本里提取第一个**可解析**的平衡 JSON 片段（原 H-12 逻辑）。

    prefer_object=True（schema 期望 dict）时优先含全部 required_keys 的对象。
    """
    candidates = _scan_balanced_blobs(s)
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1] - t[0]))
    parsed_cands: list[tuple[int, int, str, Any]] = []
    for start, end, blob in candidates:
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        parsed_cands.append((start, end, blob, obj))
    if not parsed_cands:
        return None
    if prefer_object:
        objs = [c for c in parsed_cands if isinstance(c[3], dict)]
        if required_keys:
            full = [c for c in objs if all(k in c[3] for k in required_keys)]
            if full:
                return full[0][2]
        if objs:
            return objs[0][2]
    return parsed_cands[0][2]


def _single_list_field(schema_cls: type) -> tuple[str, type] | None:
    """schema 是『唯一一个 list[BaseModel] 字段』的包裹 schema（含 Optional）→
    (字段名, item 类)；其余形状（多字段 / 非 list / 标量 list）不适用抢救。"""
    from typing import get_origin, get_args, Union
    hits: list[tuple[str, type]] = []
    for fname, fi in schema_cls.model_fields.items():
        ann = fi.annotation
        # Optional[list[X]] → 取 list[X]
        if get_origin(ann) is Union:
            inner = [a for a in get_args(ann) if get_origin(a) is list]
            ann = inner[0] if len(inner) == 1 else ann
        if get_origin(ann) is list:
            args = get_args(ann)
            if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
                hits.append((fname, args[0]))
    return hits[0] if len(hits) == 1 else None


def _validate_item_lenient(item_cls: type, item: dict) -> Any | None:
    """单条校验；失败时若 item 自身也是『单 list 字段』schema（如对白批里
    某章的 dialogues），先对子列表逐条抢救再重试整条。返回 model 或 None。"""
    try:
        return item_cls.model_validate(item)
    except ValidationError:
        pass
    fld = _single_list_field(item_cls)
    if not fld:
        return None
    field_name, sub_cls = fld
    raw = item.get(field_name)
    if not isinstance(raw, list) or not raw:
        return None
    kept: list[Any] = []
    for x in raw:
        if isinstance(x, dict):
            m = _validate_item_lenient(sub_cls, x)
            if m is not None:
                kept.append(m.model_dump())
    # 多数子条目合法才救这条（1/40 合法 ≈ 垃圾响应，交给上层重试）
    if not kept or len(kept) * 2 < len(raw):
        return None
    try:
        return item_cls.model_validate({**item, field_name: kept})
    except ValidationError:
        return None


def _validate_or_salvage(
    schema_cls: type, parsed: Any
) -> tuple[Any, int, int, bool]:
    """校验 + 抢救。返回 (model, kept_n, dropped_n, shape_fixed)。

    - 正常校验通过：(model, 0, 0, False)
    - S1 形状归一化后通过：(model, 0, 0, True) —— 零数据损失
    - S2 逐条丢弃后通过：(model, kept, dropped, ...) —— 部分数据损失
    - 全部失败：抛出原 ValidationError（照旧走重试）
    """
    try:
        return schema_cls.model_validate(parsed), 0, 0, False
    except ValidationError as outer:
        fld = _single_list_field(schema_cls)
        if not fld:
            raise
        field_name, item_cls = fld
        # ---- S1 形状归一化（裸数组 / 键名不对 / dict 包 list）----
        parsed2 = parsed
        if isinstance(parsed, list):
            parsed2 = {field_name: parsed}
        elif isinstance(parsed, dict):
            if field_name not in parsed:
                lists = {k: v for k, v in parsed.items() if isinstance(v, list)}
                if len(lists) == 1:
                    parsed2 = {field_name: next(iter(lists.values()))}
            elif isinstance(parsed.get(field_name), dict):
                inner_lists = [
                    v for v in parsed[field_name].values() if isinstance(v, list)
                ]
                if len(inner_lists) == 1:
                    parsed2 = {field_name: inner_lists[0]}
        if parsed2 is not parsed:
            try:
                return schema_cls.model_validate(parsed2), 0, 0, True
            except ValidationError:
                pass
        # ---- S2 逐条丢弃（嵌套递归）----
        items = parsed2.get(field_name) if isinstance(parsed2, dict) else None
        if not isinstance(items, list) or not items:
            raise outer
        kept: list[Any] = []
        dropped = 0
        for it in items:
            if isinstance(it, dict):
                m = _validate_item_lenient(item_cls, it)
                if m is not None:
                    kept.append(m.model_dump())
                    continue
            dropped += 1
        # 保留 ≥50% 才算抢救成功
        if kept and len(kept) * 2 >= len(items):
            model = schema_cls.model_validate({**parsed2, field_name: kept})
            return model, len(kept), dropped, parsed2 is not parsed
        raise outer


def _salvage_from_unparseable(
    schema_cls: type, s: str
) -> tuple[Any, int] | None:
    """S3：整体 JSON 解析失败（截断 / 夹杂垃圾）时的兜底抢救。

    把响应里所有括号平衡的片段逐个按 item 校验，能过的凑成一个合法响应。
    截断的尾巴条目括号不平衡，天然不会入选（自动丢弃）。
    返回 (model, kept_n) 或 None。
    """
    fld = _single_list_field(schema_cls)
    if not fld:
        return None
    field_name, item_cls = fld
    kept: list[Any] = []
    for _start, _end, blob in _scan_balanced_blobs(s):
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        m = _validate_item_lenient(item_cls, obj)
        if m is not None:
            kept.append(m.model_dump())
    if not kept:
        return None
    try:
        return schema_cls.model_validate({field_name: kept}), len(kept)
    except ValidationError:
        return None


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
        # H-22 熔断快速失败：402 配额窗口内的调用不发 HTTP、瞬间失败。
        # 静默（debug 级）—— 熔断打开时已记过一次 WARNING，此后每次调用
        # 再记只会回到"刷屏"老路（383 章书 = 数千次调用 × 日志行）。
        if time.monotonic() < _quota_blocked_until:
            raise LLMQuotaExhaustedError(
                "LLM 配额已耗尽（HTTP 402 熔断中，本周期内调用直接快速失败）。"
                "请等待配额周期重置后重试；已完成部分有 checkpoint，重跑将断点续跑。"
            )
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
        # H-19 日志降噪：逐调用 start/ok 降为 DEBUG —— 大书 prepare 期间
        # 数千次 LLM 调用 × 2 行 INFO 是 app.log 刷屏主因（383 章实测仅
        # [LLM] 前缀就 2000+ 行）。阶段级汇总（polish/dialogue done 等）仍
        # 在 INFO；排查单次调用时切 LOG_LEVEL=DEBUG 全量回来。
        logger.debug(
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
                        # H-22 402 配额耗尽：**不重试**（周期内重试必然再 402，
                        # 只会刷日志），打开熔断让窗口内后续调用零成本快速失败。
                        # 与 429 的本质区别：429 是瞬时限速（退避几秒就恢复），
                        # 402 是计费额度（要等周期重置）。
                        if resp.status_code == 402:
                            _open_quota_breaker()
                            logger.warning(
                                f"[LLM] 402 配额已耗尽（model={model}）："
                                f"熔断 {_quota_breaker_secs()}s 内所有调用快速失败，不发 HTTP。"
                                f"err={err_msg}"
                            )
                            raise LLMQuotaExhaustedError(
                                f"LLM HTTP 402: {err_msg}"
                            )
                        # H-24 422 内容审核拒绝：**不重试**（input 敏感是确定性
                        # 的——同一段原文重发必然再 422；实测 383 章剑来：4 个润色
                        # 章 × 3 次重试全 422、角色切片 17 × 3 次全 422，白白多烧
                        # 2 次 50k 字符级调用）。业务层按「该片段跳过」处理。
                        if resp.status_code == 422:
                            logger.warning(
                                f"[LLM] 422 内容审核拒绝（model={model} schema={schema_name} "
                                f"prompt_chars={prompt_chars}）：不重试，该片段按跳过处理。"
                                f"err={err_msg}"
                            )
                            raise LLMContentRejectedError(
                                f"LLM HTTP 422: {err_msg}"
                            )
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

                    # （_extract_json_blob 已上移为模块级函数，H-24 抢救层复用其扫描）

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
                            # H-24 S3：整体不可解析（截断/夹杂垃圾）→ 把所有平衡
                            # 片段逐条按 item 抢救。语音指令批撞 8k token 上限被
                            # 截断时，这里能保住截断点之前的全部完整条目（旧实现
                            # 整批作废 × 重试 3 次全废，137k 字符 prompt 级浪费）。
                            _sv = _salvage_from_unparseable(output_schema, stripped)
                            if _sv is not None:
                                _model, _kept_n = _sv
                                logger.warning(
                                    f"[LLM] salvage schema={schema_name} "
                                    f"kept={_kept_n} items（响应整体不可解析，已按完整条目抢救，"
                                    f"截断的尾巴条目丢弃）"
                                )
                                return _model
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

                    # H-24：校验 + 抢救（S1 形状归一化 / S2 逐条丢弃）。
                    # 旧实现整体校验失败 → 整批作废 → 全量重试：对白批 200k 字符
                    # prompt × 多轮重试，606~1188 条校验错里多数只是个别条目
                    # 缺字段，全部章节陪葬。partial > total loss。
                    validated, _kept_n, _dropped_n, _shape_fixed = (
                        _validate_or_salvage(output_schema, parsed)
                    )
                    if _shape_fixed or _dropped_n:
                        _why = (
                            f"dropped={_dropped_n} 条非法条目已丢弃，kept={_kept_n}"
                            if _dropped_n
                            else "响应形状已归一化（裸数组/键名偏差），零损失"
                        )
                        logger.warning(
                            f"[LLM] salvage schema={schema_name} "
                            f"kept={_kept_n}/{_kept_n + _dropped_n}（{_why}，"
                            f"避免整批作废全量重试）"
                        )
                    elapsed = _time.perf_counter() - t0
                    total_elapsed = _time.perf_counter() - total_start
                    tok_str = (
                        f"tok_p={prompt_tokens} tok_c={completion_tokens} tok_t={total_tokens}"
                        if total_tokens is not None else "tok=N/A"
                    )
                    logger.debug(
                        f"[LLM] ok model={use_model} schema={schema_name} attempt={attempt}/{max_retries} "
                        f"req_id={req_id} status={http_status} {tok_str} "
                        f"resp_chars={resp_chars} this_ms={int(elapsed*1000)} total_ms={int(total_elapsed*1000)}"
                    )
                    return validated

            except (LLMQuotaExhaustedError, LLMContentRejectedError):
                # H-22：配额耗尽 / H-24：内容审核拒绝 —— 都直接穿透：不重试
                # （窗口内必然再 402 / 同一段原文必然再 422）、不退避、不进
                # FAIL/exhausted 日志（上面对应分支已各记过一行）
                raise
            except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError, httpx.HTTPError) as e:
                last_err = e
                elapsed = _time.perf_counter() - t0
                is_last = attempt == max_retries
                lvl = logging.ERROR if is_last else logging.WARNING
                # H-12 日志降噪：ValidationError 的 str(e) 可达数千行
                # （app.log 实测 142 errors × 3 行 = 426 行/条），只打前 2 条 + 总数
                if isinstance(e, ValidationError):
                    errs = e.errors()
                    head = "; ".join(
                        f"{'.'.join(str(x) for x in er.get('loc', ()))}: {er.get('msg')}"
                        for er in errs[:2]
                    )
                    detail = f"{len(errs)} validation errors, head=[{head}]"
                else:
                    detail = f"{type(e).__name__}: {e}"
                msg = (
                    f"[LLM] FAIL model={use_model} schema={schema_name} attempt={attempt}/{max_retries} "
                    f"req_id={req_id} status={http_status} this_ms={int(elapsed*1000)} "
                    f"finish_reason={finish_reason} tok_c={completion_tokens} resp_chars={resp_chars} "
                    f"{detail}"
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
