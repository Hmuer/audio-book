"""H-28 402 配额耗尽自动重试测试。

背景（用户实测）：凌晨 3 点整本书 prepare 遇 402 → 置 failed 后**无人拉起**
（看门狗只扫 status=preparing，那是崩溃恢复语义）→ 到早上 9 点用户看到项目
还躺着，整个配额窗口（约 5 小时）浪费。用户期望：碰 402 不是等 5 小时，
而是每隔几分钟自动重试一次，配额一重置就自动断点续跑。

新行为：
- 402 失败收尾：failed + last_error_type=LLMQuotaExhausted +
  quota_retry{attempts, next_at}（改抛 LLMQuotaExhaustedError 精确识别）。
- 看门狗每轮（15s）扫到期的 failed 项目自动重新触发 prepare（断点续跑）；
  配额仍耗尽则又快速 402 中止、再等下一轮（熔断窗口内 0 HTTP，探测 402
  不耗 token，重试成本≈0）。
- 非 402 失败 / 未到期 / 源文件丢失 / 配置关闭（PREPARE_QUOTA_RETRY_MINUTES=0）
  四种情形都不自动重试。
- 跑通后清掉 quota_retry，上次错误归档 prev_error。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

import pytest

from backend.app.ai.base import LLMQuotaExhaustedError

pytest_plugins = ("pytest_asyncio",)


def _book_text(n_chapters: int, chars_per_ch: int = 3000) -> str:
    from backend.tests.test_prepare_perf_red import _book_text as _bt

    return _bt(n_chapters, chars_per_ch)


async def _get_project_row(project_id: str):
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        return await sess.get(Project, project_id)


async def _get_status(project_id: str) -> str:
    proj = await _get_project_row(project_id)
    return proj.status if proj else ""


async def _read_prog(project_id: str) -> dict:
    proj = await _get_project_row(project_id)
    return json.loads(proj.progress_json) if proj and proj.progress_json else {}


async def _make_project(name: str, n_chapters: int = 3) -> str:
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file

    await init_db()
    resp = await create_project(name)
    await import_file(
        resp.project_id, _book_text(n_chapters).encode("utf-8"), f"{name}.txt"
    )
    return resp.project_id


async def _db_surgery(project_id: str, prog_patch: dict, status: str | None = None) -> None:
    """直接手术 progress_json / status，构造各种 failed 现场（免真实跑 402）。"""
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        proj = await sess.get(Project, project_id)
        prog = json.loads(proj.progress_json) if proj.progress_json else {}
        prog.update(prog_patch)
        proj.progress_json = json.dumps(prog, ensure_ascii=False)
        if status:
            proj.status = status
        await sess.commit()


async def _run_prepare_direct(pid: str) -> None:
    from backend.app.services.project import (
        _run_prepare_project_in_background as run_prepare,
    )

    await run_prepare(pid)


async def _wait_status(pid: str, want: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _get_status(pid) == want:
            return want
        await asyncio.sleep(0.1)
    return await _get_status(pid)


def _patch_always_quota(monkeypatch, mock_llm) -> None:
    async def always_quota(*a, **kw):
        # 保留调用记录（探测用例要断言「只有 ping 探测发出、无正式调用」）
        mock_llm.calls.append({"prompt": (a[0] if a else ""), "schema": "402"})
        raise LLMQuotaExhaustedError("LLM HTTP 402: 测试配额耗尽")

    monkeypatch.setattr(mock_llm, "chat_structured", always_quota)


@pytest.mark.asyncio
async def test_h28_402_failure_marks_quota_retry(_isolate_data_dir, monkeypatch):
    """prepare 遇 402 → failed + last_error_type=LLMQuotaExhausted +
    quota_retry{attempts=1, next_at≈now+5min}，错误话术告知将自动重试。
    （旧版：只写 last_error 置 failed，此后无人拉起。）"""
    from backend.app.ai import factory as ai_factory

    mock_llm = ai_factory._llm_instance
    _patch_always_quota(monkeypatch, mock_llm)

    pid = await _make_project("H28-标记")
    await _run_prepare_direct(pid)

    assert await _get_status(pid) == "failed"
    prog = await _read_prog(pid)
    assert prog.get("last_error_type") == "LLMQuotaExhausted"
    assert "自动重试" in (prog.get("last_error") or ""), prog.get("last_error")

    qr = prog.get("quota_retry")
    assert isinstance(qr, dict), f"应写 quota_retry 标记，实际 prog={prog}"
    assert qr.get("attempts") == 1
    next_at = datetime.strptime(qr["next_at"], "%Y-%m-%d %H:%M:%S")
    remain_s = (next_at - datetime.now()).total_seconds()
    assert 0 < remain_s <= 6 * 60, f"next_at 应在 ~5 分钟后，实际还剩 {remain_s:.0f}s"


@pytest.mark.asyncio
async def test_h28_due_project_auto_resumes_to_ready(_isolate_data_dir, monkeypatch, caplog):
    """端到端主链路：402 失败 → next_at 到期 → 看门狗扫描自动拉起 →
    配额已恢复（mock 治愈）→ 断点续跑到 ready；quota_retry 清零、
    上次 402 归档 prev_error。这就是「凌晨 402、配额重置后无人值守
    自动跑完」的场景。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import (
        _scan_quota_failed_and_retry,
        _prepare_running_tasks,
    )

    mock_llm = ai_factory._llm_instance
    orig_chat = mock_llm.chat_structured
    _patch_always_quota(monkeypatch, mock_llm)

    pid = await _make_project("H28-自动续跑", n_chapters=6)
    await _run_prepare_direct(pid)
    assert await _get_status(pid) == "failed"

    # 模拟 5 分钟后（next_at 到期）且配额已恢复
    await _db_surgery(pid, {
        "quota_retry": {"attempts": 1, "next_at": "2020-01-01 00:00:00"},
    })
    monkeypatch.setattr(mock_llm, "chat_structured", orig_chat)
    caplog.set_level(logging.INFO, logger="backend.app.services.project")

    await _scan_quota_failed_and_retry(trigger="watchdog")
    assert "配额耗尽自动重试 #1" in caplog.text

    status = await _wait_status(pid, "ready")
    assert status == "ready", f"自动重试应断点续跑到 ready，实际 status={status}"

    prog = await _read_prog(pid)
    assert "quota_retry" not in prog, "跑通后应清掉自动重试标记"
    prev = prog.get("prev_error") or {}
    assert "配额" in (prev.get("msg") or ""), f"上次 402 应归档 prev_error，实际 {prev}"


