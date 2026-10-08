"""H-13：prepare / build 各阶段耗时记录与透出（页面上实时显示总耗时）。

守护三件事：
1. _stage_timing_touch 计时语义：首次进入记 started_ms（幂等不覆盖，断点续跑
   保留历史起点 → elapsed 是「该阶段累计耗时」口径）；end=True 写死 elapsed_ms；
   非白名单阶段忽略。
2. _prepare_progress_public_view 白名单必须透出 stage_timings / server_now_ms
   —— 白名单是显式列表，后续加字段时最容易把这两个漏掉，前端实时耗时直接消失。
3. build 侧 _h4_progress_meta_json 写入 phase_started_ms / completed_timings /
   server_now_ms，且形状能被 BuildProgressMeta 解析（前端 api.ts 类型对齐）。
"""
from __future__ import annotations

import json
import time
from collections import deque

from app.services.build import BuildProgressMeta, _h4_progress_meta_json
from app.services.project import (
    _PREPARE_TIMED_STAGES,
    _prepare_progress_public_view,
    _stage_timing_touch,
    _stamp_server_now,
)


def test_stage_timing_touch_start_is_idempotent_keeps_history():
    """首次进入记 started_ms；断点续跑再次进入**不覆盖**历史起点。

    这是「累计耗时」口径的关键：阶段上次挂掉前跑掉的 30 分钟必须计入，
    否则热点定位会把长耗时阶段看成短的。
    """
    prog: dict = {}
    _stage_timing_touch(prog, "characters")
    first_started = prog["stage_timings"]["characters"]["started_ms"]
    assert isinstance(first_started, int) and first_started > 0
    # 模拟断点续跑：同一阶段再次 touch（未 end）
    time.sleep(0.002)
    _stage_timing_touch(prog, "characters")
    assert prog["stage_timings"]["characters"]["started_ms"] == first_started
    # 进行中：不写 elapsed_ms（前端用 server_now_ms 实时算）
    assert "elapsed_ms" not in prog["stage_timings"]["characters"]


def test_stage_timing_touch_end_writes_final_elapsed():
    """end=True 写死 elapsed_ms ≈ now - started_ms（此后不再变化）。"""
    prog: dict = {}
    _stage_timing_touch(prog, "split")  # start
    time.sleep(0.02)
    _stage_timing_touch(prog, "split", end=True)  # end
    st = prog["stage_timings"]["split"]
    assert isinstance(st.get("elapsed_ms"), int)
    assert 15 <= st["elapsed_ms"] <= 10_000  # sleep 20ms，留宽容差
    # 再次 end 不改变（写死后幂等：elapsed 只随 started 口径重算一次）
    before = st["elapsed_ms"]
    _stage_timing_touch(prog, "split", end=True)
    assert prog["stage_timings"]["split"]["elapsed_ms"] == before


def test_stage_timing_touch_ignores_unknown_stage():
    """非 _PREPARE_TIMED_STAGES 阶段不产生计时条目（防 progress 膨胀）。"""
    prog: dict = {}
    _stage_timing_touch(prog, "idle")
    _stage_timing_touch(prog, "finished", end=True)
    assert "stage_timings" not in prog or "idle" not in prog.get("stage_timings", {})
    # 白名单本身必须覆盖 7 个 prepare 阶段（少了任何一个 → 该阶段无耗时显示）
    for stage in ("split", "polish", "characters", "dedup", "dialogues", "instructions", "voice_recs"):
        assert stage in _PREPARE_TIMED_STAGES


def test_stamp_server_now_and_public_view_expose():
    """server_now_ms 刷新 + 白名单透出守护（漏字段 = 前端实时耗时消失）。"""
    prog: dict = {"stage": "characters", "char_completed_n": 3}
    _stamp_server_now(prog)
    now_ms = prog["server_now_ms"]
    assert isinstance(now_ms, int)
    assert abs(now_ms - int(time.time() * 1000)) < 60_000  # epoch ms 量级
    _stage_timing_touch(prog, "characters")

    view = _prepare_progress_public_view(prog)
    assert view is not None
    assert "stage_timings" in view, "白名单漏 stage_timings → 前端看不到各阶段耗时"
    assert "server_now_ms" in view, "白名单漏 server_now_ms → 前端无法校准时钟"
    assert "characters" in view["stage_timings"]
    # 老数据（无 stage_timings）→ 透出层不炸、字段缺省（前端显示 —）
    old_view = _prepare_progress_public_view({"stage": "split", "split_done": 1})
    assert old_view is not None and "stage_timings" not in old_view


