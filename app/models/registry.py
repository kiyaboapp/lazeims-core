"""Registry and user models (Guide §2.1).

Constraints enforced at the database level (last line of defence):
    * region and council names unique system-wide;
    * ward name unique within its council only;
    * school centre_number unique;
    * username unique.
Geographic consistency of a school (council in region, ward in council) is
enforced by a reusable application validator (see ``app.services.geography``),
because it is a cross-row rule PostgreSQL cannot express as a simple CHECK.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazeims_common.enums import SchoolType, Sex

from ..db import Base, TimestampMixin
from ..enums import RoleScopeLevel, StandingRoleName


def _enum(py_enum, name):
    """VARCHAR-backed enum + CHECK constraint (portable, easy to migrate)."""
    return SAEnum(py_enum, name=name, native_enum=False, validate_strings=True, length=40)


class Region(Base, TimestampMixin):
    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)

    councils: Mapped[list["Council"]] = relationship(back_populates="region")


class Council(Base, TimestampMixin):
    __tablename__ = "councils"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), nullable=False, index=True)

    region: Mapped[Region] = relationship(back_populates="councils")
    wards: Mapped[list["Ward"]] = relationship(back_populates="council")


class Ward(Base, TimestampMixin):
    __tablename__ = "wards"
    __table_args__ = (
        UniqueConstraint("council_id", "name", name="uq_wards_council_id_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    council_id: Mapped[int] = mapped_column(ForeignKey("councils.id"), nullable=False, index=True)

    council: Mapped[Council] = relationship(back_populates="wards")


class School(Base, TimestampMixin):
    __tablename__ = "schools"

    id: Mapped[int] = mapped_column(primary_key=True)
    centre_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    school_type: Mapped[SchoolType] = mapped_column(
        _enum(SchoolType, "school_type"), default=SchoolType.UNKNOWN, nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_olevel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_alevel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # All geography links are optional but must be internally consistent when set.
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True, index=True)
    council_id: Mapped[int | None] = mapped_column(ForeignKey("councils.id"), nullable=True, index=True)
    ward_id: Mapped[int | None] = mapped_column(ForeignKey("wards.id"), nullable=True, index=True)
    can_download_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[StandingRoleName] = mapped_column(
        _enum(StandingRoleName, "standing_role_name"), unique=True, nullable=False
    )
    scope_level: Mapped[RoleScopeLevel] = mapped_column(
        _enum(RoleScopeLevel, "role_scope_level"), nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="role")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    surname: Mapped[str] = mapped_column(String(80), nullable=False)
    sex: Mapped[Sex] = mapped_column(_enum(Sex, "sex"), nullable=False)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False, index=True)
    # Scope binding for the standing role (nullable per scope level).
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True, index=True)
    council_id: Mapped[int | None] = mapped_column(ForeignKey("councils.id"), nullable=True, index=True)
    school_id: Mapped[int | None] = mapped_column(ForeignKey("schools.id"), nullable=True, index=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    role: Mapped[Role] = relationship(back_populates="users")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user")


class Session(Base, TimestampMixin):
    """Server-side, revocable human session. The signed cookie carries only the
    opaque session id; everything authoritative is loaded from this row."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # opaque random token id
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class Subject(Base, TimestampMixin):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_olevel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_alevel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_theory2: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_practical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    topics: Mapped[list["Topic"]] = relationship(back_populates="subject")


class Topic(Base, TimestampMixin):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    subject: Mapped[Subject] = relationship(back_populates="topics")


class ExamLevel(Base, TimestampMixin):
    __tablename__ = "exam_levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)  # SFNA|PSLE|FTNA|CSEE|ACSEE


class Board(Base, TimestampMixin):
    __tablename__ = "boards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True, index=True)
    council_id: Mapped[int | None] = mapped_column(ForeignKey("councils.id"), nullable=True, index=True)
