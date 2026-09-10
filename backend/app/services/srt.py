"""P1-3 SRT 字幕导出。

数据源：build_worker 写出的 `build_<id>_ch<NN>_timings.json` sidecar，
       里面是按章节累加的 segment 时戳（seg_start_ms, seg_dur_ms, text, speaker, kind）。

策略：
  - SRT 块基本单位 = 段（sentence 级），不强制按词（v3 字级别时间戳联调后才有；
    真实 v3 响应可能根本不带 words / sentence 内嵌，目前退化为"段级起止"）。
  - silence 段不输出字幕条目（静音没有字幕）。
  - 长段（>= 5 秒）按字符数等比切 2~3 块，缓解单条 SRT 太长难以阅读。
  - 时间格式 `HH:MM:SS,mmm`（srt 标准）。
  - 同一 speaker 连续多段 → 在字幕文本前加 `【speaker】` 前缀，便于有视听小说感的用户区分。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _fmt_ts(ms: int) -> str:
    """把 ms 整数格式化为 SRT 时间戳 HH:MM:SS,mmm（不产生负值）。"""
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, ms2 = divmod(rem, 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(ms2):03d}"


def _split_long_entry(text: str, dur_ms: int, max_chars: int = 28) -> list[tuple[str, int]]:
    """长字幕文本按字符数等比切 2~3 段（保留段落感）。"""
    if not text or len(text) <= max_chars or dur_ms <= 0:
        return [(text, dur_ms)]
    # 估算需要切几段：每段 ≈ max_chars
    n = max(2, min(3, (len(text) + max_chars - 1) // max_chars))
    # 简单按字符数等比切分（中文无空格时不按词切，按字符更稳）
    step = len(text) // n or 1
    out: list[tuple[str, int]] = []
    cursor = 0
    pieces: list[str] = []
    for i in range(n - 1):
        end = min(len(text), cursor + step)
        pieces.append(text[cursor:end])
        cursor = end
    pieces.append(text[cursor:])
    # 末尾去空字符串
    pieces = [p for p in pieces if p]
    if not pieces:
        return [(text, dur_ms)]
    per = dur_ms // len(pieces) or dur_ms
    out = [(p, per) for p in pieces]
    # 最后一段把累计误差补回来
    out[-1] = (out[-1][0], dur_ms - per * (len(out) - 1))
    return out


def timings_json_to_srt(timings: dict[str, Any]) -> str:
    """把 build_worker 写出的 sidecar JSON 转成 SRT 文本。

    预期结构：
      {"version": 1, "estimated": bool, "segs": [
        {"kind": "speech"|"silence", "speaker": str, "text": str,
         "start_ms": int, "dur_ms": int}, ...
      ]}
    """
    segs = timings.get("segs") or []
    lines: list[str] = []
    srt_idx = 0
    prev_speaker = ""
    for s in segs:
        if s.get("kind") == "silence":
            prev_speaker = ""
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        start_ms = int(s.get("start_ms") or 0)
        dur_ms = max(0, int(s.get("dur_ms") or 0))
        end_ms = start_ms + dur_ms
        speaker = (s.get("speaker") or "").strip()
        # 前缀标记：与上一段同一 speaker 时不重复【】前缀，避免视觉噪音
        if speaker and speaker != prev_speaker:
            text_with_tag = f"【{speaker}】{text}"
        else:
            text_with_tag = text
        prev_speaker = speaker
        for piece_text, piece_dur in _split_long_entry(text_with_tag, dur_ms):
            srt_idx += 1
            piece_end = start_ms + piece_dur
            lines.append(str(srt_idx))
            lines.append(f"{_fmt_ts(start_ms)} --> {_fmt_ts(piece_end)}")
            lines.append(piece_text)
            lines.append("")  # SRT 条目之间空行
            start_ms = piece_end
    return "\n".join(lines).rstrip() + "\n"


def load_chapter_srt(audio_dir: str | Path, build_id: str, ch_idx: int) -> str | None:
    """从磁盘读章节 sidecar → 转 SRT。

    Args:
        audio_dir: settings.AUDIO_DIR
        build_id: build 标识（不含 "build_" 前缀）
        ch_idx: 章节号（0 起）

    Returns:
        SRT 文本字符串；找不到 sidecar 返回 None（前端可显示"字幕生成中"）。
    """
    timings_path = Path(audio_dir) / f"build_{build_id}_ch{ch_idx:04d}_timings.json"
    if not timings_path.is_file():
        return None
    try:
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    srt = timings_json_to_srt(timings)
    # 全部是 silence / 没文字段 → 不算有效字幕
    if not srt.strip():
        return None
    return srt
