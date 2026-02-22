"""SQLAlchemy models for projects, assets, scenarios."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import CHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    """Project model."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        CHAR(36), primary_key=True, default=generate_uuid
    )
    name: Mapped[str] = mapped_column(String(255), default="New Project")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    assets: Mapped[list["Asset"]] = relationship(
        "Asset", back_populates="project", cascade="all, delete-orphan"
    )
    scenario: Mapped[Optional["Scenario"]] = relationship(
        "Scenario", back_populates="project", uselist=False, cascade="all, delete-orphan"
    )


class Asset(Base):
    """Asset model (uploaded media)."""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(
        CHAR(36), primary_key=True, default=generate_uuid
    )
    project_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    file_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # video | image
    duration_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    user_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )

    project: Mapped["Project"] = relationship("Project", back_populates="assets")


class Scenario(Base):
    """Scenario model (stores full Scenario JSON)."""

    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(
        CHAR(36), primary_key=True, default=generate_uuid
    )
    project_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft | saved
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    project: Mapped["Project"] = relationship("Project", back_populates="scenario")
