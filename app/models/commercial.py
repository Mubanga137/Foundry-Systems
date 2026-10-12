"""
Commercial Models: Proposal, Invoice, PaymentEvent, ProjectActivation

Full financial pipeline from proposal issuance through payment verification
and project activation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.schemas.states import (
    InvoiceStatus,
    MilestoneTier,
    PackageType,
    ProjectActivationStatus,
    ProposalStatus,
)


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_type: Mapped[str] = mapped_column(String(64), nullable=False)
    total_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    milestone_1_mobilization: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    milestone_2_midway: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    milestone_3_launch: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    continuity_monthly_fee: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProposalStatus.DRAFT.value
    )
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    business: Mapped["Business"] = relationship("Business", back_populates="proposals")
    invoices: Mapped[list["Invoice"]] = relationship(
        "Invoice", back_populates="proposal"
    )
    project_activations: Mapped[list["ProjectActivation"]] = relationship(
        "ProjectActivation", back_populates="proposal"
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    proposal_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    milestone_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=InvoiceStatus.DRAFT.value
    )
    payment_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    proposal: Mapped["Proposal"] = relationship("Proposal", back_populates="invoices")
    payment_events: Mapped[list["PaymentEvent"]] = relationship(
        "PaymentEvent", back_populates="invoice"
    )


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount_received: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    transaction_reference: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    verified_by: Mapped[str] = mapped_column(String(256), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    invoice: Mapped["Invoice"] = relationship(
        "Invoice", back_populates="payment_events"
    )


class ProjectActivation(Base):
    __tablename__ = "project_activations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    proposal_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    mobilization_invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProjectActivationStatus.PENDING_MOBILIZATION.value,
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    business: Mapped["Business"] = relationship(
        "Business", back_populates="project_activation"
    )
    proposal: Mapped["Proposal"] = relationship(
        "Proposal", back_populates="project_activations"
    )
    mobilization_invoice: Mapped["Invoice"] = relationship("Invoice")
