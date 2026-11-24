"""AI 前沿日报：纯逻辑单测（不联网、不碰 DB）。

覆盖方案 §二 的三个关键设计与流水线各阶段的边界：
- RSS / Atom 解析与 HTML 清洗
- 事件聚类：同一事件合并、不同事件不合并
- 打分：乘法模型、安全一票否决、ASCII 词边界、降权词
- 脚本：离线模板兜底 + AI 声明 + 标题不含日期
- TTS 分段：不切断句子、不超段长
- RSS：enclosure 绝对地址 + key 鉴权 + 文件大小
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.services.daily.cluster import cluster_items, normalize_title, tokenize
from backend.app.services.daily.collect import parse_feed_xml, strip_html
from backend.app.services.daily.feed import build_feed
from backend.app.services.daily.score import rank_topics, score_cluster, split_topics
from backend.app.services.daily.tts import split_segments
from backend.app.services.daily.types import Cluster, NewsItem, ScoredTopic

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item>
  <title>OpenAI 发布新模型</title>
  <link>https://example.com/a</link>
  <description>&lt;p&gt;这是&lt;b&gt;摘要&lt;/b&gt;内容。&lt;/p&gt;</description>
  <pubDate>Mon, 24 Nov 2025 10:00:00 GMT</pubDate>
</item>
</channel></rss>""".encode("utf-8")

ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>AI chip startup raises funding</title>
  <link href="https://example.com/b"/>
  <summary>summary text</summary>
  <updated>2025-11-24T10:00:00Z</updated>
