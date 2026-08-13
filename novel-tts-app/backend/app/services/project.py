"""
项目制服务（新架构）：把"整本小说"功能从旧 Job 表迁移到 Project → Build → BuildArtifact 三层结构。

本文件只负责 Project 层的 CRUD + 文件导入 + 识别（章节 / 角色 / 对白 / 音色）。
Build 合成逻辑见 services/build.py。
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
from .book_split import ChapterSplitError, split_book_chapters
from .character import (
    Character,
    extract_characters_with_llm,
    deduplicate_characters_with_llm,
    apply_dedup,
)
from .dialogue import (
    attribute_dialogues_with_llm,
    attribute_dialogues_batch_with_llm,
    ChapterDialogueBatchResult,
)
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


async def import_text(
    project_id: str,
    text: str,
    filename_hint: str = "pasted_text.txt",
) -> ProjectResp:
    """
    粘贴纯文本导入：直接用字符串 → 按 UTF-8 编码落盘（因为是用户浏览器直接 paste，
    无字节歧义，不需要编码检测）→ 更新 source_* 字段 → status=imported。
    逻辑等价于 import_file（bytes），只是输入源直接是 text。
    """
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")

        # 粘贴内容编码稳定（浏览器给的 JS string，直接 encode 成 utf-8）
        file_content = text.encode("utf-8")
        if not file_content.strip():
            raise ValueError("粘贴内容为空")

        ext = Path(filename_hint).suffix or ".txt"
        saved_path = _project_source_path(project_id, ext)
        Path(saved_path).write_bytes(file_content)

        charset = "utf-8"
        p.source_file_path = saved_path
        p.source_filename = filename_hint or f"proj_{project_id[:8]}_pasted.txt"
        p.source_file_size = len(file_content)
        p.source_charset = charset
        # 书名：优先用户给的 hint stem（没给就保留当前 book_title）
        stem = Path(filename_hint).stem if filename_hint else None
        if stem and stem.strip() and stem != "pasted_text":
            p.book_title = stem.strip()
        p.status = "imported"
        await session.commit()
        await session.refresh(p)
        logger.info(
            f"[project_import_text] project_id={project_id[:8]}... "
            f"filename_hint={p.source_filename} size={len(file_content)}"
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
        try:
            chapters = await split_book_chapters(raw_text)
        except ChapterSplitError as e:
            # 切章失败：不回退 LLM，不调用后续角色识别/对白归属，直接把错误信息提示给用户
            # （CHAPTER_SPLIT_HARD_FALLBACK_ENABLED 控制是否启用硬切兜底，默认关闭）
            logger.warning(
                f"[project_prepare] project_id={project_id[:8]}... chapter split failed, "
                f"no LLM fallback → raise directly"
            )
            raise
        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"split_chapters={len(chapters)} ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 4. 全书角色识别（50k 切片串行 + checkpoint：逐片写入 progress_json，
        #    重跑 prepare_project 时跳过已完成切片）
        pt = _time.perf_counter()
        full_text = "\n".join(c.text for c in chapters)

        # 4a. 加载/初始化 checkpoint
        async def _read_progress() -> dict:
            async with factory() as sess:
                proj = await sess.get(Project, project_id)
                if not proj or not proj.progress_json:
                    return {"version": 1}
                try:
                    return json.loads(proj.progress_json)
                except Exception:
                    logger.warning(
                        f"[project_prepare] project_id={project_id[:8]}... "
                        f"progress_json 解析失败，当作空 checkpoint 从头开始"
                    )
                    return {"version": 1}

        async def _write_progress(partial: dict) -> None:
            partial["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            async with factory() as sess:
                proj = await sess.get(Project, project_id)
                if proj:
                    proj.progress_json = json.dumps(partial, ensure_ascii=False)
                    await sess.commit()

        prog = await _read_progress()

        # 4b. 角色识别（50k 切片，逐片 checkpoint）
        char_slice_size = max(10000, int(settings.LLM_CHAR_EXTRACT_SLICE_SIZE) or 50000)
        char_slices: list[tuple[int, str]] = [
            (i, full_text[i : i + char_slice_size])
            for i in range(0, len(full_text), char_slice_size)
        ]
        completed_slice_idxs: set[int] = set(prog.get("char_slice_completed", []))
        char_raw_list: list[dict] = list(prog.get("char_extract_raw_list", []) or [])

        if prog.get("stage") in ("characters", "dedup", "dialogues", "voice_recs", "done") and char_raw_list:
            logger.info(
                f"[project_prepare] project_id={project_id[:8]}... "
                f"命中角色识别 checkpoint：已完成 {len(completed_slice_idxs)}/{len(char_slices)} 切片"
            )
        else:
            # 从头开始，重置
            completed_slice_idxs = set()
            char_raw_list = []
            prog = {
                "version": 1,
                "stage": "characters",
                "char_slice_completed": [],
                "char_extract_raw_list": [],
                "dialogue_completed_chapters": [],
            }
            await _write_progress(prog)

        characters_merged: list[Character] = [
            Character(**d) for d in char_raw_list
        ]
        for slice_idx, slice_text in char_slices:
            if slice_idx in completed_slice_idxs:
                logger.info(
                    f"[project_prepare] project_id={project_id[:8]}... "
                    f"chars slice {slice_idx+1}/{len(char_slices)} 跳过（checkpoint）"
                )
                continue
            logger.info(
                f"[project_prepare] project_id={project_id[:8]}... "
                f"chars slice {slice_idx+1}/{len(char_slices)} chars={len(slice_text)} start"
            )
            r = await extract_characters_with_llm(slice_text)
            characters_merged.extend(r)
            completed_slice_idxs.add(slice_idx)
            char_raw_list.extend([c.model_dump() for c in r])
            prog["stage"] = "characters"
            prog["char_slice_completed"] = sorted(completed_slice_idxs)
            prog["char_extract_raw_list"] = char_raw_list
            await _write_progress(prog)
            logger.info(
                f"[project_prepare] project_id={project_id[:8]}... "
                f"chars slice {slice_idx+1}/{len(char_slices)} done "
                f"extracted={len(r)} cum_unique_chars={len({c.name for c in characters_merged})}"
            )

        # 4c. dedup（完成后 checkpoint 跳到 dedup=done）
        if prog.get("stage") in ("dedup", "dialogues", "voice_recs", "done") and prog.get("dedup_done"):
            name_map: dict[str, str] = dict(prog.get("name_map", {}) or {})
            characters = [Character(**d) for d in prog.get("deduped_characters", []) or []]
            if not name_map:
                name_map = {c.name: c.name for c in characters}
            logger.info(
                f"[project_prepare] project_id={project_id[:8]}... "
                f"命中角色 dedup checkpoint：characters={len(characters)}"
            )
        else:
            if len(characters_merged) >= 2:
                names = [c.name for c in characters_merged]
                dedup_results = await deduplicate_characters_with_llm(names, full_text)
                characters, name_map = apply_dedup(characters_merged, dedup_results)
            else:
                characters = characters_merged
                name_map = {c.name: c.name for c in characters}
            prog["stage"] = "dedup"
            prog["dedup_done"] = True
            prog["deduped_characters"] = [c.model_dump() for c in characters]
            prog["name_map"] = name_map
            await _write_progress(prog)
        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"characters={len(characters)} ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 5. 对白归属：14 章/批批量 + DIALOGUE_BATCH_CONCURRENCY 并发跑批，
        #    checkpoint 按章节记 dialogue_completed_chapters，重跑跳过已完成章节。
        pt = _time.perf_counter()
        batch_chapters_n = max(1, int(settings.DIALOGUE_BATCH_CHAPTERS) if hasattr(settings, "DIALOGUE_BATCH_CHAPTERS") else 14)
        batch_concurrency = max(1, int(settings.DIALOGUE_BATCH_CONCURRENCY) if hasattr(settings, "DIALOGUE_BATCH_CONCURRENCY") else 2)

        completed_ch_idxs: set[int] = set(prog.get("dialogue_completed_chapters", []))
        # 从 checkpoint 里读单章级别的 attrs 缓存（按 chapter_idx 存序列化 JSON）
        chapter_attrs_cache: dict[int, list] = {}
        raw_cache = prog.get("dialogue_attrs_by_chapter_json", {}) or {}
        if isinstance(raw_cache, dict):
            for k, v in raw_cache.items():
                try:
                    chapter_attrs_cache[int(k)] = v
                except Exception:
                    pass

        all_attrs_per_chapter: list[list] = [[] for _ in chapters]
        total_dialogues = 0

        # 5a. 先把已完成章节的缓存填入
        for ch_idx in sorted(completed_ch_idxs):
            if 0 <= ch_idx < len(all_attrs_per_chapter):
                cached = chapter_attrs_cache.get(ch_idx)
                if cached:
                    try:
                        # 缓存里存的是 dict list，转回 DialogueAttribution-like dict（这里直接用就行，因为写 DB 只取字段）
                        all_attrs_per_chapter[ch_idx] = cached
                        total_dialogues += len(cached)
                    except Exception:
                        all_attrs_per_chapter[ch_idx] = []
                logger.info(
                    f"[project_prepare] project_id={project_id[:8]}... "
                    f"dialogue_attr chapter={ch_idx+1}/{len(chapters)} 跳过（checkpoint）"
                )

        # 5b. 未完成的章节 → 按 DIALOGUE_BATCH_CHAPTERS 切批
        pending_chapters: list[tuple[int, str]] = [
            (ch.idx, ch.text)
            for ch in chapters
            if ch.idx not in completed_ch_idxs
        ]
        batches: list[list[tuple[int, str]]] = [
            pending_chapters[i : i + batch_chapters_n]
            for i in range(0, len(pending_chapters), batch_chapters_n)
        ]

        # 并发 semaphore 限制批并发（provider 层另有全局 LLM semaphore 兜底串行）
        dialogue_batch_sem = asyncio.Semaphore(batch_concurrency)
        # progress 写 DB / 改 prog dict 必须串行：多个批同时改 prog 会互相覆盖
        # （如批A写 dialogue_completed_chapters=[0..13]，批B写[14..27]，不加锁的话
        #  as_completed 里会同时读-改-写 prog，出现 lost update）
        progress_write_lock = asyncio.Lock()

        async def _process_one_batch(
            batch: list[tuple[int, str]], batch_idx: int
        ) -> list[tuple[int, list]]:
            async with dialogue_batch_sem:
                logger.info(
                    f"[project_prepare] project_id={project_id[:8]}... "
                    f"dialogue batch {batch_idx+1}/{len(batches)} chapters="
                    f"{[c[0]+1 for c in batch]} start"
                )
                results = await attribute_dialogues_batch_with_llm(batch, characters)
                out: list[tuple[int, list]] = []
                for r in results:
                    attrs = r.dialogues
                    for a in attrs:
                        a.speaker = name_map.get(a.speaker, a.speaker)
                    out.append((r.chapter_idx, [a.model_dump() for a in attrs]))
                return out

        if batches:
            batch_tasks = [
                asyncio.create_task(_process_one_batch(b, i))
                for i, b in enumerate(batches)
            ]
            for t in asyncio.as_completed(batch_tasks):
                batch_results = await t
                # === 整批结果一次性在 lock 内合并，避免并发覆盖 prog dict ===
                async with progress_write_lock:
                    for ch_idx, attrs_dicts in batch_results:
                        if 0 <= ch_idx < len(all_attrs_per_chapter):
                            all_attrs_per_chapter[ch_idx] = attrs_dicts
                        completed_ch_idxs.add(ch_idx)
                        chapter_attrs_cache[ch_idx] = attrs_dicts
                        total_dialogues += len(attrs_dicts)
                        logger.info(
                            f"[project_prepare] project_id={project_id[:8]}... "
                            f"dialogue_attr chapter={ch_idx+1}/{len(chapters)} "
                            f"this_dialogues={len(attrs_dicts)} cum={total_dialogues}"
                        )
                    # 合并完本 batch 所有章后，统一写一次 checkpoint（减少写 DB 次数）
                    prog["stage"] = "dialogues"
                    prog["dialogue_completed_chapters"] = sorted(completed_ch_idxs)
                    prog["dialogue_attrs_by_chapter_json"] = chapter_attrs_cache
                    await _write_progress(prog)

        logger.info(
            f"[project_prepare] project_id={project_id[:8]}... "
            f"dialogue_attr done total_dialogues={total_dialogues} "
            f"ms={int((_time.perf_counter()-pt)*1000)}"
        )

        # 6. 音色推荐（完成后 checkpoint 跳过）
        pt = _time.perf_counter()
        if prog.get("stage") in ("voice_recs", "done") and prog.get("voice_recs_done"):
            voice_recs_raw = prog.get("voice_recs_raw", []) or []
            voice_recs: list[VoiceRecommendation] = [
                VoiceRecommendation(**d) for d in voice_recs_raw
            ]
            logger.info(
                f"[project_prepare] project_id={project_id[:8]}... "
                f"命中音色推荐 checkpoint：voice_recs={len(voice_recs)}"
            )
        else:
            voice_recs = []
            try:
                voice_recs = await recommend_voices_with_llm(characters)
            except Exception as e:
                logger.warning(f"[project_prepare] project_id={project_id[:8]}... voice_rec failed: {e}")
            prog["stage"] = "voice_recs"
            prog["voice_recs_done"] = True
            prog["voice_recs_raw"] = [r.model_dump() for r in voice_recs]
            await _write_progress(prog)
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
                    # checkpoint 里缓存的 attrs 是 dict（含 anchor=dict），直接用 key 访问；
                    # 首次 fresh 路径里的 attrs 是 DialogueAttribution Pydantic 对象，支持属性访问。
                    # 用 duck-typing 方式兼容两种。
                    def _f(obj, key, default=None):
                        if isinstance(obj, dict):
                            v = obj.get(key, default)
                        else:
                            v = getattr(obj, key, default)
                        # anchor 嵌套
                        if key == "anchor" and isinstance(v, dict):
                            return v
                        return v
                    anchor = _f(a, "anchor")
                    if isinstance(anchor, dict):
                        a_start = int(anchor.get("start", 0))
                        a_end = int(anchor.get("end", 0))
                        a_text = str(anchor.get("text", ""))
                    else:
                        a_start = int(getattr(anchor, "start", 0) or 0)
                        a_end = int(getattr(anchor, "end", 0) or 0)
                        a_text = str(getattr(anchor, "text", "") or "")
                    speaker_raw = _f(a, "speaker", "")
                    text_raw = _f(a, "text", "")
                    conf_raw = _f(a, "confidence", 1.0)
                    session.add(ProjectDialogue(
                        project_id=project_id,
                        chapter_idx=ch_idx,
                        segment_index=seg_idx,
                        anchor_start=a_start,
                        anchor_end=a_end,
                        anchor_text=a_text,
                        speaker=str(speaker_raw),
                        text=str(text_raw),
                        confidence=float(conf_raw) if conf_raw is not None else 1.0,
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
    整本小说角色识别：**全量逐 50k 切片串行跑，不截断、不抽样。**

    无论小说多长（10 万字 / 200 万字 / 1000 万字）都完整跑完：
    - 每 50k 字符切片调用一次角色识别 LLM
    - 所有切片结果合并后，再走一次去重（`deduplicate_characters_with_llm`）
    - 串行调用（配合 provider 层全局 semaphore 防 RPM 429）

    为什么不用"抽样前 50 万字"：
      后半本书出场的配角、阶段性反派、关键角色在"抽样截断"时会被漏掉，
      导致这些角色后续的对白归属全部失败（被识别成"旁白/未知"）。
      按 3000 字/章 × 1000 章 = 300 万字估算，大约需要 60 次 LLM 调用，
      即便串行跑 10s/次 也就 ~10 分钟，换来对白归属准确率完全可接受。

    输出：合并后的 Character 列表（调用方会再跑 dedup + canonical 归一化）
    """
    from ..core.config import settings
    total_chars = len(text)

    MAX = int(settings.LLM_CHAR_EXTRACT_SLICE_SIZE) if settings.LLM_CHAR_EXTRACT_SLICE_SIZE else 50000
    MAX = max(10000, MAX)
    if len(text) <= MAX:
        logger.info(f"[chars_split] total={total_chars} ≤ 50k，单切片处理")
        return await coro_fn(text)

    slices = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    logger.info(
        f"[chars_split] total={total_chars} → slices={len(slices)} "
        f"(全量串行处理，每个 slice ≤ {MAX} chars；"
        f"预计耗时 ≈ {len(slices)} × 单次 LLM 时长，不会提前截断)"
    )
    merged: list[Character] = []
    for i, s in enumerate(slices):
        logger.info(
            f"[chars_split] slice {i+1}/{len(slices)} chars={len(s)} start"
        )
        r = await coro_fn(s)
        logger.info(
            f"[chars_split] slice {i+1}/{len(slices)} done chars={len(s)} "
            f"extracted_characters={len(r)}"
        )
        merged.extend(r)
    logger.info(
        f"[chars_split] ALL DONE slices={len(slices)} "
        f"raw_merged_characters={len(merged)}（下一步会做跨切片 dedup）"
    )
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
