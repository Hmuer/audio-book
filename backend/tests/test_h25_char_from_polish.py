"""
H-25 方案一端到端测试：角色识别优先消费润色顺带产出的人物提及名单。

覆盖四条路径（全部走真实 prepare 管线 + Mock LLM，无真实 API）：
1. 纯名单路径：POLISH_ENABLED=True 且全章有名单 → 零切片扫描调用，
   char_source=polish_mentions；名单聚合 + 档案补全独立产出角色。
2. 回退路径：POLISH_ENABLED=False（无名单）→ 行为与旧版一致，
   char_source=full_scan，切片扫描照常发起。
3. 混合路径：润色单章异常失败（该章无名单）→ char_source=mixed，
   仅未覆盖章走切片扫描，名单与切片结果合并；切片已产出的名字不再补档案。
4. 断点续跑：二次 prepare 复用 sidecar 名单 + 名单档案 checkpoint，
   零新增切片/档案调用。
"""
from __future__ import annotations
import json

import pytest

pytest_plugins = ("pytest_asyncio",)


# 每章都含 3 个固定角色名（Mock 润色分支按名字扫描产出名单）+ 对白引号
_BOOK_TXT = """\
第一章 初遇

林若雪低着头，缓慢走在街道一侧。
「怎么了？」李明拍了拍她的肩膀。
「没什么。」林若雪小声说。
街角，王大爷手里拎着一串糖葫芦，正朝他们招手。

第二章 告别

夜里下起了小雨。
「明天见。」李明轻声说道。
「嗯。」林若雪点点头，转身走入雨幕。
王大爷站在糖葫芦铺子前，目送她走远。
"""

# 混合路径专用书：第 4 章带 H25FAIL 标记 → 润色必失败（无名单），
# 其余 3 章正常产出名单
def _mixed_book_text() -> str:
    parts = []
    for i in range(1, 5):
        marker = "H25FAIL " if i == 4 else ""
        body = (
            "「你怎么了？」李明拍了拍她的肩膀。"
            "「没什么。」林若雪小声说，王大爷在街角招手。" + marker
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


def _count_calls(mock_llm, predicate) -> int:
    return sum(1 for c in mock_llm.calls if predicate(c))


def _slice_scan_calls(mock_llm) -> int:
    """切片扫描 = extract_characters_with_llm 的 prompt 标记（---TEXT START---）。
    注意润色 prompt 用的是 ---RAW TEXT START---，不含该子串，无歧义。"""
    return _count_calls(
        mock_llm, lambda c: "---TEXT START---" in c["prompt"]
    )


def _profile_calls(mock_llm) -> int:
    return _count_calls(
        mock_llm, lambda c: "从人物名单生成角色档案" in c["prompt"]
    )


def _polish_calls(mock_llm) -> int:
    return _count_calls(
        mock_llm, lambda c: "---RAW TEXT START---" in c["prompt"]
    )


# =====================================================================
# 1. 纯名单路径：零切片扫描，名单聚合 + 档案补全独立产出角色
# =====================================================================
@pytest.mark.asyncio
async def test_h25_pure_mentions_path_no_slice_scan(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
    )

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)

    resp = await create_project("H25-纯名单路径")
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "h25a.txt")

    mock_llm = _isolate_data_dir["llm"]
    await prepare_project(pid)

    prog = await _read_prog(pid)
    # 来源 = 纯名单聚合，全覆盖，零切片
    assert prog.get("char_source") == "polish_mentions", (
        f"全章有名单时 char_source 应为 polish_mentions，实际 {prog.get('char_source')}"
    )
    assert prog.get("char_mention_covered_n") == 2, (
        f"名单应覆盖 2 章，实际 {prog.get('char_mention_covered_n')}"
    )
    assert prog.get("char_slice_total") == 0, "纯名单路径不应产生任何切片"

    # 核心断言：零次切片扫描调用（角色识别不重读全文）
    assert _slice_scan_calls(mock_llm) == 0, (
        "纯名单路径不应发起任何切片扫描 LLM 调用（---TEXT START---）"
    )

    # 名单档案补全恰好一次批量调用，且带上全部 3 个名字
    profile_prompts = [
        c["prompt"] for c in mock_llm.calls if "从人物名单生成角色档案" in c["prompt"]
    ]
    assert len(profile_prompts) == 1, f"档案补全应恰好 1 次批量调用，实际 {len(profile_prompts)}"
    for n in ("林若雪", "李明", "王大爷"):
        assert n in profile_prompts[0], f"档案补全名单缺 {n}"

    # 角色仍完整产出（来自名单档案，gender 允许「未知」）
    detail = await get_project(pid)
    names = {c.name for c in detail.characters}
    assert {"林若雪", "李明", "王大爷"}.issubset(names), (
        f"纯名单路径角色应完整，实际 {names}"
    )

    # 公共进度视图透出来源字段（前端阶段面板依赖）
    pv = detail.prepare_progress or {}
    assert pv.get("char_source") == "polish_mentions"
    assert pv.get("char_mention_covered_n") == 2


