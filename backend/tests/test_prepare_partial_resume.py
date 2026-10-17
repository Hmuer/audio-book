"""部分识别结果可见，重触发只补缺失章。"""
import asyncio
import json

import pytest


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
