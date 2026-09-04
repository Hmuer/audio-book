"""
项目制端到端回归测试（新架构：Project → Build → BuildArtifact）。

覆盖完整生命周期：
1. create_project → import_file → prepare_project → 项目状态机
2. 角色 + 章节识别结果落库
3. update_character_voice 修改音色
4. start_build → 后台合成 → 轮询 → success
5. ZIP 产出 + 章节级 MP3 产出
6. delete_build 磁盘清理
7. delete_project 级联清理

测试用 Mock LLM/TTS Provider（不依赖真实 API Key），关注状态机和数据一致性。
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path

import pytest

pytest_plugins = ("pytest_asyncio",)


# 一本测试书：2 章 + 3 角色 + 3 段对白（命中 Mock LLM 的名字识别 + 对白归属分支）
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


@pytest.mark.asyncio
async def test_project_full_lifecycle(_isolate_data_dir):
    """端到端：创建 → 导入 → 识别 → 改音色 → 启动 build → 完成 → 清理。"""
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
        list_projects,
        update_project,
        delete_project,
        get_project_chapters,
        get_project_characters,
        update_character_voice,
    )
    from backend.app.services.build import (
        start_build,
        get_build,
        list_builds,
        get_build_status,
        delete_build,
    )
    from backend.app.db.session import init_db
    from backend.app.core.config import settings

    await init_db()

    # ============== 1. 创建项目 ==============
    resp = await create_project("测试项目-小城雨夜")
    project_id = resp.project_id
    assert resp.name == "测试项目-小城雨夜"
    assert resp.status == "draft"
    assert resp.cover_color is not None and resp.cover_color.startswith("#")

    # ============== 2. 列表里能看到 ==============
    projects = await list_projects()
    assert any(p.project_id == project_id for p in projects)

    # ============== 3. 导入文件 ==============
    file_bytes = _BOOK_TXT.encode("utf-8")
    resp_imp = await import_file(project_id, file_bytes, "小城雨夜.txt")
    assert resp_imp.status == "imported"
    assert resp_imp.source_filename == "小城雨夜.txt"
    assert resp_imp.source_file_size == len(file_bytes)
    assert resp_imp.book_title == "小城雨夜"  # 文件名 stem 推断
    # 磁盘上有源文件
    assert Path(resp_imp.source_filename).name  # 只是确保不抛

    # ============== 4. 触发 prepare（章节 + 角色 + 对白 + 音色）==============
    prep = await prepare_project(project_id)
    assert prep.total_chapters == 2, f"应识别出 2 章，实际 {prep.total_chapters}"
    chapter_titles = [c.title for c in prep.chapters]
    assert "初遇" in chapter_titles[0], f"第一章标题不对: {chapter_titles}"
    assert "告别" in chapter_titles[1], f"第二章标题不对: {chapter_titles}"
    # Mock LLM 识别到 3 角色（林若雪 / 李明 / 王大爷）
    char_names = [c["name"] for c in prep.characters]
    assert set(["林若雪", "李明", "王大爷"]).issubset(set(char_names)), (
        f"应识别出 3 个角色，实际 {char_names}"
    )
    # 音色推荐也返回了
    assert len(prep.voice_recommendations) >= 1

    # ============== 5. 项目状态 → ready，章节数写入 ==============
    detail = await get_project(project_id)
    assert detail.status == "ready", f"prepare 后应为 ready，实际 {detail.status}"
    assert detail.chapter_count == 2
    assert len(detail.chapters) == 2
    assert len(detail.characters) >= 3
    # 角色 1 应该已经预填 assigned_voice_id（来自 Mock 推荐）
    assert any(c.assigned_voice_id for c in detail.characters), (
        f"至少一个角色应有推荐音色: {[(c.name, c.assigned_voice_id) for c in detail.characters]}"
    )
    # last_build 应该是 None（还没启动 build）
    assert detail.last_build is None

    # ============== 6. update_project 改名 / 改描述 ==============
    upd = await update_project(
        project_id, name="小城雨夜（修订版）", description="测试描述", tags="test"
    )
    assert upd.name == "小城雨夜（修订版）"
    detail2 = await get_project(project_id)
    assert detail2.description == "测试描述"
    assert detail2.tags == "test"

    # ============== 7. get_project_chapters / get_project_characters ==============
    ch_list = await get_project_chapters(project_id)
    assert len(ch_list) == 2
    assert all(c.text_len > 0 for c in ch_list), "章节摘要 text_len 应大于 0"

    chars = await get_project_characters(project_id)
    assert len(chars) >= 3

    # ============== 8. update_character_voice 改音色 ==============
    target = next(c for c in chars if c.name == "李明")
    new_voice = "male-qn-badao"
    upd_char = await update_character_voice(project_id, target.id, new_voice)
    assert upd_char.assigned_voice_id == new_voice
    # 再查一次确认持久化
    chars2 = await get_project_characters(project_id)
    target2 = next(c for c in chars2 if c.id == target.id)
    assert target2.assigned_voice_id == new_voice

    # ============== 9. 启动 build ==============
    assignments = {c.name: (c.assigned_voice_id or "male-qn-jingying") for c in chars2}
    # 李明改成 new_voice
    assignments["李明"] = new_voice
    narrator = "male-qn-jingying"

    build_resp = await start_build(
        project_id=project_id,
        voice_assignments=assignments,
        narrator_voice_id=narrator,
        speed=1.0,
    )
    build_id = build_resp.build_id
    assert build_resp.status in ("queued", "running"), (
        f"build 启动后应 queued/running，实际 {build_resp.status}"
    )
    assert build_resp.total_chapters == 2

    # ============== 10. 双重去重：同项目再 start_build 应返回当前 build ==============
    dup = await start_build(
        project_id=project_id,
        voice_assignments=assignments,
        narrator_voice_id=narrator,
    )
    # 由于第一次还在 running，应返回同一个 build_id（或至少不新建）
    # 内存锁可能瞬间释放（worker 已完成），DB 检查也是 active → 应返回 active
    assert dup.build_id == build_id, (
        f"重复 start 应去重返回当前 build，实际 dup={dup.build_id} orig={build_id}"
    )

    # ============== 11. 轮询 build 直到完成 ==============
    final_status = None
    for _ in range(60):  # 最多等 30s（mock TTS 静音合成极快）
        st = await get_build_status(build_id)
        final_status = st.status
        if st.status in ("success", "failed"):
            break
        await asyncio.sleep(0.5)

    assert final_status == "success", (
        f"build 应成功，实际 {final_status}; progress_msg={st.progress_msg}"
    )
    assert st.completed_chapters == st.total_chapters == 2, (
        f"应完成 2/2 章，实际 {st.completed_chapters}/{st.total_chapters}"
    )
    # artifacts 状态
    done_arts = [a for a in st.artifacts if a.status == "done"]
    assert len(done_arts) == 2, (
        f"应有 2 个 done artifact，实际 "
        f"{[(a.chapter_idx, a.status) for a in st.artifacts]}"
    )
    # 每个 artifact 都有 audio_url 和 duration
    for a in done_arts:
        assert a.audio_url is not None and a.audio_url.startswith("/media/")
        assert a.duration_ms is not None and a.duration_ms > 0

    # ============== 12. get_build 详情：ZIP URL / 总大小 / 总时长 ==============
    detail_b = await get_build(project_id, build_id)
    assert detail_b.zip_url is not None
    assert detail_b.total_size_kb is not None and detail_b.total_size_kb > 0
    assert detail_b.total_duration_sec is not None and detail_b.total_duration_sec > 0

    # ============== 13. 磁盘文件：每章 MP3 + ZIP ==============
    audio_dir = Path(settings.AUDIO_DIR)
    # 章节文件命名 build_{build_id}_ch{idx:04d}.mp3
    ch0 = audio_dir / f"build_{build_id}_ch0000.mp3"
    ch1 = audio_dir / f"build_{build_id}_ch0001.mp3"
    assert ch0.is_file() and ch0.stat().st_size > 0
    assert ch1.is_file() and ch1.stat().st_size > 0
    zip_path = audio_dir / f"build_{build_id}_all.zip"
    assert zip_path.is_file() and zip_path.stat().st_size > 0, "ZIP 文件应已生成"

    # ============== 14. 项目详情 last_build 已写入 ==============
    detail3 = await get_project(project_id)
    assert detail3.last_build is not None
    assert detail3.last_build.build_id == build_id
    assert detail3.last_build.status == "success"

    # ============== 15. list_builds 历史 ==============
    bl = await list_builds(project_id)
    assert len(bl) >= 1
    assert any(b.build_id == build_id for b in bl)

    # ============== 16. delete_build：DB + 磁盘 ==============
    await delete_build(project_id, build_id)
    bl2 = await list_builds(project_id)
    assert all(b.build_id != build_id for b in bl2), "build 列表应已不含被删 build"
    # 磁盘文件已删
    assert not ch0.is_file(), f"删除后章节 MP3 仍存在: {ch0}"
    assert not ch1.is_file(), f"删除后章节 MP3 仍存在: {ch1}"
    assert not zip_path.is_file(), f"删除后 ZIP 仍存在: {zip_path}"

    # ============== 17. delete_project：级联清理 ==============
    # 再起一个 build 验证级联删除
    b2 = await start_build(
        project_id=project_id,
        voice_assignments=assignments,
        narrator_voice_id=narrator,
    )
    # 等它完成（确保磁盘文件生成）
    for _ in range(60):
        s = await get_build_status(b2.build_id)
        if s.status in ("success", "failed"):
            break
        await asyncio.sleep(0.5)
    b2_ch0 = audio_dir / f"build_{b2.build_id}_ch0000.mp3"
    b2_zip = audio_dir / f"build_{b2.build_id}_all.zip"
    assert b2_ch0.is_file()
    assert b2_zip.is_file()

    # 源文件路径
    source_path = Path(detail3.source_filename)  # 仅作为存在性参考
    # 真实源文件位置：uploads/proj_{project_id}.txt
    uploads_dir = Path(settings.DATA_DIR) / "uploads"
    proj_source = uploads_dir / f"proj_{project_id}.txt"
    assert proj_source.is_file(), f"源文件应在 uploads/: {proj_source}"

    # 删除项目
    await delete_project(project_id)

    # 列表里不再有
    pl = await list_projects()
    assert all(p.project_id != project_id for p in pl), "项目删除后不应出现在列表"

    # 磁盘级联清理
    assert not b2_ch0.is_file(), "项目删除应级联删 build 章节 MP3"
    assert not b2_zip.is_file(), "项目删除应级联删 build ZIP"
    assert not proj_source.is_file(), "项目删除应删源文件"

    # get_project 应抛 ValueError
    with pytest.raises(ValueError):
        await get_project(project_id)


@pytest.mark.asyncio
async def test_project_prepare_without_import_fails(_isolate_data_dir):
    """prepare 之前必须先 import，否则报 RuntimeError + 项目置 failed。"""
    from backend.app.services.project import create_project, prepare_project, get_project
    from backend.app.db.session import init_db

    await init_db()
    resp = await create_project("空项目")
    project_id = resp.project_id

    # 没 import 就 prepare → RuntimeError
    with pytest.raises(RuntimeError):
        await prepare_project(project_id)

    # 项目状态应被标 failed
    detail = await get_project(project_id)
    assert detail.status == "failed", (
        f"prepare 失败应置项目为 failed，实际 {detail.status}"
    )


@pytest.mark.asyncio
async def test_project_prepare_idempotent_reimport(_isolate_data_dir):
    """重复 prepare 会清旧识别数据再写新数据（不残留）。"""
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
        get_project,
    )
    from backend.app.db.session import init_db
    from backend.app.db.models import ProjectCharacter, ProjectDialogue
    from sqlalchemy import select
    from backend.app.db.session import get_session_factory

    await init_db()
    pid = (await create_project("幂等测试")).project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "test.txt")

    prep1 = await prepare_project(pid)
    assert len(prep1.characters) >= 3
    # 第一次：3 角色 + 至少几条对白
    factory = get_session_factory()
    async with factory() as s:
        c1 = len(list((await s.execute(
            select(ProjectCharacter).where(ProjectCharacter.project_id == pid)
        )).scalars().all()))
        d1 = len(list((await s.execute(
            select(ProjectDialogue).where(ProjectDialogue.project_id == pid)
        )).scalars().all()))

    assert c1 >= 3
    assert d1 >= 1

    # 第二次 prepare：应先清旧再写新，数量不变（同源文件）
    prep2 = await prepare_project(pid)
    assert len(prep2.characters) == len(prep1.characters)
    async with factory() as s:
        c2 = len(list((await s.execute(
            select(ProjectCharacter).where(ProjectCharacter.project_id == pid)
        )).scalars().all()))
        d2 = len(list((await s.execute(
            select(ProjectDialogue).where(ProjectDialogue.project_id == pid)
        )).scalars().all()))
    assert c2 == c1, "二次 prepare 角色数应一致（清旧再写新）"
    assert d2 == d1, "二次 prepare 对白数应一致（清旧再写新）"

    # 项目仍 ready
    detail = await get_project(pid)
    assert detail.status == "ready"


@pytest.mark.asyncio
async def test_build_deduplication_returns_existing(_isolate_data_dir):
    """重复 start_build：第二次返回的应等于第一次的 build_id（同一 project 同期只一个）。"""
    from backend.app.services.project import (
        create_project, import_file, prepare_project,
    )
    from backend.app.services.build import start_build, get_build_status
    from backend.app.db.session import init_db

    await init_db()
    pid = (await create_project("去重测试")).project_id
    await import_file(pid, _BOOK_TXT.encode("utf-8"), "dup.txt")
    await prepare_project(pid)

    b1 = await start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="male-qn-jingying"
    )
    # 不等完成，立即第二次 start → 应返回同一个 build_id
    b2 = await start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="male-qn-jingying"
    )
    assert b2.build_id == b1.build_id, (
        f"重复 start_build 应去重，b1={b1.build_id} b2={b2.build_id}"
    )

    # 等 b1 跑完
    for _ in range(60):
        s = await get_build_status(b1.build_id)
        if s.status in ("success", "failed"):
            break
        await asyncio.sleep(0.5)
    assert s.status == "success"
