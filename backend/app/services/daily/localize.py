"""英文选题中文化：把外媒标题/摘要翻成中文，供口播脚本与选题明细使用。

为什么单独一步：
- 海外源占多数时，若直接拿英文标题写脚本，LLM 容易直译、生硬，UI 上也全是英文；
- 统一先做一次「标题 + 摘要」翻译（一次批量 LLM 调用，8 条左右），
  脚本、节目简介、选题明细都消费中文版本，原文保留在 meta 里可追溯。

失败策略：翻译失败不阻塞出片 —— 保留英文原文，run_summary 里记 warning。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .types import ScoredTopic

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class _LocalizedTopic(BaseModel):
    url: str = Field(max_length=500)
    title: str = Field(max_length=40)
    summary: str = Field(default="", max_length=200)


class _LocalizedBatch(BaseModel):
    items: list[_LocalizedTopic]


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def needs_translation(topic: ScoredTopic) -> bool:
    """标题或摘要基本是英文 → 需要翻译。"""
    title = topic.title or ""
    summary = topic.summary or ""
    return not has_cjk(title) or (len(summary) >= 40 and not has_cjk(summary))


def _build_prompt(topics: list[ScoredTopic]) -> str:
    parts = [
        "把下面的英文科技新闻标题与摘要翻译成简体中文，用于中文播客节目与选题列表。",
        "",
        "要求：",
        "1. 标题 ≤24 个汉字，通顺、像中文科技媒体标题，不要直译腔；",
        "2. 摘要 ≤120 个汉字，只保留事实，不补充、不评价、不评论政治；",
        "3. 公司/产品/人名等专有名词保留英文（如 OpenAI、Claude、Nvidia）；",
        "4. `url` 字段原样返回，用来对齐；",
        "5. 只输出 JSON：{\"items\":[{\"url\":...,\"title\":...,\"summary\":...}]}。",
        "",
    ]
    for idx, topic in enumerate(topics, start=1):
        parts += [
            f"{idx}. url: {topic.url}",
            f"   title: {topic.title}",
            f"   summary: {(topic.summary or '')[:300]}",
        ]
    return "\n".join(parts)


async def localize_topics(topics: list[ScoredTopic]) -> dict[str, dict[str, str]]:
    """返回 {url: {"title": 中文, "summary": 中文}}；失败返回空 dict。"""
    targets = [t for t in topics if needs_translation(t)]
    if not targets:
        return {}
    try:
        from ...ai.factory import get_llm

        result: _LocalizedBatch = await get_llm().chat_structured(
            _build_prompt(targets),
            _LocalizedBatch,
            system_prompt=(
                "你是中文科技媒体的资深编译，擅长把英文科技新闻改写成自然的中文标题与摘要。"
                "只翻译事实，不添加观点，不做政治评论，输出合法 JSON。"
            ),
            temperature=0.3,
            max_tokens=3000,
        )
    except Exception as e:
        logger.warning("[daily] 英文选题翻译失败，保留原文: %s: %s", type(e).__name__, e)
        return {}

    wanted = {t.url for t in targets}
    mapping: dict[str, dict[str, str]] = {}
    for item in result.items:
        if item.url in wanted and has_cjk(item.title):
            mapping[item.url] = {
                "title": item.title.strip(),
                "summary": (item.summary or "").strip(),
            }
    logger.info("[daily] 翻译完成 %d/%d 条英文选题", len(mapping), len(targets))
    return mapping


def topic_text(topic: Any, localized: dict[str, dict[str, str]] | None) -> tuple[str, str]:
    """取展示用（标题, 摘要）：优先中文翻译，否则原文。"""
    if localized:
        item = localized.get(getattr(topic, "url", "") or "")
        if item:
            return item.get("title") or topic.title, item.get("summary") or topic.summary
    return topic.title, topic.summary
