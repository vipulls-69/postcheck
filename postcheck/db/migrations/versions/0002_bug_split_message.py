"""bug.error_message → bug.title + bug.detail

Revision ID: 0002_bug_split_message
Revises: 0001_initial_schema
Create Date: 2026-05-17 00:00:00.000000

Splits the v0 ``bug.error_message`` column into ``title`` (one-line
headline) and ``detail`` (multi-line body). The single-field design
conflated the two and produced unreadable rows in both the CLI table
output and the future API. The core ``Bug`` model already exposes
``title`` and ``detail`` separately (see
:mod:`postcheck.reporting.bug_aggregator`); this migration aligns the
persisted shape with what the aggregator already emits.

Pre-Phase-F rows had everything in ``error_message`` — we preserve them
by copying that column into ``title`` and leaving ``detail`` empty.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_bug_split_message"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bug", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("title", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("detail", sa.String(), nullable=False, server_default="")
        )

    # Backfill: any existing rows had their headline in ``error_message``.
    bug = sa.table(
        "bug",
        sa.column("title", sa.String()),
        sa.column("error_message", sa.String()),
    )
    op.execute(bug.update().values(title=bug.c.error_message))

    with op.batch_alter_table("bug", schema=None) as batch_op:
        batch_op.drop_column("error_message")
        # Drop the server_default now that the column is populated; the
        # ORM supplies values for new inserts.
        batch_op.alter_column("title", server_default=None)
        batch_op.alter_column("detail", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("bug", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "error_message",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )

    bug = sa.table(
        "bug",
        sa.column("title", sa.String()),
        sa.column("detail", sa.String()),
        sa.column("error_message", sa.String()),
    )
    op.execute(
        bug.update().values(
            error_message=sa.case(
                (bug.c.detail == "", bug.c.title),
                else_=bug.c.title + sa.literal(": ") + bug.c.detail,
            )
        )
    )

    with op.batch_alter_table("bug", schema=None) as batch_op:
        batch_op.alter_column("error_message", server_default=None)
        batch_op.drop_column("detail")
        batch_op.drop_column("title")
