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

from ...core.config import settings
from . import store
from .cluster import cluster_items
from .collect import collect
from .constants import show_config, source_list
from .score import rank_topics, split_topics
from .script import generate_script
from .store import DailyError
from .tts import synthesize

logger = logging.getLogger(__name__)

_running = False
_task: asyncio.Task | None = None


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
    date = date or datetime.now().strftime("%Y-%m-%d")
    store.issue_dir(date)  # 校验日期格式
    now = datetime.now().isoformat(timespec="seconds")
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


async def _run_guarded(*, date: str, max_items: int | None) -> None:
    global _running, _task
    try:
        await run_pipeline(date=date, max_items=max_items)
    except DailyError as e:
        logger.warning("[daily] 生成失败：%s", e)
        store.write_run_status(
            status="failed", error=str(e), stage="failed", progress=0,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        store.append_run_log(f"[failed] {e}")
    except Exception as e:
        logger.exception("[daily] 生成异常")
        store.write_run_status(
            status="failed", error=f"{type(e).__name__}: {e}", stage="failed", progress=0,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        store.append_run_log(f"[failed] {type(e).__name__}: {e}")
    finally:
        _running = False
        _task = None


def _topic_public(topic: Any) -> dict[str, Any]:
    return {
        "title": topic.title,
        "url": topic.url,
        "source": topic.source,
        "summary": (topic.summary or "")[:400],
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
    sources = source_list()
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
    topics = rank_topics(clusters, exclude_titles=store.previous_titles(days=3))
    if len(topics) < 3:
        raise DailyError(f"有效选题不足（闸门后 {len(topics)} 条），本期跳过")
    selected = split_topics(topics)
    store.append_run_log(f"[score] 保留 {len(topics)} 个选题，主 1 / 次 2 / 速览 5")

    _stage("script", 60, "[script] 生成口播稿")
    script = await generate_script(
        selected, date=date, show_number=store.next_issue_number(),
    )
    store.append_run_log(
        f"[script] {len(script.text)} 字，{'LLM' if script.used_llm else '模板'}"
    )

    _stage("tts", 75, "[tts] 合成音频")
    audio, duration_ms, tts_stats = await synthesize(
        script.text, voice_id=settings.DAILY_TTS_VOICE_ID,
    )
    store.append_run_log(
        f"[tts] {tts_stats['segments']} 段 / {duration_ms / 1000:.0f}s / "
        f"{len(audio) / 1024 / 1024:.1f} MB"
    )

    _stage("store", 92, "[store] 写入产物")
    selected_topics = [
        t for t in (selected["main"], *selected["subs"], *selected["quicks"]) if t is not None
    ]
    meta: dict[str, Any] = {
        "date": date,
        "title": script.title,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "duration_ms": int(duration_ms),
        "item_count": len(items),
        "topics": [_topic_public(t) for t in selected_topics],
        "sections": script.sections,
        "stats": {
            "candidates": len(items),
            "clusters": len(clusters),
            "scored": len(topics),
            "kept": len(selected_topics),
            "used_llm": script.used_llm,
            "tts": tts_stats,
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
    )

    store.write_run_status(
        status="success",
        stage="done",
        progress=100,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        error=None,
        date=date,
        stats=meta["stats"],
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
    store.write_run_status(
        status="failed",
        stage="failed",
        error="上次生成未完成（服务重启/进程退出），请重新触发生成",
        finished_at=datetime.now().isoformat(timespec="seconds"),
    )
    store.append_run_log("[recover] 检测到未完成的运行，已置为 failed")
    return True
