"""phase13: encrypt processing API key at rest

Renames ``api_key`` (String(120)) to ``api_key_encrypted`` (Text) and encrypts
existing plaintext values using MultiFernet. The encryption key is derived from
``session_secret_key`` when ``PROCESSING_KEY_ENCRYPTION_KEYS`` is not set, so
existing deployments work without adding new env vars.

Also adds ``exam_processing_requests`` and ``exametrics_webhook_receipts`` tables
for the processing approval gate (C6) and webhook receiver (C9).

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-01 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add the new encrypted column alongside the old one.
    op.add_column(
        "exam_processing_links",
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
    )

    # 2. Data migration: encrypt existing plaintext keys.
    # Import inside the function to avoid import-time side effects.
    from app.services.key_crypto import encrypt_key

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, api_key FROM exam_processing_links WHERE api_key IS NOT NULL")
    ).fetchall()
    for row_id, plaintext in rows:
        encrypted = encrypt_key(plaintext)
        conn.execute(
            sa.text("UPDATE exam_processing_links SET api_key_encrypted = :enc WHERE id = :id"),
            {"enc": encrypted, "id": row_id},
        )
    # Rows without an existing key get a placeholder (should not happen in practice).
    conn.execute(
        sa.text(
            "UPDATE exam_processing_links SET api_key_encrypted = '' "
            "WHERE api_key_encrypted IS NULL"
        )
    )

    # 3. Make the new column NOT NULL and drop the old plaintext column.
    op.alter_column("exam_processing_links", "api_key_encrypted", nullable=False)
    op.drop_column("exam_processing_links", "api_key")

    # 4. Create exam_processing_requests table.
    op.create_table(
        "exam_processing_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("exam_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("exams.id"), nullable=False, index=True),
        sa.Column("request_id", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(30), nullable=False, server_default="PENDING_APPROVAL"),
        sa.Column("closeout_revision", sa.Integer(), nullable=False),
        sa.Column("configuration_hash", sa.String(128), nullable=False),
        sa.Column("quote_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("exam_id", "closeout_revision", "configuration_hash", name="uq_processing_requests_exam_rev_hash"),
    )

    # 5. Create exametrics_webhook_receipts table.
    op.create_table(
        "exametrics_webhook_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False, index=True),
        sa.Column("event", sa.String(60), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("external_ref", sa.String(64), nullable=True),
        sa.Column("payload_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id", "event", "occurred_at", name="uq_webhook_receipts_req_event_time"),
    )


def downgrade() -> None:
    op.drop_table("exametrics_webhook_receipts")
    op.drop_table("exam_processing_requests")
    # Restore the plaintext column (data recovery is not possible).
    op.add_column(
        "exam_processing_links",
        sa.Column("api_key", sa.String(120), nullable=True),
    )
    op.execute("UPDATE exam_processing_links SET api_key = ''")
    op.alter_column("exam_processing_links", "api_key", nullable=False)
    op.drop_column("exam_processing_links", "api_key_encrypted")
