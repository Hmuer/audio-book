"""
供应商用量统计：LLM（prepare 阶段）与 TTS（build 阶段）的真实调用计数。

设计：
- LLM 调用分散在 polish/character/dialogue/voice_recommender 各 service，
  通过 ContextVar 作用域（llm_usage_scope(project_id)）收集：各调用点只管
  track_llm(calls=1, chars=len(prompt))，prepare worker 结束时 flush_usage_events()
  一次性写 UsageEvent 表。
- TTS 用量在 build worker 内直接累加（每段合成是明确的一次调用），完成时
  写 Build.tts_calls/tts_chars + UsageEvent，不走 ContextVar。
- prepare 中途崩溃：flush 在 finally 里尽力执行，未 flush 的部分丢弃 ——
  LLM 计数是辅助参考，不做强一致。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger("novel-tts")

# 当前 prepare 作用域：{"project_id": str, "events": {detail: {"calls": int, "chars": int}}}
_llm_scope: ContextVar[dict | None] = ContextVar("llm_usage_scope", default=None)


@contextmanager
def llm_usage_scope(project_id: str):
    """包住一次 prepare 运行；退出时自动 flush 到 UsageEvent 表。"""
    token = _llm_scope.set({"project_id": project_id, "events": {}})
    try:
        yield
    finally:
        try:
            _flush_llm_scope()
        except Exception as e:
            logger.warning(f"[usage] flush LLM 用量失败: {type(e).__name__}: {e}")
        finally:
            _llm_scope.reset(token)


def track_llm(calls: int = 1, chars: int = 0, detail: str = "misc") -> None:
    """记录一次 LLM 调用（不在 scope 内时静默忽略，如单测直接调 service）。"""
    scope = _llm_scope.get()
    if not scope:
        return
    ev = scope["events"].setdefault(detail, {"calls": 0, "chars": 0})
    ev["calls"] += calls
    ev["chars"] += chars


def _flush_llm_scope() -> None:
    """把 scope 内累积的 LLM 用量写库（llm_usage_scope 退出时调用）。"""
    scope = _llm_scope.get()
    if not scope:
        return
    events = scope.get("events") or {}
    total_calls = sum(e["calls"] for e in events.values())
    total_chars = sum(e["chars"] for e in events.values())
    if total_calls <= 0:
        return
    project_id = scope["project_id"]
    _write_event(project_id, kind="llm", detail="prepare", calls=total_calls, chars=total_chars)
    logger.info(
        f"[usage] project_id={project_id[:8]}... LLM 用量: {total_calls} 次调用 / {total_chars} 字符"
    )


def record_tts_usage(project_id: str, build_id: str, calls: int, chars: int, detail: str = "build") -> None:
    """build 完成时写一条 TTS 用量（与 Build.tts_calls/tts_chars 同源冗余）。"""
    if calls <= 0:
        return
    try:
        _write_event(project_id, kind="tts", detail=detail, calls=calls, chars=chars, build_id=build_id)
    except Exception as e:
        logger.warning(f"[usage] 写 TTS 用量失败: {type(e).__name__}: {e}")


def _write_event(project_id: str, *, kind: str, detail: str, calls: int, chars: int, build_id: str | None = None) -> None:
    from ..db.session import get_session_factory
    from ..db.models import UsageEvent
    import asyncio

    async def _insert() -> None:
        factory = get_session_factory()
        async with factory() as s:
            s.add(UsageEvent(
                project_id=project_id,
                build_id=build_id,
                kind=kind,
                detail=detail,
                calls=calls,
                chars=chars,
            ))
            await s.commit()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_insert())
        return
    # 已在事件循环内：直接调度（UsageEvent 写失败只告警，不影响业务终态）
    task = loop.create_task(_insert())

    def _on_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            logger.warning(f"[usage] UsageEvent 写入失败: {t.exception()}")

    task.add_done_callback(_on_done)