def test_build_meta_json_carries_phase_timing_fields():
    """build 侧 meta JSON 含 H-13 三字段，且形状可被 BuildProgressMeta 解析。"""
    raw = _h4_progress_meta_json(
        phase="synthesizing",
        processed=2,
        target_total=10,
        recent_ts=deque([time.time() - 60, time.time()], maxlen=50),
        phase_started_ms=int(time.time() * 1000) - 123_456,
        completed_timings=None,
    )
    m = json.loads(raw)
    assert isinstance(m["phase_started_ms"], int) and m["phase_started_ms"] > 0
    assert m["completed_timings"] == {}  # 进行中无已完成 phase → 空对象（非 null）
    assert isinstance(m["server_now_ms"], int) and m["server_now_ms"] > 0

    meta = BuildProgressMeta.model_validate(m)
    assert meta.phase == "synthesizing"
    assert meta.phase_started_ms == m["phase_started_ms"]
    assert meta.server_now_ms == m["server_now_ms"]
    assert meta.completed_timings == {}

    # 终态形状：packaging 进行中 + synthesizing 已完成
    raw2 = _h4_progress_meta_json(
        phase="packaging",
        processed=10,
        target_total=10,
        recent_ts=deque([time.time()], maxlen=50),
        phase_started_ms=int(time.time() * 1000) - 5_000,
        completed_timings={"synthesizing": 730_000},
    )
    meta2 = BuildProgressMeta.model_validate(json.loads(raw2))
    assert meta2.completed_timings == {"synthesizing": 730_000}
    assert meta2.phase == "packaging" and meta2.phase_started_ms is not None


# =====================================================================
# H-13b：计时 touch 与 checkpoint 的边界守护。
#
# 曾引入的回归：让 _read_write_progress_timing 顺带把 stage 写成 "split"，
# 结果「上次中断在 characters 的断点续跑」在重跑经过 split touch 后 stage 被
# 改写 → characters 阶段的续跑判断（stage in ("characters",...)）失效 →
# 已完成切片被静默重跑（test_h11_checkpoint_resume_skips_completed_slices 红灯）。
# 结论：touch 只写 stage_timings / server_now_ms，**绝不碰 stage**；
# split/polish 期间「页面卡在初始化」的显示问题由前端从 stage_timings 推断解决。
# =====================================================================

async def test_timing_touch_never_overwrites_stage(_isolate_data_dir):
    """计时 touch 不得改写 prog["stage"] —— 它是 checkpoint 续跑判断的依据。

    场景还原：断点续跑（stage=characters 的 checkpoint）→ 重跑经过
    split/polish touch → stage 必须保持 "characters"（一旦被写成 "split"，
    characters 阶段判断不命中 → 已完成切片被重跑）。
    """
    import uuid
    from sqlalchemy import select
    from app.db.models import Project, User
    from app.db.session import init_db, get_session_factory
    from app.services.auth import seed_admin_user
    from app.services.project import _read_write_progress_timing

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    checkpoint = json.dumps({
        "version": 1,
        "stage": "characters",
        "char_slice_mode": "chapter",
        "char_slice_completed": [0, 1, 2],
        "char_extract_raw_list": [{"name": "角色1号"}],
    }, ensure_ascii=False)
    async with factory() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(project_id=pid, name="T13b", status="preparing",
                      owner_user_id=admin.id, progress_json=checkpoint))
        await s.commit()

    # 重跑经过 split / polish 的计时 touch —— stage 与 checkpoint 字段必须原样
    await _read_write_progress_timing(pid, "split", end=False)
    await _read_write_progress_timing(pid, "split", end=True)
    await _read_write_progress_timing(pid, "polish", end=False)
    await _read_write_progress_timing(pid, "polish", end=True)  # 心跳同路径（幂等 touch）
    async with factory() as s:
        prog = json.loads((await s.get(Project, pid)).progress_json)
    assert prog["stage"] == "characters", (
        "touch 改写 stage 会破坏 characters 续跑判断 → 已完成切片被重跑"
    )
    assert prog["char_slice_completed"] == [0, 1, 2]
    assert prog["char_extract_raw_list"] == [{"name": "角色1号"}]
    # timing 本身正常落库：split/polish 有起止，characters 未动
    assert isinstance(prog["stage_timings"]["split"]["elapsed_ms"], int)
    assert isinstance(prog["stage_timings"]["polish"]["elapsed_ms"], int)
    assert "characters" not in prog["stage_timings"]
    assert isinstance(prog["server_now_ms"], int)


async def test_timing_touch_refreshes_server_now(_isolate_data_dir):
    """touch / 心跳必须刷 server_now_ms（前端时钟校准基准），且 started 幂等。"""
    import time as _time
    import uuid
    from sqlalchemy import select
    from app.db.models import Project, User
    from app.db.session import init_db, get_session_factory
    from app.services.auth import seed_admin_user
    from app.services.project import _read_write_progress_timing

    await init_db()
    await seed_admin_user()
    factory = get_session_factory()
    pid = uuid.uuid4().hex
    async with factory() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(project_id=pid, name="T13b2", status="preparing",
                      owner_user_id=admin.id,
                      progress_json='{"version": 1, "stage": "start"}'))
        await s.commit()

    await _read_write_progress_timing(pid, "polish", end=False)
    async with factory() as s:
        prog = json.loads((await s.get(Project, pid)).progress_json)
    started = prog["stage_timings"]["polish"]["started_ms"]
    now1 = prog["server_now_ms"]
    assert isinstance(now1, int) and abs(now1 - int(_time.time() * 1000)) < 60_000

    _time.sleep(0.005)
    # 再次 touch（polish 心跳每 10 章走一次同路径）：started 不覆盖（累计口径），
    # server_now_ms 刷新
    await _read_write_progress_timing(pid, "polish", end=False)
    async with factory() as s:
        prog = json.loads((await s.get(Project, pid)).progress_json)
    assert prog["stage_timings"]["polish"]["started_ms"] == started
    assert prog["server_now_ms"] >= now1
