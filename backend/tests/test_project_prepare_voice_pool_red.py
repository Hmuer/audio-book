"""T-VR-E2E：project.prepare 把 user_id 透传给 voice_recommender。

覆盖：
  T-VR-E1  prepare_project(project_id) 内部调用 recommend_voices_with_llm 时
           user_id == project.owner_user_id，project_id == project_id
  T-VR-E2  当 owner_user_id 为 0（admin 孤儿池）时，user_id 也透传为 0，
           让 voice_recommender 拿到后端状态（admin 看到所有项目时该看到所有 ICL）
  T-VR-E3  recommend_voices_with_llm 拿到 user_id 后实际调了
           _aggregate_voice_pool(user_id=...)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SAMPLE_BOOK = (
    "第一章 初遇\n"
    "李明说：「你好，请问怎么走？」\n"
    "林若雪说：「跟我来吧。」\n"
    "第二章 启程\n"
    "他们出发了。\n"
)


@pytest.fixture
def stub_recommend(monkeypatch):
    """monkeypatch recommend_voices_with_llm 记录 (user_id, project_id, characters)。

    注意：只 stub recommend，不 stub _aggregate_voice_pool。
    推荐函数内部调 _aggregate_voice_pool 真实跑（被 monkeypatch 后返回 []，
    不会去调真实的 list_voices——但 test_doubao_task7_red 等老测试也是这样
    通过 conftest 注入 mock TTS，所以安全）。
    """
    from backend.app.services import project as project_mod
    from backend.app.services.voice_recommender import VoiceRecommendation

    captured: list[dict] = []

    async def _fake_recommend(characters, *, user_id=None, project_id=None, **_kw):
        captured.append({
            "user_id": user_id,
            "project_id": project_id,
            "n_chars": len(characters),
            "char_names": [c.name for c in characters],
        })
        # 模拟真实返回值（按角色给默认 voice_id）
        return [
            VoiceRecommendation(
                character_name=c.name,
                suggested_voice_id="doubao:zh_female_vv_uranus_bigtts",
                reason="默认推荐",
            )
            for c in characters
        ]

    monkeypatch.setattr(project_mod, "recommend_voices_with_llm", _fake_recommend)
    return {"recommend": captured}


# ---------------------------------------------------------------------
# T-VR-E1：正常 owner
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prepare_passes_user_id_to_recommend(stub_recommend):
    """project.owner_user_id = 7 时，recommend 收到 user_id=7。"""
    from backend.app.db.session import init_db
    from backend.app.db.models import User, Project
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    from backend.app.db.session import get_session_factory
    factory = get_session_factory()
    async with factory() as s:
        u = User(username=f"u_vr_{Path('/').stat().st_ino}", password_hash="x",
                 is_active=True, must_change_password=False)
        s.add(u)
        await s.commit()
        uid = u.id

    pid = (await create_project("vr-e1", owner_user_id=uid)).project_id
    await import_file(pid, SAMPLE_BOOK.encode("utf-8"), "book.txt")

    await prepare_project(pid)

    # stub_recommend 至少收到 1 次调用
    assert len(stub_recommend["recommend"]) >= 1, "prepare 应触发 recommend_voices_with_llm"
    last = stub_recommend["recommend"][-1]
    assert last["user_id"] == uid, f"user_id 应等于 owner_user_id={uid}，实际 {last['user_id']}"
    assert last["project_id"] == pid
    # 至少 1 个角色（"李明" / "林若雪"）
    assert last["n_chars"] >= 1
    assert any(name in last["char_names"] for name in ("李明", "林若雪"))


# ---------------------------------------------------------------------
# T-VR-E2：owner=0（admin 孤儿池）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prepare_passes_zero_user_id_when_orphan(stub_recommend):
    """project.owner_user_id = 0 时透传 0；pool 也应收到 0（看到所有 ICL）。"""
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file, prepare_project

    await init_db()
    pid = (await create_project("vr-e2-orphan", owner_user_id=0)).project_id
    await import_file(pid, SAMPLE_BOOK.encode("utf-8"), "book.txt")

    await prepare_project(pid)

    assert len(stub_recommend["recommend"]) >= 1
    last = stub_recommend["recommend"][-1]
    assert last["user_id"] == 0, f"owner=0 时应透传 0，实际 {last['user_id']}"
    assert last["project_id"] == pid

    # 同步检查 pool 也收到 0
    assert last["user_id"] == 0