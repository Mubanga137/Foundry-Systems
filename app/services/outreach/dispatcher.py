"""
FS-CAE Persistent Dispatcher & Safety Throttle

Executes queued outreach jobs while strictly enforcing:
1. Operating Hours Gate (08:00 - 16:30 CAT, Mon-Fri)
2. Rolling 24-Hour Daily Cap
3. Pacing & Randomized Jitter
4. Idempotency & Re-entrancy Guards
5. Conversation Handshake & Message Logging
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.business import Business
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.schemas.states import (
    ConversationState,
    DeliveryStatus,
    DispatchChannel,
    DispatchStatus,
    MessageDirection,
)
from app.services.outreach.adapters.assisted_adapter import AssistedAdapter
from app.services.outreach.adapters.base import BaseChannelAdapter
from app.services.outreach.adapters.email_adapter import EmailAdapter
from app.services.outreach.adapters.messenger_adapter import MessengerAdapter
from app.services.state_machine import _write_audit_log, transition_dispatch

logger = logging.getLogger(__name__)

# Central Africa Time is UTC+2 with no daylight saving time
try:
    import zoneinfo
    CAT_TIMEZONE = zoneinfo.ZoneInfo("Africa/Lusaka")
except Exception:
    CAT_TIMEZONE = timezone(timedelta(hours=2))

OPERATING_START = dtime(8, 0)
OPERATING_END = dtime(16, 30)


@dataclass
class DispatchRunSummary:
    """Summary of a dispatcher execution run."""

    total_processed: int = 0
    dispatched: int = 0
    blocked: int = 0
    failed: int = 0
    held_by_operating_hours: bool = False
    daily_cap_reached: bool = False
    jobs_dispatched: List[str] = field(default_factory=list)


def is_within_operating_hours(dt: Optional[datetime] = None) -> bool:
    """
    Check if a datetime falls within active operating hours:
    08:00 to 16:30 CAT (Africa/Lusaka), Monday through Friday.
    """
    if dt is None:
        dt = datetime.now(CAT_TIMEZONE)
    else:
        # Convert to CAT if timezone-aware, or assume CAT if naive
        if dt.tzinfo is not None:
            dt = dt.astimezone(CAT_TIMEZONE)

    # Monday = 0, Sunday = 6
    if dt.weekday() >= 5:
        return False

    current_time = dt.time()
    return OPERATING_START <= current_time <= OPERATING_END


def count_recent_dispatches(db: Session, hours: int = 24) -> int:
    """Count successful dispatches in the rolling 24-hour window."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = (
        db.query(func.count(DispatchJob.id))
        .filter(
            DispatchJob.sent_at >= since,
            DispatchJob.status.in_([DispatchStatus.SENT.value, DispatchStatus.DELIVERED.value]),
        )
        .scalar()
    )
    return count or 0


def record_conversation_handshake(
    db: Session,
    business: Business,
    job: DispatchJob,
) -> Conversation:
    """
    When outreach is successfully sent, create or update the business's
    Conversation record in AWAITING_REPLY and log the outbound message.
    """
    now = datetime.now(timezone.utc)
    conv = db.query(Conversation).filter(Conversation.business_id == business.id).first()

    if conv is None:
        conv = Conversation(
            business_id=business.id,
            channel=job.channel,
            state=ConversationState.AWAITING_REPLY.value,
            last_activity_at=now,
        )
        db.add(conv)
        db.flush()
        _write_audit_log(
            db,
            entity_type="Conversation",
            entity_id=conv.id,
            old_state=None,
            new_state=ConversationState.AWAITING_REPLY.value,
            actor="DISPATCHER",
            reason="Initial outreach handshake created conversation in AWAITING_REPLY",
        )
    else:
        conv.last_activity_at = now
        db.flush()

    # Log outbound message in messages table
    msg = Message(
        conversation_id=conv.id,
        direction=MessageDirection.OUTBOUND.value,
        content=job.rendered_content,
        delivery_status=DeliveryStatus.SENT.value,
        raw_metadata={
            "dispatch_job_id": job.id,
            "channel": job.channel,
            "recipient_target": job.recipient_target,
        },
    )
    db.add(msg)
    db.flush()
    return conv


