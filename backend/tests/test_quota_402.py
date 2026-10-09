"""H-22 402 配额熔断专项测试。

背景（MiniMax Token Plan 5 小时周期实测）：配额用完时所有调用必然 402，
旧实现把 402 当普通瞬时错误——每调用 3 次重试 × 每次 FAIL/exhausted
ERROR（带 traceback），383 章书 prepare 期间产生数百条重复日志；且后续
阶段照常起跑，全是 doomed 调用。

新行为：
- provider：402 → LLMQuotaExhaustedError，**不重试**（1 次 HTTP）、
  1 行 WARNING、无 traceback；打开熔断，窗口内（LLM_QUOTA_RETRY_SECS，
  默认 300s）所有调用**不发 HTTP、瞬间失败、静默**；到期放一次真实探测，
  恢复则自动闭合，仍未恢复则重新熔断。
- prepare：捕获 LLMQuotaExhaustedError → 置 quota_hit，未起跑任务静默
  跳过，阶段收尾统一中止整个 prepare（checkpoint 已保住，重跑断点续跑）；
  指令/音色推荐阶段配额错误**穿透**（否则全空结果会写成 done 的投毒
  checkpoint）。
"""
from __future__ import annotations

import json
import logging
import time

import pytest
from pydantic import BaseModel

from backend.app.ai.base import LLMQuotaExhaustedError

pytest_plugins = ("pytest_asyncio",)


# =====================================================================
# Part 1：provider 层（真实 MiniMaxLLMProvider + 假 httpx）
# =====================================================================

