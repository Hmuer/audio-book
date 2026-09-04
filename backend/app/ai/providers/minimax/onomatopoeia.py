"""
中文拟声词 → MiniMax Sound Tag 替换。

MiniMax Speech 2.x（speech-2.8-turbo / speech-02-hd 等）原生支持在文本中
插入形如 (laughs)、(coughs) 的「Sound Tag」，会合成真实的人声效果
（笑声、咳嗽、叹息、抽泣等），而非把"哈哈""咳咳"当字面读出。

本模块负责在送入 TTS 前，把中文小说里常见的拟声词替换为对应的 Sound Tag，
让"哈哈哈哈""咳咳咳咳""呜呜呜"等表达变成拟真声音。

注意：
- 仅适用于 MiniMax Speech 2.x 系列。其他 TTS provider 不一定支持这种语法。
- Sound Tag 不宜连续堆叠太多，否则合成效果会变得不自然。
- 替换时保留前后的标点和上下文，避免破坏对白节奏。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# 中文拟声词 → MiniMax Sound Tag 映射表
# 顺序很重要：更长的先匹配（如"哈哈哈哈"先于"哈哈"），避免短串先被替换。
# 每条 = (正则模式, 替换成的 Sound Tag, 是否吃掉紧邻的标点)
# 这里只用单一映射表，按字面匹配；歧义场景由 _replace_with_context 微调。
_ONOMATOPOEIA_RULES: list[tuple[re.Pattern[str], str]] = [
    # ---------- 笑声 ----------
    # 4 个及以上的"哈" 视作大笑
    (re.compile(r"哈{4,}"), "(laughs)"),
    (re.compile(r"哈{3}"), "(laughs)"),
    (re.compile(r"哈哈"), "(chuckle)"),
    # 呵/嘿 视作轻笑
    (re.compile(r"呵{2,}"), "(chuckle)"),
    (re.compile(r"嘿{2,}"), "(chuckle)"),
    # 嘻嘻嘻
    (re.compile(r"嘻{2,}"), "(chuckle)"),

    # ---------- 咳嗽 / 清嗓 ----------
    (re.compile(r"咳{2,}"), "(coughs)"),
    (re.compile(r"咳哼"), "(clear-throat)"),
    # 单个"咳"如果带标点也认为是咳嗽（如"咳！"），保守处理
    (re.compile(r"咳(?=[！!。])"), "(coughs)"),

    # ---------- 呼吸 / 叹气 ----------
    (re.compile(r"啊{2,}(?=…)"), "(sighs)"),
    (re.compile(r"唉{2,}"), "(sighs)"),
    (re.compile(r"唉"), "(sighs)"),
    (re.compile(r"哎{2,}"), "(sighs)"),
    # 嗯~
    (re.compile(r"嗯{1,}[~～]+"), "(emm)"),

    # ---------- 哭泣 ----------
    (re.compile(r"呜{2,}"), "(crying)"),
    (re.compile(r"哇{2,}(?=[！!。])"), "(crying)"),

    # ---------- 惊讶 ----------
    (re.compile(r"啊{2,}(?=[！!])"), "(gasps)"),
    # 单个"啊！"惊讶
    (re.compile(r"啊(?=[！!])"), "(gasps)"),

    # ---------- 哼 / 不屑 ----------
    (re.compile(r"哼{2,}"), "(snorts)"),
    (re.compile(r"哼(?=[！!。])"), "(snorts)"),

    # ---------- 鼻音 / 吸鼻涕 ----------
    (re.compile(r"哼{1,}嗯"), "(sniffs)"),

    # ---------- 喷嚏 ----------
    (re.compile(r"阿嚏"), "(sneezes)"),
    (re.compile(r"哈嚏"), "(sneezes)"),
]


def replace_onomatopoeia(text: str) -> tuple[str, int]:
    """
    把文本中的中文拟声词替换为 MiniMax Sound Tag。

    Returns:
        (replaced_text, replacement_count)
        - replaced_text: 处理后的文本
        - replacement_count: 本次替换的次数（用于日志统计）
    """
    if not text:
        return text, 0

    replaced = text
    count = 0
    for pattern, tag in _ONOMATOPOEIA_RULES:
        new, n = pattern.subn(tag, replaced)
        if n > 0:
            replaced = new
            count += n

    # 清理连续重复的 Sound Tag：MiniMax 不建议连续堆叠，
    # 若同一种 tag 连续出现（如"(laughs)(laughs)"），合并成一个。
    if count > 0:
        replaced = re.sub(r"(\([a-z-]+\))(?:\s*\1)+", r"\1", replaced)

    return replaced, count


def apply_onomatopoeia(text: str, *, voice_id: str = "") -> str:
    """
    供 TTS provider 调用的入口：
    1) 做拟声词 → Sound Tag 替换；
    2) 记录一条 INFO 日志，便于调试与调参。

    text 为空或只含空白时直接返回，不产生日志噪音。
    """
    if not text or not text.strip():
        return text

    try:
        replaced, count = replace_onomatopoeia(text)
    except Exception as e:
        # 替换失败不应影响主流程，直接返回原文
        logger.warning(
            f"[onomatopoeia] replace failed voice={voice_id} "
            f"chars={len(text)} err={type(e).__name__}: {e}",
            exc_info=False,
        )
        return text

    if count > 0:
        logger.info(
            f"[onomatopoeia] replaced voice={voice_id} "
            f"chars={len(text)} replacements={count}"
        )
        return replaced
    return text