# =====================================================================
# 2. 回退路径：POLISH_ENABLED=False → 与旧版行为一致（全量切片扫描）
# =====================================================================
@pytest.mark.asyncio
async def test_h25_polish_disabled_falls_back_to_full_scan(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", False)

    resp = await create_project("H25-回退全量")
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "h25b.txt")

    mock_llm = _isolate_data_dir["llm"]
    await prepare_project(pid)

    prog = await _read_prog(pid)
    assert prog.get("char_source") == "full_scan", (
        f"无名单时应回退 full_scan，实际 {prog.get('char_source')}"
    )
    assert prog.get("char_mention_covered_n") == 0
    assert prog.get("char_slice_total", 0) >= 1, "回退路径应有切片"
    assert _slice_scan_calls(mock_llm) >= 1, "回退路径必须发起切片扫描"
    assert _profile_calls(mock_llm) == 0, "无名单时不应有档案补全调用"


# =====================================================================
# 3. 混合路径：润色失败章无名单 → 仅该章回退切片，名单+切片合并
# =====================================================================
@pytest.mark.asyncio
async def test_h25_partial_coverage_mixed_path(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services import polish as polish_mod
    from backend.app.services.polish import PolishResult
    from backend.app.services.project import create_project, import_file, prepare_project, get_project

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)
    monkeypatch.setattr(settings, "POLISH_MODE", "rewrite")

    async def fake_polish(raw_text: str) -> PolishResult:
        if "H25FAIL" in raw_text:
            raise RuntimeError("mock polish failure")
        # 成功章顺带产出名单（林若雪/李明；王大爷不在成功章名单里，
        # 只能由失败章的切片扫描发现）
        return PolishResult(
            polished_text=raw_text,
            is_reasonable=True,
            reason="ok",
            characters_mentioned=["林若雪", "李明"],
        )

    monkeypatch.setattr(polish_mod, "polish_with_llm", fake_polish)

    resp = await create_project("H25-混合路径")
    pid = resp.project_id
    await import_file(pid, _mixed_book_text().encode("utf-8"), "h25c.txt")

    mock_llm = _isolate_data_dir["llm"]
    await prepare_project(pid)

    prog = await _read_prog(pid)
    # 润色：3 章成功（带名单），1 章失败（无名单）
    assert prog.get("polish_failed_n") == 1, f"应有 1 章润色失败：{prog}"
    # 来源 = mixed：名单覆盖 3 章，失败章回退切片
    assert prog.get("char_source") == "mixed", (
        f"部分覆盖时 char_source 应为 mixed，实际 {prog.get('char_source')}"
    )
    assert prog.get("char_mention_covered_n") == 3, (
        f"名单应覆盖 3 章，实际 {prog.get('char_mention_covered_n')}"
    )
    assert prog.get("char_slice_total") == 1, (
        f"仅失败章应装 1 个桶，实际 {prog.get('char_slice_total')}"
    )

    # 恰好 1 次切片扫描（失败章），且切片已产出全部名字 → 不再有档案补全
    assert _slice_scan_calls(mock_llm) == 1, (
        f"只应对未覆盖章发起 1 次切片扫描，实际 {_slice_scan_calls(mock_llm)}"
    )
    assert _profile_calls(mock_llm) == 0, (
        "切片已产出同名角色时，名单里的名字不应再补档案"
    )

    # 角色完整：名单名 + 切片在失败章里发现的王大爷
    detail = await get_project(pid)
    names = {c.name for c in detail.characters}
    assert {"林若雪", "李明", "王大爷"}.issubset(names), (
        f"混合路径角色应完整（含失败章切片发现的王大爷），实际 {names}"
    )


# =====================================================================
# 4. 断点续跑：二次 prepare 复用 sidecar 名单 + 档案 checkpoint（零新调用）
# =====================================================================
@pytest.mark.asyncio
async def test_h25_rerun_prepare_reuses_mention_checkpoints(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)

    resp = await create_project("H25-断点续跑")
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "h25d.txt")

    mock_llm = _isolate_data_dir["llm"]
    await prepare_project(pid)
    first_slices = _slice_scan_calls(mock_llm)
    first_profiles = _profile_calls(mock_llm)
    first_polish = _polish_calls(mock_llm)
    prog1 = await _read_prog(pid)
    assert prog1.get("char_source") == "polish_mentions"
    assert first_slices == 0

    calls_before = len(mock_llm.calls)
    await prepare_project(pid)

    prog2 = await _read_prog(pid)
    # 来源口径不变，仍然纯名单
    assert prog2.get("char_source") == "polish_mentions"
    assert prog2.get("char_mention_covered_n") == 2
    # 复用而非重调：切片/档案/润色均零新增
    assert _slice_scan_calls(mock_llm) == first_slices == 0
    assert _profile_calls(mock_llm) == first_profiles, "档案 checkpoint 应复用，不重调"
    assert _polish_calls(mock_llm) == first_polish, "sidecar 润色文本应复用，不重调"
    # 第二次 prepare 对同一源文件是全程 checkpoint 命中：对白/指令/音色
    # 等阶段也全部复用 → LLM 调用总数应零增长（真正的断点续跑语义）
    assert len(mock_llm.calls) == calls_before, (
        f"同源文件重跑 prepare 应零新增 LLM 调用（全程 checkpoint 命中），"
        f"实际新增 {len(mock_llm.calls) - calls_before} 次"
    )
