from datetime import datetime, UTC
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey, Integer, String, Text, Float, DateTime, Boolean


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# =====================================================================
# 用户与鉴权
# =====================================================================

class User(Base):
    """登录用户。启动时自动 seed admin/admin（密码 bcrypt 加密）。"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 默认密码（seed 的 admin/admin）需要首次登录后强制修改，避免被默认口令扫
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# =====================================================================
# 单章模式（保留旧 Job 表，兼容单章流程）
# =====================================================================

class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="preparing")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    polished_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    polish_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_count: Mapped[int] = mapped_column(Integer, default=1)
    final_audio_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    final_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    # ---------- 整本小说相关字段（兼容旧 book 流程，新项目模式走 Project/Build 表）----------
    is_book: Mapped[bool] = mapped_column(default=False)
    source_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    book_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    book_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_chapters: Mapped[int] = mapped_column(Integer, default=0)
    progress_msg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    zip_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)

    characters: Mapped[list["Character"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    dialogues: Mapped[list["Dialogue"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    chapter_results: Mapped[list["ChapterResult"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


# =====================================================================
# 项目制模式（新架构）
# =====================================================================

class Project(Base):
    """一本书一个项目。"""
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    book_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_charset: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 项目状态机：draft → importing → imported → preparing → ready → synthesizing → done → failed
    status: Mapped[str] = mapped_column(String(32), default="draft")
    chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)

    # P1 #5 修复 — 资源归属。
    # nullable=True 是为兼容旧项目（DB 演进）；历史 NULL owner 在启动时一次性
    # 由 claim_orphan_projects() 归属到 SEED_ADMIN_USER。新建项目永远显式写 owner。
    # 0 表示"管理员孤儿池"（任意登录用户可读；仅 admin 可写/删），
    # 正常用户项目 owner_user_id = 当前 User.id。
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Prepare 进度 checkpoint（prepare_project 中断后重跑可恢复）
    # 结构：
    # {
    #   "version": 1,
    #   "stage": "characters" | "dedup" | "dialogues" | "voice_recs" | "done" | "failed",
    #   "char_slice_completed": [0,1,2,...],        # 已完成角色识别的切片 index
    #   "char_extract_raw_json": "[{name,...},...]"  # 已提取但未 dedup 的角色原始结果
    #   "dialogue_completed_chapters": [0,1,5,...],  # 已完成对白归属的章节 index
    #   "updated_at": "2025-..."
    # }
    progress_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 项目级默认配置
    default_narrator_voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_speed: Mapped[float] = mapped_column(Float, default=1.0)
    # 项目级 TTS 厂商 + 构建模式默认值（可被 start_build 入参覆写）
    default_tts_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_build_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 元信息
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cover_color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    builds: Mapped[list["Build"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    project_characters: Mapped[list["ProjectCharacter"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    project_dialogues: Mapped[list["ProjectDialogue"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    pronunciation_rules: Mapped[list["ProjectPronunciationRule"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Build(Base):
    """一次生成任务（类似 GitHub Actions Run）。"""
    __tablename__ = "builds"

    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued / running / success / partial_success / failed / cancelled
    progress_msg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    completed_chapters: Mapped[int] = mapped_column(Integer, default=0)
    total_chapters: Mapped[int] = mapped_column(Integer, default=0)
    # 失败章节索引列表（JSON int list），retry-failed 接口只合成这里面的章
    failed_chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 是否是 retry-failed 产生的 build（用于前端展示"重试"标签）
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False)

    # 本次配置快照
    narrator_voice_id: Mapped[str] = mapped_column(String(128), default="")
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    voice_assignments_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 多厂商 + 多模式（classic/multicast）
    mode: Mapped[str] = mapped_column(String(32), default="classic")
    tts_provider: Mapped[str] = mapped_column(String(32), default="minimax")

    # 配置哈希：同一个 project + (narrator,speed,voice_assignments) 相同 → 若上次 build 成功，
    # 可直接返回已存在的 build_id（Synthesize 幂等第一层去重）；
    # 细粒度的段级缓存走段级 sha256，不在这里比。
    config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # 产出
    zip_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    total_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    project: Mapped[Project] = relationship(back_populates="builds")
    artifacts: Mapped[list["BuildArtifact"]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )


class BuildArtifact(Base):
    """每次 Build 的每章产出。"""
    __tablename__ = "build_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    build_id: Mapped[str] = mapped_column(String(64), ForeignKey("builds.build_id", ondelete="CASCADE"))
    chapter_idx: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    # pending / synthesizing / done / failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    audio_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    build: Mapped[Build] = relationship(back_populates="artifacts")


class ProjectCharacter(Base):
    """项目级角色识别结果。"""
    __tablename__ = "project_characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    gender: Mapped[str] = mapped_column(String(16), default="未知")
    age: Mapped[str] = mapped_column(String(64), default="")
    personality: Mapped[str] = mapped_column(String(512), default="")
    canonical_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    assigned_voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    project: Mapped[Project] = relationship(back_populates="project_characters")


class ProjectDialogue(Base):
    """项目级对白归属。"""
    __tablename__ = "project_dialogues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"))
    chapter_idx: Mapped[int] = mapped_column(Integer, default=0)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    anchor_start: Mapped[int] = mapped_column(Integer, default=0)
    anchor_end: Mapped[int] = mapped_column(Integer, default=0)
    anchor_text: Mapped[str] = mapped_column(Text, default="")
    speaker: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    project: Mapped[Project] = relationship(back_populates="project_dialogues")


class ProjectPronunciationRule(Base):
    """项目级发音规则（别名替换 / 正则替换）。

    - type=alias  : 简单字符串替换（pattern → replacement）
    - type=regex  : 正则替换（pattern 为正则，replacement 支持 \1 等回引）
    - character_id: 关联到 project_characters.id；为 NULL 时表示全局规则
    """
    __tablename__ = "project_pronunciation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"))
    character_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("project_characters.id", ondelete="CASCADE"), nullable=True)
    rule_type: Mapped[str] = mapped_column(String(16), default="alias")   # alias | regex
    pattern: Mapped[str] = mapped_column(String(512))
    replacement: Mapped[str] = mapped_column(String(512))
    priority: Mapped[int] = mapped_column(Integer, default=0)            # 越小越先执行
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: Mapped[str] = mapped_column(String(32), default=lambda: datetime.now().isoformat(timespec="seconds"))

    project: Mapped[Project] = relationship(back_populates="pronunciation_rules")


# =====================================================================
# 单章模式旧表（Character/Dialogue/ChapterResult 仍关联到 jobs）
# =====================================================================

class Character(Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    gender: Mapped[str] = mapped_column(String(16), default="未知")
    age: Mapped[str] = mapped_column(String(64), default="")
    personality: Mapped[str] = mapped_column(String(512), default="")
    canonical_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    assigned_voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    job: Mapped[Job] = relationship(back_populates="characters")


class Dialogue(Base):
    __tablename__ = "dialogues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"))
    chapter_idx: Mapped[int] = mapped_column(Integer, default=0)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    anchor_start: Mapped[int] = mapped_column(Integer, default=0)
    anchor_end: Mapped[int] = mapped_column(Integer, default=0)
    anchor_text: Mapped[str] = mapped_column(Text, default="")
    speaker: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    audio_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    job: Mapped[Job] = relationship(back_populates="dialogues")


class ChapterResult(Base):
    """整本合成时的每章结果记录（仅 is_book=True 的旧 Job 用，新项目模式走 BuildArtifact）。"""
    __tablename__ = "chapter_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"))
    chapter_idx: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    audio_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="chapter_results")


# =====================================================================
# ICL 声音复刻（豆包 ICL 2.0 训练任务）
# =====================================================================

class IclTrainingTask(Base):
    """豆包 ICL 声音复刻训练任务。

    status 语义同豆包官方：
        0 排队 / 1 训练中 / 2 成功 / 3 失败 / 4 可用
    （豆包 ICL 2.0 通常把 2/4 都视为可用，本实现 4 视为可合成状态）
    """
    __tablename__ = "icl_training_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    voice_name: Mapped[str] = mapped_column(String(128), default="")
    reference_audio_path: Mapped[str] = mapped_column(String(512), default="")
    reference_audio_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 0 排队 / 1 训练中 / 2 成功 / 3 失败 / 4 可用
    status: Mapped[int] = mapped_column(Integer, default=0, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    doubao_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 训练完成后豆包返回的自定义音色 id，合成接口把 icl:<cloned_voice_id> 映射到这个值
    cloned_voice_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# P1 #6：媒体签名 URL（一次性 + 资源绑定 + 短时），用于避免在 URL 里
# 携带完整登录 JWT。
class MediaSignToken(Base):
    __tablename__ = "media_sign_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))   # chapter_mp3 | all_zip
    chapter_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


# P1 #7：后台任务持久化表 —— 用于服务重启 / 进程崩溃后，
# 把"上次没跑完"的后台作业自动恢复/标记失败，避免 UI 永远显示"合成中"。
#
# 关键设计：
# - 后台 worker 启动时 register(pending → running)，结束 write_terminal(success/failed/cancelled)
# - worker 每隔 ~HEARTBEAT_INTERVAL_SECONDS 更新 last_heartbeat_at
# - lifespan startup 扫 `status=running AND last_heartbeat_at < now - ORPHAN_THRESHOLD` → 视为孤儿
#   - prepare 孤儿 → 走 _recover_one_preparing_project 从 progress_json checkpoint 恢复
#   - build 孤儿 → 走 _run_build_inner 从 BuildArtifact 已 done 章跳过继续
#   - 其它 kind → 直接置 failed 写 last_error
# - 与现有 _prepare_running_tasks / _ACTIVE_BUILDS 进程锁兼容：
#   - 启动恢复时如果发现本进程内已经有同 target 的活跃 task，跳过注册，避免双跑
class JobTask(Base):
    __tablename__ = "job_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # prepare | build | icl_train | media_clean | …（便于未来扩展更多后台任务）
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # project_id / build_id / task_id：分别对应不同 kind 的"业务实体 id"
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    # pending / running / success / failed / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 触发来源：api / startup / watchdog / retry
    trigger: Mapped[str] = mapped_column(String(32), default="api")
    # 重启次数：服务恢复 / 看门狗检测到孤儿后递增；前端可显示"已自动恢复 N 次"
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    # 心跳：worker 周期性写当前时间；与 now 差 > 阈值视为孤儿
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 透传进度（白名单 JSON，例如 prepare 的 stage / dialogue_completed_chapters_count）
    progress_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