class _TinyResult(BaseModel):
    ok: bool = True


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.headers: dict = {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return json.loads(self.text)


def _ok_payload() -> dict:
    return {
        "request_id": "req-test",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "choices": [
            {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
        ],
    }


def _quota_402_payload() -> dict:
    return {
        "request_id": "req-test",
        "error": {"message": "当前已达到 Token Plan 用量上限（测试）"},
    }


def _make_fake_client(script: list[_FakeResp]):
    """按脚本回放响应的假 httpx.AsyncClient；脚本耗尽后重复最后一个响应。"""
    state = {"post_calls": 0, "next": 0}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            state["post_calls"] += 1
            i = min(state["next"], len(script) - 1)
            state["next"] += 1
            return script[i]

    _FakeAsyncClient.state = state  # type: ignore[attr-defined]
    return _FakeAsyncClient


def _get_llm_mod():
    from backend.app.ai.providers.minimax import llm as llm_mod

    return llm_mod


def _make_provider():
    llm_mod = _get_llm_mod()
    return llm_mod.MiniMaxLLMProvider(api_key="test-key", base_url="http://test.local")


@pytest.mark.asyncio
async def test_h22_402_raises_once_without_retry(monkeypatch, caplog):
    """402 → 立即抛 LLMQuotaExhaustedError：只发 1 次 HTTP（旧实现 3 次）、
    1 行 WARNING、无 ERROR/traceback。"""
    llm_mod = _get_llm_mod()
    llm_mod.reset_quota_breaker()
    fake = _make_fake_client([_FakeResp(402, _quota_402_payload())])
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", fake)

    caplog.set_level(logging.WARNING, logger="backend.app.ai.providers.minimax.llm")
    with pytest.raises(LLMQuotaExhaustedError, match="402"):
        await _make_provider().chat_structured(
            "test", _TinyResult, max_retries=3
        )

    # 不重试：恰好 1 次 HTTP（旧实现 3 次尝试）
    assert fake.state["post_calls"] == 1, (
        f"402 不应重试，HTTP 调用 {fake.state['post_calls']} 次"
    )
    # 熔断已打开
    assert llm_mod._quota_blocked_until > time.monotonic()
    # 日志恰好 1 行 WARNING、无 ERROR（无 exhausted、无 traceback）
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "402" in r.message]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(warns) == 1, f"402 应只记 1 行 WARNING，实际 {len(warns)}"
    assert not errors, f"402 不应产生 ERROR 日志：{[r.message for r in errors]}"
    llm_mod.reset_quota_breaker()


@pytest.mark.asyncio
async def test_h22_breaker_fast_fail_skips_http(monkeypatch):
    """熔断窗口内：后续调用瞬间失败、0 次 HTTP（mock 抛出的异常消息与
    首次 402 无关——快速失败有自己的统一话术）。"""
    llm_mod = _get_llm_mod()
    llm_mod.reset_quota_breaker()
    fake = _make_fake_client([_FakeResp(402, _quota_402_payload())])
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", fake)
    provider = _make_provider()

    with pytest.raises(LLMQuotaExhaustedError):
        await provider.chat_structured("t1", _TinyResult)

    # 熔断中：瞬间失败，不再发 HTTP
    with pytest.raises(LLMQuotaExhaustedError, match="熔断"):
        await provider.chat_structured("t2", _TinyResult)
    with pytest.raises(LLMQuotaExhaustedError, match="熔断"):
        await provider.chat_structured("t3", _TinyResult)

    assert fake.state["post_calls"] == 1, "熔断期间不应有新 HTTP 调用"
    llm_mod.reset_quota_breaker()


@pytest.mark.asyncio
async def test_h22_breaker_expiry_probes_then_reopens(monkeypatch):
    """熔断到期 → 放一次真实探测；仍未恢复（再 402）→ 重新熔断。"""
    llm_mod = _get_llm_mod()
    llm_mod.reset_quota_breaker()
    fake = _make_fake_client([_FakeResp(402, _quota_402_payload())])
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", fake)
    provider = _make_provider()

    with pytest.raises(LLMQuotaExhaustedError):
        await provider.chat_structured("t1", _TinyResult)
    assert fake.state["post_calls"] == 1

    # 模拟熔断到期
    llm_mod._quota_blocked_until = time.monotonic() - 0.001
    # 探测：真实 HTTP（仍 402）→ 重新熔断
    with pytest.raises(LLMQuotaExhaustedError, match="402"):
        await provider.chat_structured("t2", _TinyResult)
    assert fake.state["post_calls"] == 2, "到期后应放行一次真实探测"

    # 重新熔断：后续又不发 HTTP
    with pytest.raises(LLMQuotaExhaustedError, match="熔断"):
        await provider.chat_structured("t3", _TinyResult)
    assert fake.state["post_calls"] == 2
    llm_mod.reset_quota_breaker()


@pytest.mark.asyncio
async def test_h22_breaker_recovers_on_success(monkeypatch):
    """配额重置后（探测调用成功）→ 正常返回，熔断闭合。"""
    llm_mod = _get_llm_mod()
    llm_mod.reset_quota_breaker()
    fake = _make_fake_client([
        _FakeResp(402, _quota_402_payload()),
        _FakeResp(200, _ok_payload()),
    ])
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", fake)
    provider = _make_provider()

    with pytest.raises(LLMQuotaExhaustedError):
        await provider.chat_structured("t1", _TinyResult)

    llm_mod._quota_blocked_until = time.monotonic() - 0.001
    result = await provider.chat_structured("t2", _TinyResult)
    assert result.ok is True
    assert fake.state["post_calls"] == 2
    llm_mod.reset_quota_breaker()


# =====================================================================
# Part 2：prepare 层（Mock LLM 抛 LLMQuotaExhaustedError）
# =====================================================================

def _book_text(n_chapters: int, chars_per_ch: int = 3000) -> str:
    from backend.tests.test_prepare_perf_red import _book_text as _bt

    return _bt(n_chapters, chars_per_ch)


async def _get_project_status(project_id: str) -> str:
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        p = await sess.get(Project, project_id)
        return p.status if p else ""


async def _read_prog(project_id: str) -> dict:
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        p = await sess.get(Project, project_id)
        return json.loads(p.progress_json) if p and p.progress_json else {}


@pytest.mark.asyncio
async def test_h22_prepare_aborts_fast_and_resumes(_isolate_data_dir, monkeypatch, caplog):
    """polish 第 3 章遇 402 → prepare 快速中止（无逐章刷屏）、sidecar 保住
    已完成章；配额恢复后重跑：已完成章 0 次 LLM 复用，仅补跑剩余章。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services.project import (
        create_project,
        import_file,
        _run_prepare_project_in_background as run_prepare,
    )

    await init_db()
    N = 6
    # 串行 polish → 第 3 章抛配额是确定性的
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)
    monkeypatch.setattr(settings, "POLISH_CONCURRENCY", 1)

    mock_llm = ai_factory._llm_instance
    orig_chat = mock_llm.chat_structured
    polish_calls = {"n": 0}

    async def quota_on_3rd_polish(*a, **kw):
        schema = kw.get("output_schema")
        if getattr(schema, "__name__", "") == "PolishDiffResult":
            polish_calls["n"] += 1
            if polish_calls["n"] == 3:
                raise LLMQuotaExhaustedError("LLM HTTP 402: 测试配额耗尽")
        return await orig_chat(*a, **kw)

    monkeypatch.setattr(mock_llm, "chat_structured", quota_on_3rd_polish)
    caplog.set_level(logging.INFO, logger="backend.app.services.project")

    resp = await create_project("H22-配额中止")
    await import_file(resp.project_id, _book_text(N).encode("utf-8"), "h22.txt")

    # ---- 第 1 跑：第 3 章配额耗尽 → 中止 ----
    await run_prepare(resp.project_id)

    assert await _get_project_status(resp.project_id) == "failed"
    prog = await _read_prog(resp.project_id)
    assert "配额" in (prog.get("last_error") or ""), prog.get("last_error")

    # 日志不刷屏：配额事件只记 1 次（旧实现：每章 3 重试 × FAIL + ERROR + traceback）
    assert caplog.text.count("遇到 402 配额耗尽") == 1, (
        "配额事件应只记 1 行，实际：\n" + "\n".join(
            r.getMessage() for r in caplog.records
            if "402" in r.getMessage() or "配额" in r.getMessage()
        )
    )
    # 剩余章未被记为章节级失败（它们是"跳过"，重跑自动补跑；
    # 旧实现语义下它们会各打一行 FAIL + traceback）
    assert "失败，保留原文" not in caplog.text, "配额中止的章不应走『章节失败』日志路径"

    # ---- 第 2 跑：配额恢复 → 断点续跑 ----
    caplog.clear()

    async def healed_chat(*a, **kw):
        schema = kw.get("output_schema")
        if getattr(schema, "__name__", "") == "PolishDiffResult":
            polish_calls["n"] += 1
        return await orig_chat(*a, **kw)

    polish_calls["n"] = 0
    monkeypatch.setattr(mock_llm, "chat_structured", healed_chat)
    await run_prepare(resp.project_id)

    assert await _get_project_status(resp.project_id) == "ready"
    # 前 2 章 sidecar 复用（0 次 LLM），只补跑 4 章
    assert polish_calls["n"] == N - 2, (
        f"重跑应只补跑 {N-2} 章（前 2 章 sidecar 复用），实际调 LLM {polish_calls['n']} 次"
    )
    prog2 = await _read_prog(resp.project_id)
    assert prog2.get("polish_reused_n") == 2
    assert prog2.get("last_error") is None or "配额" not in (prog2.get("last_error") or "")


@pytest.mark.asyncio
async def test_h22_instructions_checkpoint_not_poisoned(_isolate_data_dir, monkeypatch):
    """指令阶段遇 402 → prepare 中止且**不写** instructions_done 投毒
    checkpoint（旧实现吞异常后把全空指令写成 done，重跑直接跳过 →
    指令永远全空）；配额恢复后重跑能真正生成指令。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.db.session import init_db
    from backend.app.services.project import (
        create_project,
        import_file,
        _run_prepare_project_in_background as run_prepare,
    )

    await init_db()
    N = 6

    mock_llm = ai_factory._llm_instance
    orig_chat = mock_llm.chat_structured

    async def quota_on_instructions(*a, **kw):
        schema = kw.get("output_schema")
        if getattr(schema, "__name__", "") == "VoiceInstructionBatchResponse":
            raise LLMQuotaExhaustedError("LLM HTTP 402: 测试配额耗尽")
        return await orig_chat(*a, **kw)

    monkeypatch.setattr(mock_llm, "chat_structured", quota_on_instructions)

    resp = await create_project("H22-指令投毒防护")
    await import_file(resp.project_id, _book_text(N).encode("utf-8"), "h22b.txt")

    # 第 1 跑：指令阶段配额耗尽 → 中止
    await run_prepare(resp.project_id)
    assert await _get_project_status(resp.project_id) == "failed"
    prog = await _read_prog(resp.project_id)
    # 关键断言：没有投毒的 done checkpoint
    assert not prog.get("instructions_done"), (
        "指令阶段配额中止时不得写 instructions_done=True（投毒 checkpoint）"
    )
    assert "配额" in (prog.get("last_error") or "")

    # 第 2 跑：配额恢复 → 指令真正生成
    monkeypatch.setattr(mock_llm, "chat_structured", orig_chat)
    await run_prepare(resp.project_id)
    assert await _get_project_status(resp.project_id) == "ready"
    prog2 = await _read_prog(resp.project_id)
    assert prog2.get("instructions_done") is True
    raw = prog2.get("instructions_raw") or {}
    non_empty = sum(1 for v in raw.values() if v)
    assert non_empty > 0, "配额恢复后重跑必须真正生成指令（非全空）"
