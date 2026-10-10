"""H-26：422 内容审核拒绝章与可补跑失败章分离（798 角色实测 app.log）。

实测问题（app.log 2026-10-10）：4 章 polish 因 422 input sensitive 失败，
被计入 polish_failed_n → 前端显示「润色纠错失败 4/375 章（可补跑）」。
但同样的文本重跑永远再被 422 拒（确定性拒绝，非瞬时错误）——用户每次
重跑 prepare 都白烧 4 章的 LLM 调用且永远不成功，「可补跑」分类误导。

修复：
  1. LLMContentRejectedError 单独归类 content_rejected（不进 failed）
  2. 拒绝章记入润色 sidecar 的 content_rejected 集 → 重跑直接跳过
  3. prog 透出 polish_content_rejected_n / _chapters，前端单独展示
     （不混进「可补跑失败部分」）
"""
from __future__ import annotations

import json

import pytest

pytest_plugins = ("pytest_asyncio",)


def _book_text() -> str:
    """4 章小书：第 2 章带 H26SENS 标记 → fake 润色抛 422，其余正常。"""
    parts = []
    for i in range(1, 5):
        marker = "H26SENS " if i == 2 else ""
        body = (
            "「你怎么了？」李明拍了拍她的肩膀。"
            "「没什么。」林若雪小声说。" + marker
        )
        parts.append(f"第{i}章 情景{i}\n\n{body * 8}")
    return "\n\n".join(parts)


async def _read_prog(project_id: str) -> dict:
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        p = await sess.get(Project, project_id)
        return json.loads(p.progress_json) if p and p.progress_json else {}


def _polish_calls(mock_llm) -> int:
    return sum(1 for c in mock_llm.calls if "---RAW TEXT START---" in c["prompt"])


@pytest.mark.asyncio
async def test_h26_content_rejected_not_counted_as_failed(_isolate_data_dir, monkeypatch):
    """422 拒绝章：content_rejected=1、failed=0、prog 透出 + sidecar 记录。"""
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.ai.base import LLMContentRejectedError
    from backend.app.services import polish as polish_mod
    from backend.app.services.polish import PolishResult
    from backend.app.services.project import (
        create_project, import_file, prepare_project, get_project,
    )

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)
    monkeypatch.setattr(settings, "POLISH_MODE", "rewrite")

    async def fake_polish(raw_text: str) -> PolishResult:
        if "H26SENS" in raw_text:
            raise LLMContentRejectedError("LLM HTTP 422: input new_sensitive (1026)")
        return PolishResult(
            polished_text=raw_text,
            is_reasonable=True,
            reason="ok",
            characters_mentioned=["李明", "林若雪"],
        )

    monkeypatch.setattr(polish_mod, "polish_with_llm", fake_polish)

    resp = await create_project("H26-审核拒绝分类")
    pid = resp.project_id
    await import_file(pid, _book_text().encode("utf-8"), "h26a.txt")
    await prepare_project(pid)

    prog = await _read_prog(pid)
    # 核心断言：422 拒绝不再混进 failed（failed=0 → 不出现在「可补跑」里）
    assert prog.get("polish_content_rejected_n") == 1, (
        f"应有 1 章被审核拒绝：{prog}"
    )
    assert prog.get("polish_content_rejected_chapters") == [2], (
        f"拒绝章号应为 [2]（1-based）：{prog}"
    )
    assert prog.get("polish_failed_n") == 0, (
        f"422 是确定性拒绝，不应计入可补跑 failed：{prog}"
    )

    # 公共进度视图透出（前端审核拒绝面板依赖）
    detail = await get_project(pid)
    pv = detail.prepare_progress or {}
    assert pv.get("polish_content_rejected_n") == 1
    assert pv.get("polish_content_rejected_chapters") == [2]

    # sidecar 记录拒绝章（重跑跳过的依据）
    from backend.app.core.config import settings as _s
    from backend.app.services.polish import load_polish_sidecar_full, polish_sidecar_fingerprint
    from backend.app.db.session import get_session_factory
    from backend.app.services.chapter_store import load_chapters
    from pathlib import Path
    factory = get_session_factory()
    async with factory() as sess:
        rows = await load_chapters(sess, pid)
    texts = [r.text for r in rows]
    fp = polish_sidecar_fingerprint(texts)
    sidecar_path = Path(_s.DATA_DIR) / f"polish_{pid}.json"
    _, _, rejected = load_polish_sidecar_full(sidecar_path, fp)
    assert "1" in rejected, f"sidecar 应记录被拒章 idx=1（0-based）：{rejected}"


@pytest.mark.asyncio
async def test_h26_rerun_skips_content_rejected_chapters(_isolate_data_dir, monkeypatch):
    """重跑 prepare：被拒章直接跳过，不再发起注定失败的 LLM 调用。"""
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.ai.base import LLMContentRejectedError
    from backend.app.services import polish as polish_mod
    from backend.app.services.polish import PolishResult
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)
    monkeypatch.setattr(settings, "POLISH_MODE", "rewrite")

    state = {"rejected_calls": 0}

    async def fake_polish(raw_text: str) -> PolishResult:
        if "H26SENS" in raw_text:
            state["rejected_calls"] += 1
            raise LLMContentRejectedError("LLM HTTP 422: input new_sensitive (1026)")
        return PolishResult(
            polished_text=raw_text,
            is_reasonable=True,
            reason="ok",
            characters_mentioned=["李明", "林若雪"],
        )

    monkeypatch.setattr(polish_mod, "polish_with_llm", fake_polish)

    resp = await create_project("H26-重跑跳过拒绝章")
    pid = resp.project_id
    await import_file(pid, _book_text().encode("utf-8"), "h26b.txt")

    mock_llm = _isolate_data_dir["llm"]
    await prepare_project(pid)
    first_rejected = state["rejected_calls"]
    first_polish_calls = _polish_calls(mock_llm)
    assert first_rejected == 1, "首次 prepare 应尝试并拒绝该章 1 次"

    # 重跑：被拒章命中 sidecar content_rejected → 直接跳过
    await prepare_project(pid)
    assert state["rejected_calls"] == first_rejected, (
        "重跑 prepare 不应对审核拒绝章再次发起 LLM 调用（确定性拒绝）"
    )
    # 重跑后 polish 相关调用零增长（其余章 checkpoint 复用，被拒章跳过）
    assert _polish_calls(mock_llm) == first_polish_calls, (
        "重跑 prepare 不应产生任何新的润色 LLM 调用"
    )

    prog = await _read_prog(pid)
    assert prog.get("polish_content_rejected_n") == 1, "重跑后拒绝计数保持"
    assert prog.get("polish_failed_n") == 0
