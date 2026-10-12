"""
Business Model

Core entity representing a business discovered from any source.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    company_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    phone_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    fb_page_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    industry_sector: Mapped[str | None] = mapped_column(String(256), nullable=True)
    location_district: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suppressed_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    qualification: Mapped["ProspectQualification | None"] = relationship(
        "ProspectQualification", back_populates="business", uselist=False
    )
    dispatch_jobs: Mapped[list["DispatchJob"]] = relationship(
        "DispatchJob", back_populates="business"
    )
    conversation: Mapped["Conversation | None"] = relationship(
        "Conversation", back_populates="business", uselist=False
    )
    commercial_audits: Mapped[list["CommercialAudit"]] = relationship(
        "CommercialAudit", back_populates="business"
    )
    proposals: Mapped[list["Proposal"]] = relationship(
        "Proposal", back_populates="business"
    )
    project_activation: Mapped["ProjectActivation | None"] = relationship(
        "ProjectActivation", back_populates="business", uselist=False
    )

    __table_args__ = (
        Index("ix_businesses_source_type", "source_type"),
    )
