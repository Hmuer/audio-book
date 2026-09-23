"""章节正文存取层（F-4）。

背景：`Project.chapters_json` 把整本书正文塞在**一列**里，于是「取章节列表 /
看单章详情 / 生成歌词」都要反序列化**全书**（数千章时是几十 MB、每次请求都付一遍）。
本模块把正文落到 `project_chapters` 表（一行一章）：

- 章节列表：只读 `text_len`，完全不碰正文
- 单章详情 / 单章歌词：只读**一行**
- 全书合成 / 批量歌词：一次 `SELECT` 取回，不再解析大 JSON

`Project.chapters_json` 仍会写入，作为**兼容快照**（老代码路径 / 回滚用），但不是读取主路径。

兼容策略（老库没有行数据时）：
- 读取一律**回落到 chapters_json 解析**，保证功能不受影响；
- 回填只发生在两处，避免读路径产生意外的写副作用：
  1. 启动迁移 `session._migrate_existing_sync`（尽力而为，逐项目容错）
  2. 下一次 `save_chapters`（prepare 重跑时自然补齐）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Project, ProjectChapter
from .chapter import Chapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChapterRow:
    """章节摘要（**不含正文**）。"""

    idx: int
    title: str
    text_len: int


def _chapters_from_json(raw: str | None) -> list[Chapter]:
    """兼容回落：解析 chapters_json 快照。"""
    try:
        items = json.loads(raw or "[]")
    except Exception:
        return []
    out: list[Chapter] = []
    for c in items:
        if not isinstance(c, dict):
            continue
        out.append(Chapter(
            idx=int(c.get("idx", 0)),
            title=str(c.get("title", "") or ""),
            text=str(c.get("text", "") or ""),
        ))
    return out


async def save_chapters(
    session: AsyncSession, project_id: str, chapters: list[Chapter]
) -> None:
    """整本覆盖写入 `project_chapters`（prepare 落库时调用）。

    调用方负责提交；这里只做 delete + add_all。
    """
    await session.execute(
        delete(ProjectChapter).where(ProjectChapter.project_id == project_id)
    )
    session.add_all([
        ProjectChapter(
            project_id=project_id,
            idx=c.idx,
            title=(c.title or "")[:512],
            text=c.text or "",
            text_len=len(c.text or ""),
        )
        for c in chapters
    ])


async def load_chapter_rows(session: AsyncSession, project_id: str) -> list[ChapterRow]:
    """章节摘要列表（**不读正文**）。表为空时回落到 chapters_json。"""
    rows = (
        await session.execute(
            select(ProjectChapter.idx, ProjectChapter.title, ProjectChapter.text_len)
            .where(ProjectChapter.project_id == project_id)
            .order_by(ProjectChapter.idx)
        )
    ).all()
    if rows:
        return [ChapterRow(idx=i, title=t or "", text_len=int(n or 0)) for i, t, n in rows]

    proj = await session.get(Project, project_id)
    return [
        ChapterRow(idx=c.idx, title=c.title, text_len=len(c.text))
        for c in _chapters_from_json(proj.chapters_json if proj else None)
    ]


async def load_chapters(session: AsyncSession, project_id: str) -> list[Chapter]:
    """全书章节（含正文）。表为空时回落到 chapters_json。"""
    rows = (
        await session.execute(
            select(ProjectChapter.idx, ProjectChapter.title, ProjectChapter.text)
            .where(ProjectChapter.project_id == project_id)
            .order_by(ProjectChapter.idx)
        )
    ).all()
    if rows:
        return [
            Chapter(idx=int(i), title=t or "", text=x or "") for i, t, x in rows
        ]

    proj = await session.get(Project, project_id)
    return _chapters_from_json(proj.chapters_json if proj else None)


async def load_chapter(
    session: AsyncSession, project_id: str, idx: int
) -> Chapter | None:
    """单章（含正文）—— 只读一行，不再解析全书。"""
    row = (
        await session.execute(
            select(ProjectChapter.idx, ProjectChapter.title, ProjectChapter.text)
            .where(
                ProjectChapter.project_id == project_id,
                ProjectChapter.idx == idx,
            )
            .limit(1)
        )
    ).first()
    if row:
        return Chapter(idx=int(row[0]), title=row[1] or "", text=row[2] or "")

    proj = await session.get(Project, project_id)
    for c in _chapters_from_json(proj.chapters_json if proj else None):
        if c.idx == idx:
            return c
    return None


async def count_chapters(session: AsyncSession, project_id: str) -> tuple[int, int]:
    """(章节数, 总字数) —— 聚合查询，不拉正文。表为空时回落到 chapters_json。"""
    row = (
        await session.execute(
            select(func.count(ProjectChapter.id), func.coalesce(func.sum(ProjectChapter.text_len), 0))
            .where(ProjectChapter.project_id == project_id)
        )
    ).first()
    if row and int(row[0] or 0) > 0:
        return int(row[0]), int(row[1] or 0)

    proj = await session.get(Project, project_id)
    chapters = _chapters_from_json(proj.chapters_json if proj else None)
    return len(chapters), sum(len(c.text) for c in chapters)


def backfill_rows_sync(conn, project_id: str, raw_json: str) -> int:
    """把某个项目的 chapters_json 快照补写进 project_chapters（启动迁移用，同步执行）。

    返回写入行数；解析失败返回 0（调用方只记日志，不阻断启动）。
    """
    from sqlalchemy import text

    try:
        items = json.loads(raw_json or "[]")
    except Exception:
        return 0
    if not isinstance(items, list) or not items:
        return 0

    payload = []
    for c in items:
        if not isinstance(c, dict):
            continue
        txt = str(c.get("text", "") or "")
        payload.append({
            "pid": project_id,
            "idx": int(c.get("idx", 0)),
            "title": str(c.get("title", "") or "")[:512],
            "text": txt,
            "n": len(txt),
        })
    if not payload:
        return 0
    conn.execute(
        text(
            "INSERT INTO project_chapters (project_id, idx, title, text, text_len) "
            "VALUES (:pid, :idx, :title, :text, :n)"
        ),
        payload,
    )
    return len(payload)
