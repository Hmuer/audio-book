"""部分识别结果可见，重触发只补缺失章。"""
import asyncio
import json
import zipfile

import pytest


def test_untitled_book_placeholder_titles_are_not_double_spoken():
    from backend.app.services.chapter import Chapter, _title_tts_text

    assert _title_tts_text(Chapter(idx=0, title="正文", text="正文内容")) == ""
    assert _title_tts_text(Chapter(idx=1, title="第 2 部分", text="正文内容")) == "第 2 部分"
    assert _title_tts_text(Chapter(idx=1, title="第二章 原题", text="正文内容")) == "第二章 原题"


@pytest.mark.asyncio
async def test_partial_dialogue_resume_keeps_completed_work(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.models import Project
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services import project as service

    await init_db()
    monkeypatch.setattr(settings, "DIALOGUE_BATCH_CHAPTERS", 1)
    monkeypatch.setattr(settings, "DIALOGUE_BATCH_RETRY_COUNT", 0)
    pid = (await service.create_project("补跑验证")).project_id
    text = "\n".join(
        f"第{i}章 测试\n" + "林若雪说：『你好。』李明答：『好。』" * 30
        for i in range(1, 4)
    )
    await service.import_file(pid, text.encode(), "resume.txt")

    original = service.attribute_dialogues_batch_with_llm

    async def fail_second(batch, characters):
        if batch[0][0] == 1:
            raise RuntimeError("模拟第二章暂时失败")
        return await original(batch, characters)

    monkeypatch.setattr(service, "attribute_dialogues_batch_with_llm", fail_second)
    await service.prepare_project(pid)
    factory = get_session_factory()
    async with factory() as session:
        project = await session.get(Project, pid)
        progress = json.loads(project.progress_json)
        assert project.status == "partial_success"
        assert progress["stage"] == "partial"
        assert progress["dialogue_completed_chapters"] == [0, 2]

    monkeypatch.setattr(service, "attribute_dialogues_batch_with_llm", original)
    mock = _isolate_data_dir["llm"]
    calls_before = len(mock.calls)
    await service.trigger_prepare_project(pid)
    task = service._prepare_running_tasks[pid]["task"]
    await asyncio.wait_for(asyncio.shield(task), 15)

    async with factory() as session:
        project = await session.get(Project, pid)
        progress = json.loads(project.progress_json)
        assert project.status == "ready"
        assert progress["stage"] == "done"
        assert progress["dialogue_completed_chapters"] == [0, 1, 2]
    new_calls = mock.calls[calls_before:]
    prompts = [c["prompt"] for c in new_calls if c["schema"] == "DialogueBatchResponse"]
    assert prompts and all("CHAPTER idx=1 START" in p for p in prompts)
    assert not any(c["schema"] == "_ListWrapper" for c in new_calls)


@pytest.mark.asyncio
async def test_narration_only_book_can_prepare(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.services.build import get_build, get_build_status, start_build
    from backend.app.db.session import init_db
    from backend.app.services import project as service

    await init_db()

    async def no_characters(_text):
        return []

    monkeypatch.setattr(service, "extract_characters_with_llm", no_characters)
    pid = (await service.create_project("散文集")).project_id
    await service.import_file(pid, ("秋天的窗前有风。" * 250).encode(), "散文.txt")
    await service.prepare_project(pid)
    detail = await service.get_project(pid)
    assert detail.status == "ready"
    assert detail.chapter_count == 1
    assert detail.characters == []

    build = await start_build(pid, {}, "male-qn-jingying")
    for _ in range(60):
        status = await get_build_status(build.build_id)
        if status.status in ("success", "partial_success", "failed"):
            break
        await asyncio.sleep(0.1)
    assert status.status == "success", status.progress_msg
    assert len(status.artifacts) == 1
    assert status.artifacts[0].status == "done"
    detail_build = await get_build(pid, build.build_id)
    assert len(detail_build.zip_shards) == 1
    with zipfile.ZipFile(settings.AUDIO_DIR / detail_build.zip_shards[0].filename) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".mp3")]) == 1
        assert len([name for name in archive.namelist() if name.endswith(".lrc")]) == 1


@pytest.mark.asyncio
async def test_untitled_long_book_builds_separate_audio_units(_isolate_data_dir, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.session import init_db
    from backend.app.services import project as service
    from backend.app.services.build import get_build, get_build_status, start_build

    await init_db()
    monkeypatch.setattr(settings, "CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS", 1000)

    async def no_characters(_text):
        return []

    monkeypatch.setattr(service, "extract_characters_with_llm", no_characters)
    pid = (await service.create_project("无标题长书")).project_id
    await service.import_file(pid, ("春风经过窗前。" * 390).encode(), "无标题.txt")
    await service.prepare_project(pid)
    project = await service.get_project(pid)
    assert project.status == "ready"
    assert project.chapter_count > 1
    assert [ch.title for ch in project.chapters] == [
        f"第 {i} 部分" for i in range(1, project.chapter_count + 1)
    ]

    build = await start_build(pid, {}, "male-qn-jingying")
    for _ in range(100):
        status = await get_build_status(build.build_id)
        if status.status in ("success", "partial_success", "failed"):
            break
        await asyncio.sleep(0.1)
    assert status.status == "success", status.progress_msg
    assert len(status.artifacts) == project.chapter_count
    assert all(art.status == "done" for art in status.artifacts)
    detail_build = await get_build(pid, build.build_id)
    assert len(detail_build.zip_shards) == 1
    with zipfile.ZipFile(settings.AUDIO_DIR / detail_build.zip_shards[0].filename) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".mp3")]) == project.chapter_count
        assert len([name for name in archive.namelist() if name.endswith(".lrc")]) == project.chapter_count
