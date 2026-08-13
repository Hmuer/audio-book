"""
项目制服务（新架构）：把"整本小说"功能从 Job 表迁移到 Project → Build → BuildArtifact 三层结构。

本文件只负责 Project 层的 CRUD + 文件导入 + 识别（章节 / 角色 / 对白 / 音色）。
Build 合成逻辑见 services/build.py。

旧 services/book.py 仍然保留给 /api/book/* 路由（单章模式 + 旧 BookFlow.tsx 兼容）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select, delete

from ..core.config import settings
from ..db.models import (
    Project,
    Build,
    BuildArtifact,
    ProjectCharacter,
    ProjectDialogue,
)
from ..db.session import get_session_factory
from .book_split import split_book_chapters
from .chapter import Chapter
from .character import (
    Character,
    extract_characters_with_llm,
    deduplicate_characters_with_llm,
    apply_dedup,
)
from .dialogue import attribute_dialogues_with_llm
from .voice_recommender import VoiceRecommendation, recommend_voices_with_llm

logger = logging.getLogger(__name__)

# 项目封面 6 个预设色（按 project_id hash 分配，避免用户选色负担）
COVER_COLOR_PRESETS: list[str] = [
    "#5B8FF9",  # 蓝
    "#5AD8A6",  # 绿
    "#F6BD16",  # 黄
    "#E86452",  # 红
    "#6DC8EC",  # 青
    "#945FB9",  # 紫
]


# =====================================================================
# Pydantic response 模型（全部定义在本文件内）
# =====================================================================

class ProjectResp(BaseModel):
    """项目基础信息（创建/导入/更新后返回）。"""
    project_id: str
    name: str
    book_title: str | None = None
    status: str
    source_filename: str | None = None
    source_file_size: int | None = None
    chapter_count: int = 0
    cover_color: str | None = None
    description: str | None = None
    tags: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ChapterSummary(BaseModel):
    """章节摘要（不返回正文，避免列表过大）。"""
    idx: int
    title: str
    text_len: int


class CharacterWithVoice(BaseModel):
    """角色 + 已分配音色。"""
    id: int
    name: str
    gender: str
    age: str
    personality: str
    canonical_name: str | None
    assigned_voice_id: str | None


class CharacterResp(BaseModel):
    """更新角色音色后返回。"""
    id: int
    name: str
    assigned_voice_id: str | None


class BuildBrief(BaseModel):
    """项目详情里嵌入的最近 build 简要信息。"""
    build_id: str
    status: str
    completed_chapters: int
    total_chapters: int
    created_at: str | None


class ProjectDetailResp(BaseModel):
    """项目详情：基础信息 + 章节摘要 + 角色 + 最近 build。"""
    project_id: str
    name: str
    book_title: str | None
    status: str
    source_filename: str | None
    source_file_size: int | None
    chapter_count: int
    cover_color: str | None
    description: str | None
    tags: str | None
    default_narrator_voice_id: str | None
    default_speed: float
    created_at: str | None
    updated_at: str | None
    chapters: list[ChapterSummary]
    characters: list[CharacterWithVoice]
    last_build: BuildBrief | None = None


class ProjectListItem(BaseModel):
    """项目列表项（精简）。"""
    project_id: str
    name: str
    book_title: str | None
    status: str
    source_filename: str | None
    chapter_count: int
    cover_color: str | None
    created_at: str | None
    updated_at: str | None


class ProjectPrepareResp(BaseModel):
    """prepare 完成后返回识别结果摘要。"""
    project_id: str
    book_title: str | None
    total_chapters: int
    chapters: list[ChapterSummary]
    characters: list[dict]
    voice_recommendations: list[dict]


# =====================================================================
# 内部工具
# =====================================================================

def _pick_cover_color(project_id: str) -> str:
    """按 project_id 字符串 hash 在 6 个预设色中选一个。"""
    # 注意：Python 内置 hash 在不同进程间不稳定（PYTHONHASHSEED），
    # 改用对字符 ord 累加，确保同一 project_id 在不同进程下得到同一颜色
    s = 0
    for ch in project_id:
        s = (s * 31 + ord(ch)) & 0xFFFFFFFF
    return COVER_COLOR_PRESETS[s % len(COVER_COLOR_PRESETS)]


def _detect_encoding(raw_bytes: bytes) -> str:
    """尝试常见中文编码，返回第一个能成功解码的编码名。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5", "utf-16"):
        try:
            raw_bytes.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"  # 兜底


