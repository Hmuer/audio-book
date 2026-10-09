"""H-19 日志滚动专项测试：按天滚动 + gzip 压缩 + 按保留天数清理。

背景：旧 RotatingFileHandler(10MB×5) 无压缩无按天切片，大书 prepare 期间
app.log 刷屏且 5 份很快滚没。新方案 _DailyGzipFileHandler（main.py）：
- 每天 0 点滚动：app.log → app.log.YYYY-MM-DD → 压成 .gz
- 保留最近 LOG_RETENTION_DAYS 天（默认 14），启动/滚动时都清理
- 保留期从 settings 实时读（改配置立即生效）
"""
from __future__ import annotations

import gzip
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest


def _make_handler(tmp_path: Path) -> object:
    from backend.app.main import _DailyGzipFileHandler

    log_file = tmp_path / "app.log"
    return _DailyGzipFileHandler(filename=str(log_file), encoding="utf-8")


def _write_and_rollover(handler, content: str) -> None:
    """写入一条日志并模拟"跨过午夜后的滚动"。

    rolloverAt 拨到当前时刻 → 归档日期 = rolloverAt - interval = 昨天，
    与真实滚动（午夜后首写触发）的时间轴一致，且不会落入保留期清理
    （⚠️ 不能拨到 0：那会滚出 1969-12-31 的归档，被按天清理当场删掉
    —— 恰是本测试首轮翻车的根因，也反向验证了清理逻辑在真实工作）。
    """
    import time as _time

    handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, content, None, None))
    handler.flush()
    handler.rolloverAt = int(_time.time())
    handler.doRollover()


def test_h19_rollover_compresses_to_gz(tmp_path):
    """滚动后归档变成 .gz 且原裸归档被删、新日志继续写入新 app.log。"""
    from datetime import timedelta

    h = _make_handler(tmp_path)
    try:
        _write_and_rollover(h, "day-one-logs" * 100)

        yesterday = date.today() - timedelta(days=1)
        archive = tmp_path / f"app.log.{yesterday:%Y-%m-%d}.gz"
        assert archive.exists(), f"应存在昨天的 .gz 归档：{list(tmp_path.glob('app.log.*'))}"
        # 所有归档都是 .gz（无裸归档残留）
        archives = list(tmp_path.glob("app.log.*"))
        assert archives and all(p.name.endswith(".gz") for p in archives), \
            f"不应残留未压缩的裸归档：{archives}"

        # 压缩内容可读且完整
        with gzip.open(archive, "rt", encoding="utf-8") as f:
            assert "day-one-logs" in f.read()

        # 滚动后 app.log 被重开，可继续写入
        h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "day-two", None, None))
        h.flush()
        assert "day-two" in (tmp_path / "app.log").read_text(encoding="utf-8")
    finally:
        h.close()


def test_h19_startup_cleanup_deletes_expired_archives(tmp_path, monkeypatch):
    """构造 handler 时（=启动）即清理过期归档：15 天前的删、13 天前的留。"""
    from backend.app.core.config import settings
    from backend.app.main import _DailyGzipFileHandler

    monkeypatch.setattr(settings, "LOG_RETENTION_DAYS", 14)
    today = date.today()
    old = today - timedelta(days=15)
    fresh = today - timedelta(days=13)
    # 文件名日期格式必须与 TimedRotatingFileHandler MIDNIGHT suffix 一致
    (tmp_path / f"app.log.{old:%Y-%m-%d}.gz").write_bytes(b"expired")
    (tmp_path / f"app.log.{fresh:%Y-%m-%d}.gz").write_bytes(b"keep")
    (tmp_path / "app.log").write_text("", encoding="utf-8")

    h = _DailyGzipFileHandler(filename=str(tmp_path / "app.log"), encoding="utf-8")
    try:
        assert not (tmp_path / f"app.log.{old:%Y-%m-%d}.gz").exists(), "15 天前归档应被清理"
        assert (tmp_path / f"app.log.{fresh:%Y-%m-%d}.gz").exists(), "13 天前归档应保留"
    finally:
        h.close()


def test_h19_retention_reads_settings_live(tmp_path, monkeypatch):
    """保留期从 settings 实时读：改小后无需重建 handler 即在下次滚动时生效。"""
    from backend.app.core.config import settings

    h = _make_handler(tmp_path)
    try:
        monkeypatch.setattr(settings, "LOG_RETENTION_DAYS", 1)
        five_days_ago = date.today() - timedelta(days=5)
        p = tmp_path / f"app.log.{five_days_ago:%Y-%m-%d}.gz"
        p.write_bytes(b"old")

        h._cleanup_expired()
        assert not p.exists(), "保留期改为 1 天后，5 天前的归档应被清理"
    finally:
        h.close()


def test_h19_rollover_cleans_expired_and_compresses_leftovers(tmp_path, monkeypatch):
    """滚动时：① 过期归档被清；② 上次崩溃残留的裸归档被补压缩（幂等）。"""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "LOG_RETENTION_DAYS", 14)
    h = _make_handler(tmp_path)
    try:
        old = date.today() - timedelta(days=30)
        recent = date.today() - timedelta(days=2)
        (tmp_path / f"app.log.{old:%Y-%m-%d}.gz").write_bytes(b"expired")
        # 模拟上次滚动在压缩前崩溃：裸归档残留（2 天前 → 在保留期内，压缩后应存活）
        leftover = tmp_path / f"app.log.{recent:%Y-%m-%d}"
        leftover.write_text("crash-leftover", encoding="utf-8")

        _write_and_rollover(h, "today")

        assert not (tmp_path / f"app.log.{old:%Y-%m-%d}.gz").exists(), "30 天前归档应被清理"
        assert not leftover.exists(), "崩溃残留的裸归档应被压缩并删除"
        leftover_gz = tmp_path / f"app.log.{recent:%Y-%m-%d}.gz"
        assert leftover_gz.exists(), f"裸归档应已补压缩：{list(tmp_path.glob('app.log.*'))}"
        with gzip.open(leftover_gz, "rt", encoding="utf-8") as f:
            assert f.read() == "crash-leftover"
    finally:
        h.close()


def test_h19_bad_date_falls_back_to_mtime(tmp_path, monkeypatch):
    """文件名日期解析失败（手改乱名）→ 回退 mtime 判定去留。"""
    import os

    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "LOG_RETENTION_DAYS", 7)
    weird = tmp_path / "app.log.not-a-date.gz"
    weird.write_bytes(b"weird")
    # mtime 设为 30 天前 → 超过 7 天保留期 → 应删
    ts = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(weird, (ts, ts))

    h = _make_handler(tmp_path)
    try:
        h._cleanup_expired()
        assert not weird.exists(), "mtime 超期的乱名归档应被清理"
    finally:
        h.close()
