"""脚本：LLM 可插拔，离线模板兜底。

产物：
- `script.md`  ｜口播全文（TTS 输入，纯文本）
- `shownotes.md`（节目简介：话题 + 速览 + 信源链接 + AI 声明）
- `meta.json`（见 store.py）

硬约束（写进 prompt + 后处理双重保证）：
- 只使用给定事实，不编造、不补充外部信息；
- 不做时政/军事/外交评论；
- 有"AI 辅助制作"声明（平台要求，但放在开场价值陈述之后，不喧宾夺主）；
- 总字数受 `DAILY_MAX_SCRIPT_CHARS` 限制（TTS 成本/时长护栏）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ...core.config import settings
from .types import ScoredTopic, ScriptResult
from .localize import topic_text

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")


class DailyScriptLLM(BaseModel):
    """LLM 结构化输出（字段即脚本段落）。"""

    title: str = Field(max_length=40)
    intro: str = Field(max_length=600)
    main: str = Field(max_length=2000)
    sub1: str = Field(max_length=1200)
    sub2: str = Field(max_length=1200)
    quick: str = Field(max_length=1600)
    outro: str = Field(max_length=300)


def _fmt_facts(
    topic: ScoredTopic, index: int,
    localized: dict[str, dict[str, str]] | None = None,
) -> str:
    title, summary = topic_text(topic, localized)
    lines = [
        f"{index}. 标题：{title}",
        f"   来源：{topic.source}",
        f"   摘要：{summary[:300] or '（无摘要）'}",
    ]
    return "\n".join(lines)


def build_prompt(
    topics: dict[str, Any], *, date: str,
    localized: dict[str, dict[str, str]] | None = None,
) -> str:
    main = topics["main"]
    subs = topics["subs"]
    quicks = topics["quicks"]
    parts = [
        "你在为一档 12–15 分钟的科技前沿单人口播播客写脚本（科技为主，包含 AI、芯片、",
        "机器人、航天、生物科技等）。",
        f"日期：{date}。听众：开发者与产品经理。",
        "",
        "【主话题】",
        _fmt_facts(main, 1, localized) if main else "（无）",
        "",
        "【次话题】",
    ]
    for i, t in enumerate(subs, start=1):
        parts.append(_fmt_facts(t, i, localized))
    parts.append("")
    parts.append("【速览（条目数以实际列出为准，每条 1–2 句）】")
    for i, t in enumerate(quicks, start=1):
        parts.append(_fmt_facts(t, i, localized))
    parts.extend([
        "",
        "写作要求：",
        "1. 只用上面给出的事实；不要补充、不要推测、不要编造数字。",
        "2. 口语化、短句、有节奏；不要念 URL、域名，不要提「积分/评论/点赞/排名」，"
        "不要出现 markdown 标题符号。",
        "3. 全篇 3400–3900 字，对应约 12–13 分钟；main 约 1300 字，sub1/sub2 各约 600 字，"
        "quick 约 600 字，intro/outro 各 120 字以内。素材少时用「为什么重要 / 谁受影响 / "
        "接下来看什么」展开每个话题，但**不得编造事实、数字与引语**。",
        "4. intro 必须包含节目名「科技前沿日报」与日期；先讲今天有什么值得听，"
        "最后用一句中性的「本节目为 AI 辅助制作，内容经人工审核」。"
        "不要在开头强调 AI、也不要写「由 AI 生成」。",
        "5. 不做时政、军事、外交评论；观点与事实分离，不写「据传」「据说」；"
        "不要只谈 AI，芯片、机器人、航天、生物科技、能源等科技议题同样优先。",
        "6. title 不超过 24 字，以中文表达为主（专有名词可保留英文），"
        "不要直译腔（例如不要写「与…新负载」），不含日期，不夸张、不制造悬念。",
        "7. 输出 JSON：字段 title / intro / main / sub1 / sub2 / quick / outro。",
    ])
    return "\n".join(parts)


def _fallback_sections(
    topics: dict[str, Any], *, date: str,
    localized: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    main = topics["main"]
    subs = topics["subs"]
    quicks = topics["quicks"]
    show = settings.DAILY_SHOW_TITLE or "科技前沿日报"

    intro = (
        f"欢迎收听《{show}》，今天是{date}。我们用十几分钟，把科技领域——尤其是人工智能"
        f"——最值得关注的进展讲清楚。本节目为 AI 辅助制作，内容经人工审核。"
    )
    if main:
        intro += f"头条是：{topic_text(main, localized)[0]}。"

    def _topic_block(topic: ScoredTopic) -> str:
        title, summary = topic_text(topic, localized)
        summary = summary or "这条消息的具体细节仍在披露中。"
        return f"{title}。{summary} 这条消息来自{topic.source}。"

    main_text = _topic_block(main) + "我们把它拆开来看：它意味着什么，谁会受影响，以及接下来值得观察什么。" if main else ""
    sub_parts: list[str] = []
    for topic in subs:
        sub_parts.append(_topic_block(topic))
    quick_lines: list[str] = []
    for i, topic in enumerate(quicks, start=1):
        title, summary = topic_text(topic, localized)
        quick_lines.append(f"第{'一二三四五'[i - 1] if i <= 5 else i}条，{title}。{summary[:80]}")
    quick = "接下来是几条速览。" + "".join(quick_lines) if quick_lines else "今天没有更多速览。"

    outro = (
        f"以上就是今天的《{show}》。本节目为 AI 辅助制作、内容经人工审核，"
        f"观点仅供参考，所有事实与信源都列在节目简介里。我们明天见。"
    )
    return {
        "intro": intro,
        "main": main_text,
        "sub1": sub_parts[0] if len(sub_parts) > 0 else "",
        "sub2": sub_parts[1] if len(sub_parts) > 1 else "",
        "quick": quick,
        "outro": outro,
    }


def _assemble(sections: dict[str, str]) -> str:
    parts = [sections.get(k, "").strip() for k in ("intro", "main", "sub1", "sub2", "quick", "outro")]
    return "\n\n".join(p for p in parts if p)


def _truncate(text: str, limit: int) -> str:
    """按句号截断到 limit 以内，避免半句。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    matches = list(_SENTENCE_END.finditer(cut))
    if matches:
        return cut[: matches[-1].end()]
    return cut


