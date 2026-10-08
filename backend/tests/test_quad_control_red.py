"""批次 10「四控」回归测试（H-1 ~ H-5）。

- H-1 并发信号量热更新（LLM / TTS）
- H-2 prepare 可取消
- H-3 prepare 调用量估算透出
- H-4 build 实时速率 + 平滑 ETA + phase
- H-5 质检报告 + 可疑章重跑
"""
from __future__ import annotations

import pytest

pytest_plugins = ("pytest_asyncio",)


# 一本测试书：2 章 + 3 角色（与 test_project_e2e 相同的命中 Mock LLM 的文本）
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
"""


# =====================================================================
# H-1 并发信号量热更新
# =====================================================================

@pytest.mark.asyncio
async def test_h1_llm_sem_rebuilds_on_concurrency_change(_isolate_data_dir, monkeypatch):
    """改 settings.LLM_MAX_CONCURRENCY 后 _get_llm_sem() 必须返回新并发度的 sem。"""
    from backend.app.ai.providers.minimax import llm as llm_mod
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "LLM_MAX_CONCURRENCY", 1)
    sem1 = llm_mod._get_llm_sem()
    assert llm_mod._llm_sem_value == 1

    # 调到 3 → 立即重建（无需重启）
    monkeypatch.setattr(settings, "LLM_MAX_CONCURRENCY", 3)
    sem2 = llm_mod._get_llm_sem()
    assert sem2 is not sem1, "并发值变化后必须重建 sem（旧实现永远复用旧 sem）"
    assert llm_mod._llm_sem_value == 3
    assert sem2._value == 3

    # 值不变 → 复用同一个实例（避免抖动）
    sem3 = llm_mod._get_llm_sem()
    assert sem3 is sem2


@pytest.mark.asyncio
async def test_h1_tts_sem_rebuilds_on_concurrency_change(_isolate_data_dir, monkeypatch):
    """改 settings.TTS_MAX_CONCURRENCY 后 get_tts_sem() 必须返回新并发度的 sem。"""
    from backend.app.ai import factory
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "TTS_MAX_CONCURRENCY", 5)
    sem1 = factory.get_tts_sem()
    assert factory._tts_sem_value == 5

    monkeypatch.setattr(settings, "TTS_MAX_CONCURRENCY", 12)
    sem2 = factory.get_tts_sem()
    assert sem2 is not sem1
    assert factory._tts_sem_value == 12
    assert sem2._value == 12

    assert factory.get_tts_sem() is sem2


@pytest.mark.asyncio
async def test_h1_llm_global_concurrency_effective(_isolate_data_dir, monkeypatch):
    """H-1 性能守护：LLM_MAX_CONCURRENCY=4 时 8 个 chat_structured 的
    在飞峰值 ≥ 3、总耗时 ≈ 2×单延迟（而不是 8×串行）。

    背景：用户反馈「最新代码识别更慢」。Mock LLM 不经过全局 sem，
    本测试直接打生产路径 MiniMaxLLMProvider.chat_structured（含
    _get_llm_sem()），注入固定 HTTP 延迟，验证并发真实生效、
    无「sem 热更新导致串行化」回退。
    """
    import asyncio
    import time as _time

    import httpx
    from pydantic import BaseModel

    from backend.app.ai.providers.minimax.llm import MiniMaxLLMProvider
    from backend.app.core.config import settings

    class _PingOut(BaseModel):
        ok: bool

    CALL_DELAY = 0.2
    N_REQ = 8

    class _FakeResp:
        status_code = 200
        headers: dict = {}
        text = '{"ok": true}'

        def json(self):
            return {
                "request_id": "req_test",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "choices": [
                    {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
                ],
            }

    state = {"in_flight": 0, "peak": 0}

    async def _fake_post(self, url, **kw):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(CALL_DELAY)
        state["in_flight"] -= 1
        return _FakeResp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    async def _run_n(n_conc: int) -> float:
        state["peak"] = 0
        monkeypatch.setattr(settings, "LLM_MAX_CONCURRENCY", n_conc)
        prov = MiniMaxLLMProvider(api_key="x", base_url="http://test.local")
        t0 = _time.perf_counter()
        await asyncio.gather(
            *[prov.chat_structured("hi", _PingOut) for _ in range(N_REQ)]
        )
        return _time.perf_counter() - t0

    # 并发 4：8 请求 ≈ 2 批 × 0.2s ≈ 0.4s（放宽到 5×delay 防 CI 抖动）
    dt4 = await _run_n(4)
    assert state["peak"] >= 3, (
        f"并发=4 时在飞峰值应 ≥3，实际 {state['peak']} —— 全局 sem 未生效/被串行化"
    )
    assert dt4 < CALL_DELAY * 5, (
        f"8 请求在并发 4 下耗时 {dt4:.2f}s，接近串行（8×{CALL_DELAY}s）——识别速度回退实锤"
    )

    # 对照组：并发 1 → 应接近 8×delay（串行）
    dt1 = await _run_n(1)
    assert dt1 >= CALL_DELAY * N_REQ * 0.8, (
        f"并发=1 时总耗时 {dt1:.2f}s 应 ≈ {N_REQ}×{CALL_DELAY}s"
    )
    # 4 并发必须显著快于串行（≥2x 加速）
    assert dt4 < dt1 / 2, (
        f"并发 4 ({dt4:.2f}s) 未显著快于并发 1 ({dt1:.2f}s) —— 并发调度失效"
    )


# =====================================================================
# H-3 prepare 调用量估算透出
# =====================================================================

@pytest.mark.asyncio
async def test_h3_estimate_written_to_progress(_isolate_data_dir):
    """prepare 后 progress_json 必须带 llm_estimate，且经公开白名单透出。"""
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
    )
    from backend.app.db.session import init_db

    await init_db()
    resp = await create_project("H3-估算透出")
    await import_file(resp.project_id, _BOOK_TXT.encode("utf-8"), "h3.txt")
    await prepare_project(resp.project_id)

    detail = await get_project(resp.project_id)
    prog = detail.prepare_progress
    assert prog, "prepare 后应透出 prepare_progress"
    est = prog.get("llm_estimate")
    assert est, f"公开视图应包含 llm_estimate，实际 keys={sorted(prog.keys())}"
    assert est["chapters"] == 2
    # 2 章 → 角色识别 1 + 对白归属 1 + 指令 1 + 音色推荐 1 = 4 次
    assert est["total_calls"] == 4
    assert est["concurrency"] >= 1
    assert isinstance(est["est_hours"], (int, float))


def test_h3_estimate_dict_shape(_isolate_data_dir):
    """_log_prepare_llm_estimate 返回 dict 且字段齐全（H-3 改造后不再只返回 int）。"""
    from backend.app.services.project import _log_prepare_llm_estimate
    from backend.app.services.chapter import Chapter
    from backend.app.core.config import settings

    chapters = [
        Chapter(idx=0, title="第一章", text="字" * 1200),
        Chapter(idx=1, title="第二章", text="字" * 800),
    ]
    est = _log_prepare_llm_estimate("testproj00000000", chapters, settings)
    assert isinstance(est, dict)
    assert est["chapters"] == 2
    assert est["total_chars"] == 2000
    assert est["total_calls"] >= 3
    for key in ("char_calls", "dialogue_calls", "instruction_calls",
                "polish_calls", "concurrency", "est_hours"):
        assert key in est, f"估算 dict 缺字段 {key}"


# =====================================================================
# H-2 prepare 可取消
# =====================================================================

@pytest.mark.asyncio
async def test_h2_cancel_running_prepare(_isolate_data_dir, monkeypatch):
    """H-2 核心：prepare 运行中取消 → 不算失败，status 回 imported、stage=cancelled，
    登记清空；重新触发可断点续跑直至 ready。"""
    import asyncio

    from backend.app.services import project as proj_mod
    from backend.app.services.project import (
        create_project,
        import_file,
        trigger_prepare_project,
        cancel_prepare_project,
        get_project,
    )
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Project

    await init_db()

    # 门闩：把 mock LLM 的 chat_structured 挂住，保证取消时后台任务真的在跑
    from backend.app.ai import factory as ai_factory
    mock_llm = ai_factory._llm_instance
    assert mock_llm is not None, "conftest 应已 monkeypatch mock LLM"
    entered = asyncio.Event()
    release = asyncio.Event()
    orig_chat = mock_llm.chat_structured

    async def gated_chat(*a, **kw):
        entered.set()
        await release.wait()
        return await orig_chat(*a, **kw)

    monkeypatch.setattr(mock_llm, "chat_structured", gated_chat)

    resp = await create_project("H2-取消识别")
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "h2.txt")

    trig = await trigger_prepare_project(pid)
    assert trig.status == "preparing"

    # 等后台任务真正进入 LLM 调用（角色识别）
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert proj_mod._is_prepare_running_for(pid), "后台任务应在跑"
    old_task = proj_mod._prepare_running_tasks[pid]["task"]

    # ---- 取消 ----
    cancel = await cancel_prepare_project(pid)
    assert cancel.cancelled is True

    # 1) 状态回到 imported（不是 failed）
    factory_db = get_session_factory()
    async with factory_db() as s:
        p = await s.get(Project, pid)
        assert p.status == "imported", f"取消后应为 imported，实际 {p.status}"

    # 2) progress 透出 stage=cancelled + cancelled_at（非 last_error）
    detail = await get_project(pid)
    prog = detail.prepare_progress
    assert prog["stage"] == "cancelled"
    assert "cancelled_at" in prog, f"白名单应透出 cancelled_at，实际 keys={sorted(prog.keys())}"
    assert not prog.get("last_error"), "用户取消不是错误，不应写 last_error"

    # 3) 登记清空
    assert not proj_mod._is_prepare_running_for(pid)

    # 释放门闩，等旧任务完全退出（收尾不写 DB，kind=user）
    release.set()
    _done, pending = await asyncio.wait({old_task}, timeout=10)
    assert not pending, "被取消的旧任务应及时退出"

    # ---- 重新触发（断点续跑）→ 完成 ----
    monkeypatch.setattr(mock_llm, "chat_structured", orig_chat)
    trig2 = await trigger_prepare_project(pid)
    assert trig2.status == "preparing"

    for _ in range(200):  # 最多等 20s
        d = await get_project(pid)
        if d.status == "ready":
            break
        assert d.status == "preparing", f"重跑中状态异常: {d.status}"
        await asyncio.sleep(0.1)
    else:
        d = await get_project(pid)
        raise AssertionError(
            f"取消后重跑未完成: status={d.status} prog={d.prepare_progress}"
        )
    assert d.prepare_progress["stage"] == "done"


@pytest.mark.asyncio
async def test_h2_cancel_idle_project_idempotent(_isolate_data_dir):
    """H-2 幂等：没有在跑的任务时取消返回 cancelled=False，不改项目状态。"""
    from backend.app.services.project import (
        create_project,
        import_file,
        cancel_prepare_project,
        get_project,
    )
    from backend.app.db.session import init_db

    await init_db()
    resp = await create_project("H2-幂等取消")
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "h2b.txt")

    cancel = await cancel_prepare_project(pid)
    assert cancel.cancelled is False
    assert "没有正在运行" in cancel.message

    detail = await get_project(pid)
    assert detail.status == "imported", "幂等取消不应改变项目状态"
    # 没跑过 prepare → 没有 cancelled 痕迹
    prog = detail.prepare_progress or {}
    assert prog.get("stage") != "cancelled"


@pytest.mark.asyncio
async def test_h2_cancel_api_route(_isolate_data_dir, admin_token):
    """H-2 API 层：POST /projects/{id}/prepare/cancel 鉴权 + 幂等返回。"""
    from httpx import AsyncClient, ASGITransport
    from backend.app.main import app
    from backend.app.services.project import create_project, import_file
    from backend.app.db.session import init_db

    await init_db()
    resp = await create_project("H2-API取消")
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "h2c.txt")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # 未登录 → 401
        r0 = await client.post(f"/api/projects/{pid}/prepare/cancel")
        assert r0.status_code == 401, r0.text

        # 不存在的项目 → 404
        r404 = await client.post(
            "/api/projects/nonexistent/prepare/cancel", headers=headers
        )
        assert r404.status_code == 404, r404.text

        # 正常幂等取消 → 200 + cancelled=False
        r = await client.post(f"/api/projects/{pid}/prepare/cancel", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cancelled"] is False
        assert body["project_id"] == pid


# =====================================================================
# H-4 build 实时速率 + 平滑 ETA + phase
# =====================================================================

async def _h4_make_ready_project(name: str, fname: str) -> str:
    """建项目 → 导入 → prepare → 返回 project_id（ready）。"""
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
    )
    from backend.app.db.session import init_db

    await init_db()
    resp = await create_project(name)
    pid = resp.project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), fname)
    await prepare_project(pid)
    return pid


@pytest.mark.asyncio
async def test_h4_meta_phases_and_eta(_isolate_data_dir, monkeypatch):
    """H-4 核心：运行中 phase=synthesizing（含 remaining/eta 字段），
    终态 phase=done + remaining=0；status/list/detail 三处都透出 meta。"""
    import asyncio

    from backend.app.services.project import get_project_characters
    from backend.app.services.build import (
        start_build,
        get_build_status,
        get_build,
        list_builds,
    )

    pid = await _h4_make_ready_project("H4-phase与ETA", "h4.txt")
    chars = await get_project_characters(pid)
    assignments = {c.name: (c.assigned_voice_id or "male-qn-jingying") for c in chars}

    # 门闩：挂住 mock TTS 的 synthesize_to_bytes，保证轮询时 worker 还在合成
    mock_tts = _isolate_data_dir["tts"]
    entered = asyncio.Event()
    release = asyncio.Event()
    orig_synth = mock_tts.synthesize_to_bytes

    async def gated_synth(*a, **kw):
        entered.set()
        await release.wait()
        return await orig_synth(*a, **kw)

    monkeypatch.setattr(mock_tts, "synthesize_to_bytes", gated_synth)

    build_resp = await start_build(
        project_id=pid,
        voice_assignments=assignments,
        narrator_voice_id="male-qn-jingying",
        speed=1.0,
    )
    bid = build_resp.build_id

    # 等 worker 真正进入 TTS 合成
    await asyncio.wait_for(entered.wait(), timeout=10)

    # ---- 运行中：phase=synthesizing + remaining ----
    st = await get_build_status(bid)
    assert st.meta is not None, "运行中应透出 meta（progress_meta_json）"
    assert st.meta.phase == "synthesizing", f"运行中 phase 应为 synthesizing，实际 {st.meta.phase}"
    assert st.meta.remaining is not None and st.meta.remaining >= 1, (
        f"运行中 remaining 应 >=1，实际 {st.meta.remaining}"
    )

    # 释放门闩 → 跑完
    release.set()
    final = None
    for _ in range(120):  # 最多 60s
        st2 = await get_build_status(bid)
        final = st2.status
        if final in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert final == "success", f"build 应成功，实际 {final} msg={st2.progress_msg}"

    # ---- 终态：phase=done + remaining=0 + 分片进度 ----
    assert st2.meta is not None
    assert st2.meta.phase == "done", f"终态 phase 应为 done，实际 {st2.meta.phase}"
    assert st2.meta.remaining == 0
    assert st2.meta.shard_total is not None and st2.meta.shard_total >= 1
    assert st2.meta.shard_done == st2.meta.shard_total

    # list / detail 也透出
    bl = await list_builds(pid)
    item = next(b for b in bl if b.build_id == bid)
    assert item.meta is not None and item.meta.phase == "done"
    d = await get_build(pid, bid)
    assert d.meta is not None and d.meta.phase == "done"


@pytest.mark.asyncio
async def test_h4_rate_excludes_reused_chapters(_isolate_data_dir, monkeypatch):
    """H-4 速率口径：段级缓存全部命中（无真实 TTS 调用）的重建 build，
    速率样本为空 → rate/eta 为 None，不会虚高。

    场景构造：第一次 build 成功后 delete_build（清掉历史成功记录与章节产物，
    但段级缓存 sha256(voice+speed+text) 与 build 无关、仍然保留）→ 第二次
    start_build 因历史成功 build 已删除必然新建 → worker 段级缓存全命中。
    """
    import asyncio

    from backend.app.services.project import get_project_characters
    from backend.app.services.build import (
        start_build,
        get_build_status,
        delete_build,
    )

    pid = await _h4_make_ready_project("H4-速率口径", "h4b.txt")
    chars = await get_project_characters(pid)
    assignments = {c.name: (c.assigned_voice_id or "male-qn-jingying") for c in chars}

    # 第一次：真实合成（段缓存写入磁盘）
    b1 = await start_build(
        project_id=pid,
        voice_assignments=assignments,
        narrator_voice_id="male-qn-jingying",
        speed=1.0,
    )
    for _ in range(120):
        st1 = await get_build_status(b1.build_id)
        if st1.status in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert st1.status == "success", f"第一次 build 应成功，实际 {st1.status}"

    # 删除第一次 build（DB 历史成功记录 + 章节产物；段缓存独立目录保留）
    await delete_build(pid, b1.build_id)

    # 第二次：必然新建（历史成功 build 已删），段级缓存全部命中
    b2 = await start_build(
        project_id=pid,
        voice_assignments=assignments,
        narrator_voice_id="male-qn-jingying",
        speed=1.0,
    )
    assert b2.build_id != b1.build_id
    for _ in range(120):
        st2 = await get_build_status(b2.build_id)
        if st2.status in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert st2.status == "success", f"第二次 build 应成功，实际 {st2.status}"

    # 段级缓存命中 → 第二次 build 无真实合成章 → 速率样本为空
    assert st2.meta is not None
    assert st2.meta.rate_ch_per_min is None, (
        f"全缓存命中时 rate 应为 None（无真实合成样本），实际 {st2.meta.rate_ch_per_min}"
    )
    assert st2.meta.eta_secs is None


@pytest.mark.asyncio
async def test_h4_parse_progress_meta_tolerant(_isolate_data_dir):
    """H-4 解析容错：老 build（无 meta）/ 损坏 JSON → None，不抛异常。"""
    from backend.app.services.build import _parse_progress_meta
    from backend.app.db.models import Build

    b = Build(
        build_id="oldbuild000000000000000000000000",
        project_id="oldproject00000000000000000000",
        status="success",
        total_chapters=1,
        completed_chapters=1,
        narrator_voice_id="v",
        speed=1.0,
    )
    # 无 meta → None
    assert _parse_progress_meta(b) is None

    # 损坏 JSON → None
    b.progress_meta_json = "{not-json"
    assert _parse_progress_meta(b) is None

    # 非法类型（list）→ None
    b.progress_meta_json = "[1,2]"
    assert _parse_progress_meta(b) is None

    # 正常 dict → 模型
    import json
    b.progress_meta_json = json.dumps({"phase": "packaging", "shard_done": 2, "shard_total": 5})
    m = _parse_progress_meta(b)
    assert m is not None
    assert m.phase == "packaging"
    assert m.shard_done == 2
    assert m.shard_total == 5
    assert m.rate_ch_per_min is None


# =====================================================================
# H-5 质检报告 + 可疑章重跑
# =====================================================================

def test_h5_quality_summary_judgement(_isolate_data_dir):
    """H-5 判据（纯函数）：占位/极短音频必中可疑；正常语速不误报；failed 单列。"""
    from backend.app.services.build import _h5_quality_summary
    from backend.app.services.chapter import Chapter
    from backend.app.db.models import BuildArtifact

    chapters = [
        Chapter(idx=0, title="正常章", text="字" * 100),
        Chapter(idx=1, title="占位章", text="字" * 2500),
        Chapter(idx=2, title="失败章", text="字" * 50),
        Chapter(idx=3, title="无时长章", text="字" * 80),
    ]
    arts = [
        # 正常语速：100 字 / 20s = 5 字/s → 不可疑
        BuildArtifact(build_id="t", chapter_idx=0, title="正常章",
                      status="done", duration_ms=20000),
        # 1s 静音占位：2500 字 / 1s = 2500 字/s → 可疑
        BuildArtifact(build_id="t", chapter_idx=1, title="占位章",
                      status="done", duration_ms=1000),
        # failed 章 → 计入 failed_n，不进 suspicious
        BuildArtifact(build_id="t", chapter_idx=2, title="失败章",
                      status="failed", duration_ms=1000),
        # duration 缺失 → 计入 checked 但无法判定，不标可疑
        BuildArtifact(build_id="t", chapter_idx=3, title="无时长章",
                      status="done", duration_ms=None),
    ]
    q = _h5_quality_summary(chapters, arts)
    assert q["checked_n"] == 3, "3 个 done 章参与质检"
    assert q["failed_n"] == 1
    assert len(q["suspicious"]) == 1, "只有占位章可疑，正常/无时长章不误报"
    assert q["suspicious"][0]["chapter_idx"] == 1
    assert q["suspicious"][0]["chars"] == 2500
    assert q["suspicious"][0]["chars_per_sec"] == 2500.0

    # 上限 200：205 个可疑章只保留前 200（防爆 payload）
    many_arts = [
        BuildArtifact(build_id="t", chapter_idx=i, title="x",
                      status="done", duration_ms=1000)
        for i in range(205)
    ]
    many_chs = [Chapter(idx=i, title="x", text="字" * 100) for i in range(205)]
    q2 = _h5_quality_summary(many_chs, many_arts)
    assert len(q2["suspicious"]) == 200
    assert q2["checked_n"] == 205


@pytest.mark.asyncio
async def test_h5_retry_chapters_reruns_only_specified(_isolate_data_dir, monkeypatch):
    """H-5 端到端：chapters=[0] 指定重跑第 1 章（可疑章场景）。
    - 其余章按 B-5 硬链接复用（同 inode）；
    - 指定章重新合成；
    - 终态 quality_report 透出（BuildDetailResp.quality_report）。"""
    import asyncio
    import os
    from pathlib import Path

    from backend.app.services.project import get_project_characters
    from backend.app.services.build import (
        start_build,
        get_build_status,
        get_build,
        retry_failed_build,
    )
    from backend.app.core.config import settings

    pid = await _h4_make_ready_project("H5-指定章重跑", "h5.txt")
    chars = await get_project_characters(pid)
    assignments = {c.name: (c.assigned_voice_id or "male-qn-jingying") for c in chars}

    # 第一次 build：全部真实合成
    b1 = await start_build(
        project_id=pid,
        voice_assignments=assignments,
        narrator_voice_id="male-qn-jingying",
        speed=1.0,
    )
    for _ in range(120):
        st1 = await get_build_status(b1.build_id)
        if st1.status in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert st1.status == "success", f"第一次 build 应成功，实际 {st1.status}"

    audio_dir = Path(settings.AUDIO_DIR)
    ch1_src = audio_dir / f"build_{b1.build_id}_ch0001.mp3"
    assert ch1_src.is_file(), "源 build 的第 2 章 MP3 应存在"

    # 第一次 build（无失败、无占位）也应带质检摘要
    d1 = await get_build(pid, b1.build_id)
    assert d1.quality_report is not None, "终态应透出 quality_report"
    assert d1.quality_report.checked_n == 2
    assert d1.quality_report.failed_n == 0
    assert d1.quality_report.suspicious == [], "正常章不应被标可疑"

    # ---- 指定重跑 ch0（模拟可疑章重跑；源 build 无失败章也允许）----
    b2 = await retry_failed_build(b1.build_id, chapters=[0])
    assert b2.build_id != b1.build_id, "指定章重跑应生成新 build"

    # 新 build 建立即复用 ch1（completed=1）
    st_init = await get_build_status(b2.build_id)
    assert st_init.completed_chapters == 1, (
        f"非目标章应在创建时即复用（completed=1），实际 {st_init.completed_chapters}"
    )

    for _ in range(120):
        st2 = await get_build_status(b2.build_id)
        if st2.status in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert st2.status == "success", f"重跑 build 应成功，实际 {st2.status}"

    arts = {a.chapter_idx: a for a in st2.artifacts}
    assert arts[0].status == "done" and (arts[0].duration_ms or 0) > 0, "指定章应重新合成"
    assert arts[1].status == "done", "非目标章应复用"

    # B-5：ch1 是硬链接复用（与源文件同 inode），未重新合成
    ch1_new = audio_dir / f"build_{b2.build_id}_ch0001.mp3"
    assert ch1_new.is_file(), "复用章应已有 MP3"
    assert os.path.samefile(ch1_src, ch1_new), "复用章应是硬链接（同 inode），而非重合成"

    # 新 build 的质检摘要也透出
    d2 = await get_build(pid, b2.build_id)
    assert d2.quality_report is not None
    assert d2.quality_report.checked_n == 2


@pytest.mark.asyncio
async def test_h5_retry_chapters_validates_range(_isolate_data_dir):
    """H-5 越界校验：chapters 越界抛 ValueError（0-based，2 章书有效范围 0~1）。"""
    from backend.app.services.build import retry_failed_build

    # 构造一个假 source build id 即可触发前置校验前的大部分逻辑？
    # 不行 —— retry 需要真实 build。先建一个成功 build。
    pid = await _h4_make_ready_project("H5-越界", "h5b.txt")
    from backend.app.services.project import get_project_characters
    from backend.app.services.build import start_build, get_build_status
    import asyncio

    chars = await get_project_characters(pid)
    assignments = {c.name: (c.assigned_voice_id or "male-qn-jingying") for c in chars}
    b1 = await start_build(
        project_id=pid,
        voice_assignments=assignments,
        narrator_voice_id="male-qn-jingying",
        speed=1.0,
    )
    for _ in range(120):
        st1 = await get_build_status(b1.build_id)
        if st1.status in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert st1.status == "success"

    # 越界（全书只有 2 章 → 0~1 有效）
    with pytest.raises(ValueError, match="越界"):
        await retry_failed_build(b1.build_id, chapters=[5])

    # 合法值仍可用
    b2 = await retry_failed_build(b1.build_id, chapters=[1])
    assert b2.build_id != b1.build_id
