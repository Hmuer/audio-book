"""字幕：从 TTS 分段计时生成 SRT。

喜马拉雅上传页需要「字幕文件」（SRT）。日报是单人口播，最准确的来源是 TTS
合成的**分段真实时长**：每段文本 → 按标点切成 ≤22 字的字幕条 → 按字数占比
分配该段时长。老期数没有计时数据时，用「全文按字数比例投影到总时长」兜底，
误差会比真实分段大，但比没有字幕强。
"""
from __future__ import annotations

import re
from typing import Any

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;…\n])")
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、：:])")
MAX_CUE = 22
MIN_CUE_MS = 700


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def split_cues(text: str, max_chars: int = MAX_CUE) -> list[str]:
    """把一段文本切成适合做字幕的短句（优先按句号，再按逗号）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    cues: list[str] = []
    for sentence in _SENT_SPLIT.split(raw):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            cues.append(sentence)
            continue
        buf = ""
        for clause in _CLAUSE_SPLIT.split(sentence):
            clause = clause.strip()
            if not clause:
                continue
            if len(buf) + len(clause) <= max_chars:
                buf += clause
            else:
                if buf:
                    cues.append(buf)
                buf = clause
                while len(buf) > max_chars:
                    cues.append(buf[:max_chars])
                    buf = buf[max_chars:]
        if buf:
            cues.append(buf)
    # 合并过短的尾巴
    merged: list[str] = []
    for cue in cues:
        if merged and len(cue) < 4 and len(merged[-1]) + len(cue) <= max_chars:
            merged[-1] += cue
        else:
            merged.append(cue)
    return merged


def build_srt(rows: list[tuple[int, int, str]]) -> str:
    """rows: [(start_ms, end_ms, text)] → SRT 文本。"""
    lines: list[str] = []
    cursor = 0
    for idx, (start, end, text) in enumerate(rows, start=1):
        start = max(int(start), cursor)
        end = max(int(end), start + 200)
        cursor = end
        lines += [str(idx), f"{_ts(start)} --> {_ts(end)}", text.strip(), ""]
    return "\n".join(lines)


def build_from_timings(timings: list[dict[str, Any]]) -> str:
    """按 TTS 分段真实时长生成字幕。timings: [{text,start_ms,duration_ms}]。"""
    rows: list[tuple[int, int, str]] = []
    for item in timings or []:
        text = str(item.get("text") or "")
        start = int(item.get("start_ms") or 0)
        duration = int(item.get("duration_ms") or 0)
        cues = split_cues(text)
        if not cues or duration <= 0:
            continue
        total_chars = sum(len(c) for c in cues) or 1
        acc = start
        for cue in cues:
            span = max(MIN_CUE_MS, int(duration * len(cue) / total_chars))
            rows.append((acc, acc + span, cue))
            acc += span
    return build_srt(rows)


def build_from_script(text: str, duration_ms: int) -> str:
    """兜底：没有分段计时（老期数）时按字数比例投影到总时长。"""
    cues = split_cues(text)
    if not cues or duration_ms <= 0:
        return ""
    total_chars = sum(len(c) for c in cues) or 1
    rows: list[tuple[int, int, str]] = []
    acc = 0
    for cue in cues:
        span = max(MIN_CUE_MS, int(duration_ms * len(cue) / total_chars))
        rows.append((acc, acc + span, cue))
        acc += span
    return build_srt(rows)
