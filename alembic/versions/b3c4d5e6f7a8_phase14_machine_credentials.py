"""phase14: per-package machine credentials and package signing

Adds:
- station_machine_credentials: per-package machine credential with Argon2id
  hash, credential identifier, and lifecycle status.
- supersedes_package_id column on station_packages (nullable FK to self).
- admin_username column on station_credentials (dedicated field; stops
  overloading initials for the admin login).

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-08-01 03:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "station_machine_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("credential_id", sa.String(80), nullable=False, unique=True),
        sa.Column("package_id", sa.String(80), sa.ForeignKey("station_packages.package_id"), nullable=False, index=True),
        sa.Column("station_id", sa.Integer(), sa.ForeignKey("stations.id"), nullable=False, index=True),
        sa.Column("secret_hash", sa.String(255), nullable=False),
        sa.Column("algorithm", sa.String(20), nullable=False, server_default="argon2id"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.add_column(
        "station_packages",
        sa.Column("supersedes_package_id", sa.String(80), nullable=True),
    )
    op.create_foreign_key(
        "fk_station_packages_supersedes",
        "station_packages", "station_packages",
        ["supersedes_package_id"], ["package_id"],
    )

    op.add_column(
        "station_credentials",
        sa.Column("admin_username", sa.String(60), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("station_credentials", "admin_username")
    op.drop_constraint("fk_station_packages_supersedes", "station_packages", type_="foreignkey")
    op.drop_column("station_packages", "supersedes_package_id")
    op.drop_table("station_machine_credentials")
