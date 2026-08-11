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

    characters: Mapped[list["Character"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    dialogues: Mapped[list["Dialogue"]] = relationship(
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
