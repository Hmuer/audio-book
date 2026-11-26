"""事件聚类：把同一件事的多个来源合并成一个选题。

做法（刻意保持简单、可解释）：
- 中文按"连续汉字串"切，取 2 字 bigram + 原串（≤6 字）；
- 英文/数字按词切，小写；
- 两个标题的 token 集合 Jaccard ≥ 0.34 视为同一事件；
- 归一化标题完全相同也合并。

为什么不用向量：这一步的召回/精度只要"别把两件事并成一件、别把一件事
拆成两件"，token 重叠对新闻标题足够；向量反而不可解释、要额外依赖。
"""
from __future__ import annotations

import re
from typing import Iterable

from .types import Cluster, NewsItem

_ASCII_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
#: 标题里无信息量的词（合并前先剔除）
_STOP_TOKENS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "by", "at", "from", "as", "it", "its",
    "的", "了", "和", "与", "在", "是", "有", "对", "为", "将", "把", "被",
}

#: Jaccard 阈值（0.34 在真实中文标题上：两个报道同一事件的标题通常 ≥0.4，
#: 不同事件通常 ≤0.2；取中间偏保守，宁拆勿并）
MERGE_THRESHOLD = 0.34


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    for word in _ASCII_RE.findall(text):
        if len(word) >= 2 and word not in _STOP_TOKENS:
            tokens.add(word)
    for run in _CJK_RE.findall(text):
        if len(run) <= 2:
            tokens.add(run)
            continue
        if len(run) <= 6:
            tokens.add(run)
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in _STOP_TOKENS:
                tokens.add(bigram)
    return tokens


def normalize_title(title: str) -> str:
    """去掉空白与常见标点，用于"完全同名"判断。"""
    text = re.sub(r"[\s\-—_|:：,，.。!！?？\"'“”‘’()（）\[\]【】]+", "", title or "")
    return text.lower()


def overlap_coefficient(a: set[str], b: set[str]) -> float:
    """重叠系数：交集 / 较小集合（对"短标题 vs 长标题"比 Jaccard 更敏感）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def lcs_len(a: str, b: str, cap: int = 120) -> int:
    """最长公共子串长度（归一化标题，用于识别同一公司的不同措辞）。"""
    x, y = (a or "")[:cap], (b or "")[:cap]
    if not x or not y:
        return 0
    prev = [0] * (len(y) + 1)
    best = 0
    for i in range(1, len(x) + 1):
        cur = [0] * (len(y) + 1)
        for j in range(1, len(y) + 1):
            if x[i - 1] == y[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def cluster_items(items: Iterable[NewsItem], *, threshold: float = MERGE_THRESHOLD) -> list[Cluster]:
    """按时间倒序扫描，把相似条目并进已有簇。"""
    ordered = sorted(items, key=lambda i: (i.published_ts, i.title), reverse=True)
    clusters: list[Cluster] = []
    cluster_tokens: list[set[str]] = []
    cluster_titles: list[str] = []
    cluster_sources: list[set[str]] = []

    for item in ordered:
        tokens = tokenize(item.title)
        norm = normalize_title(item.title)
        best_idx = -1
        best_score = 0.0
        for idx, existing in enumerate(cluster_tokens):
            if norm and cluster_titles[idx] and (
                norm == cluster_titles[idx]
                or (len(norm) >= 8 and (norm in cluster_titles[idx] or cluster_titles[idx] in norm))
            ):
                best_idx = idx
                break
            # 同一来源 + 标题有 ≥5 字连续公共片段（常见于同一公司的两种措辞）→ 视为同一事件
            if item.source in cluster_sources[idx] and lcs_len(norm, cluster_titles[idx]) >= 5:
                best_idx = idx
                break
            score = jaccard(tokens, existing)
            overlap = overlap_coefficient(tokens, existing)
            if score >= threshold or overlap >= 0.45:
                if score > best_score:
                    best_idx, best_score = idx, score
        if best_idx >= 0:
            clusters[best_idx].items.append(item)
            cluster_tokens[best_idx] |= tokens
            cluster_titles[best_idx] = cluster_titles[best_idx] or norm
            cluster_sources[best_idx].add(item.source)
        else:
            clusters.append(Cluster(items=[item]))
            cluster_tokens.append(set(tokens))
            cluster_titles.append(norm)
            cluster_sources.append({item.source})

    # 每簇内部按信息量排序：摘要长的在前（作为 primary）
    for cluster in clusters:
        cluster.items.sort(key=lambda i: (len(i.summary), i.published_ts), reverse=True)
    return clusters
