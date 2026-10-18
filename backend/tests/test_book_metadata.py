"""书籍资料自动生成及旁白官方音色校验。"""
import pytest

from backend.app.services.book_metadata import BookMetadata, clean_metadata
from backend.app.services.chapter import Chapter


def test_metadata_rejects_fabricated_voice_and_enforces_category_gender():
    voices = [
        {"id": "demo:m1", "gender": "male"},
        {"id": "demo:f1", "gender": "female"},
    ]
    raw = BookMetadata(
        category="女频", tags=["成长", "成长", "治愈"],
        selling_points="亮点", description="介绍", narrator_voice_id="fake:voice",
    )
    cleaned = clean_metadata(raw, voices)
    assert cleaned.narrator_voice_id == "demo:f1"
    assert cleaned.tags == ["成长", "治愈"]
    raw.narrator_voice_id = "demo:m1"
    assert clean_metadata(raw, voices).narrator_voice_id == "demo:f1"


@pytest.mark.asyncio
async def test_generate_project_metadata_fills_blanks_and_preserves_manual_edits(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Project
    from backend.app.services.chapter_store import save_chapters
    from backend.app.services.project import create_project, generate_project_metadata, import_file

    await init_db()
    project_id = (await create_project("成长之书", owner_user_id=0)).project_id
    await import_file(project_id, "一个关于成长的故事。".encode(), "成长之书.txt")
    async with get_session_factory()() as session:
        await save_chapters(session, project_id, [Chapter(idx=0, title="第一章", text="一个关于成长的故事。")])
        await session.commit()

    await generate_project_metadata(project_id)
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        assert project.book_category == "文学"
        assert project.tags == "成长,温暖,人生"
        assert project.selling_points
        assert project.description
        assert project.default_narrator_voice_id
        project.description = "人工介绍"
        await session.commit()

    await generate_project_metadata(project_id)
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        assert project.description == "人工介绍"

    await generate_project_metadata(project_id, overwrite=True)
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        assert project.description != "人工介绍"

    await import_file(project_id, "一篇新的散文。".encode(), "新作品.txt")
    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        assert project.book_title == "新作品"
        assert project.description is None
        assert project.default_narrator_voice_id is None


@pytest.mark.asyncio
async def test_prepare_generates_metadata_before_recommending_voices(_isolate_data_dir, monkeypatch):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Project
    from backend.app.services import project as project_service

    await init_db()
    project_id = (await project_service.create_project("新书", owner_user_id=0)).project_id
    await project_service.import_file(
        project_id,
        "第一章 初遇\n李明说：你好。\n林若雪说：再见。".encode(),
        "新书.txt",
    )
    observed = []

    async def check_recommend(characters, **kwargs):
        async with get_session_factory()() as session:
            book = await session.get(Project, project_id)
            observed.append((book.book_category, book.tags, book.selling_points,
                             book.description, book.default_narrator_voice_id))
        from backend.app.services.voice_recommender import VoiceRecommendation
        return [VoiceRecommendation(character_name=c.name, suggested_voice_id="male-qn-qingse", reason="测试")
                for c in characters]

    monkeypatch.setattr(project_service, "recommend_voices_with_llm", check_recommend)
    await project_service.prepare_project(project_id)
    assert observed and all(observed[0])
