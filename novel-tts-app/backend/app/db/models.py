from datetime import datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey, Integer, String, Text, Float, DateTime, Boolean


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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

    # 元信息
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cover_color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    builds: Mapped[list["Build"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    project_characters: Mapped[list["ProjectCharacter"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    project_dialogues: Mapped[list["ProjectDialogue"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Build(Base):
    """一次生成任务（类似 GitHub Actions Run）。"""
    __tablename__ = "builds"

    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued / running / success / failed / cancelled
    progress_msg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    completed_chapters: Mapped[int] = mapped_column(Integer, default=0)
    total_chapters: Mapped[int] = mapped_column(Integer, default=0)

    # 本次配置快照
    narrator_voice_id: Mapped[str] = mapped_column(String(128), default="")
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    voice_assignments_json: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped[Job] = relationship(back_populates="chapter_results")
