from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from ..core.config import settings
from .models import Base


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """B-7：给 SQLite 连接设置并发相关 PRAGMA（仅 sqlite 引擎调用）。

    - `journal_mode=WAL`：读写不再互相阻塞。默认的 rollback journal 下，
      build worker 每章多次 commit，会与 prepare 后台、JobTask 看门狗、API 请求
      抢同一把写锁，很容易触发 `database is locked`——而该异常在 worker 内会被
      当作「整章失败」降级成静音占位。
    - `busy_timeout=5000`：遇到锁时最多等 5s 再报错，避免瞬时争抢即失败。
    - `synchronous=NORMAL`：WAL 下的推荐搭配，兼顾安全与写入吞吐。

    说明：这里**不**开启 `foreign_keys=ON`。本项目的级联删除全部由 ORM 的
    `cascade="all, delete-orphan"` 显式完成（见 delete_project 等），开启 FK
    强制校验收益很小，但若历史库中存在悬挂外键，会直接导致写入被拒 —— 风险
    大于收益，故暂不开启。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        is_sqlite = "sqlite" in settings.DATABASE_URL
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False} if is_sqlite else {},
        )
        if is_sqlite:
            _install_sqlite_pragmas(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


# Job 表新增字段（用于从旧 schema 迁移）
# 列名 -> CREATE TABLE 时的 DDL 定义
_JOB_NEW_COLUMNS = {
    "is_book": "BOOLEAN DEFAULT 0",
    "source_filename": "VARCHAR(256)",
    "book_title": "VARCHAR(256)",
    "book_status": "VARCHAR(32)",
    "completed_chapters": "INTEGER DEFAULT 0",
    "progress_msg": "VARCHAR(256)",
    "chapters_json": "TEXT",
    "total_size_bytes": "INTEGER",
    "zip_filename": "VARCHAR(256)",
}

# Doubao 扩展：项目/构建新增列（FR-18/FR-19）
_PROJECT_NEW_COLUMNS = {
    "default_tts_provider": "VARCHAR(32)",
    "default_build_mode": "VARCHAR(32)",
    # P1 #5：资源归属字段
    "owner_user_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
}
_BUILD_NEW_COLUMNS = {
    # 注意：必须保留 SQL 级默认值，旧行自动补齐 classic / minimax（符合 T-TR3 向后兼容）
    "mode": "VARCHAR(32) DEFAULT 'classic'",
    "tts_provider": "VARCHAR(32) DEFAULT 'minimax'",
    # 情感/语气配置快照 + TTS 用量
    "narrator_emotion": "VARCHAR(32) DEFAULT ''",
    "narrator_instruction": "VARCHAR(512) DEFAULT ''",
    "voice_styles_json": "TEXT",
    "tts_calls": "INTEGER DEFAULT 0",
    "tts_chars": "INTEGER DEFAULT 0",
}
# 角色：情感/语气字段（旧库补列）
_PROJECT_CHARACTERS_NEW_COLUMNS = {
    "emotion": "VARCHAR(32) DEFAULT ''",
    "instruction": "VARCHAR(512) DEFAULT ''",
}
# 对白：逐段语音指令（豆包 2.0 context_texts，旧库补列；空串=不下发）
_PROJECT_DIALOGUES_NEW_COLUMNS = {
    "instruction": "VARCHAR(512) DEFAULT ''",
}


def _migrate_existing_sync(conn) -> None:
    """检测旧 schema 的 jobs/projects/builds 表，自动 ALTER TABLE 补齐缺失字段。"""
    insp = inspect(conn)
    tables = insp.get_table_names()

    if "jobs" in tables:
        existing_cols = {c["name"] for c in insp.get_columns("jobs")}
        for col, ddl in _JOB_NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}"))

    if "projects" in tables:
        existing_cols = {c["name"] for c in insp.get_columns("projects")}
        for col, ddl in _PROJECT_NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE projects ADD COLUMN {col} {ddl}"))

    if "builds" in tables:
        existing_cols = {c["name"] for c in insp.get_columns("builds")}
        for col, ddl in _BUILD_NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE builds ADD COLUMN {col} {ddl}"))

    if "project_characters" in tables:
        existing_cols = {c["name"] for c in insp.get_columns("project_characters")}
        for col, ddl in _PROJECT_CHARACTERS_NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE project_characters ADD COLUMN {col} {ddl}"))

    if "project_dialogues" in tables:
        existing_cols = {c["name"] for c in insp.get_columns("project_dialogues")}
        for col, ddl in _PROJECT_DIALOGUES_NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE project_dialogues ADD COLUMN {col} {ddl}"))


async def init_db() -> None:
    # 确保 data dir 存在
    from pathlib import Path
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 旧库迁移：补齐 Job 表新字段
        await conn.run_sync(_migrate_existing_sync)

    # app.db 现在可能包含敏感字段（API Key 等），收紧权限 0600。
    # 仅当 backend 用的是 SQLite 且文件存在时执行；其他数据库或首次 create_all
    # 未刷盘时静默忽略。
    try:
        if "sqlite" in settings.DATABASE_URL:
            from sqlalchemy.engine.url import make_url
            from ..core.config import settings as _s
            url = make_url(_s.DATABASE_URL)
            db_path = url.database
            if db_path:
                from pathlib import Path as _P
                import os as _os
                p = _P(db_path)
                # 主库 + WAL/SHM 伴生文件（启用 WAL 后会出现）都可能含敏感数据，
                # 一律收紧到 0600，避免只收主库、留下可读的 -wal/-shm。
                targets = [
                    p,
                    p.with_name(p.name + "-wal"),
                    p.with_name(p.name + "-shm"),
                ]
                for target in targets:
                    if target.is_file():
                        _os.chmod(target, 0o600)
    except Exception:
        pass