@pytest.mark.asyncio
async def test_h28_not_due_not_retried(_isolate_data_dir):
    """next_at 未到期 → 扫描不拉起（状态/计数/任务都不动）。"""
    from backend.app.services.project import (
        _scan_quota_failed_and_retry,
        _prepare_running_tasks,
    )

    pid = await _make_project("H28-未到期")
    await _db_surgery(pid, {
        "last_error_type": "LLMQuotaExhausted",
        "last_error": "LLMQuotaExhausted: 测试",
        "quota_retry": {
            "attempts": 1,
            "next_at": (datetime.now() + timedelta(minutes=5)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        },
    }, status="failed")

    await _scan_quota_failed_and_retry(trigger="watchdog")

    assert await _get_status(pid) == "failed"
    assert pid not in _prepare_running_tasks
    prog = await _read_prog(pid)
    assert prog["quota_retry"]["attempts"] == 1


@pytest.mark.asyncio
async def test_h28_non_quota_failure_not_retried(_isolate_data_dir):
    """非 402 失败（last_error_type=ValueError）即使 next_at 到期也**永不**
    自动重试——业务错误重试只会重复失败；且 0 次 LLM 调用。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import (
        _scan_quota_failed_and_retry,
        _prepare_running_tasks,
    )

    pid = await _make_project("H28-业务失败")
    # 用 last_error_type=RuntimeError 但**不含**配额文案：专防存量兼容匹配器
    # （_is_quota_failed_prog 的 RuntimeError 分支）误伤普通运行时错误
    await _db_surgery(pid, {
        "last_error_type": "RuntimeError",
        "last_error": "RuntimeError: 普通业务运行时错误（非配额）",
        "quota_retry": {"attempts": 1, "next_at": "2020-01-01 00:00:00"},
    }, status="failed")

    calls_before = len(ai_factory._llm_instance.calls)
    await _scan_quota_failed_and_retry(trigger="watchdog")

    assert await _get_status(pid) == "failed"
    assert pid not in _prepare_running_tasks
    assert len(ai_factory._llm_instance.calls) == calls_before, "业务失败不得触发任何 LLM 调用"


@pytest.mark.asyncio
async def test_h28_disabled_by_config(_isolate_data_dir, monkeypatch):
    """PREPARE_QUOTA_RETRY_MINUTES=0 → 关闭自动重试：402 失败不写 quota_retry
    （回到旧行为：等用户手动触发），扫描空转。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.core.config import settings
    from backend.app.services.project import (
        _scan_quota_failed_and_retry,
        _prepare_running_tasks,
    )

    monkeypatch.setattr(settings, "PREPARE_QUOTA_RETRY_MINUTES", 0)
    mock_llm = ai_factory._llm_instance
    _patch_always_quota(monkeypatch, mock_llm)

    pid = await _make_project("H28-关闭")
    await _run_prepare_direct(pid)

    assert await _get_status(pid) == "failed"
    prog = await _read_prog(pid)
    assert prog.get("last_error_type") == "LLMQuotaExhausted"
    assert "quota_retry" not in prog, "配置关闭时不得写自动重试标记"
    assert "自动重试" not in (prog.get("last_error") or "")

    # 即使手术出 next_at 也不拉起（扫描入口直接短路）
    await _db_surgery(pid, {
        "quota_retry": {"attempts": 1, "next_at": "2020-01-01 00:00:00"},
    })
    await _scan_quota_failed_and_retry(trigger="watchdog")
    assert await _get_status(pid) == "failed"
    assert pid not in _prepare_running_tasks


@pytest.mark.asyncio
async def test_h28_legacy_quota_failure_retried_and_normalized(_isolate_data_dir, monkeypatch, caplog):
    """存量兼容（H-28 上线**前**的 402 项目，正是本需求的原案例）：旧代码收尾
    格式 last_error_type=RuntimeError + 旧话术、无 quota_retry 标记 → 扫描
    **立即**拉起（不等 next_at）；重试再遇 402 → 归一化为新格式
    （LLMQuotaExhausted + quota_retry.attempts=1），此后进入正常 5 分钟循环。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import _scan_quota_failed_and_retry

    mock_llm = ai_factory._llm_instance
    _patch_always_quota(monkeypatch, mock_llm)
    # 探测放行（本用例聚焦「拉起后再 402 → 归一化」；探测拦截行为另有专测）
    async def _probe_ok():
        return True
    monkeypatch.setattr(
        "backend.app.services.project._probe_llm_quota", _probe_ok
    )

    pid = await _make_project("H28-存量兼容")
    await _db_surgery(pid, {
        "last_error_type": "RuntimeError",
        "last_error": (
            "RuntimeError: LLM 配额已耗尽（HTTP 402，套餐周期内额度用完，约每 5 小时重置）。"
            "prepare 已中止：已完成章节/切片有 checkpoint，"
            "等配额重置后重新触发 prepare 将自动断点续跑，无需重新导入。"
        ),
    }, status="failed")

    caplog.set_level(logging.INFO, logger="backend.app.services.project")
    await _scan_quota_failed_and_retry(trigger="startup")
    assert "配额耗尽自动重试" in caplog.text

    # 重试又 402 → 轮询等新格式收尾落库（归一化 + 标记写入）
    deadline = time.monotonic() + 20
    prog = {}
    while time.monotonic() < deadline:
        prog = await _read_prog(pid)
        if prog.get("last_error_type") == "LLMQuotaExhausted":
            break
        await asyncio.sleep(0.1)
    assert prog.get("last_error_type") == "LLMQuotaExhausted", (
        f"重试再 402 后应归一化为新格式，实际 last_error_type={prog.get('last_error_type')}"
    )
    qr = prog.get("quota_retry") or {}
    assert qr.get("attempts") == 1
    next_at = datetime.strptime(qr["next_at"], "%Y-%m-%d %H:%M:%S")
    assert next_at > datetime.now(), "归一化后 next_at 应在未来（进入正常重试循环）"


@pytest.mark.asyncio
async def test_h28_probe_402_skips_launch_and_defers(_isolate_data_dir, monkeypatch, caplog):
    """H-28 探测降本：拉起前 mini 调用测配额，仍 402 → **不启动 prepare**、
    只把 next_at 推到下一轮。旧实现拉起后第一调是 50k 字符级正式调用
    （402 也按 input 计费，用户账单低谷时段 ~1M 字符/小时全是这种探测）。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import (
        _scan_quota_failed_and_retry,
        _prepare_running_tasks,
    )

    mock_llm = ai_factory._llm_instance
    _patch_always_quota(monkeypatch, mock_llm)

    pid = await _make_project("H28-探测拦截")
    await _db_surgery(pid, {
        "last_error_type": "LLMQuotaExhausted",
        "last_error": "LLMQuotaExhausted: 测试",
        "quota_retry": {"attempts": 3, "next_at": "2020-01-01 00:00:00"},
    }, status="failed")

    caplog.set_level(logging.INFO, logger="backend.app.services.project")
    await _scan_quota_failed_and_retry(trigger="watchdog")

    # 不拉起：状态不变、无任务、attempts 不变、next_at 被推向未来
    assert await _get_status(pid) == "failed"
    assert pid not in _prepare_running_tasks
    prog = await _read_prog(pid)
    qr = prog.get("quota_retry") or {}
    assert qr.get("attempts") == 3, "探测失败不增加 attempts（还没真正重试）"
    next_at = datetime.strptime(qr["next_at"], "%Y-%m-%d %H:%M:%S")
    assert next_at > datetime.now(), "next_at 应被推迟到下一窗口"
    assert "探测仍 402" in caplog.text
    # 探测唯一 LLM 调用是几十字符的 ping（而非 50k 正式切片 prompt）
    calls = ai_factory._llm_instance.calls
    assert len(calls) == 1 and calls[0]["prompt"] == "ping", (
        f"应只发 1 次探测调用，实际 calls={[c.get('prompt', '')[:20] for c in calls]}"
    )


@pytest.mark.asyncio
async def test_h28_probe_recovered_launches_and_completes(_isolate_data_dir, monkeypatch):
    """探测成功（配额恢复）→ 正常拉起断点续跑到 ready（探测降本不影响主链路）。"""
    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import _scan_quota_failed_and_retry

    pid = await _make_project("H28-探测放行", n_chapters=4)
    await _db_surgery(pid, {
        "last_error_type": "LLMQuotaExhausted",
        "last_error": "LLMQuotaExhausted: 测试",
        "quota_retry": {"attempts": 1, "next_at": "2020-01-01 00:00:00"},
    }, status="failed")
    # mock 正常（配额已恢复）：probe 走 mock 兜底分支（NotImplementedError
    # → 探测函数对非配额异常保守放行 True）→ 拉起 → 正常跑到 ready
    await _scan_quota_failed_and_retry(trigger="watchdog")
    status = await _wait_status(pid, "ready")
    assert status == "ready", f"探测放行后应续跑到 ready，实际 {status}"


@pytest.mark.asyncio
async def test_h28_source_missing_stops_retry(_isolate_data_dir, caplog):
    """源文件丢失的 quota-failed 项目：到期重试发现无源文件 → 改写
    last_error_type=SourceFileMissing 收口，不再永扫永试。"""
    from backend.app.services.project import (
        _scan_quota_failed_and_retry,
        _prepare_running_tasks,
    )

    pid = await _make_project("H28-源文件缺失")
    await _db_surgery(pid, {
        "last_error_type": "LLMQuotaExhausted",
        "last_error": "LLMQuotaExhausted: 测试",
        "quota_retry": {"attempts": 1, "next_at": "2020-01-01 00:00:00"},
    }, status="failed")
    proj = await _get_project_row(pid)
    assert proj.source_file_path and os.path.isfile(proj.source_file_path)
    os.remove(proj.source_file_path)

    caplog.set_level(logging.WARNING, logger="backend.app.services.project")
    await _scan_quota_failed_and_retry(trigger="watchdog")

    assert await _get_status(pid) == "failed"
    assert pid not in _prepare_running_tasks
    assert "已停止自动重试" in caplog.text
    prog = await _read_prog(pid)
    assert prog.get("last_error_type") == "SourceFileMissing"
    # 再扫一轮：last_error_type 已不是 LLMQuotaExhausted → 不再匹配
    await _scan_quota_failed_and_retry(trigger="watchdog")
    assert pid not in _prepare_running_tasks