def _safe_title(raw: str, topics: dict[str, Any]) -> str:
    """LLM 标题的硬约束兜底：不涉安全/降权词、不超长、不带日期。"""
    from .constants import DEMOTE_TERMS, SAFETY_TERMS
    from .score import count_hits

    title = (raw or "").strip().strip("《》")
    bad = count_hits(title, SAFETY_TERMS) or count_hits(title, DEMOTE_TERMS)
    has_date = bool(re.search(r"\d{4}\s*[-年/]", title))
    if not bad and not has_date and 0 < len(title) <= 34:
        return title
    main = topics.get("main")
    short = (main.title if main else "").strip()
    if short and not count_hits(short, SAFETY_TERMS):
        short = re.sub(r"\s+", " ", short)[:24]
    else:
        short = "AI 与科技前沿"
    return f"{settings.DAILY_SHOW_TITLE or '科技前沿日报'}：{short}"


def _build_shownotes(
    title: str, topics: dict[str, Any], *, date: str, used_llm: bool,
    localized: dict[str, dict[str, str]] | None = None,
) -> str:
    show = settings.DAILY_SHOW_TITLE or "科技前沿日报"
    lines = [f"# {title}", "", f"{date} · {show}", ""]
    main = topics["main"]
    if main:
        lines += ["## 本期主话题", f"- [{topic_text(main, localized)[0]}]({main.url}) — {main.source}", ""]
    subs = topics["subs"]
    if subs:
        lines.append("## 次话题")
        for t in subs:
            lines.append(f"- [{topic_text(t, localized)[0]}]({t.url}) — {t.source}")
        lines.append("")
    quicks = topics["quicks"]
    if quicks:
        lines.append("## 速览")
        for t in quicks:
            lines.append(f"- [{topic_text(t, localized)[0]}]({t.url}) — {t.source}")
        lines.append("")
    lines += [
        "## 信源",
        *[
            f"- {t.source}：[{topic_text(t, localized)[0]}]({t.url})"
            for t in [main, *subs, *quicks] if t is not None
        ],
        "",
        "> 本节目为 AI 辅助制作（脚本与语音），内容经人工审核后发布。",
        f"> 脚本生成方式：{'LLM + 规则后处理' if used_llm else '离线模板（未启用 LLM）'}。",
    ]
    return "\n".join(lines)


async def generate_script(
    topics: dict[str, Any], *,
    date: str | None = None,
    show_number: int = 0,
    localized: dict[str, dict[str, str]] | None = None,
) -> ScriptResult:
    """生成脚本：优先 LLM，失败/关闭则模板。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    used_llm = False
    sections = _fallback_sections(topics, date=date, localized=localized)
    title = ""

    if settings.DAILY_LLM_ENABLED:
        try:
            from ...ai.factory import get_llm

            llm = get_llm()
            out: DailyScriptLLM = await llm.chat_structured(
                build_prompt(topics, date=date, localized=localized),
                DailyScriptLLM,
                system_prompt=(
                    "你是一位严谨的 AI 科技播客主编。只使用用户提供的事实，"
                    "不编造、不推测、不做时政评论。输出必须是合法 JSON。"
                ),
                temperature=0.5,
                max_tokens=8000,
            )
            sections = {
                "intro": out.intro,
                "main": out.main,
                "sub1": out.sub1,
                "sub2": out.sub2,
                "quick": out.quick,
                "outro": out.outro,
            }
            title = out.title.strip()
            used_llm = True
        except Exception as e:  # LLM 不可用/配额/校验失败都退回模板
            logger.warning("[daily] LLM 脚本失败，改用模板: %s: %s", type(e).__name__, e)

    text = _assemble(sections)
    if show_number > 0:
        text = text.replace("我是你的 AI 主播。", f"我是你的 AI 主播。这是第 {show_number} 期。", 1)
    text = _truncate(text, int(settings.DAILY_MAX_SCRIPT_CHARS or 4200))

    if not title:
        main = topics["main"]
        short = (topic_text(main, localized)[0] if main else "科技与前沿").strip()
        title = f"{settings.DAILY_SHOW_TITLE or '科技前沿日报'}：{short[:28]}"
    title = _safe_title(title, topics)

    shownotes = _build_shownotes(
        title, topics, date=date, used_llm=used_llm, localized=localized,
    )
    return ScriptResult(
        title=title,
        text=text,
        shownotes=shownotes,
        sections=sections,
        used_llm=used_llm,
    )
