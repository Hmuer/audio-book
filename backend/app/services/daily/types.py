"""流水线内部数据结构（只在本包内使用，不做 API 序列化）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class NewsItem:
    """一条采集到的资讯。"""

    title: str
    link: str
    summary: str
    source: str
    published: datetime | None = None

    @property
    def published_ts(self) -> float:
        return self.published.timestamp() if self.published else 0.0


@dataclass(slots=True)
class Cluster:
    """同一事件的多条报道（跨源合并）。"""

    items: list[NewsItem] = field(default_factory=list)

    @property
    def primary(self) -> NewsItem:
        # 取"信息量最大"的一条：摘要更长优先，其次更新。
        return max(self.items, key=lambda i: (len(i.summary), i.published_ts))

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for it in self.items:
            if it.source not in seen:
                seen.append(it.source)
        return seen


@dataclass(slots=True)
class ScoredTopic:
    """打分后的选题。"""

    cluster: Cluster
    score: float
    dims: dict[str, float]
    reasons: list[str]

    @property
    def title(self) -> str:
        return self.cluster.primary.title

    @property
    def url(self) -> str:
        return self.cluster.primary.link

    @property
    def source(self) -> str:
        return self.cluster.primary.source

    @property
    def summary(self) -> str:
        return self.cluster.primary.summary


@dataclass(slots=True)
class ScriptResult:
    """一期节目的脚本产物。"""

    title: str
    text: str
    shownotes: str
    sections: dict[str, str]
    used_llm: bool