def _project_source_path(project_id: str, ext: str = ".txt") -> str:
    """项目源文件磁盘路径：uploads/proj_{project_id}{ext}。"""
    uploads_dir = Path(settings.DATA_DIR) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return str(uploads_dir / f"proj_{project_id}{ext}")


def _to_project_resp(p: Project) -> ProjectResp:
    return ProjectResp(
        project_id=p.project_id,
        name=p.name,
        book_title=p.book_title,
        status=p.status,
        source_filename=p.source_filename,
        source_file_size=p.source_file_size,
        chapter_count=p.chapter_count,
        cover_color=p.cover_color,
        description=p.description,
        tags=p.tags,
        created_at=p.created_at.isoformat() if p.created_at else None,
        updated_at=p.updated_at.isoformat() if p.updated_at else None,
    )


# =====================================================================
# CRUD
# =====================================================================

async def create_project(name: str) -> ProjectResp:
    """创建空项目（status=draft）。"""
    project_id = uuid.uuid4().hex
    factory = get_session_factory()
    async with factory() as session:
        p = Project(
            project_id=project_id,
            name=name or "未命名项目",
            status="draft",
            cover_color=_pick_cover_color(project_id),
        )
        session.add(p)
        await session.commit()
        # 重新 load 一次拿到默认值（created_at 等）
        await session.refresh(p)
        logger.info(f"[project_create] project_id={project_id[:8]}... name={p.name!r}")
        return _to_project_resp(p)


async def import_file(project_id: str, file_content: bytes, filename: str) -> ProjectResp:
    """
    上传/替换项目源文件：保存到磁盘 → 检测编码 → 更新 source_* 字段 → status=imported。
    """
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")

        # 旧文件若存在则覆盖（同一路径写覆盖即可）
        ext = Path(filename).suffix or ".txt"
        saved_path = _project_source_path(project_id, ext)
        Path(saved_path).write_bytes(file_content)

        # 检测编码
        charset = _detect_encoding(file_content)

        p.source_file_path = saved_path
        p.source_filename = filename or f"proj_{project_id[:8]}{ext}"
        p.source_file_size = len(file_content)
        p.source_charset = charset
        # 推断 book_title：取文件名 stem
        p.book_title = Path(filename).stem if filename else p.book_title
        p.status = "imported"
        await session.commit()
        await session.refresh(p)
        logger.info(
            f"[project_import] project_id={project_id[:8]}... "
            f"file={p.source_filename} size={len(file_content)} charset={charset}"
        )
        return _to_project_resp(p)


