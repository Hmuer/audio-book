import logging

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from ..core.config import settings
from .models import Base

logger = logging.getLogger(__name__)


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
    "book_category": "VARCHAR(64)",
    "selling_points": "TEXT",
    "ximalaya_tags_json": "TEXT",
    "cover_key": "VARCHAR(512)",
    "cover_status": "VARCHAR(24)",
    "cover_progress": "INTEGER DEFAULT 0",
    "cover_error": "VARCHAR(512)",
    "default_tts_provider": "VARCHAR(32)",
    "default_build_mode": "VARCHAR(32)",
    # P1 #5：资源归属字段
    "owner_user_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
}
_BUILD_NEW_COLUMNS = {
    # 注意：必须保留 SQL 级默认值，旧行自动补齐 classic / minimax（符合 T-TR3 向后兼容）
    "mode": "VARCHAR(32) DEFAULT 'classic'",
    "tts_provider": "VARCHAR(32) DEFAULT 'minimax'",
    "default_male_voice_id": "VARCHAR(128) DEFAULT ''",
    "default_female_voice_id": "VARCHAR(128) DEFAULT ''",
    "tts_model_id": "VARCHAR(64) DEFAULT ''",
    # 情感/语气配置快照 + TTS 用量
    "narrator_emotion": "VARCHAR(32) DEFAULT ''",
    "narrator_instruction": "VARCHAR(512) DEFAULT ''",
    "tts_calls": "INTEGER DEFAULT 0",
    "tts_chars": "INTEGER DEFAULT 0",
    # F-7：ZIP 分片清单（JSON）；NULL = 老库单包
    "zip_filenames_json": "TEXT",
    # H-4/H-5：结构化进度元数据（phase / rate / remaining / quality）
    "progress_meta_json": "TEXT",
}
# 对白：逐段语音指令（豆包 2.0 context_texts，旧库补列；空串=不下发）
_PROJECT_DIALOGUES_NEW_COLUMNS = {
    "instruction": "VARCHAR(512) DEFAULT ''",
}

# 需要在旧库上补建的索引（SQLAlchemy 的 create_all 只对「新建的表」建索引，
# 已存在的表不会补建 → 必须显式 CREATE INDEX IF NOT EXISTS）。
# 命名与 models.py 的 Index(...) 保持一致，便于对照。
_NEW_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_project_dialogues_project_chapter",
        "project_dialogues",
        "CREATE INDEX IF NOT EXISTS ix_project_dialogues_project_chapter "
        "ON project_dialogues (project_id, chapter_idx)",
    ),
)


def _drop_stale_notnull_columns_sync(conn, insp, tables) -> tuple[list[str], list[str]]:
    """H-29：删除「表里有、ORM 没有、NOT NULL 且无默认值」的僵尸列。

    返回 (已删列, 删不掉仍残留的列)。残留场景：SQLite < 3.35 不支持
    DROP COLUMN（现在极罕见）或权限异常——此时把列名记进
    _SCHEMA_ISSUES，prepare 入口会 fail-fast 拒跑（零 LLM 消耗），
    绝不让「全量跑完才在写库时炸」的烧钱循环再发生。
    """
    dropped: list[str] = []
    kept: list[str] = []
    for table_name, orm_table in Base.metadata.tables.items():
        if table_name not in tables:
            continue
        orm_cols = set(orm_table.columns.keys())
        # PRAGMA table_info 行: (cid, name, type, notnull, dflt_value, pk)
        for row in conn.execute(text(f'PRAGMA table_info("{table_name}")')):
            col_name, notnull, dflt, pk = row[1], row[3], row[4], row[5]
            if pk or col_name in orm_cols:
                continue
            if not notnull or dflt is not None:
                continue  # 可空或带默认：INSERT 能成功，留着无害
            try:
                conn.execute(
                    text(f'ALTER TABLE "{table_name}" DROP COLUMN "{col_name}"')
                )
                dropped.append(f"{table_name}.{col_name}")
            except Exception as e:
                kept.append(f"{table_name}.{col_name}")
                logger.error(
                    f"[migrate] H-29 无法删除遗留 NOT NULL 列 "
                    f"{table_name}.{col_name}（{type(e).__name__}: {e}）。"
                    f"prepare 将被阻止以免烧 LLM 配额后写库报废；"
                    f"请升级 SQLite(≥3.35) 或手动执行: "
                    f'ALTER TABLE "{table_name}" DROP COLUMN "{col_name}";'
                )
    return dropped, kept


