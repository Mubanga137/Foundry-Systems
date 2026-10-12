"""
ProspectQualification Model

Tracks the qualification state of a Business through the discovery pipeline.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.schemas.states import QualificationState


class ProspectQualification(Base):
    __tablename__ = "prospect_qualifications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    qualification_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=QualificationState.DISCOVERED.value,
    )
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sector_match: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    asset_footprint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    observed_friction_points: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    disqualification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    business: Mapped["Business"] = relationship("Business", back_populates="qualification")
