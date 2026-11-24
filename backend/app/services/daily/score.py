"""打分：乘法模型 + 安全一票否决 + 主题闸门。

	Score = (relevance^w1 × recency^w2 × impact^w3 × narratability^w4)
	        × safety × freshness × demote

任一项趋零则整体归零 —— 这是方案里锁定的第一条关键设计：
**乘法**意味着"某维度很差，其他维度再好也救不回来"；加法会掩盖短板。

ASCII 词（ai / llm / gpu…）一律按词边界匹配，否则 `said` / `email` /
`available` 会让英文源无条件通过闸门。
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from functools import lru_cache

from .constants import (
    DEMOTE_TERMS,
    SAFETY_TERMS,
    SOURCE_WEIGHTS,
    STORY_HINTS,
    TOPIC_TERMS,
)
from .types import Cluster, ScoredTopic

#: 权重（指数）：越大越"挑剔"。相关性最高，其次影响力/可讲述性/新鲜度。
WEIGHTS = {
    "relevance": 1.2,
    "recency": 0.8,
    "impact": 1.0,
    "narratability": 0.9,
}

#: 打分低于该值的选题直接丢弃（0.0 必然丢弃）
MIN_SCORE = 1e-6

_ASCII_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 .\-]*$")


@lru_cache(maxsize=512)
def _ascii_pattern(term: str) -> re.Pattern[str]:
    # 前后都不是字母/数字/下划线才算独立词
    return re.compile(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])")


def has_term(text: str, term: str) -> bool:
    """CJK 走子串；ASCII 走词边界。"""
    if not text or not term:
        return False
    lowered = text.lower()
    if _ASCII_TERM_RE.match(term.lower()):
        return bool(_ascii_pattern(term.lower()).search(lowered))
    return term.lower() in lowered


def count_hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if has_term(text, t)]


def _topic_relevance(title: str, summary: str) -> tuple[float, list[str]]:
    """主题闸门：标题命中 → 1.0；仅摘要命中 ≥2 个 → 0.85；否则 0（淘汰）。

    只看标题会让"标题党/短标题"漏判；只看摘要会让泛资讯混进来 ——
    方案里的分层闸门：标题严格优先 + 摘要多词共振兜底。
    """
    title_hits = count_hits(title, TOPIC_TERMS)
    if title_hits:
        return 1.0, [f"标题命中主题词：{'、'.join(title_hits[:3])}"]
    summary_hits = count_hits(summary, TOPIC_TERMS)
    if len(summary_hits) >= 2:
        return 0.85, [f"摘要多词共振：{'、'.join(summary_hits[:3])}"]
    return 0.0, []


def _recency(published: datetime | None, now: datetime) -> tuple[float, str | None]:
    if published is None:
        return 0.6, "无发布时间，按半数新鲜度计"
    hours = max(0.0, (now - published).total_seconds() / 3600.0)
    # 连续衰减（12 小时半衰期，下限 0.2）。不要用"6 小时内一律 1.0"的分段——
    # 那样同批新闻会大量同分，排序退化成按标题字典序。
    score = max(0.2, math.exp(-hours / 12.0))
    return score, f"发布 {hours:.1f} 小时前"


def _impact(cluster: Cluster) -> tuple[float, str]:
    source = cluster.primary.source
    weight = SOURCE_WEIGHTS.get(source, 1.0)
    # 多源报道同一事件 = 影响更大
    multi = 1.0 + 0.12 * (len(cluster.sources) - 1)
    score = min(1.0, weight * multi)
    detail = f"来源 {source}（{len(cluster.sources)} 源共振）" if len(cluster.sources) > 1 else f"来源 {source}"
    return score, detail


def _narratability(cluster: Cluster) -> tuple[float, list[str]]:
    """启发式"好不好讲"：摘要够长、有里程碑动词、有数字/产品名。"""
    primary = cluster.primary
    score = 0.45
    reasons: list[str] = []
    if len(primary.summary) >= 120:
        score += 0.2
        reasons.append("摘要信息量足够")
    elif len(primary.summary) < 80:
        # 一句话标题（典型英文源）撑不起 12 分钟口播，明确降权
        score -= 0.2
        reasons.append("摘要过短，信息量不足")
    hints = count_hits(primary.title + " " + primary.summary, STORY_HINTS)
    if hints:
        score += 0.2
        reasons.append(f"有事件钩子：{'、'.join(hints[:2])}")
    if re.search(r"\d", primary.title):
        score += 0.1
        reasons.append("标题含数字/版本号")
    return min(1.0, score), reasons


def score_cluster(cluster: Cluster, *, now: datetime | None = None) -> ScoredTopic:
    now = now or datetime.now(UTC)
    primary = cluster.primary
    reasons: list[str] = []

    relevance, topic_reasons = _topic_relevance(primary.title, primary.summary)
    reasons.extend(topic_reasons)

    safety_hits = count_hits(primary.title + " " + primary.summary, SAFETY_TERMS)
    safety = 0.0 if safety_hits else 1.0
    if safety_hits:
        reasons.append(f"安全否决：命中「{'、'.join(safety_hits[:3])}」")

    demote_hits = count_hits(primary.title, DEMOTE_TERMS)
    demote = 0.35 if demote_hits else 1.0
    if demote_hits:
        reasons.append(f"降权：命中「{'、'.join(demote_hits[:2])}」")

    recency, recency_reason = _recency(primary.published, now)
    if recency_reason:
        reasons.append(recency_reason)

    impact, impact_reason = _impact(cluster)
    reasons.append(impact_reason)

    narratability, narr_reasons = _narratability(cluster)
    reasons.extend(narr_reasons)

    base = (
        (relevance ** WEIGHTS["relevance"])
        * (recency ** WEIGHTS["recency"])
        * (impact ** WEIGHTS["impact"])
        * (narratability ** WEIGHTS["narratability"])
    )
    score = base * safety * demote
    if score < MIN_SCORE:
        score = 0.0

    dims = {
        "relevance": round(relevance, 4),
        "recency": round(recency, 4),
        "impact": round(impact, 4),
        "narratability": round(narratability, 4),
        "safety": safety,
        "demote": round(demote, 4),
    }
    return ScoredTopic(cluster=cluster, score=round(score, 6), dims=dims, reasons=reasons)


def rank_topics(
    clusters: list[Cluster], *,
    now: datetime | None = None,
    exclude_titles: set[str] | None = None,
) -> list[ScoredTopic]:
    """打分 + 过滤 0 分 + 历史去重（3 天内出现过的选题降权）。"""
    now = now or datetime.now(UTC)
    excluded = exclude_titles or set()
    topics = [score_cluster(c, now=now) for c in clusters]
    topics = [t for t in topics if t.score > 0]

    from .cluster import normalize_title

    for topic in topics:
        norm = normalize_title(topic.title)
        if norm and norm in excluded:
            topic.score = round(topic.score * 0.15, 6)
            topic.reasons.append("近 3 天已讲过，降权避免重复")

    topics.sort(key=lambda t: (t.score, t.title), reverse=True)
    return topics


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def split_topics(topics: list[ScoredTopic]) -> dict[str, object]:
    """按 1 主 + 2 次 + 5 速览 切分（不足则自动收缩）。

    主话题优先中文：英文源即使分高，也不让整期节目以一条英文一句话新闻开场
    （降低"翻译外媒"观感，也避免 LLM 拿不出事实只能复述标题）。
    """
    ordered = list(topics)
    if ordered and not _CJK_RE.search(ordered[0].title):
        best = ordered[0].score * 0.7
        for idx in range(1, min(4, len(ordered))):
            if _CJK_RE.search(ordered[idx].title) and ordered[idx].score >= best:
                ordered[0], ordered[idx] = ordered[idx], ordered[0]
                break

    # 来源多样性：同一来源最多 2 条（避免整期变成某一家媒体的通稿联播）。
    max_per_source = 2
    picked: list[ScoredTopic] = []
    counts: dict[str, int] = {}
    for topic in ordered:
        if counts.get(topic.source, 0) >= max_per_source:
            continue
        picked.append(topic)
        counts[topic.source] = counts.get(topic.source, 0) + 1
        if len(picked) >= 8:
            break
    # 信源不足时宁可少几条，也不用同一家媒体的稿子硬凑 8 条

    return {
        "main": picked[0] if picked else None,
        "subs": picked[1:3],
        "quicks": picked[3:8],
    }
