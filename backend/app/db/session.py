from sqlalchemy import inspect, text
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


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
        )
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
