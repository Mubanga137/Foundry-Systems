"""
FS-CAE State & Enum Definitions

All typed enums used across the domain models.
Defined centrally to guarantee consistency.
"""

import enum


# --------------------------------------------------------------------------- #
#  Prospect Qualification
# --------------------------------------------------------------------------- #

class QualificationState(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    EVALUATING = "EVALUATING"
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"
    STAGED_FOR_OUTREACH = "STAGED_FOR_OUTREACH"
    SUPPRESSED = "SUPPRESSED"


# --------------------------------------------------------------------------- #
#  Dispatch
# --------------------------------------------------------------------------- #

class DispatchChannel(str, enum.Enum):
    FB_MESSENGER = "FB_MESSENGER"
    EMAIL = "EMAIL"
    MANUAL = "MANUAL"


class DispatchStatus(str, enum.Enum):
    DRAFTED = "DRAFTED"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


# Alias for backward/forward naming compatibility
DispatchState = DispatchStatus


# --------------------------------------------------------------------------- #
#  Conversation
# --------------------------------------------------------------------------- #

class ConversationChannel(str, enum.Enum):
    FB_MESSENGER = "FB_MESSENGER"
    EMAIL = "EMAIL"


class ConversationState(str, enum.Enum):
    AWAITING_REPLY = "AWAITING_REPLY"
    POSITIVE_REPLY = "POSITIVE_REPLY"
    OBJECTION_OR_QUESTION = "OBJECTION_OR_QUESTION"
    OPTED_OUT = "OPTED_OUT"
    LOST = "LOST"
    AUDIT_PENDING_APPROVAL = "AUDIT_PENDING_APPROVAL"
    AUDIT_DISPATCHED = "AUDIT_DISPATCHED"
    OFFER_PENDING_APPROVAL = "OFFER_PENDING_APPROVAL"
    OFFER_DISPATCHED = "OFFER_DISPATCHED"
    PROPOSAL_REQUESTED = "PROPOSAL_REQUESTED"
    INVOICED = "INVOICED"


# Attach post-initialization for Layer 4/5 extended lifecycle without altering enum iteration
ConversationState.WON = "WON"  # type: ignore
ConversationState.HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"  # type: ignore


class InboundIntent(str, enum.Enum):
    POSITIVE_INTEREST = "POSITIVE_INTEREST"
    REQUEST_PRICING = "REQUEST_PRICING"
    HAS_EXISTING_WEBSITE = "HAS_EXISTING_WEBSITE"
    CALL_OR_MEETING_REQUEST = "CALL_OR_MEETING_REQUEST"
    TIMING_DELAY = "TIMING_DELAY"
    OPT_OUT = "OPT_OUT"
    GENERAL_QUESTION = "GENERAL_QUESTION"


class MessageDirection(str, enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    RECEIVED = "RECEIVED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------- #
#  Commercial
# --------------------------------------------------------------------------- #

class CommercialAuditStatus(str, enum.Enum):
    PENDING_GENERATION = "PENDING_GENERATION"
    GENERATED = "GENERATED"
    APPROVED = "APPROVED"
    DELIVERED = "DELIVERED"


class PackageType(str, enum.Enum):
    CAPABILITY_PORTAL_CORE = "CAPABILITY_PORTAL_CORE"
    LAUNCH_SYSTEM_BASIC = "LAUNCH_SYSTEM_BASIC"


class ProposalStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class MilestoneTier(str, enum.Enum):
    MOBILIZATION = "MOBILIZATION"
    MID_PROJECT = "MID_PROJECT"
    PRE_LAUNCH = "PRE_LAUNCH"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    VOID = "VOID"


class ProjectActivationStatus(str, enum.Enum):
    PENDING_MOBILIZATION = "PENDING_MOBILIZATION"
    ACTIVATED = "ACTIVATED"
    IN_DELIVERY = "IN_DELIVERY"
    COMPLETED = "COMPLETED"