class DispatchWorker:
    """
    Outreach dispatch worker with rate limiting, jitter, and platform safety.
    """

    def __init__(
        self,
        daily_cap: Optional[int] = None,
        min_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        settings = get_settings()
        self.daily_cap = daily_cap if daily_cap is not None else settings.DAILY_DISPATCH_CAP
        self.min_delay = (
            min_delay if min_delay is not None else settings.DISPATCH_MIN_DELAY_SECONDS
        )
        self.max_delay = (
            max_delay if max_delay is not None else settings.DISPATCH_MAX_DELAY_SECONDS
        )
        self.sleep_fn = sleep_fn or time.sleep

    def get_adapter_for_channel(
        self,
        channel: str,
        assisted_confirmed: bool = True,
    ) -> BaseChannelAdapter:
        """Route job to appropriate channel adapter."""
        ch = channel.upper()
        if ch == DispatchChannel.FB_MESSENGER.value:
            return MessengerAdapter()
        elif ch == DispatchChannel.EMAIL.value:
            return EmailAdapter()
        elif ch in (DispatchChannel.MANUAL.value, "ASSISTED"):
            return AssistedAdapter(operator_confirmed=assisted_confirmed)
        else:
            return AssistedAdapter(operator_confirmed=assisted_confirmed)

    def dispatch_queue(
        self,
        db: Session,
        limit: Optional[int] = None,
        channel: Optional[str] = None,
        force: bool = False,
        dry_run: bool = False,
        simulated_time: Optional[datetime] = None,
        assisted_confirmed: bool = True,
    ) -> DispatchRunSummary:
        """
        Execute queued outreach jobs.
        """
        summary = DispatchRunSummary()

        # 1. Operating Hours Gate
        check_time = simulated_time or datetime.now(CAT_TIMEZONE)
        if not force and not is_within_operating_hours(check_time):
            logger.warning("Outside permitted operating window (CAT). Holding queue.")
            summary.held_by_operating_hours = True
            return summary

        # 2. Query QUEUED jobs
        query = (
            db.query(DispatchJob)
            .filter(DispatchJob.status == DispatchStatus.QUEUED.value)
            .order_by(DispatchJob.scheduled_for.asc())
        )
        if channel is not None:
            query = query.filter(DispatchJob.channel == channel)

        if limit is not None:
            query = query.limit(limit)

        jobs = query.all()
        if not jobs:
            return summary

        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        for idx, job in enumerate(jobs):
            # 3. Daily Cap Enforcement
            recent_count = count_recent_dispatches(db, hours=24)
            if recent_count >= self.daily_cap:
                logger.warning(
                    "Daily dispatch cap reached (%s / %s in last 24h). Stopping queue.",
                    recent_count,
                    self.daily_cap,
                )
                summary.daily_cap_reached = True
                break

            summary.total_processed += 1
            business = job.business

            # 4. Idempotency Check: Already sent today to this business?
            already_sent_today = (
                db.query(DispatchJob)
                .filter(
                    DispatchJob.business_id == business.id,
                    DispatchJob.id != job.id,
                    DispatchJob.status.in_([
                        DispatchStatus.SENT.value,
                        DispatchStatus.DELIVERED.value,
                    ]),
                    func.date(DispatchJob.sent_at) == today_str,
                )
                .first()
            )
            if already_sent_today:
                logger.warning(
                    "Idempotency Guard: Business %s already received outreach today (%s). Blocking job %s.",
                    business.id,
                    today_str,
                    job.id,
                )
                transition_dispatch(
                    db,
                    job,
                    DispatchStatus.BLOCKED.value,
                    blocked_reason="Duplicate outreach attempt on same campaign date",
                    actor="DISPATCHER",
                    reason="Idempotency guard prevented duplicate outreach",
                )
                db.commit()
                summary.blocked += 1
                continue

            # Dry run check
            if dry_run:
                logger.info("[DRY RUN] Would dispatch job %s via %s", job.id, job.channel)
                summary.dispatched += 1
                summary.jobs_dispatched.append(job.id)
                continue

            # 5. Transition to SENDING
            transition_dispatch(
                db,
                job,
                DispatchStatus.SENDING.value,
                actor="DISPATCHER",
                reason=f"Attempting delivery via {job.channel}",
            )
            db.commit()

            # 6. Channel Adapter Execution
            adapter = self.get_adapter_for_channel(
                job.channel, assisted_confirmed=assisted_confirmed
            )
            result = adapter.send(job, business)

            if result.success:
                # Transition SENDING -> SENT
                transition_dispatch(
                    db,
                    job,
                    DispatchStatus.SENT.value,
                    actor="DISPATCHER",
                    reason=f"Delivered via {job.channel} (ext_id: {result.external_id})",
                )
                # Handshake
                record_conversation_handshake(db, business, job)
                db.commit()

                summary.dispatched += 1
                summary.jobs_dispatched.append(job.id)

            elif result.status == DispatchStatus.BLOCKED:
                transition_dispatch(
                    db,
                    job,
                    DispatchStatus.BLOCKED.value,
                    blocked_reason=result.error_message or "Blocked by channel adapter policy",
                    actor="DISPATCHER",
                    reason=result.error_message or "Blocked by channel adapter policy",
                )
                db.commit()
                summary.blocked += 1

            else:
                # Failure
                transition_dispatch(
                    db,
                    job,
                    DispatchStatus.FAILED.value,
                    last_error=result.error_message or "Delivery failed",
                    actor="DISPATCHER",
                    reason=result.error_message or "Delivery failed",
                )
                db.commit()
                summary.failed += 1

            # 7. Pacing & Jitter delay if more jobs remain
            if idx < len(jobs) - 1 and self.max_delay > 0:
                delay = random.uniform(self.min_delay, self.max_delay)
                self.sleep_fn(delay)

        return summary
