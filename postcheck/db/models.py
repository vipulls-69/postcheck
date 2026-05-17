"""SQLModel tables (v0): Organization, Project, Run, Bug.

All tables carry a UUID ``id`` and UTC ``created_at`` / ``updated_at``
timestamps. Domain tables additionally carry an ``org_id`` foreign key —
multi-tenancy is foundational (CLAUDE.md principle 8) even though v0 ships
single-tenant with a single default organization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON, TypeDecorator
from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------


RunStatus = Literal["running", "succeeded", "failed", "errored"]
BugConfidence = Literal["deterministic", "heuristic", "llm_judged"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _JSONType(TypeDecorator[Any]):
    """JSON column that uses JSONB on PostgreSQL and JSON elsewhere (e.g. SQLite)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class _UTCDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` with strict UTC enforcement.

    SQLite drops tzinfo when persisting datetimes — values come back naive.
    This decorator coerces every inbound value to UTC and re-attaches
    ``timezone.utc`` on every outbound value so callers always see aware
    datetimes regardless of dialect (CLAUDE.md: "UTC, no naive datetimes").
    """

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _timestamp_column(*, onupdate: Any = None) -> Column[datetime]:
    return Column(
        _UTCDateTime(),
        nullable=False,
        default=_utcnow,
        onupdate=onupdate,
    )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Organization(SQLModel, table=True):
    """Tenant boundary. v0 ships with a single default org."""

    __tablename__ = "organization"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(sa_column=Column(String, nullable=False))
    slug: str = Field(sa_column=Column(String, nullable=False, unique=True, index=True))
    created_at: datetime = Field(sa_column=_timestamp_column())
    updated_at: datetime = Field(sa_column=_timestamp_column(onupdate=_utcnow))


class Project(SQLModel, table=True):
    """A codebase under verification, identified by its absolute local path."""

    __tablename__ = "project"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(
            ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    name: str = Field(sa_column=Column(String, nullable=False))
    local_path: str = Field(sa_column=Column(String, nullable=False, unique=True, index=True))
    default_adapter: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(_JSONType, nullable=False, default=dict),
    )
    created_at: datetime = Field(sa_column=_timestamp_column())
    updated_at: datetime = Field(sa_column=_timestamp_column(onupdate=_utcnow))


class Run(SQLModel, table=True):
    """A single verification execution."""

    __tablename__ = "run"
    __table_args__ = (
        Index("ix_run_project_started_desc", "project_id", "started_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(
            ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    project_id: UUID = Field(
        sa_column=Column(
            ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    status: RunStatus = Field(sa_column=Column(String, nullable=False))
    since_ref: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    started_at: datetime = Field(sa_column=_timestamp_column())
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(_UTCDateTime(), nullable=True)
    )
    total_bugs: int = Field(default=0, nullable=False)
    report_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(_JSONType, nullable=False, default=dict),
    )
    created_at: datetime = Field(sa_column=_timestamp_column())
    updated_at: datetime = Field(sa_column=_timestamp_column(onupdate=_utcnow))


class Bug(SQLModel, table=True):
    """A single bug emitted by a probe during a Run."""

    __tablename__ = "bug"
    __table_args__ = (Index("ix_bug_run_id", "run_id"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(
            ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    run_id: UUID = Field(
        sa_column=Column(
            ForeignKey("run.id", ondelete="CASCADE"), nullable=False
        )
    )
    probe: str = Field(sa_column=Column(String, nullable=False))
    route: str = Field(sa_column=Column(String, nullable=False))
    interaction_summary: str = Field(sa_column=Column(String, nullable=False))
    # ``title`` is the one-line headline (e.g. ``"Network error: GET
    # /api/x → 500"``); ``detail`` is the multi-line body (stack trace,
    # error context). Together they replace the v0-pre-Phase-F
    # ``error_message`` column, which conflated headline and body into a
    # single field that produced unreadable rows in both the CLI table
    # output and the future API. ``detail`` may be empty (some kinds
    # have no body beyond the title), but ``title`` is always set —
    # bug_aggregator's recipe table guarantees a non-empty headline.
    title: str = Field(sa_column=Column(String, nullable=False))
    detail: str = Field(sa_column=Column(String, nullable=False, default=""))
    suspected_file: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    suspected_line: int | None = Field(default=None, nullable=True)
    confidence: BugConfidence = Field(sa_column=Column(String, nullable=False))
    raw_event: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(_JSONType, nullable=False, default=dict),
    )
    created_at: datetime = Field(sa_column=_timestamp_column())
    updated_at: datetime = Field(sa_column=_timestamp_column(onupdate=_utcnow))


__all__ = [
    "Bug",
    "BugConfidence",
    "Organization",
    "Project",
    "Run",
    "RunStatus",
]
