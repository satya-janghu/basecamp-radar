import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    ForeignKey,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    projects: Mapped[list] = mapped_column(JSONB, default=list)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="account", lazy="raise")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("basecamp_bucket_id", "basecamp_todo_id", name="uq_task_basecamp"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_priority_tier", "priority_tier"),
        Index("ix_tasks_account_status_tier", "account_id", "status", "priority_tier"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    basecamp_account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    basecamp_bucket_id: Mapped[str] = mapped_column(String, nullable=False)
    basecamp_todo_id: Mapped[str] = mapped_column(String, nullable=False)
    basecamp_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True
    )
    project_type: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    assignee_names: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    completion_subscribers: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    due_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    closed_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_known_activity_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_checked_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_analyzed_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    has_new_activity: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_comments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    priority_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    previous_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_analyzed_comments_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    ai_signals: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ai_recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_ball_with: Mapped[str | None] = mapped_column(String, nullable=True)
    ai_current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_status: Mapped[str | None] = mapped_column(
        String, default="pending", server_default="pending", nullable=True
    )
    analysis_error_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    vote: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    voted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vote_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dismissed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    added_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    account: Mapped["Account | None"] = relationship("Account", back_populates="tasks")


class Config(Base):
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PollRun(Base):
    __tablename__ = "poll_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
