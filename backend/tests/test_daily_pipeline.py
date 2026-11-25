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
    summary_hit = score_cluster(_cluster("某公司宣布新进展", "本次发布涉及大模型、算力与智能体调度。"), now=now)
    assert summary_hit.score > 0
    assert summary_hit.dims["relevance"] == 0.85

    # 只有 2 个摘要词 → 不再通过（聚合/泛科技内容太多，闸门收紧）
    weak = score_cluster(_cluster("某公司宣布新进展", "本次发布涉及大模型与调度。"), now=now)
    assert weak.score == 0


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
    assert "AI 辅助" in result.text
    assert "由 AI 生成" not in result.text
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
    assert 'url="https://pod.example.com/api/daily/issues/2025-11-24/audio.mp3?key=secret-key"' in xml
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
    assert "AI 辅助" in detail["script_md"]
    assert "由 AI 生成" not in detail["script_md"]
    assert "https://example.com/" in detail["shownotes_md"]

    status = store.read_run_status()
    assert status["status"] == "success"
    assert status["progress"] == 100


def test_circuit_breaker_after_two_failures(monkeypatch, tmp_path):
    from backend.app.core import config as cfgmod
    from backend.app.services.daily import pipeline, store

    monkeypatch.setattr(cfgmod.settings, "DAILY_OUTPUT_DIR", tmp_path / "daily")
    store.write_run_status(status="running", consecutive_failures=0, circuit_open=False)

    pipeline._record_failure("第一次失败")
    assert store.read_run_status()["consecutive_failures"] == 1
    assert store.read_run_status()["circuit_open"] is False

    pipeline._record_failure("第二次失败")
    status = store.read_run_status()
    assert status["consecutive_failures"] == 2
    assert status["circuit_open"] is True

    # 熔断后 start_run 必须拒绝（且不能置 _running）
    import pytest as _pytest
    with _pytest.raises(Exception) as exc:
        pipeline.start_run(date="2025-11-24")
    assert "熔断" in str(exc.value)
    assert pipeline.is_running() is False


def test_safety_terms_veto_politics_and_finance():
    now = datetime.now(UTC)
    for title in (
        "Who in Washington owns and trades Nvidia stock",
        "国会听证会讨论 AI 监管",
        "英伟达股价创下新高",
    ):
        topic = score_cluster(_cluster(title, "AI 相关新闻，nvidia 与算力。"), now=now)
        assert topic.score == 0, title


def test_safe_title_falls_back_on_date_or_veto():
    from backend.app.services.daily.script import _safe_title

    topic = ScoredTopic(
        cluster=_cluster("OpenAI 发布新模型", "模型发布。"),
        score=1.0, dims={}, reasons=[],
    )
    topics = {"main": topic}
    # 带日期 → 兜底
    assert "2025" not in _safe_title("2025-11-25 AI 日报", topics)
    # 命中安全词 → 兜底
    assert "华盛顿" not in _safe_title("华盛顿AI监管新进展", topics)
    # 正常标题原样保留
    assert _safe_title("OpenAI 发布新模型", topics) == "OpenAI 发布新模型"