def _verify_schema_health_sync(conn, kept_stale: list[str]) -> None:
    """H-29：迁移后校验核心表可写性，残留问题记入进程级 _SCHEMA_ISSUES。

    prepare 入口（trigger_prepare_project）每次启动前调 schema_issues()
    检查——有残留问题时直接 fail 项目，last_error 给出手动修复指引，
    保证零 LLM 消耗（而不是烧完整轮配额后才在收尾写库时 IntegrityError）。
    """
    global _SCHEMA_ISSUES
    issues: list[str] = list(kept_stale)
    if issues:
        _SCHEMA_ISSUES = issues
        logger.error(
            f"[migrate] H-29 检测到 {len(issues)} 个无法自动清理的 schema 残留列，"
            f"prepare 将 fail-fast 拒跑: {issues}"
        )
    else:
        _SCHEMA_ISSUES = []


# 进程级 schema 残留问题（H-29 fail-fast 用；None=未检查，[]=健康）
_SCHEMA_ISSUES: list[str] | None = None


def schema_issues() -> list[str]:
    """H-29：返回启动迁移后仍残留的 schema 问题（空列表 = 健康/未启用 DB）。"""
    return list(_SCHEMA_ISSUES or [])


def _migrate_existing_sync(conn) -> None:
    """检测旧 schema 的 jobs/projects/builds 表，自动 ALTER TABLE 补齐缺失字段。"""
    insp = inspect(conn)
    tables = insp.get_table_names()

    # H-29：清理「ORM 已删除、表里残留」的 NOT NULL 无默认列。
    #
    # 背景（用户实测 2026-10-12）：旧版角色模型有 emotion 字段（角色绑定语气），
    # 后来重构为「角色不绑语气、对白指令逐段生成」时 ORM 删掉了它——但旧库的
    # project_characters 表里 emotion TEXT NOT NULL 还在（本迁移只补列、从不
    # 删列）。后果：prepare 全流程跑完（大书一轮烧上千万字符 LLM）到最后
    # 收尾写库时 INSERT 缺列 → IntegrityError → 事务回滚 → **全部成果报废**。
    # 配额自动重试再拉起 → 又全量烧一遍 → 又炸 → 无限循环烧钱（用户账单
    # 5 小时连续 7M/小时）。确定性失败烧光配额窗口比 402 本身危害大得多。
    #
    # 修复：启动迁移时把这类「僵尸列」直接 DROP（SQLite ≥ 3.35 支持）。
    # 通用规则（不只针对 emotion）：ORM 没有且 NOT NULL 无默认的列 INSERT 必炸，
    # 一律清理；可空列/带默认值列 INSERT 能成功，留着无害（兼容用户回滚代码）。
    _dropped, _kept = _drop_stale_notnull_columns_sync(conn, insp, tables)
    if _dropped:
        logger.warning(
            f"[migrate] H-29 已删除 ORM 不再使用且 NOT NULL 无默认的遗留列: {_dropped}"
        )
    _verify_schema_health_sync(conn, _kept)

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

    if "project_dialogues" in tables:
        existing_cols = {c["name"] for c in insp.get_columns("project_dialogues")}
        for col, ddl in _PROJECT_DIALOGUES_NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE project_dialogues ADD COLUMN {col} {ddl}"))

    # F-2：补建缺失索引。CREATE INDEX IF NOT EXISTS 本身幂等，重复启动无副作用；
    # 仍先确认表存在，避免在极老/残缺库上报错中断整个迁移。
    for _name, table, ddl in _NEW_INDEXES:
        if table in tables:
            conn.execute(text(ddl))

    # F-4：把老库的 Project.chapters_json 快照回填进 project_chapters（一行一章）。
    # 读取侧保留了 chapters_json 回落，所以这里失败只丢优化、不影响功能 —— 逐项目容错，
    # 绝不因为某本书解析失败就中断启动。
    if "projects" in tables and "project_chapters" in tables:
        try:
            rows = conn.execute(text(
                "SELECT project_id, chapters_json FROM projects "
                "WHERE chapters_json IS NOT NULL AND chapters_json != ''"
            )).fetchall()
        except Exception:
            rows = []
        for _pid, _raw in rows:
            try:
                exists = conn.execute(
                    text("SELECT 1 FROM project_chapters WHERE project_id = :p LIMIT 1"),
                    {"p": _pid},
                ).fetchone()
                if exists:
                    continue
                from ..services.chapter_store import backfill_rows_sync
                _n = backfill_rows_sync(conn, _pid, _raw)
                if _n:
                    logger.info(
                        f"[migrate] F-4 回填 project_chapters project_id={str(_pid)[:8]}... rows={_n}"
                    )
            except Exception as e:
                logger.warning(
                    f"[migrate] F-4 回填失败（不影响功能，读取侧会回落 chapters_json）"
                    f" project_id={str(_pid)[:8]}...: {type(e).__name__}: {e}"
                )


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