</entry>
</feed>""".encode("utf-8")


def test_parse_rss_and_atom():
    items = parse_feed_xml(RSS_XML, "测试源")
    assert len(items) == 1
    assert items[0].title == "OpenAI 发布新模型"
    assert items[0].link == "https://example.com/a"
    assert "摘要" in items[0].summary and "<" not in items[0].summary
    assert items[0].published is not None and items[0].published.tzinfo is not None

    atom = parse_feed_xml(ATOM_XML, "Atom源")
    assert len(atom) == 1
    assert atom[0].link == "https://example.com/b"
    assert atom[0].published is not None


def test_strip_html_unescapes_and_collapses():
    assert strip_html("<p>a &amp; b</p>\n\n  c") == "a & b c"
    assert len(strip_html("x" * 500)) == 400


def _item(title: str, summary: str = "", source: str = "机器之心", hours_ago: float = 1.0) -> NewsItem:
    now = datetime.now(UTC)
    return NewsItem(
        title=title,
        link=f"https://example.com/{abs(hash(title)) % 10**8}",
        summary=summary,
        source=source,
        published=now - timedelta(hours=hours_ago),
    )


def test_cluster_merges_same_event_but_not_different_ones():
    a = _item("OpenAI 发布 GPT-6 模型，推理能力大幅提升", "新模型在推理基准上提升明显。")
    b = _item("OpenAI 发布 GPT-6：推理能力大幅提升", "官方称推理能力提升。", source="量子位")
    c = _item("小米发布新款手机", "影像升级。", source="TechCrunch AI")

    clusters = cluster_items([a, b, c])
    assert len(clusters) == 2
    merged = [cl for cl in clusters if len(cl.items) == 2]
    assert len(merged) == 1
    assert set(merged[0].sources) == {"机器之心", "量子位"}


def test_tokenize_keeps_ascii_words_and_cjk_bigrams():
    tokens = tokenize("OpenAI 发布 GPT-6 模型")
    assert "openai" in tokens and "gpt" in tokens
    assert "发布" in tokens and "模型" in tokens
    assert normalize_title("OpenAI：发布 GPT-6！") == "openai发布gpt6"


def _cluster(title: str, summary: str = "", source: str = "机器之心", hours_ago: float = 1.0) -> Cluster:
    return Cluster(items=[_item(title, summary, source, hours_ago)])


def test_score_gate_and_multiplier():
    now = datetime.now(UTC)
    good = score_cluster(_cluster("OpenAI 发布 GPT-6 模型，推理能力大幅提升", "新模型发布，基准评测提升 30%。" * 3), now=now)
    assert good.score > 0
    assert good.dims["relevance"] == 1.0

    # 标题里的 "said"/"email" 不能命中 "ai"（词边界）
    irrelevant = score_cluster(_cluster("Said the CEO in an email", "available soon"), now=now)
    assert irrelevant.score == 0

    # 摘要命中 2 个主题词也算相关
    summary_hit = score_cluster(_cluster("某公司宣布新进展", "本次发布涉及大模型与算力调度。"), now=now)
    assert summary_hit.score > 0
    assert summary_hit.dims["relevance"] == 0.85


def test_score_safety_veto_and_demote():
    now = datetime.now(UTC)
    vetoed = score_cluster(_cluster("AI 芯片出口与关税谈判", "涉及芯片与关税。"), now=now)
    assert vetoed.score == 0

    demoted = score_cluster(_cluster("AI 课程报名开启", "培训课程报名。"), now=now)
    assert demoted.dims["demote"] < 1.0
    assert demoted.score > 0


def test_rank_topics_filters_zero_and_splits():
    now = datetime.now(UTC)
    clusters = [
        _cluster("OpenAI 发布 GPT-6 模型，推理能力大幅提升", "基准评测提升。"),
        _cluster("英伟达发布新一代 GPU，算力提升", "数据中心芯片。"),
        _cluster("某 AI 公司开源智能体框架", "开源框架发布。"),
        _cluster("Said the CEO in an email", "available soon"),
    ]
    topics = rank_topics(clusters, now=now)
    assert len(topics) == 3
    selected = split_topics(topics)
    assert selected["main"] is not None
    assert len(selected["quicks"]) == 0  # 只有 3 个选题时没有速览

    # 历史去重降权
    excluded = {normalize_title(topics[0].title)}
    reranked = rank_topics(clusters, now=now, exclude_titles=excluded)
    assert reranked[0].title != topics[0].title


@pytest.mark.asyncio
async def test_script_fallback_has_disclosure_and_sources(monkeypatch):
    from backend.app.core import config as cfgmod
    from backend.app.services.daily.script import generate_script

    monkeypatch.setattr(cfgmod.settings, "DAILY_LLM_ENABLED", False)

    topic = ScoredTopic(
        cluster=_cluster("OpenAI 发布 GPT-6 模型", "官方称推理能力提升。"),
        score=1.0, dims={}, reasons=[],
    )
    result = await generate_script(
        {"main": topic, "subs": [], "quicks": []}, date="2025-11-24",
    )
    assert "AI 生成" in result.text
    assert "2025-11-24" in result.text
    assert result.used_llm is False
    assert "https://example.com/" in result.shownotes
    assert "2025-11-24" not in result.title


def test_split_segments_keeps_sentences_whole():
    text = "第一句话。第二句话！第三句话？" * 10
    # 函数把段长下限钳在 40，避免切得过碎
    segments = split_segments(text, max_chars=40)
    assert segments
    assert all(len(s) <= 40 for s in segments)
    assert "".join(segments).replace(" ", "") == text.replace(" ", "")


def test_feed_enclosure_is_absolute_and_keyed(tmp_path):
    from backend.app.core.mp3_util import make_silent_mp3

    date = "2025-11-24"
    meta = {
        "date": date, "title": "AI 前沿日报：测试", "created_at": "2025-11-24T08:00:00",
        "duration_ms": 500, "item_count": 3,
        "topics": [], "sections": {}, "stats": {}, "show": {},
        "review": {"decision": None, "note": "", "reviewed_at": None, "reviewed_by": None},
    }
    mp3 = make_silent_mp3(500)
    from backend.app.services.daily import store as store_mod

    # 直接写文件（store.save_issue 需要 settings，用 monkeypatch 改目录）
    base = tmp_path / "daily" / date
    base.mkdir(parents=True)
    (base / "meta.json").write_text(__import__("json").dumps(meta), encoding="utf-8")
    (base / "audio.mp3").write_bytes(mp3)

    xml = build_feed(
        [store_mod.issue_summary(meta)],
        base_url="https://pod.example.com",
        feed_key="secret-key",
        issue_dir=tmp_path / "daily",
    )
    assert 'url="https://pod.example.com/api/daily/issues/2025-11-24/audio?key=secret-key"' in xml
    assert f'length="{len(mp3)}"' in xml
    assert "<itunes:explicit>false</itunes:explicit>" in xml


@pytest.mark.asyncio
async def test_run_pipeline_end_to_end(monkeypatch, tmp_path):
    """整条流水线（采集/TTS 用桩）：落盘产物 + run_status=success + 可读详情。"""
    from backend.app.core import config as cfgmod
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.services.daily import pipeline, store
    from backend.app.services.daily.types import NewsItem

    monkeypatch.setattr(cfgmod.settings, "DAILY_OUTPUT_DIR", tmp_path / "daily")
    monkeypatch.setattr(cfgmod.settings, "DAILY_LLM_ENABLED", False)

    now = datetime.now(UTC)

    def _news(title: str, summary: str, source: str = "机器之心") -> NewsItem:
        return NewsItem(
            title=title, link=f"https://example.com/{abs(hash(title))}",
            summary=summary, source=source, published=now - timedelta(hours=1),
        )

    items = [
        _news("OpenAI 发布 GPT-6 模型，推理能力大幅提升",
              "官方称新模型在推理基准上提升 30%，并开放 API。"),
        _news("英伟达发布新一代 GPU，算力提升", "新卡面向数据中心，训练成本下降。", "量子位"),
        _news("某 AI 公司开源智能体框架", "开源框架支持多工具调用与记忆。", "TechCrunch AI"),
    ]

    async def fake_collect(sources, **_kw):
        return items, [{"source": "坏源", "url": "https://bad.example", "error": "HTTP 403"}]

    async def fake_synthesize(text, **_kw):
        return make_silent_mp3(300), 300, {"segments": 1, "chars": len(text), "voice_id": "mock"}

    monkeypatch.setattr(pipeline, "collect", fake_collect)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)

    meta = await pipeline.run_pipeline(date="2025-11-24")

    assert meta["date"] == "2025-11-24"
    assert meta["item_count"] == 3
    assert meta["topics"], "应至少产出一个选题"
    assert meta["stats"]["used_llm"] is False
    assert meta["review"]["decision"] is None

    detail = store.read_issue("2025-11-24")
    assert detail["issue"]["has_audio"] is True
    assert detail["issue"]["duration_ms"] == 300
    assert "AI 生成" in detail["script_md"]
    assert "https://example.com/" in detail["shownotes_md"]

    status = store.read_run_status()
    assert status["status"] == "success"
    assert status["progress"] == 100