async def prepare_project(project_id: str) -> ProjectPrepareResp:
    """
    触发识别：读文件 → 章节识别 → 角色识别 → 对白归属 → 音色推荐 → 落库。
    status: imported → preparing → ready（失败时 → failed）
    """
    import time as _time
    t0 = _time.perf_counter()
    factory = get_session_factory()

    # 1. 取项目 + 校验源文件已导入；缺失时标记 failed
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        if not p.source_file_path or not os.path.isfile(p.source_file_path):
            p.status = "failed"
            await session.commit()
            raise RuntimeError("项目尚未导入源文件，请先调用 /import")
        p.status = "preparing"
        await session.commit()
        source_path = p.source_file_path
        charset = p.source_charset or "utf-8"
        original_filename = p.source_filename or ""

    # 2. 读文件
    try:
        raw_bytes = Path(source_path).read_bytes()
        try:
            raw_text = raw_bytes.decode(charset)
        except (UnicodeDecodeError, LookupError):
            # charset 错了，回退自动探测
            charset = _detect_encoding(raw_bytes)
            raw_text = raw_bytes.decode(charset)
        if not raw_text.strip():
            raise RuntimeError("源文件内容为空")
    except Exception as e:
        async with factory() as s2:
            p2 = await s2.get(Project, project_id)
            if p2:
                p2.status = "failed"
                await s2.commit()
        raise RuntimeError(f"读取源文件失败: {type(e).__name__}: {e}")

    logger.info(
        f"[project_prepare] project_id={project_id[:8]}... "
        f"file={original_filename} chars={len(raw_text)} charset={charset}"
    )

    try:
        # 3. 章节识别
        pt = _time.perf_counter()
        chapters = await split_book_chapters(raw_text)
        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"split_chapters={len(chapters)} ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 4. 全书角色识别（50k 切片串行：小说情节是串行的，并发会触发限流且
        #    可能导致跨切片识别错乱；同时 provider 层有 semaphore 兜底）
        pt = _time.perf_counter()
        full_text = "\n".join(c.text for c in chapters)
        characters = await _split_50k_and_run_chars_serial(full_text, extract_characters_with_llm)
        if len(characters) >= 2:
            names = [c.name for c in characters]
            dedup_results = await deduplicate_characters_with_llm(names, full_text)
            characters, name_map = apply_dedup(characters, dedup_results)
        else:
            name_map = {c.name: c.name for c in characters}
        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"characters={len(characters)} ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 5. 每章对白归属（串行：章节间存在上下文依赖，按章节顺序识别更稳；
        #    且避免瞬时并发触发供应商 RPM 限制）
        pt = _time.perf_counter()
        all_attrs_per_chapter: list[list] = []
        total_dialogues = 0
        for ch_idx, ch in enumerate(chapters):
            attrs = await attribute_dialogues_with_llm(ch.text, characters)
            # speaker 做 name → canonical 映射
            for a in attrs:
                a.speaker = name_map.get(a.speaker, a.speaker)
            all_attrs_per_chapter.append(attrs)
            total_dialogues += len(attrs)
            logger.info(
                f"[project_prepare] project_id={project_id[:8]}... "
                f"dialogue_attr chapter={ch_idx+1}/{len(chapters)} "
                f"this_dialogues={len(attrs)} cum={total_dialogues}"
            )
        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"dialogue_attr done total_dialogues={total_dialogues} "
            f"ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 6. 音色推荐
        pt = _time.perf_counter()
        voice_recs: list[VoiceRecommendation] = []
        try:
            voice_recs = await recommend_voices_with_llm(characters)
        except Exception as e:
            logger.warning(f"[project_prepare] project_id={project_id[:8]}... voice_rec failed: {e}")
        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"voice_recs={len(voice_recs)} ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 7. 落库：先清旧数据 → 写新数据
        # voice_recs 转 name → voice_id 映射，方便直接落 assigned_voice_id
        voice_id_by_name: dict[str, str] = {
            r.character_name: r.suggested_voice_id for r in voice_recs
        }

        chapters_json = json.dumps(
            [{"idx": c.idx, "title": c.title, "text": c.text} for c in chapters],
            ensure_ascii=False,
        )

        async with factory() as session:
            p = await session.get(Project, project_id)
            assert p is not None
            # 清旧识别数据（重新 prepare 时复用）
            await session.execute(
                delete(ProjectCharacter).where(ProjectCharacter.project_id == project_id)
            )
            await session.execute(
                delete(ProjectDialogue).where(ProjectDialogue.project_id == project_id)
            )
            # 写新数据
            for c in characters:
                session.add(ProjectCharacter(
                    project_id=project_id,
                    name=c.name,
                    gender=c.gender,
                    age=c.age,
                    personality=c.personality,
                    canonical_name=c.name,
                    assigned_voice_id=voice_id_by_name.get(c.name),
                ))
            for ch_idx, attrs in enumerate(all_attrs_per_chapter):
                for seg_idx, a in enumerate(attrs):
                    session.add(ProjectDialogue(
                        project_id=project_id,
                        chapter_idx=ch_idx,
                        segment_index=seg_idx,
                        anchor_start=a.anchor.start,
                        anchor_end=a.anchor.end,
                        anchor_text=a.anchor.text,
                        speaker=a.speaker,
                        text=a.text,
                        confidence=a.confidence,
                    ))
            p.chapters_json = chapters_json
            p.chapter_count = len(chapters)
            p.book_title = p.book_title or Path(original_filename).stem
            p.status = "ready"
            await session.commit()

        total_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[project_prepare] DONE project_id={project_id[:8]}... "
            f"total_ms={total_ms} chapters={len(chapters)} "
            f"characters={len(characters)} dialogues={total_dialogues}"
        )

        return ProjectPrepareResp(
            project_id=project_id,
            book_title=p.book_title,
            total_chapters=len(chapters),
            chapters=[
                ChapterSummary(idx=c.idx, title=c.title, text_len=len(c.text))
                for c in chapters
            ],
            characters=[c.model_dump() for c in characters],
            voice_recommendations=[r.model_dump() for r in voice_recs],
        )
    except Exception as e:
        # 任何失败都把项目置为 failed，便于前端显示
        logger.error(
            f"[project_prepare] FAIL project_id={project_id[:8]}... "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        try:
            async with factory() as s2:
                p2 = await s2.get(Project, project_id)
                if p2:
                    p2.status = "failed"
                    await s2.commit()
        except Exception:
            pass
        raise


async def _split_50k_and_run_chars_serial(text: str, coro_fn) -> list[Character]:
    """
    长文本角色识别主入口：
    - <= LLM_CHAR_EXTRACT_LIMIT（默认 50 万字）：逐 50k 切片串行 + 合并（准确）
    - >  LLM_CHAR_EXTRACT_LIMIT：只抽样前 LLM_CHAR_EXTRACT_LIMIT 字跑一次
      （长篇小说前 50 万字通常已出场几乎全部主要角色，对白归属够用；
       1000 章 200 万字书从 40 次 LLM → 1 次）
    """
    from ..core.config import settings
    total_chars = len(text)
    sample_limit = max(50000, int(settings.LLM_CHAR_EXTRACT_LIMIT))
    if total_chars > sample_limit:
        logger.warning(
            f"[chars_split] total={total_chars} > LLM_CHAR_EXTRACT_LIMIT={sample_limit}, "
            f"只抽样前 {sample_limit} 字识别角色（后续章节识别仍基于该角色库匹配）"
        )
        text = text[:sample_limit]

    MAX = 50000
    if len(text) <= MAX:
        return await coro_fn(text)
    slices = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    merged: list[Character] = []
    for i, s in enumerate(slices):
        logger.info(
            f"[chars_split] slice {i+1}/{len(slices)} chars={len(s)} start"
        )
        r = await coro_fn(s)
        merged.extend(r)
    return merged


async def get_project(project_id: str) -> ProjectDetailResp:
    """返回项目详情（含 chapters 摘要 + characters + 最近 build）。"""
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")

        # chapters 摘要（从 chapters_json 解析）
        chapters: list[ChapterSummary] = []
        if p.chapters_json:
            try:
                ch_list = json.loads(p.chapters_json)
                chapters = [
                    ChapterSummary(
                        idx=c["idx"],
                        title=c.get("title", ""),
                        text_len=len(c.get("text", "")),
                    )
                    for c in ch_list
                ]
            except Exception:
                pass

        # 角色
        stmt_c = select(ProjectCharacter).where(
            ProjectCharacter.project_id == project_id
        ).order_by(ProjectCharacter.id)
        char_rows = list((await session.execute(stmt_c)).scalars().all())
        characters = [
            CharacterWithVoice(
                id=c.id,
                name=c.name,
                gender=c.gender,
                age=c.age,
                personality=c.personality,
                canonical_name=c.canonical_name,
                assigned_voice_id=c.assigned_voice_id,
            )
            for c in char_rows
        ]

        # 最近 build（按 created_at desc 取一条）
        stmt_b = select(Build).where(
            Build.project_id == project_id
        ).order_by(Build.created_at.desc()).limit(1)
        last_build_row = (await session.execute(stmt_b)).scalar_one_or_none()
        last_build = (
            BuildBrief(
                build_id=last_build_row.build_id,
                status=last_build_row.status,
                completed_chapters=last_build_row.completed_chapters,
                total_chapters=last_build_row.total_chapters,
                created_at=last_build_row.created_at.isoformat() if last_build_row.created_at else None,
            )
            if last_build_row
            else None
        )

        return ProjectDetailResp(
            project_id=p.project_id,
            name=p.name,
            book_title=p.book_title,
            status=p.status,
            source_filename=p.source_filename,
            source_file_size=p.source_file_size,
            chapter_count=p.chapter_count,
            cover_color=p.cover_color,
            description=p.description,
            tags=p.tags,
            default_narrator_voice_id=p.default_narrator_voice_id,
            default_speed=p.default_speed,
            created_at=p.created_at.isoformat() if p.created_at else None,
            updated_at=p.updated_at.isoformat() if p.updated_at else None,
            chapters=chapters,
            characters=characters,
            last_build=last_build,
        )


async def list_projects() -> list[ProjectListItem]:
    """项目列表（按创建时间倒序）。"""
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(Project).order_by(Project.created_at.desc())
        rows = list((await session.execute(stmt)).scalars().all())
        return [
            ProjectListItem(
                project_id=p.project_id,
                name=p.name,
                book_title=p.book_title,
                status=p.status,
                source_filename=p.source_filename,
                chapter_count=p.chapter_count,
                cover_color=p.cover_color,
                created_at=p.created_at.isoformat() if p.created_at else None,
                updated_at=p.updated_at.isoformat() if p.updated_at else None,
            )
            for p in rows
        ]


async def update_project(
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    tags: str | None = None,
    default_narrator_voice_id: str | None = None,
    default_speed: float | None = None,
    cover_color: str | None = None,
) -> ProjectResp:
    """更新项目名称/备注/标签/配置。"""
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        if name is not None:
            p.name = name
        if description is not None:
            p.description = description
        if tags is not None:
            p.tags = tags
        if default_narrator_voice_id is not None:
            p.default_narrator_voice_id = default_narrator_voice_id
        if default_speed is not None:
            p.default_speed = default_speed
        if cover_color is not None:
            p.cover_color = cover_color
        await session.commit()
        await session.refresh(p)
        return _to_project_resp(p)


async def delete_project(project_id: str) -> None:
    """
    级联删除：DB（Project + Builds + BuildArtifacts + ProjectCharacters + ProjectDialogues）
    + 磁盘文件（源文件 + 所有 build 产生的 MP3/ZIP）。
    """
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            # 已不存在，幂等返回
            return

        # 收集所有 build 产物文件名，用于稍后删磁盘
        stmt_art = select(BuildArtifact.audio_filename).where(
            BuildArtifact.build_id.in_(
                select(Build.build_id).where(Build.project_id == project_id)
            )
        )
        art_filenames = [r for r in (await session.execute(stmt_art)).scalars().all() if r]

        stmt_zip = select(Build.zip_filename).where(Build.project_id == project_id)
        zip_filenames = [r for r in (await session.execute(stmt_zip)).scalars().all() if r]

        source_path = p.source_file_path

        # 删 DB（cascade=all,delete-orphan 会自动连带 Build/BuildArtifact/ProjectCharacter/ProjectDialogue）
        await session.delete(p)
        await session.commit()

    # 删磁盘文件（在 session 关闭后做，避免占用 DB 连接）
    audio_dir = Path(settings.AUDIO_DIR)
    for fname in art_filenames + zip_filenames:
        try:
            fpath = audio_dir / fname
            if fpath.is_file():
                fpath.unlink()
        except OSError as e:
            logger.warning(f"[project_delete] 删音频文件失败: {fname} -> {e}")

    if source_path:
        try:
            sp = Path(source_path)
            if sp.is_file():
                sp.unlink()
        except OSError as e:
            logger.warning(f"[project_delete] 删源文件失败: {source_path} -> {e}")

    logger.info(
        f"[project_delete] project_id={project_id[:8]}... "
        f"deleted audio_files={len(art_filenames)} zips={len(zip_filenames)}"
    )


async def get_project_chapters(project_id: str) -> list[ChapterSummary]:
    """章节列表（不返回正文）。"""
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        if not p.chapters_json:
            return []
        try:
            ch_list = json.loads(p.chapters_json)
        except Exception:
            return []
        return [
            ChapterSummary(
                idx=c["idx"],
                title=c.get("title", ""),
                text_len=len(c.get("text", "")),
            )
            for c in ch_list
        ]


async def get_project_characters(project_id: str) -> list[CharacterWithVoice]:
    """角色 + 已分配音色。"""
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        stmt = select(ProjectCharacter).where(
            ProjectCharacter.project_id == project_id
        ).order_by(ProjectCharacter.id)
        rows = list((await session.execute(stmt)).scalars().all())
        return [
            CharacterWithVoice(
                id=c.id,
                name=c.name,
                gender=c.gender,
                age=c.age,
                personality=c.personality,
                canonical_name=c.canonical_name,
                assigned_voice_id=c.assigned_voice_id,
            )
            for c in rows
        ]


async def update_character_voice(
    project_id: str, character_id: int, voice_id: str | None
) -> CharacterResp:
    """更新角色音色（voice_id 可为 None，表示清除）。"""
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(ProjectCharacter).where(
            ProjectCharacter.id == character_id,
            ProjectCharacter.project_id == project_id,
        )
        c = (await session.execute(stmt)).scalar_one_or_none()
        if not c:
            raise ValueError(f"角色不存在: char_id={character_id} project_id={project_id}")
        c.assigned_voice_id = voice_id
        await session.commit()
        await session.refresh(c)
        return CharacterResp(
            id=c.id,
            name=c.name,
            assigned_voice_id=c.assigned_voice_id,
        )
