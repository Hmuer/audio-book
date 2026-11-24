"""产物读写：每期一个目录，状态与审核都落 `meta.json`。

目录布局（默认 `$DATA_DIR/daily/`）：

    daily/
    ├── run_status.json          # 最近一次生成的状态（进度/阶段/错误/日志尾）
    ├── 2026-11-24/
    │   ├── meta.json            # 选题、得分、统计、审核状态
    │   ├── script.md            # 口播全文（TTS 输入）
    │   ├── shownotes.md         # 节目简介（含信源链接）
    │   └── audio.mp3            # 合成音频
    └── ...

为什么用文件而不是 DB：一期节目的产物天然是"文件包"（音频 + 文稿 + 元数据），
审核状态跟在产物旁边最不容易出现"DB 有记录、文件丢了"的撕裂；单机单用户
场景下扫描目录的代价可以忽略。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ...core.config import settings
from .constants import show_config

logger = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AUDIO_NAME = "audio.mp3"
META_NAME = "meta.json"
SCRIPT_NAME = "script.md"
SHOWNOTES_NAME = "shownotes.md"
RUN_STATUS_NAME = "run_status.json"
MAX_LOG_LINES = 200


class DailyError(RuntimeError):
    """可预期的业务错误（API 层转 4xx）。"""


def daily_root() -> Path:
    root = settings.DAILY_OUTPUT_DIR or (Path(settings.DATA_DIR) / "daily")
    return Path(root)


def issue_dir(date: str) -> Path:
    if not DATE_RE.match(date or ""):
        raise DailyError(f"日期格式必须是 YYYY-MM-DD：{date}")
    return daily_root() / date


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_text(path: Path, text: str) -> None:
    _atomic_write(path, text.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------- 单期 ----------

def load_meta(date: str) -> dict[str, Any]:
    meta = read_json(issue_dir(date) / META_NAME)
    if not isinstance(meta, dict):
        raise DailyError(f"没有这一期节目：{date}")
    return meta


def issue_summary(meta: dict[str, Any]) -> dict[str, Any]:
    review = meta.get("review") or {}
    decision = str(review.get("decision") or "")
    status = {"approve": "approved", "reject": "rejected"}.get(decision, "draft")
    date = str(meta.get("date") or "")
    audio_path = issue_dir(date) / AUDIO_NAME if date else None
    return {
        "date": date,
        "title": str(meta.get("title") or ""),
        "status": status,
        "duration_ms": int(meta.get("duration_ms") or 0),
        "item_count": int(meta.get("item_count") or 0),
        "created_at": meta.get("created_at"),
        "reviewed_at": review.get("reviewed_at"),
        "review_note": review.get("note") or None,
        "error": meta.get("error"),
        "has_audio": bool(audio_path and audio_path.is_file()),
    }


def read_issue(date: str) -> dict[str, Any]:
    meta = load_meta(date)
    base = issue_dir(date)

    def _read(name: str) -> str:
        try:
            return (base / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    return {
        "issue": issue_summary(meta),
        "shownotes_md": _read(SHOWNOTES_NAME),
        "script_md": _read(SCRIPT_NAME),
        "meta": meta,
        "audio_path": base / AUDIO_NAME,
    }


def save_issue(
    *,
    date: str,
    title: str,
    script_md: str,
    shownotes_md: str,
    audio: bytes,
    duration_ms: int,
    meta: dict[str, Any],
) -> dict[str, Any]:
    base = issue_dir(date)
    base.mkdir(parents=True, exist_ok=True)
    write_text(base / SCRIPT_NAME, script_md)
    write_text(base / SHOWNOTES_NAME, shownotes_md)
    if audio:
        _atomic_write(base / AUDIO_NAME, audio)
    write_json(base / META_NAME, meta)
    logger.info("[daily] 已落盘 %s（%.1f MB）", date, len(audio) / 1024 / 1024)
    return meta


def update_review(date: str, *, decision: str, note: str, reviewed_by: str) -> dict[str, Any]:
    meta = load_meta(date)
    review = meta.get("review") or {}
    review.update({
        "decision": decision,
        "note": (note or "").strip()[:500],
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "reviewed_by": reviewed_by,
    })
    meta["review"] = review
    write_json(issue_dir(date) / META_NAME, meta)
    logger.info("[daily] 审核 %s → %s by %s", date, decision, reviewed_by)
    return meta


# ---------- 列表 ----------

def list_issues(limit: int = 30) -> list[dict[str, Any]]:
    root = daily_root()
    if not root.is_dir():
        return []
    dates: list[str] = []
    for child in root.iterdir():
        if child.is_dir() and DATE_RE.match(child.name) and (child / META_NAME).is_file():
            dates.append(child.name)
    dates.sort(reverse=True)
    issues: list[dict[str, Any]] = []
    for date in dates[: max(1, min(int(limit), 200))]:
        meta = read_json(issue_dir(date) / META_NAME)
        if isinstance(meta, dict):
            issues.append(issue_summary(meta))
    return issues


def previous_titles(days: int = 3) -> set[str]:
    """近 N 期已讲过的归一化标题（用于去重降权）。"""
    from .cluster import normalize_title

    root = daily_root()
    if not root.is_dir():
        return set()
    dates = sorted(
        (c.name for c in root.iterdir() if c.is_dir() and DATE_RE.match(c.name)),
        reverse=True,
    )[: max(1, int(days))]
    titles: set[str] = set()
    for date in dates:
        meta = read_json(issue_dir(date) / META_NAME)
        if not isinstance(meta, dict):
            continue
        for topic in meta.get("topics") or []:
            title = normalize_title(str((topic or {}).get("title") or ""))
            if title:
                titles.add(title)
    return titles


def next_issue_number() -> int:
    return len(list_issues(limit=500)) + 1


# ---------- 运行状态 ----------

def _run_status_path() -> Path:
    return daily_root() / RUN_STATUS_NAME


def _default_run_status() -> dict[str, Any]:
    return {
        "status": "idle",
        "stage": None,
        "progress": 0,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "date": None,
        "run_id": None,
        "stats": {},
        "log_tail": [],
    }


def read_run_status() -> dict[str, Any]:
    value = read_json(_run_status_path(), None)
    if not isinstance(value, dict):
        return _default_run_status()
    status = _default_run_status()
    status.update(value)
    return status


def write_run_status(**fields: Any) -> dict[str, Any]:
    current = read_run_status()
    current.update(fields)
    write_json(_run_status_path(), current)
    return current


def append_run_log(line: str) -> None:
    current = read_run_status()
    log = list(current.get("log_tail") or [])
    log.append(f"{datetime.now().strftime('%H:%M:%S')} {line}")
    current["log_tail"] = log[-MAX_LOG_LINES:]
    write_json(_run_status_path(), current)
