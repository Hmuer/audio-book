"""AI 前沿日报：默认信源与关键词表。

这些表是对附件方案 §二「三个关键设计」的直接实现：

- `TOPIC_TERMS` 严格：决定"什么算 AI/科技新闻"（闸门）；
- `SAFETY_TERMS` 一票否决：时政 / 军事 / 外交 / 社会突发，规避平台规则；
- `DEMOTE_TERMS` 降权：招聘、课程、促销等低信息量内容；
- ASCII 词一律按**词边界**匹配（`ai` 不能命中 `said` / `email` / `available`）。

信源列表可用 `DAILY_SOURCES_JSON` 整体覆盖；关键词表暂不支持运行时覆盖
（P2 再开），改这里即可。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ...core.config import settings

logger = logging.getLogger(__name__)

#: 默认信源：自有 RSS / 官方博客即可，不做外媒翻译与复述。
#: 服务器在国内，部分源可能超时/403 —— collect 阶段逐源容错，不阻塞整期。
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"name": "机器之心", "url": "https://www.jiqizhixin.com/rss", "enabled": True},
    {"name": "量子位", "url": "https://www.qbitai.com/feed", "enabled": True},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage?points=100", "enabled": True},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "enabled": True},
    {"name": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "enabled": True},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/", "enabled": True},
    {"name": "MIT Tech Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed", "enabled": True},
]

#: 主题闸门：标题命中任意一个即视为 AI/科技话题；标题未命中时要求摘要命中 ≥2 个。
TOPIC_TERMS: list[str] = [
    "ai", "aigc", "llm", "agent", "agentic", "rag", "mcp", "copilot",
    "人工智能", "大模型", "模型", "算法", "算力", "智能体", "多模态", "推理",
    "训练", "微调", "开源", "芯片", "gpu", "数据中心", "机器人", "自动驾驶",
    "生成式", "语音合成", "语音识别", "计算机视觉", "具身智能", "transformer",
    "openai", "deepseek", "anthropic", "claude", "gemini", "llama", "qwen",
    "英伟达", "nvidia", "智谱", "月之暗面", "字节", "阿里云", "腾讯云", "华为云",
]

#: 安全一票否决：命中即当期不选（宁可少一期，不要一次事故）。
SAFETY_TERMS: list[str] = [
    "时政", "政治局", "外交", "军事", "国防", "战争", "台海", "南海", "领导人",
    "选举", "抗议", "骚乱", "恐怖袭击", "事故", "死亡", "遇难", "疫情", "灾难",
    "地震", "洪灾", "火灾", "枪击", "制裁", "关税",
]

#: 降权词：低信息量的运营类内容。
DEMOTE_TERMS: list[str] = [
    "招聘", "课程", "培训", "报名", "直播预告", "促销", "优惠", "抽奖",
    "广告", "赞助", "带货", "峰会报名", "白皮书下载", "问卷调查",
]

#: 信源权重（未列出的按 1.0）。
SOURCE_WEIGHTS: dict[str, float] = {
    "机器之心": 1.1,
    "量子位": 1.05,
    "Hacker News": 1.0,
    "TechCrunch AI": 1.0,
    "The Verge AI": 0.95,
    "VentureBeat AI": 0.9,
    "MIT Tech Review AI": 1.0,
}

#: 可增强"可讲述性"的信号词（融资/产品/研究里程碑）。
STORY_HINTS: list[str] = [
    "发布", "推出", "开源", "融资", "收购", "突破", "首次", "刷新", "超越",
    "上线", "公测", "论文", "benchmark", "基准", "评测", "涨价", "降价",
]


def _parse_json_list(raw: str, name: str) -> list[str] | None:
    if not (raw or "").strip():
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("[daily] %s 不是合法 JSON，使用内置默认值", name)
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        logger.warning("[daily] %s 必须是字符串数组，使用内置默认值", name)
        return None
    return [x.strip() for x in value]


def source_list() -> list[dict[str, Any]]:
    """读取信源配置（JSON 覆盖，缺省内置）。只接受 http(s) URL。"""
    raw = (settings.DAILY_SOURCES_JSON or "").strip()
    if not raw:
        return [dict(s) for s in DEFAULT_SOURCES]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("[daily] DAILY_SOURCES_JSON 解析失败，使用内置默认源")
        return [dict(s) for s in DEFAULT_SOURCES]
    if not isinstance(value, list):
        logger.warning("[daily] DAILY_SOURCES_JSON 必须是数组，使用内置默认源")
        return [dict(s) for s in DEFAULT_SOURCES]
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "name": str(item.get("name") or url).strip()[:40],
            "url": url,
            "enabled": bool(item.get("enabled", True)),
        })
    return out or [dict(s) for s in DEFAULT_SOURCES]


def show_config() -> dict[str, Any]:
    """节目信息（页面 / RSS / meta 共用）。"""
    return {
        "title": settings.DAILY_SHOW_TITLE or "AI 前沿日报",
        "description": settings.DAILY_SHOW_DESCRIPTION or "",
        "author": settings.DAILY_SHOW_AUTHOR or "",
    }
