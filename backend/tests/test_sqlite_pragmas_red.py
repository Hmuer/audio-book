"""B-7（整体优化批次 2）— SQLite 并发相关 PRAGMA。

背景：build worker 每章会多次 commit，与 prepare 后台、JobTask 看门狗、API 请求
共用同一个 SQLite 文件。默认的 rollback journal 下写锁是独占的，很容易出现
`database is locked`——而该异常在 worker 内会被当作「整章失败」降级为静音占位，
表现为用户拿到残缺产物。

修复：连接建立时统一设置
  - journal_mode=WAL     读写不互相阻塞
  - busy_timeout=5000    遇锁最多等 5s 而不是立即失败
  - synchronous=NORMAL   WAL 下的推荐搭配
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_b7_sqlite_pragmas_applied(_isolate_data_dir):
    """init_db 后连接必须处于 WAL + busy_timeout=5000 + synchronous=NORMAL。"""
    from sqlalchemy import text

    from backend.app.db.session import get_engine, init_db

    await init_db()
    engine = get_engine()
    async with engine.connect() as conn:
        journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        synchronous = (await conn.execute(text("PRAGMA synchronous"))).scalar()

    assert str(journal_mode).lower() == "wal", (
        f"journal_mode 应为 wal（避免读写互相阻塞），实际 {journal_mode!r}"
    )
    assert int(busy_timeout) == 5000, (
        f"busy_timeout 应为 5000，实际 {busy_timeout!r}"
    )
    # synchronous: 0=OFF, 1=NORMAL, 2=FULL
    assert int(synchronous) == 1, (
        f"synchronous 应为 NORMAL(1)，实际 {synchronous!r}"
    )


@pytest.mark.asyncio
async def test_b7_pragmas_survive_multiple_connections(_isolate_data_dir):
    """PRAGMA 必须作用于**每一条**新连接（连接池会新建连接，不能只在首条生效）。"""
    from sqlalchemy import text

    from backend.app.db.session import get_engine, init_db

    await init_db()
    engine = get_engine()
    # 连续取两条连接，逐条校验
    for _ in range(2):
        async with engine.connect() as conn:
            busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert int(busy_timeout) == 5000
