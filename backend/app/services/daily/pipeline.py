"""编排：采集 → 聚类 → 打分 → 脚本 → TTS → 落盘。

一次运行是一期节目；同一时刻只允许一个运行（模块级 `_running` 标志在
`start_run()` 里**同步**置位，避免 API 双击/并发请求创建两个任务）。

失败处理：
- 任何阶段抛错 → run_status 置 failed，写错误与日志尾，**不落半成品**；
- 人工可以在 UI 上重跑（同日重跑会覆盖该期）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ...core.config import settings
from . import localize, prefs, store, subtitles
from .cluster import cluster_items
from .collect import collect
from .constants import show_config
from .score import rank_topics, split_topics
from .script import generate_script
from .store import DailyError
from .tts import synthesize

logger = logging.getLogger(__name__)

_running = False
_task: asyncio.Task | None = None

#: 连续失败多少期触发熔断（方案 §五：连续 2 期异常 → 自动停更并告警）
CIRCUIT_FAILURES = 2


def is_running() -> bool:
    return _running


def _stage(stage: str, progress: int, message: str) -> None:
    store.write_run_status(stage=stage, progress=progress)
    store.append_run_log(message)


def start_run(*, date: str | None = None, max_items: int | None = None) -> dict[str, Any]:
    """启动一次后台生成（同步返回 run_id；真正的活由事件循环跑）。"""
    global _running, _task
    if _running:
        raise DailyError("已有生成任务在运行，请等待本期完成")
    if store.read_run_status().get("circuit_open"):
        raise DailyError(
            f"连续 {CIRCUIT_FAILURES} 期生成失败，已自动熔断；"
            "请检查日志并点「重置熔断」后再试"
        )
    date = date or datetime.now().strftime("%Y-%m-%d")
    store.issue_dir(date)  # 校验日期格式
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if date > today:
        raise DailyError(f"不能生成未来日期：{date}（今天是 {today}）")
    now = store.utcnow_iso()
    run_id = f"run_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    store.write_run_status(
        status="running", stage="queued", progress=0,
        started_at=now, finished_at=None, error=None,
        date=date, run_id=run_id, stats={}, log_tail=[],
    )
    store.append_run_log(f"[start] 开始生成 {date}（run_id={run_id}）")
    _running = True
    _task = asyncio.create_task(_run_guarded(date=date, max_items=max_items))
    return {"run_id": run_id, "started_at": now, "date": date}


async def _post_alert(url: str, message: str) -> None:
    """尽力而为的告警推送：失败只记日志，绝不影响流水线状态。"""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={
                "msgtype": "text",
                "text": {"content": message},
                "message": message,
            })
        logger.info("[daily] 告警已推送")
    except Exception as e:
        logger.warning("[daily] 告警推送失败: %s: %s", type(e).__name__, e)


def _notify_alert(message: str) -> None:
    """在事件循环内异步推送（调用方是 async/启动恢复；不在循环里就跳过）。"""
    url = (settings.DAILY_ALERT_WEBHOOK or "").strip()
    if not url:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[daily] 无事件循环，跳过告警：%s", message)
        return
    loop.create_task(_post_alert(url, message))


def _record_failure(message: str) -> None:
    """记录失败并维护熔断计数（连续 N 期失败 → circuit_open）。"""
    status = store.read_run_status()
    failures = int(status.get("consecutive_failures") or 0) + 1
    circuit = failures >= CIRCUIT_FAILURES
    store.write_run_status(
        status="failed",
        error=message,
        stage="failed",
        progress=0,
        finished_at=store.utcnow_iso(),
        consecutive_failures=failures,
        circuit_open=circuit,
    )
    store.append_run_log(f"[failed] {message}")
    _notify_alert(
        f"[科技前沿日报] 生成失败（连续第 {failures} 次）\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"错误：{message}\n"
        f"状态：{'已熔断，需人工重置' if circuit else '可重试'}"
    )
    if circuit:
        store.append_run_log(
            f"[circuit] 连续 {failures} 期失败，已熔断；需人工重置后才会恢复"
        )


async def _run_guarded(*, date: str, max_items: int | None) -> None:
    global _running, _task
    try:
        await run_pipeline(date=date, max_items=max_items)
    except DailyError as e:
        logger.warning("[daily] 生成失败：%s", e)
        _record_failure(str(e))
    except Exception as e:
        logger.exception("[daily] 生成异常")
        _record_failure(f"{type(e).__name__}: {e}")
    finally:
        _running = False
        _task = None


def _topic_public(
    topic: Any, localized: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    title, summary = localize.topic_text(topic, localized)
    return {
        "title": title,
        "title_original": topic.title if title != topic.title else None,
        "url": topic.url,
        "source": topic.source,
        "summary": (summary or "")[:400],
        "summary_original": (
            (topic.summary or "")[:400] if summary != topic.summary else None
        ),
        "score": topic.score,
        "dims": topic.dims,
        "reasons": topic.reasons,
    }


async def run_pipeline(*, date: str | None = None, max_items: int | None = None) -> dict[str, Any]:
    """同步执行一期生成（被 `start_run` 包成后台任务；测试也直接调它）。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    store.issue_dir(date)
    started = datetime.now()

    _stage("collect", 10, "[collect] 开始采集")
    sources = prefs.effective_sources()
    items, errors = await collect(
        sources,
        window_hours=settings.DAILY_WINDOW_HOURS,
        max_items=max_items or settings.DAILY_MAX_ITEMS,
    )
    if not items:
        raise DailyError(f"没有采集到任何条目（信源 {len(sources)} 个，失败 {len(errors)} 个）")
    store.append_run_log(f"[collect] {len(items)} 条候选，失败源 {len(errors)}")

    _stage("cluster", 30, "[cluster] 事件去重")
    clusters = cluster_items(items)
    store.append_run_log(f"[cluster] {len(clusters)} 个事件簇")

    _stage("score", 45, "[score] 打分与闸门")
    history = store.previous_topics(
        days=int(settings.DAILY_DEDUPE_DAYS or 7), exclude_date=date,
    )
    dropped: list = []
    topics = rank_topics(clusters, history=history, dropped=dropped)
    if len(topics) < 3:
        raise DailyError(f"有效选题不足（闸门后 {len(topics)} 条），本期跳过")
    selected = split_topics(topics)
    selected_topics = [
        t for t in (selected["main"], *selected["subs"], *selected["quicks"])
        if t is not None
    ]
    store.append_run_log(
        f"[score] 保留 {len(topics)} 个选题，跨期重复排除 {len(dropped)} 个"
    )

    _stage("localize", 52, "[localize] 英文选题中文化")
    localized = await localize.localize_topics(selected_topics)
    if localized:
        store.append_run_log(f"[localize] 翻译 {len(localized)} 条英文选题")

    _stage("script", 60, "[script] 生成口播稿")
    script = await generate_script(
        selected, date=date, show_number=store.next_issue_number(),
        localized=localized,
    )
    store.append_run_log(
        f"[script] {len(script.text)} 字，{'LLM' if script.used_llm else '模板'}"
    )

    _stage("tts", 75, "[tts] 合成音频")
    voices = await prefs.available_voices()
    voice_id = prefs.resolve_voice_id(voices)
    voice_name = prefs.voice_name_of(voices, voice_id)
    audio, duration_ms, tts_stats = await synthesize(script.text, voice_id=voice_id)
    timings = tts_stats.pop("segment_timings", [])
    srt = (
        subtitles.build_from_timings(timings)
        if timings else subtitles.build_from_script(script.text, duration_ms)
    )
    lrc = (
        subtitles.build_lrc_from_timings(timings)
        if timings else subtitles.build_lrc_from_script(script.text, duration_ms)
    )
    store.append_run_log(
        f"[tts] {tts_stats['segments']} 段 / {duration_ms / 1000:.0f}s / "
        f"{len(audio) / 1024 / 1024:.1f} MB / 字幕 {srt.count('-->')} 条"
    )

    _stage("store", 92, "[store] 写入产物")
    meta: dict[str, Any] = {
        "date": date,
        "title": script.title,
        "created_at": store.utcnow_iso(),
        "duration_ms": int(duration_ms),
        "item_count": len(selected_topics),
        "topics": [_topic_public(t, localized) for t in selected_topics],
        "sections": script.sections,
        "stats": {
            "candidates": len(items),
            "clusters": len(clusters),
            "scored": len(topics),
            "kept": len(selected_topics),
            "duplicates_removed": len(dropped),
            "history_days": int(settings.DAILY_DEDUPE_DAYS or 7),
            "used_llm": script.used_llm,
            "tts": tts_stats,
            "subtitles_cues": srt.count("-->"),
            "translated_topics": len(localized),
            "source_errors": errors,
            "elapsed_seconds": None,
        },
        "review": {
            "decision": None,
            "note": "",
            "reviewed_at": None,
            "reviewed_by": None,
        },
        "show": show_config(),
        "voice": {"id": voice_id, "name": voice_name},
    }
    meta["stats"]["elapsed_seconds"] = round((datetime.now() - started).total_seconds(), 1)
    store.save_issue(
        date=date,
        title=script.title,
        script_md=script.text,
        shownotes_md=script.shownotes,
        audio=audio,
        duration_ms=int(duration_ms),
        meta=meta,
        subtitles_srt=srt,
        subtitles_lrc=lrc,
    )

    store.write_run_status(
        status="success",
        stage="done",
        progress=100,
        finished_at=store.utcnow_iso(),
        error=None,
        date=date,
        stats=meta["stats"],
        consecutive_failures=0,
        circuit_open=False,
    )
    store.append_run_log(
        f"[done] {date} 完成，用时 {meta['stats']['elapsed_seconds']}s"
    )
    return meta


def recover_stale_run() -> bool:
    """进程重启后清理"假运行中"状态。

    场景：生成到一半进程被杀/重启，`run_status.json` 仍是 running，但内存里
    没有任务了 —— UI 会永远显示"生成中"。启动时把这种状态改成 failed，
    让用户能直接重跑。
    """
    status = store.read_run_status()
    if status.get("status") != "running" or _running:
        return False
    failures = int(status.get("consecutive_failures") or 0) + 1
    store.write_run_status(
        status="failed",
        stage="failed",
        error="上次生成未完成（服务重启/进程退出），请重新触发生成",
        finished_at=store.utcnow_iso(),
        consecutive_failures=failures,
        circuit_open=failures >= CIRCUIT_FAILURES,
    )
    store.append_run_log("[recover] 检测到未完成的运行，已置为 failed")
    return True
