"""
歌词生成：把一次 Build 的章节音频 + 段级时间轴导出为 LRC。

时间轴数据源（按优先级）：
1. 章节时间轴 sidecar JSON（build_<bid>_ch<N>_timings.json，合成时写入，逐段真实时长）
2. 无 sidecar（旧 build / sidecar 丢失）→ 用 _build_segments_for_chapter 重切分段，
   按字符占比把 BuildArtifact.duration_ms 分摊到各段（估算，误差秒级）

输出：
- LRC：**按章**生成（每章一个 .lrc，时间轴为该章内相对时间，与章节 MP3 对齐）；
  对白行带说话人前缀，用于歌词滚动
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sqlalchemy import select

from ..core.config import settings
from ..db.models import Build, BuildArtifact, Project, ProjectDialogue
from ..db.session import get_session_factory
from .build import _timings_filename
from .chapter import Chapter, _build_segments_for_chapter

logger = logging.getLogger(__name__)


def _fmt_lrc_ts(ms: int) -> str:
    """LRC 时间戳：[mm:ss.xx]"""
    ms = max(int(ms), 0)
    m, rem = divmod(ms, 60_000)
    s, milli = divmod(rem, 1000)
    return f"[{m:02d}:{s:02d}.{milli // 10:02d}]"


# LRC 单行内的空白（含全角空格）折叠成一个半角空格
_LRC_WS = re.compile(r"[ \t\u3000]+")
# 段落分隔（\n / \r\n / \r）
_LRC_BREAK = re.compile(r"[\r\n]+")
# 行内是否含可展示字符（\w 在 Unicode 下可匹配中日韩汉字；纯标点/引号行为 False）
_LRC_HAS_WORD = re.compile(r"\w", re.UNICODE)
# 归属出错时残留在旁白段首尾的孤立引号
_LRC_ORPHAN_QUOTES = "“”「」『』\"'‘’"


def _normalize_lrc_line(line: str) -> str:
    """把一段文本规整为单行 LRC 文本；无实际内容（纯标点/引号）时返回空串。"""
    t = _LRC_WS.sub(" ", line or "").strip()
    t = t.strip(_LRC_ORPHAN_QUOTES).strip()
    if not t or not _LRC_HAS_WORD.search(t):
        return ""
    return t


def _iter_cue_lines(e: dict, *, with_speaker: bool) -> list[tuple[int, str]]:
    """把一个 cue 拆成若干 (start_ms, text) —— 每个元素对应一行 LRC。

    为什么必须拆：narrator 段常由多个段落拼成（见 chapter._split_long_text，
    段落以 "\\n" 连接）。若整段直接写成 "[ts]段1\\n段2"，则段 2 那行没有时间戳，
    LRC 播放器只认行首时间戳 → 这些没有时间戳的文本永远不滚动、时间轴错乱。

    拆行后按字符占比把该段的 dur_ms 分摊到各行（段内估算，误差被限制在单段内）。
    """
    kind = e.get("kind") or "narrator"
    speaker = e.get("speaker") or ""
    texts: list[str] = []
    for raw in _LRC_BREAK.split(e.get("text") or ""):
        t = _normalize_lrc_line(raw)
        if t:
            texts.append(t)
    if not texts:
        return []

    prefix = f"{speaker}：" if (with_speaker and kind == "dialogue" and speaker) else ""
    start_ms = int(e.get("start_ms") or 0)
    if len(texts) == 1:
        return [(start_ms, prefix + texts[0])]

    weights = [max(len(t), 1) for t in texts]
    total_w = sum(weights)
    dur_ms = int(e.get("dur_ms") or 0)
    out: list[tuple[int, str]] = []
    cursor = start_ms
    for t, w in zip(texts, weights):
        out.append((cursor, prefix + t))
        cursor += int(dur_ms * w / total_w)
    return out


def _load_sidecar(build_id: str, ch_idx: int) -> dict | None:
    p = Path(settings.AUDIO_DIR) / _timings_filename(build_id, ch_idx)
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("segs"), list):
                return data
    except Exception as e:
        logger.warning(f"[subtitles] 读 sidecar 失败 {p.name}: {type(e).__name__}: {e}")
    return None


def _estimate_chapter_segments(
    segs, total_dur_ms: int
) -> list[dict]:
    """无 sidecar 的回退：按字符占比把章节时长分摊到各段。"""
    weights: list[int] = []
    for s in segs:
        if s.kind == "silence":
            weights.append(max(int(s.silence_ms or 0), 1))
        else:
            weights.append(max(len(s.text or ""), 1))
    sum_w = sum(weights) or 1
    entries = []
    cursor = 0
    for s, w in zip(segs, weights):
        dur = int(total_dur_ms * w / sum_w)
        entries.append({
            "kind": s.kind,
            "speaker": s.speaker or "",
            "text": s.text or "",
            "start_ms": cursor,
            "dur_ms": dur,
        })
        cursor += dur
    return entries


async def _collect_build_segments(
    build_id: str, *, ch_idx: int | None = None, require_final: bool = True
) -> list[dict]:
    """收集 build 的 cue 段（ch_idx 为空时收全部章节）。

    返回：[{"chapter_idx","title","kind","speaker","text","start_ms","dur_ms","estimated"}...]
    start_ms 为"章内相对时间"。
    ch_idx 非空时只收集该章（减少无谓计算）。
    require_final 为 True 时要求 build 已成功/部分成功（对外 API 的兜底校验）。
    Build worker 在 _finalize 打包 ZIP 时终态尚未写回，需传 require_final=False。
    """
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b:
            raise ValueError(f"Build 不存在: {build_id}")
        if require_final and b.status not in ("success", "partial_success"):
            raise ValueError(f"Build 尚未完成（status={b.status}），无法生成歌词")
        proj = await s.get(Project, b.project_id)
        chapters_dicts = json.loads(proj.chapters_json or "[]") if proj else []
        chapters = [
            Chapter(idx=c["idx"], title=c.get("title", ""), text=c.get("text", ""))
            for c in chapters_dicts
        ]
        stmt_d = select(ProjectDialogue).where(ProjectDialogue.project_id == b.project_id)
        all_dialogues = list((await s.execute(stmt_d)).scalars().all())
        stmt_a = (
            select(BuildArtifact)
            .where(BuildArtifact.build_id == build_id)
            .order_by(BuildArtifact.chapter_idx)
        )
        artifacts = list((await s.execute(stmt_a)).scalars().all())

    dialogues_by_chapter: dict[int, list] = {}
    for d in all_dialogues:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)
    art_by_idx = {a.chapter_idx: a for a in artifacts}

    narrator_voice_id = b.narrator_voice_id or ""
    try:
        voice_assignments = json.loads(b.voice_assignments_json or "{}")
    except Exception:
        voice_assignments = {}

    out: list[dict] = []
    for ch in chapters:
        if ch_idx is not None and ch.idx != ch_idx:
            continue
        art = art_by_idx.get(ch.idx)
        if not art or not art.audio_filename or art.duration_ms is None:
            # 失败章（1s 静音占位）没有意义歌词，跳过
            continue
        ch_dur = int(art.duration_ms or 0)
        sidecar = _load_sidecar(build_id, ch.idx)
        if sidecar:
            entries = sidecar["segs"]
            estimated = bool(sidecar.get("estimated"))
        else:
            segs, _ = _build_segments_for_chapter(
                ch, dialogues_by_chapter.get(ch.idx, []),
                narrator_voice_id=narrator_voice_id,
                voice_assignments=voice_assignments,
                segment_overrides=None,
                start_idx=0,
            )
            entries = _estimate_chapter_segments(segs, ch_dur)
            estimated = True
        for e in entries:
            if not (e.get("text") or "").strip():
                continue
            out.append({
                "chapter_idx": ch.idx,
                "title": ch.title,
                "kind": e.get("kind") or "narrator",
                "speaker": e.get("speaker") or "",
                "text": e.get("text") or "",
                "start_ms": int(e.get("start_ms") or 0),
                "dur_ms": int(e.get("dur_ms") or 0),
                "estimated": estimated,
            })
    return out


def _dialogue_text_set(segs: list[dict]) -> set[str]:
    """本章所有对白行的归一化文本集合（不含说话人前缀）。

    用途：对白锚点没盖住原文引号时，同一句会既留在旁白切片里、又作为 dialogue 段
    出现一次 —— 歌词里就重复了。用它把旁白中的重复行剔掉。
    """
    out: set[str] = set()
    for e in segs:
        if (e.get("kind") or "") != "dialogue":
            continue
        for raw in _LRC_BREAK.split(e.get("text") or ""):
            t = _normalize_lrc_line(raw)
            if t:
                out.add(t)
    return out


async def generate_chapter_lrc(
    build_id: str,
    ch_idx: int,
    *,
    title: str | None = None,
    with_speaker: bool = True,
    require_final: bool = True,
) -> tuple[str, str]:
    """生成单章 LRC 歌词。返回 (download_filename, content_text)。

    时间轴用章内相对时间（与章节 MP3 对齐），不做整本书章间偏移。
    require_final：对外 API 兜底校验 build 是否终态（worker 打包 ZIP 时传 False）。
    """
    segs = await _collect_build_segments(
        build_id, ch_idx=ch_idx, require_final=require_final
    )
    if not segs:
        raise ValueError(f"章节 {ch_idx} 没有可用的章节音频…无法生成歌词")

    body: list[str] = []
    last_text = ""
    dialogue_texts = _dialogue_text_set(segs)
    for e in segs:
        is_dialogue = (e.get("kind") or "") == "dialogue"
        for ms, text in _iter_cue_lines(e, with_speaker=with_speaker):
            # 旁白切片里混入的对白原文（锚点没盖住引号）已由 dialogue 行呈现，去重
            if not is_dialogue and text in dialogue_texts:
                continue
            # 相邻重复行去重
            if text == last_text:
                continue
            body.append(f"{_fmt_lrc_ts(ms)}{text}")
            last_text = text
    content = "\n".join(body)

    clean_title = (title or "").strip()
    fname = f"{clean_title or f'第{ch_idx + 1:03d}章'}.lrc"
    return fname, content
