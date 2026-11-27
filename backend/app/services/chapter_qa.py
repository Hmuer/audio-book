"""章节级质检（质量门，M-QA）。

目的：把"整本质检只在最后跑一次、只看时长"升级为**逐章质检**，在合成完成后
立刻给每章打分并给出可疑原因，供前端标红 / 只重跑可疑章。判定维度：

1. 音频时长与文本字数比例（chars/sec）异常 —— 静音占位 / 卡带 / 漏读。
2. 对白覆盖率：确定性引号句数 vs 已识别对白数。漏识别会让旁白平读对白。
3. 未知说话人占比过高 —— 归属失败，回退默认音色。
4. 低置信度说话人占比过高 —— 需要人工复核。

纯函数，不依赖 DB / 网络，便于单测。
"""
from __future__ import annotations

from typing import Any

try:
    from .quote_segmenter import segment_quotes
except Exception:  # pragma: no cover
    segment_quotes = None  # type: ignore

# 正常朗读语速约 3~6 字/秒；0.5~2 倍速都应在该区间内
CPS_MIN = 0.8
CPS_MAX = 25.0

_UNKNOWN_SPEAKERS = {"", "unknown", "未知", "旁白", "其他", "narrator"}


def _field(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def assess_chapter_quality(
    chapter_text: str,
    dialogues: list | None,
    duration_ms: int | None,
) -> dict:
    """返回该章的质检结果。

    结构：{"suspicious": bool, "reasons": [str], "chars": int,
           "chars_per_sec": float|None, "quote_count": int,
           "dialogue_count": int, "low_conf_ratio": float, "unknown_ratio": float}
    """
    text = chapter_text or ""
    dlgs = list(dialogues or [])
    chars = len(text)
    reasons: list[str] = []

    quote_count = 0
    if segment_quotes is not None and text:
        try:
            quote_count = len(segment_quotes(text))
        except Exception:
            quote_count = 0

    dur_s = (duration_ms or 0) / 1000.0
    cps: float | None = None
    if dur_s > 0 and chars > 0:
        cps = chars / dur_s
        if not (CPS_MIN <= cps <= CPS_MAX):
            reasons.append("duration_anomaly")

    dlg_count = len(dlgs)
    # 对白覆盖率：引号句明显多于已识别对白 → 有漏识别（旁白平读）
    if quote_count >= 3 and dlg_count < quote_count * 0.9:
        reasons.append("dialogue_missed")

    low_conf_ratio = 0.0
    unknown_ratio = 0.0
    if dlg_count:
        low = 0
        unk = 0
        for d in dlgs:
            conf = _field(d, "confidence", 1.0)
            try:
                if conf is not None and float(conf) < 0.5:
                    low += 1
            except (TypeError, ValueError):
                pass
            spk = str(_field(d, "speaker", "") or "").strip().lower()
            if spk in _UNKNOWN_SPEAKERS:
                unk += 1
        low_conf_ratio = low / dlg_count
        unknown_ratio = unk / dlg_count
        if unknown_ratio > 0.15:
            reasons.append("many_unknown_speakers")
        if low_conf_ratio > 0.30:
            reasons.append("low_confidence_speakers")

    return {
        "suspicious": bool(reasons),
        "reasons": reasons,
        "chars": chars,
        "chars_per_sec": round(cps, 2) if cps is not None else None,
        "quote_count": quote_count,
        "dialogue_count": dlg_count,
        "low_conf_ratio": round(low_conf_ratio, 3),
        "unknown_ratio": round(unknown_ratio, 3),
    }
