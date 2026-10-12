"""
CommercialAudit Model

Tracks the technical audit / assessment performed on a qualified business.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.schemas.states import CommercialAuditStatus


class CommercialAudit(Base):
    __tablename__ = "commercial_audits"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CommercialAuditStatus.PENDING_GENERATION.value,
    )
    finding_1: Mapped[str | None] = mapped_column(Text, nullable=True)
    finding_2: Mapped[str | None] = mapped_column(Text, nullable=True)
    finding_3: Mapped[str | None] = mapped_column(Text, nullable=True)
    commercial_implications: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    business: Mapped["Business"] = relationship(
        "Business", back_populates="commercial_audits"
    )
