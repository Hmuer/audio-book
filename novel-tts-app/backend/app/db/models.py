from datetime import datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey, Integer, String, Text, Float, DateTime


class Base(DeclarativeBase):
    pass


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

    # ---------- 整本小说相关字段（is_book=False 时这些字段无意义）----------
    is_book: Mapped[bool] = mapped_column(default=False)
    source_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    book_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # 整本流程状态：uploading/preparing/prepared/synthesizing/done/failed
    book_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 已完成章数（仅 synthesize 阶段递增，供前端轮询进度）
    completed_chapters: Mapped[int] = mapped_column(Integer, default=0)
    # 当前阶段的人类可读描述，例如 "正在合成第 3/20 章"
    progress_msg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # 整本 prepare 后的章节 JSON（[{idx,title,text},...]），合成时按此切分
    chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 所有章 MP3 字节总和（仅整本书模式）
    total_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 整包 ZIP 文件名（仅整本书模式，done 时生成）
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
    """整本合成时的每章结果记录（仅 is_book=True 的 Job 用）"""
    __tablename__ = "chapter_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.job_id", ondelete="CASCADE"))
    chapter_idx: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    # pending / synthesizing / done / failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    audio_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped[Job] = relationship(back_populates="chapter_results")
