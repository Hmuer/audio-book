"""prepare 端到端性能剖析 + H-11 角色识别切片并发化回归（用户反馈「识别速度更慢了」）。

做法：给 Mock LLM 注入固定延迟（LLM_DELAY_S），跑一本 24 章 × 3000 字的书，
从服务日志解析各阶段耗时（split / characters / dialogues / instructions /
voice_recs），打印剖析表。用固定延迟可以把「代码调度结构」与「上游 LLM 快慢」
解耦——若某阶段耗时 ≈ 调用数 × 延迟，说明该阶段是串行调度；若 ≈ 批数/并发 ×
延迟，说明并发生效。

已确认的结构性事实（2026-10-08 剖析，H-11 修复前）：
- 角色识别（characters）此前逐切片【串行】调 LLM（8 片 × 40ms ≈ 443ms 实测），
  不受 LLM_MAX_CONCURRENCY 影响 —— 大书（数百万字 → 数十个 50k 切片 × 每片
  数十秒）时是 prepare 的最大串行热点。H-11 并发化后：8 片 / 并发 4 ≈ 2 轮。
- 对白归属（dialogues）/ 语音指令（instructions）按批并发，
  受全局 LLM sem 约束。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest

pytest_plugins = ("pytest_asyncio",)


def _make_chapter(i: int, target_chars: int = 3000, with_marks: bool = False) -> str:
    """造一章 ~target_chars 字、含对白的文本（Mock LLM 能命中对白归属）。

    with_marks=True 时嵌入「角色N号」标记，供 H-11 测试的 fake extract 识别。
    """
    lines = [f"第{i}章 情景{i}", ""]
    mark = f"角色{i}号缓步走来。" if with_marks else ""
    body = (
        mark
        + "街边杨柳依依，行人往来如织。"
        + "「你怎么了？」李明拍了拍她的肩膀。"
        + "「没什么。」林若雪小声说，低下头继续走路。"
    )
    n_repeat = max(1, target_chars // len(body))
    lines.append(body * n_repeat)
    return "\n".join(lines)


def _book_text(n_chapters: int, chars_per_ch: int = 3000, with_marks: bool = False) -> str:
    return "\n\n".join(
        _make_chapter(i + 1, chars_per_ch, with_marks) for i in range(n_chapters)
    )


def _parse_stage_ms(caplog_text: str) -> dict[str, int]:
    """从日志文本抽各阶段 ms。"""
    out: dict[str, int] = {}
    m = re.search(r"split_chapters=(\d+) ms=(\d+)", caplog_text)
    if m:
        out["split"] = int(m.group(2))
    m = re.search(r"characters=(\d+) ms=(\d+)", caplog_text)
    if m:
        out["characters"] = int(m.group(2))
    m = re.search(r"dialogue_attr done.*?ms=(\d+)", caplog_text, re.DOTALL)
    if m:
        out["dialogues"] = int(m.group(1))
    m = re.search(r"instructions=(\d+) non_empty=(\d+) \n?\s*ms=(\d+)", caplog_text)
    if m:
        out["instructions"] = int(m.group(3))
    m = re.search(r"voice_recs=(\d+) ms=(\d+)", caplog_text)
    if m:
        out["voice_recs"] = int(m.group(2))
    m = re.search(
        r"polish done: changed=\d+ reused=\d+ rejected=\d+ failed=\d+ / total=\d+ ms=(\d+)",
        caplog_text,
    )
    if m:
        out["polish"] = int(m.group(1))
    return out


@pytest.mark.asyncio
async def test_prepare_stage_profiling(_isolate_data_dir, monkeypatch, caplog):
    """LLM 固定延迟下 prepare 各阶段耗时剖析 + 调度并发生效守护。

    H-11 后角色识别切片并发（CHAR_EXTRACT_CONCURRENCY 默认 4）：
    8 片 ≈ 2 轮 ≈ 2×delay，断言显著低于串行下限（8×delay）的 60%。
    """
    from backend.app.ai import factory as ai_factory
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
    )

    # 0.08s 而非 0.04s：全量套件并发跑时 CPU 负载会给每次 asyncio.sleep
    # 叠加 ~100ms 级调度抖动，40ms 延迟下「0.6×串行下限」余量会被吃光
    # （实测 194ms vs 阈值 192ms 临界翻车）。加倍延迟让 sleep 主导抖动，
    # 阈值不变、断言强度不降。
    LLM_DELAY_S = 0.08
    N_CHAPTERS = 24
    N_CHAR_SLICES = 8  # 72k 字 / 9k 桶

    await init_db()

    # 切片调小 → 24 章 × ~3000 字 → 8 个桶（模拟大书多切片）
    monkeypatch.setattr(settings, "LLM_CHAR_EXTRACT_SLICE_SIZE", 9000)

    # 给 Mock LLM 注入固定延迟 + 统计并发峰值
    mock_llm = ai_factory._llm_instance
    assert mock_llm is not None
    orig_chat = mock_llm.chat_structured
    stats = {"calls": 0, "in_flight": 0, "peak": 0}

    async def slow_chat(*a, **kw):
        stats["calls"] += 1
        stats["in_flight"] += 1
        stats["peak"] = max(stats["peak"], stats["in_flight"])
        try:
            await asyncio.sleep(LLM_DELAY_S)
            return await orig_chat(*a, **kw)
        finally:
            stats["in_flight"] -= 1

    monkeypatch.setattr(mock_llm, "chat_structured", slow_chat)

    caplog.set_level(logging.INFO, logger="backend.app.services.project")

    resp = await create_project("PERF-剖析")
    await import_file(resp.project_id, _book_text(N_CHAPTERS).encode("utf-8"), "perf.txt")
    await prepare_project(resp.project_id)

    stages = _parse_stage_ms(caplog.text)

    # ---- 剖析表（人工核对用）----
    total_llm_s = stats["calls"] * LLM_DELAY_S
    print("\n===== prepare 阶段剖析（LLM 固定延迟 %.2fs）=====" % LLM_DELAY_S)
    print(f"chapters={N_CHAPTERS} llm_calls={stats['calls']} llm_peak_concurrency={stats['peak']}")
    print(f"纯 LLM 时间（串行下限）= {total_llm_s:.2f}s")
    for k in ("split", "characters", "dialogues", "instructions", "voice_recs"):
        print(f"  stage {k:14s} = {stages.get(k, -1)} ms")
    print("===============================================")

    # ---- 守护断言 ----
    # 1) 全书 LLM 调用必须按预期发生（8 切片 + 2 对白批 + 指令批 + 推荐 + dedup）
    assert stats["calls"] >= 14, f"LLM 调用数异常少: {stats['calls']}"
    # 2) 角色识别切片并发必须真实生效（窗口 4 → 峰值 ≥ 3）
    assert stats["peak"] >= 3, (
        f"LLM 并发峰值仅 {stats['peak']} —— 切片并发被串行化（识别变慢的实锤）"
    )
    # 3) characters 阶段 8 片 / 并发 4 ≈ 2 轮 ≈ 2×delay（串行版实测 ≈ 8×delay）
    ch_ms = stages.get("characters", 0)
    assert ch_ms > 0, "未解析到 characters 阶段耗时，日志格式变化？"
    serial_lower = LLM_DELAY_S * 1000 * N_CHAR_SLICES
    assert ch_ms < serial_lower * 0.6, (
        f"characters 阶段 {ch_ms}ms 接近串行下限 {serial_lower:.0f}ms —— "
        f"H-11 切片并发未生效"
    )


# =====================================================================
# H-11 角色识别切片并发化
# =====================================================================

def _fake_extract_factory(call_log: list[str]):
    """造一个假的 extract_characters_with_llm：
    - 从 text 中抓「角色N号」标记 → 每章 1 个角色；
    - 按**最早出现的章号**决定延迟：偶数片 0.15s（慢）、奇数片 0.01s（快）
      → 奇数片先完成（乱序完成），验证结果顺序仍按片升序、不重不漏。
    """

    async def fake_extract(text: str):
        from backend.app.services.character import Character

        names = [int(n) for n in re.findall(r"角色(\d+)号", text)]
        call_log.append(text)
        first_ch = min(names) if names else 0
        # 3 章/桶 → 桶 idx = first_ch // 3
        await asyncio.sleep(0.15 if (first_ch // 3) % 2 == 0 else 0.01)
        uniq = sorted(set(names))
        return [
            Character(name=f"角色{n}号", gender="女", age="青年", personality="沉静")
            for n in uniq
        ]

    return fake_extract


async def _read_prog(project_id: str) -> dict:
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        p = await sess.get(Project, project_id)
        return json.loads(p.progress_json) if p and p.progress_json else {}


@pytest.mark.asyncio
async def test_h11_concurrent_completion_keeps_slice_order(_isolate_data_dir, monkeypatch):
    """H-11：切片乱序完成后，char_extract_raw_list 必须按片升序、不重不漏。

    旧实现（串行 for）天然有序；并发实现若直接 extend（乱序）或重复 extend
    （每片回调重放全部新完成片）都会在此暴露。
    """
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services import project as proj_mod
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
    )

    await init_db()
    monkeypatch.setattr(settings, "LLM_CHAR_EXTRACT_SLICE_SIZE", 9000)
    monkeypatch.setattr(settings, "CHAR_EXTRACT_CONCURRENCY", 4)

    call_texts: list[str] = []
    monkeypatch.setattr(
        proj_mod, "extract_characters_with_llm", _fake_extract_factory(call_texts)
    )

    resp = await create_project("H11-乱序顺序")
    pid = resp.project_id
    await import_file(pid, _book_text(24, with_marks=True).encode("utf-8"), "h11a.txt")
    await prepare_project(pid)

    detail = await get_project(pid)
    assert detail.prepare_progress, "prepare 应完成"
    prog = await _read_prog(pid)

    raw = prog.get("char_extract_raw_list") or []
    names = [c["name"] for c in raw]
    # 24 章 × 1 角色，不重不漏（章号从 1 起 → 角色1..24号）
    assert len(names) == 24, f"应有 24 个角色，实际 {len(names)}（重复合并 bug？）：{names}"
    assert len(set(names)) == 24, f"存在重复角色：{names}"
    # 顺序确定性：按章号（=片内升序 + 片升序）排列
    expected = [f"角色{i}号" for i in range(1, 25)]
    assert names == expected, (
        f"角色顺序应按片升序（与串行版一致），实际：{names}"
    )
    # checkpoint 完成片完整
    assert sorted(prog.get("char_slice_completed") or []) == list(range(8))


@pytest.mark.asyncio
async def test_h11_checkpoint_resume_skips_completed_slices(_isolate_data_dir, monkeypatch):
    """H-11：断点续跑——旧 checkpoint 已完成的片不重跑，新片结果正确合并在其后。

    手工构造「串行版跑完前 3 片」的 checkpoint（char_slice_mode=chapter），
    fake extract 记录本轮收到的文本：必须不含前 9 章标记（片 0-2 已完成跳过）。
    """
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Project
    from backend.app.core.config import settings
    from backend.app.services import project as proj_mod
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
    )

    await init_db()
    monkeypatch.setattr(settings, "LLM_CHAR_EXTRACT_SLICE_SIZE", 9000)
    monkeypatch.setattr(settings, "CHAR_EXTRACT_CONCURRENCY", 4)

    call_texts: list[str] = []
    monkeypatch.setattr(
        proj_mod, "extract_characters_with_llm", _fake_extract_factory(call_texts)
    )

    resp = await create_project("H11-断点续跑")
    pid = resp.project_id
    await import_file(pid, _book_text(24, with_marks=True).encode("utf-8"), "h11b.txt")

    # 手工写「串行版跑完片 0-2」的 checkpoint（3 章/桶 → 片 0-2 = 章 1..9 = 角色1..9号）
    factory = get_session_factory()
    async with factory() as sess:
        p = await sess.get(Project, pid)
        p.progress_json = json.dumps({
            "version": 1,
            "stage": "characters",
            "char_slice_mode": "chapter",
            "char_slice_total": 8,
            "char_slice_completed": [0, 1, 2],
            "char_slice_completed_n": 3,
            "char_extract_raw_list": [
                {"name": f"角色{i}号", "gender": "女", "age": "青年", "personality": "沉静"}
                for i in range(1, 10)
            ],
            "char_failed_slices": {},
            "char_full_text_len": 100,
            "dialogue_completed_chapters": [],
        }, ensure_ascii=False)
        await sess.commit()

    await prepare_project(pid)

    detail = await get_project(pid)
    assert detail.prepare_progress, "prepare 应完成"
    prog = await _read_prog(pid)

    # 本轮 fake extract 只应收到片 3-7（章 10..24），片 0-2（角色1..9号）跳过
    all_called = "\n".join(call_texts)
    for i in range(1, 10):
        assert f"角色{i}号" not in all_called, f"已完成片 0-2 的角色{i}号不应被重跑"
    for i in range(10, 25):
        assert f"角色{i}号" in all_called, f"未跑片应被识别：角色{i}号"

    # 最终角色 = 旧 9 个 + 新 15 个，按序合并且不重不漏
    raw = prog.get("char_extract_raw_list") or []
    names = [c["name"] for c in raw]
    assert names == [f"角色{i}号" for i in range(1, 25)], (
        f"断点续跑合并结果错误：{names}"
    )
    assert sorted(prog.get("char_slice_completed") or []) == list(range(8))


@pytest.mark.asyncio
async def test_h11_failed_slice_retried_on_rerun(_isolate_data_dir, monkeypatch):
    """H-11：某片重试耗尽记入 char_failed_slices，重跑 prepare 自动补跑成功。"""
    from backend.app.db.session import init_db
    from backend.app.core.config import settings
    from backend.app.services import project as proj_mod
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
    )

    await init_db()
    monkeypatch.setattr(settings, "LLM_CHAR_EXTRACT_SLICE_SIZE", 9000)
    monkeypatch.setattr(settings, "CHAR_EXTRACT_CONCURRENCY", 2)

    # 片 4（章13-15，first_ch=13）第 1 轮连续失败 3 次（默认 CHAR_EXTRACT_RETRY_COUNT=2
    # → 1 次首试 + 2 次重试全部失败）→ 记 failed_slices；第 2 轮恢复成功
    p4_failures = {"n": 0}
    inner = _fake_extract_factory([])

    async def flaky_extract(text: str):
        names = [int(n) for n in re.findall(r"角色(\d+)号", text)]
        first_ch = min(names) if names else 0
        if first_ch == 13 and p4_failures["n"] < 3:
            p4_failures["n"] += 1
            raise RuntimeError("mock slice failure")
        return await inner(text)

    monkeypatch.setattr(proj_mod, "extract_characters_with_llm", flaky_extract)

    resp = await create_project("H11-失败补跑")
    pid = resp.project_id
    await import_file(pid, _book_text(24, with_marks=True).encode("utf-8"), "h11c.txt")

    # 第 1 轮：片 4 失败被跳过，其余 7 片完成，prepare 仍应正常结束
    await prepare_project(pid)
    prog = await _read_prog(pid)
    assert sorted(prog.get("char_slice_completed") or []) == [0, 1, 2, 3, 5, 6, 7], (
        f"第 1 轮完成片应缺第 4 片：{sorted(prog.get('char_slice_completed') or [])}"
    )
    failed = prog.get("char_failed_slices") or {}
    assert "4" in failed, f"第 4 片应记录在 char_failed_slices：{failed.keys()}"

    # 第 2 轮：只补跑片 4，全部完成（p4_failures 已满 3 → 不再触发失败分支）
    await prepare_project(pid)
    prog = await _read_prog(pid)
    assert sorted(prog.get("char_slice_completed") or []) == list(range(8))
    raw = prog.get("char_extract_raw_list") or []
    names = [c["name"] for c in raw]
    assert names == [f"角色{i}号" for i in range(1, 25)], f"补跑后结果应完整按序：{names}"


# =====================================================================
# H-17 润色并发化（383 章实测 4h11m，prepare 最大热点）
# =====================================================================

def _h17_book_text(n: int) -> str:
    """H-17 专用书文本：每章 ~2800 字、含对白；第 3 章嵌 H17FAIL 标记。"""
    parts = []
    for i in range(1, n + 1):
        marker = "H17FAIL" if i == 3 else ""
        body = (
            "「你怎么了？」李明拍了拍她的肩膀。"
            "「没什么。」林若雪小声说，低下头继续走路。" + marker
        )
        parts.append(f"第{i}章 情景{i}\n\n{body * 80}")
    return "\n\n".join(parts)


@pytest.mark.asyncio
async def test_h17_polish_concurrency_speeds_up_stage(
    _isolate_data_dir, monkeypatch, caplog
):
    """H-17：润色并发真实生效（12 章 / 并发 4 ≈ 3 轮，串行版 12 轮）。

    旧实现逐章串行 await → 383 章 × ~39s/章 = 4h11m（用户实测）。
    固定延迟解耦调度结构与上游快慢：
    - 并发版 polish 阶段 ≈ ceil(12/4) × delay = 3 × delay
    - 串行下限 = 12 × delay；断言 < 60% 才算并发生效
    """
    from backend.app.ai import factory as ai_factory
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
    )

    # 同 test_prepare_stage_profiling：0.08s 让 sleep 主导全量套件下的调度抖动
    LLM_DELAY_S = 0.08
    N = 12

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)
    monkeypatch.setattr(settings, "POLISH_CONCURRENCY", 4)

    mock_llm = ai_factory._llm_instance
    orig_chat = mock_llm.chat_structured
    stats = {"polish_calls": 0, "in_flight": 0, "peak": 0}

    async def slow_chat(*a, **kw):
        prompt = a[0] if a else kw.get("prompt", "")
        is_polish = "---RAW TEXT START---" in prompt
        if is_polish:
            stats["polish_calls"] += 1
            stats["in_flight"] += 1
            stats["peak"] = max(stats["peak"], stats["in_flight"])
        try:
            if is_polish:
                await asyncio.sleep(LLM_DELAY_S)
            return await orig_chat(*a, **kw)
        finally:
            if is_polish:
                stats["in_flight"] -= 1

    monkeypatch.setattr(mock_llm, "chat_structured", slow_chat)
    caplog.set_level(logging.INFO, logger="backend.app.services.project")

    resp = await create_project("H17-并发提速")
    await import_file(resp.project_id, _h17_book_text(N).encode("utf-8"), "h17a.txt")
    await prepare_project(resp.project_id)

    stages = _parse_stage_ms(caplog.text)
    assert stats["polish_calls"] == N, (
        f"每章恰好一次润色调用，实际 {stats['polish_calls']}"
    )
    assert stats["peak"] >= 3, (
        f"润色并发峰值仅 {stats['peak']} —— H-17 并发被串行化（提速失效实锤）"
    )
    polish_ms = stages.get("polish", -1)
    assert polish_ms > 0, "未解析到 polish 阶段耗时，polish done 日志格式变化？"
    serial_lower = LLM_DELAY_S * 1000 * N
    assert polish_ms < serial_lower * 0.6, (
        f"polish 阶段 {polish_ms}ms 接近串行下限 {serial_lower:.0f}ms —— "
        f"H-17 润色并发未生效"
    )


@pytest.mark.asyncio
async def test_h17_polish_checkpoint_reuse_skips_llm(_isolate_data_dir, monkeypatch):
    """H-17：断点续跑——sidecar 已润色过的章不重跑 LLM，DB 文本直接复用。

    并发化后该语义必须原样保留：12 章预写 2 章进 sidecar → 只应剩 10 次润色调用。
    """
    from pathlib import Path as _P

    from backend.app.core.config import settings
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import ProjectChapter
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)

    resp = await create_project("H17-断点复用")
    pid = resp.project_id
    await import_file(pid, _h17_book_text(12).encode("utf-8"), "h17b.txt")

    # 预写 sidecar：第 0/1 章已润色过（模拟上次 prepare 中断）
    sidecar = _P(settings.DATA_DIR) / f"polish_{pid}.json"
    sidecar.write_text(
        json.dumps(
            {"0": "第0章-已润色文本", "1": "第1章-已润色文本"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mock_llm = _isolate_data_dir["llm"]
    await prepare_project(pid)

    prog = await _read_prog(pid)
    assert prog.get("polish_total") == 12
    assert prog.get("polish_reused_n") == 2, (
        f"sidecar 里 2 章应被复用，实际 reused={prog.get('polish_reused_n')}"
    )
    polish_calls = sum(
        1 for c in mock_llm.calls if "---RAW TEXT START---" in c["prompt"]
    )
    assert polish_calls == 10, (
        f"12 章中 2 章复用 sidecar，应只调 10 次润色，实际 {polish_calls}"
    )

    # DB 章节文本必须等于 sidecar 内容（复用生效，而不是被重跑覆盖）
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ProjectChapter)
                    .where(ProjectChapter.project_id == pid)
                    .order_by(ProjectChapter.idx)
                )
            )
            .scalars()
            .all()
        )
    assert rows[0].text == "第0章-已润色文本"
    assert rows[1].text == "第1章-已润色文本"


@pytest.mark.asyncio
async def test_h17_polish_failure_keeps_original_and_changed_persisted(
    _isolate_data_dir, monkeypatch, caplog
):
    """H-17：单章润色失败只保留原文（不阻塞 prepare），成功章持久化润色结果。

    覆盖 changed / failed 两条路径 + sidecar 收尾删除（prepare 完成后不残留）。
    """
    from pathlib import Path as _P

    from backend.app.core.config import settings
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import ProjectChapter
    from backend.app.services import polish as polish_mod
    from backend.app.services.polish import PolishResult
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    monkeypatch.setattr(settings, "POLISH_ENABLED", True)

    async def fake_polish(raw_text: str) -> PolishResult:
        if "H17FAIL" in raw_text:
            raise RuntimeError("mock polish failure")
        # 长度比例 ≈ 1 → 走 changed 路径
        return PolishResult(
            polished_text=raw_text + "【润】", is_reasonable=True, reason="ok"
        )

    monkeypatch.setattr(polish_mod, "polish_with_llm", fake_polish)
    caplog.set_level(logging.INFO, logger="backend.app.services.project")

    resp = await create_project("H17-失败语义")
    pid = resp.project_id
    await import_file(pid, _h17_book_text(12).encode("utf-8"), "h17c.txt")

    await prepare_project(pid)

    prog = await _read_prog(pid)
    assert prog.get("polish_changed_n") == 11, f"changed 应为 11：{prog}"
    assert prog.get("polish_failed_n") == 1, f"failed 应为 1：{prog}"

    # 失败章保留原文（H17FAIL 标记还在），成功章带【润】后缀
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ProjectChapter)
                    .where(ProjectChapter.project_id == pid)
                    .order_by(ProjectChapter.idx)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 12
    assert "H17FAIL" in rows[2].text and "【润】" not in rows[2].text, (
        "第 3 章（idx=2）失败后必须保留原文"
    )
    for i in (0, 1, 3, 5, 11):
        assert rows[i].text.endswith("【润】"), f"第 {i+1} 章应采用润色后文本"

    # 失败详情在日志里可见（章号按升序展示）
    assert "polish ch 3 失败" in caplog.text
    assert "章号=[3]" in caplog.text

    # sidecar 收尾删除（不残留半成品文件）
    sidecar = _P(settings.DATA_DIR) / f"polish_{pid}.json"
    assert not sidecar.exists(), "prepare 完成后 polish sidecar 应被删除"