def test_previous_titles_excludes_target_date(monkeypatch, tmp_path):
    import json as _json

    from backend.app.core import config as cfgmod
    from backend.app.services.daily import store

    monkeypatch.setattr(cfgmod.settings, "DAILY_OUTPUT_DIR", tmp_path / "daily")
    today = datetime.now(UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_s = today.isoformat()
    for date, title in ((yesterday, "旧闻甲"), (today_s, "今日乙")):
        base = tmp_path / "daily" / date
        base.mkdir(parents=True)
        (base / "meta.json").write_text(
            _json.dumps({"date": date, "title": title, "topics": [{"title": title}]}),
            encoding="utf-8",
        )
    titles = store.previous_titles(days=3, exclude_date=today_s)
    assert normalize_title("旧闻甲") in titles
    assert normalize_title("今日乙") not in titles


def test_clean_title_strips_emoji_codes_and_punctuation():
    from backend.app.services.daily.collect import clean_title

    assert clean_title(":fire::fire: Claude Code 最佳实践") == "Claude Code 最佳实践"
    assert clean_title("【重磅】OpenAI 发布新模型") == "OpenAI 发布新模型"
    assert clean_title("  --- AI 日报") == "AI 日报"


def test_split_topics_limits_single_source():
    from backend.app.services.daily.types import Cluster, ScoredTopic

    def mk(title: str, source: str, score: float) -> ScoredTopic:
        return ScoredTopic(cluster=_cluster(title, "摘要" * 60, source=source), score=score, dims={}, reasons=[])

    topics = [
        mk("雷峰网新闻一", "雷峰网", 1.0),
        mk("雷峰网新闻二", "雷峰网", 0.99),
        mk("雷峰网新闻三", "雷峰网", 0.98),
        mk("量子位新闻一", "量子位", 0.9),
        mk("量子位新闻二", "量子位", 0.89),
        mk("量子位新闻三", "量子位", 0.88),
        mk("钛媒体新闻一", "钛媒体", 0.7),
    ]
    selected = split_topics(topics)
    picked = [t for t in (selected["main"], *selected["subs"], *selected["quicks"]) if t]
    per_source: dict[str, int] = {}
    for t in picked:
        per_source[t.source] = per_source.get(t.source, 0) + 1
    assert all(n <= 2 for n in per_source.values()), per_source
    # 3 个来源、每家最多 2 条 → 最多 5 条；信源不足时不硬凑
    assert len(picked) == 5


def test_feed_description_uses_shownotes_excerpt(tmp_path):
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.services.daily import store

    date = "2025-11-24"
    base = tmp_path / "daily" / date
    base.mkdir(parents=True)
    (base / "audio.mp3").write_bytes(make_silent_mp3(100))
    (base / "shownotes.md").write_text(
        "# 本期标题\n\n## 主话题\n- [OpenAI 发布新模型](https://example.com/a)\n\n信源说明",
        encoding="utf-8",
    )
    meta = {
        "date": date, "title": "标题", "created_at": "2025-11-24T08:00:00",
        "duration_ms": 100, "item_count": 1, "topics": [], "sections": {},
        "stats": {}, "show": {},
        "review": {"decision": None, "note": "", "reviewed_at": None, "reviewed_by": None},
    }
    (base / "meta.json").write_text(__import__("json").dumps(meta), encoding="utf-8")

    xml = build_feed(
        [store.issue_summary(meta)], base_url="https://pod.example.com",
        feed_key="k", issue_dir=tmp_path / "daily",
    )
    # 第一个 <description> 是频道描述，item 描述在后面
    desc = [ln for ln in xml.splitlines() if "<description>" in ln][-1]
    assert "OpenAI 发布新模型" in desc
    assert "https://example.com/a" not in desc


def test_failure_triggers_alert(monkeypatch, tmp_path):
    from backend.app.core import config as cfgmod
    from backend.app.services.daily import pipeline, store

    monkeypatch.setattr(cfgmod.settings, "DAILY_OUTPUT_DIR", tmp_path / "daily")
    monkeypatch.setattr(cfgmod.settings, "DAILY_ALERT_WEBHOOK", "https://hook.example/x")
    captured: list[str] = []
    monkeypatch.setattr(pipeline, "_notify_alert", lambda msg: captured.append(msg))

    store.write_run_status(status="running", consecutive_failures=1, circuit_open=False)
    pipeline._record_failure("boom")

    assert captured and "生成失败" in captured[0] and "boom" in captured[0]
    status = store.read_run_status()
    assert status["consecutive_failures"] == 2
    assert status["circuit_open"] is True


def test_cross_day_dedupe_excludes_repeat_and_demotes_similar():
    from backend.app.services.daily.score import rank_topics

    now = datetime.now(UTC)
    a = _cluster("OpenAI 发布 GPT-6 模型", "新模型发布。")
    b = _cluster("英伟达发布新一代 GPU", "数据中心芯片。")
    c = _cluster("某 AI 公司开源智能体框架", "开源框架。")

    # 1) 相同 URL（标题不同）→ 直接排除
    dropped: list = []
    history_url = [{"date": "2025-11-20", "title": "旧闻标题", "url": a.primary.link}]
    kept = rank_topics([a, b, c], now=now, history=history_url, dropped=dropped)
    assert len(dropped) == 1 and "近期已讲过" in dropped[0].reasons[-1]
    assert len(kept) == 2

    # 2) 标题高度相似 → 直接排除
    dropped2: list = []
    history_title = [{"date": "2025-11-20", "title": "OpenAI 发布 GPT-6 模型", "url": "https://other.example/x"}]
    kept2 = rank_topics([a, b, c], now=now, history=history_title, dropped=dropped2)
    assert len(dropped2) == 1 and "标题相似" in dropped2[0].reasons[-1]
    assert len(kept2) == 2

    # 3) 中等相似度（约 0.3–0.45）→ 降权但保留
    topic = _cluster("苹果发布 AI 芯片", "芯片发布。")
    history_similar = [{"date": "2025-11-20", "title": "苹果 AI 芯片 量产 计划", "url": "https://other.example/y"}]
    baseline = rank_topics([topic], now=now)
    demoted = rank_topics([topic], now=now, history=history_similar)
    assert len(baseline) == 1 and len(demoted) == 1
    assert demoted[0].score < baseline[0].score
    assert "降权" in demoted[0].reasons[-1]


def test_split_topics_dedupes_same_entity_stories():
    from backend.app.services.daily.types import ScoredTopic

    def mk(title: str, score: float, source: str) -> ScoredTopic:
        return ScoredTopic(cluster=_cluster(title, "摘要" * 60, source=source), score=score, dims={}, reasons=[])

    topics = [
        mk("Jensen Huang’s Return Shows Nvidia Is Too Big to Police", 0.9, "Wired AI"),
        mk("Nvidia CEO Jensen Huang is back. What happens next?", 0.88, "The Verge AI"),
        mk("Anthropic tops 700 million MCP SDK downloads", 0.8, "The Decoder"),
        mk("Google releases new Gemini model", 0.7, "Google AI Blog"),
    ]
    picked = split_topics(topics)
    titles = [t.title for t in (picked["main"], *picked["subs"], *picked["quicks"]) if t]
    # 前两条共享 nvidia/jensen/huang → 只保留一条
    assert sum(1 for t in titles if "Nvidia" in t or "nvidia" in t) == 1
    assert len(titles) >= 3


def test_general_tech_source_requires_title_hit():
    now = datetime.now(UTC)
    # 开源中国属于泛科技源：标题不含 AI 词，摘要即使共振也不选
    item = _item(
        "酷瓜云课堂 v2.0.2 发布，修复 nginx 与 php 漏洞",
        "本次更新涉及大模型、算力与智能体相关的演示模块。",
        source="开源中国",
    )
    assert score_cluster(Cluster(items=[item]), now=now).score == 0

    # AI 垂类源允许摘要兜底
    item2 = _item("某公司宣布新进展", "本次发布涉及大模型、算力与智能体调度。", source="TechCrunch AI")
    assert score_cluster(Cluster(items=[item2]), now=now).score > 0


def test_effective_sources_merges_new_defaults(monkeypatch, tmp_path):
    """旧偏好只保存了 8 个源时，升级后新增的内置源也要自动出现（不被锁死）。"""
    import json as _json

    from backend.app.core import config as cfgmod
    from backend.app.services.daily import prefs

    monkeypatch.setattr(cfgmod.settings, "DAILY_OUTPUT_DIR", tmp_path / "daily")
    (tmp_path / "daily").mkdir(parents=True)
    saved = [
        {"name": "量子位", "url": "https://www.qbitai.com/feed", "enabled": False, "custom": False},
        {"name": "我的源", "url": "https://custom.example/rss", "enabled": True, "custom": True},
    ]
    (tmp_path / "daily" / "prefs.json").write_text(
        _json.dumps({"sources": saved}), encoding="utf-8",
    )

    out = prefs.effective_sources()
    urls = {s["url"] for s in out}
    assert len(out) >= 20, "新增内置源应自动合并进来"
    assert "https://www.leiphone.com/feed" in urls
    assert "https://custom.example/rss" in urls
    qbit = next(s for s in out if s["url"] == "https://www.qbitai.com/feed")
    assert qbit["enabled"] is False, "旧偏好里的启停状态要保留"
    custom = next(s for s in out if s["url"] == "https://custom.example/rss")
    assert custom["custom"] is True
