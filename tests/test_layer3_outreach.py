"""
FS-CAE Layer 3 Outreach & Dispatcher Test Suite

Tests cover:
  1. Message Compiler ($100M Leads direct-response grammar, fallback, constraints)
  2. Batch Workflow (Drafting, approval, rejection, state transitions)
  3. Channel Adapters & Policy Compliance (Email suppression, Meta policy honesty, Assisted mode)
  4. Dispatcher Safety & Throttles (Operating hours gate, daily cap, jitter, idempotency, conversation handshake)
"""

from __future__ import annotations

import datetime
from datetime import datetime as dt, time as dtime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.models.business import Business
from app.models.conversation import Conversation, Message
from app.models.dispatch import DispatchJob
from app.models.prospect import ProspectQualification
from app.schemas.states import (
    ConversationState,
    DeliveryStatus,
    DispatchChannel,
    DispatchState,
    DispatchStatus,
    MessageDirection,
    QualificationState,
)
from app.services.outreach.adapters.assisted_adapter import AssistedAdapter
from app.services.outreach.adapters.base import AdapterResult
from app.services.outreach.adapters.email_adapter import EmailAdapter
from app.services.outreach.adapters.messenger_adapter import (
    MessengerAdapter,
    extract_fb_username,
)
from app.services.outreach.batch_manager import (
    approve_batch,
    draft_outreach_for_qualified,
    reject_batch,
)
from app.services.outreach.dispatcher import (
    CAT_TIMEZONE,
    DispatchWorker,
    is_within_operating_hours,
)
from app.services.outreach.message_compiler import (
    BANNED_PHRASES,
    MAX_WORD_COUNT,
    MessageCompiler,
    compile_deterministic_fallback,
    validate_outreach_grammar,
)
from app.services.state_machine import transition_qualification


# --------------------------------------------------------------------------- #
#  Helpers & Fixtures
# --------------------------------------------------------------------------- #

def _create_biz(session, **kwargs) -> Business:
    defaults = dict(
        company_name="Zambezi Plant Hire Ltd",
        location_district="Kafue Road, Lusaka",
        industry_sector="Plant Hire & Earthmoving",
        phone_normalized="+260977123456",
        email="operations@zambezi-plant.com",
        fb_page_url="https://facebook.com/zambeziplant",
        source_type="INSTANT_DATA_SCRAPER",
        is_suppressed=False,
    )
    defaults.update(kwargs)
    biz = Business(**defaults)
    session.add(biz)
    session.flush()
    return biz


def _create_qual(session, biz: Business, **kwargs) -> ProspectQualification:
    defaults = dict(
        business_id=biz.id,
        qualification_state=QualificationState.QUALIFIED.value,
        fit_score=85,
        sector_match=True,
        asset_footprint={"heavy_machinery": ["CAT 330 Excavator", "20-ton Tipper"]},
        observed_friction_points={"digital_deficit": "No fleet catalog online"},
    )
    defaults.update(kwargs)
    qual = ProspectQualification(**defaults)
    session.add(qual)
    session.flush()
    return qual


# =========================================================================== #
#  1. MESSAGE COMPILER TESTS
# =========================================================================== #

class TestMessageCompiler:
    """Verifies $100M Leads message grammar, word limits, and deterministic fallback."""

    def test_word_count_ceiling(self, session):
        biz = _create_biz(session)
        qual = _create_qual(session, biz)
        compiler = MessageCompiler()

        msg = compiler.compile(biz, qual)
        words = msg.split()
        assert len(words) <= MAX_WORD_COUNT, f"Message too long: {len(words)} words"

    def test_absence_of_banned_buzzwords_and_exclamations(self, session):
        biz = _create_biz(session)
        qual = _create_qual(session, biz)
        compiler = MessageCompiler()

        msg = compiler.compile(biz, qual)

        # Zero exclamation marks
        assert "!" not in msg, "Message must have zero exclamation marks"

        # No banned buzzwords
        msg_lower = msg.lower()
        for banned in BANNED_PHRASES:
            assert banned not in msg_lower, f"Found banned phrase '{banned}' in message"

    def test_inclusion_of_detected_asset_and_location(self, session):
        biz = _create_biz(
            session,
            location_district="Chingola Mining Corridor",
        )
        qual = _create_qual(
            session,
            biz,
            asset_footprint={"heavy_machinery": ["Komatsu D375 Bulldozer"]},
        )
        compiler = MessageCompiler()

        msg = compiler.compile(biz, qual)

        assert "Komatsu D375 Bulldozer" in msg
        assert "Chingola Mining Corridor" in msg
        assert "Mind if I drop it here?" in msg

    def test_fallback_when_gemini_client_is_none(self, session):
        biz = _create_biz(session)
        qual = _create_qual(session, biz)
        compiler = MessageCompiler(gemini_client=None)

        msg = compiler.compile(biz, qual)
        valid, reason = validate_outreach_grammar(msg)
        assert valid, f"Fallback output failed validation: {reason}"
        assert "CAT 330 Excavator" in msg

    def test_fallback_when_gemini_client_throws_exception(self, session):
        mock_client = MagicMock()
        mock_client.generate_text.side_effect = RuntimeError("Quota exhausted or connection refused")

        biz = _create_biz(session)
        qual = _create_qual(session, biz)
        compiler = MessageCompiler(gemini_client=mock_client)

        msg = compiler.compile(biz, qual)
        valid, reason = validate_outreach_grammar(msg)
        assert valid, f"Output failed validation after Gemini error: {reason}"
        assert len(msg.split()) <= MAX_WORD_COUNT

    def test_gemini_output_violating_rules_falls_back(self, session):
        mock_client = MagicMock()
        # Returns message with exclamation mark and banned word "Excited"
        mock_client.generate_text.return_value = "We are Excited to share our revolutionary game-changer! Check it out!"

        biz = _create_biz(session)
        qual = _create_qual(session, biz)
        compiler = MessageCompiler(gemini_client=mock_client)

        msg = compiler.compile(biz, qual)
        # Should have fallen back to deterministic copy
        assert "!" not in msg
        assert "excited" not in msg.lower()
        assert "Mind if I drop it here?" in msg

    def test_gemini_output_compliant_is_accepted(self, session):
        compliant_copy = (
            "Noticed your CAT excavator fleet along Kafue Road. Commercial procurement teams "
            "cannot verify your machinery specs on mobile. We built a 3-point capability "
            "review for your contracts. Mind if I drop it here?"
        )
        mock_client = MagicMock()
        mock_client.generate_text.return_value = compliant_copy

        biz = _create_biz(session)
        qual = _create_qual(session, biz)
        compiler = MessageCompiler(gemini_client=mock_client)

        msg = compiler.compile(biz, qual)
        assert msg == compliant_copy


# =========================================================================== #
#  2. BATCH WORKFLOW TESTS
# =========================================================================== #

class TestBatchWorkflow:
    """Verifies staging, batch approval, and rejection transitions."""

    def test_qualified_leads_transition_to_staged_with_drafted_job(self, session):
        biz = _create_biz(session)
        qual = _create_qual(session, biz)

        jobs = draft_outreach_for_qualified(session, limit=10)

        assert len(jobs) == 1
        job = jobs[0]
        assert job.status == DispatchStatus.DRAFTED.value
        assert job.business_id == biz.id
        assert job.channel == DispatchChannel.FB_MESSENGER.value
        assert qual.qualification_state == QualificationState.STAGED_FOR_OUTREACH.value

    def test_draft_skips_suppressed_businesses(self, session):
        biz = _create_biz(session, is_suppressed=True)
        _create_qual(session, biz)

        jobs = draft_outreach_for_qualified(session, limit=10)
        assert len(jobs) == 0

    def test_batch_approval_moves_jobs_to_queued(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session, limit=10)
        assert len(jobs) == 1
        job_id = jobs[0].id

        approved = approve_batch(session, job_ids=[job_id])

        assert len(approved) == 1
        assert approved[0].status == DispatchStatus.QUEUED.value

    def test_batch_approval_with_channel_override(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session, limit=10)
        job_id = jobs[0].id

        approved = approve_batch(session, job_ids=[job_id], channel="EMAIL")

        assert len(approved) == 1
        assert approved[0].channel == "EMAIL"
        assert approved[0].status == DispatchStatus.QUEUED.value

    def test_batch_approval_invalid_channel_raises(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session, limit=10)
        job_id = jobs[0].id

        with pytest.raises(ValueError, match="Invalid target channel"):
            approve_batch(session, job_ids=[job_id], channel="TELEPATHY")

    def test_batch_rejection_moves_jobs_to_blocked_with_reason(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session, limit=10)
        job_id = jobs[0].id

        rejected = reject_batch(session, job_ids=[job_id], reason="Competitor relationship identified")

        assert len(rejected) == 1
        assert rejected[0].status == DispatchStatus.BLOCKED.value
        assert rejected[0].blocked_reason == "Competitor relationship identified"

    def test_batch_rejection_requires_non_empty_reason(self, session):
        biz = _create_biz(session)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session, limit=10)
        job_id = jobs[0].id

        with pytest.raises(ValueError, match="Rejection reason is required"):
            reject_batch(session, job_ids=[job_id], reason="   ")


# =========================================================================== #
#  3. CHANNEL ADAPTERS & POLICY HONESTY TESTS
# =========================================================================== #

class TestChannelAdaptersAndPolicyHonesty:
    """Verifies channel adapters respect Meta policy, suppression, and test mode."""

    def test_email_adapter_blocks_suppressed_business(self, session):
        biz = _create_biz(session, is_suppressed=True, email="test@suppressed.com")
        adapter = EmailAdapter(test_mode=True)
        job = DispatchJob(
            business_id=biz.id,
            channel="EMAIL",
            status="QUEUED",
            recipient_target=biz.email,
            rendered_content="Test message",
            idempotency_key="suppressed_test_1",
            scheduled_for=dt.now(timezone.utc),
        )

        res = adapter.send(job, biz)
        assert res.success is False
        assert res.status == DispatchStatus.BLOCKED
        assert "suppressed" in res.error_message.lower()

    def test_email_adapter_dispatches_valid_email_in_test_mode(self, session):
        biz = _create_biz(session, is_suppressed=False, email="valid.procurement@mining.zm")
        adapter = EmailAdapter(test_mode=True)
        job = DispatchJob(
            business_id=biz.id,
            channel="EMAIL",
            status="QUEUED",
            recipient_target=biz.email,
            rendered_content="Test message",
            idempotency_key="email_test_1",
            scheduled_for=dt.now(timezone.utc),
        )

        res = adapter.send(job, biz)
        assert res.success is True
        assert res.status == DispatchStatus.SENT
        assert res.external_id is not None
        assert res.external_id.startswith("mock_email_")

    def test_email_adapter_fails_for_invalid_syntax(self, session):
        biz = _create_biz(session, is_suppressed=False, email="invalid-email-address")
        adapter = EmailAdapter(test_mode=True)
        job = DispatchJob(
            business_id=biz.id,
            channel="EMAIL",
            status="QUEUED",
            recipient_target="invalid-email-address",
            rendered_content="Test message",
            idempotency_key="email_test_invalid",
            scheduled_for=dt.now(timezone.utc),
        )

        res = adapter.send(job, biz)
        assert res.success is False
        assert res.status == DispatchStatus.FAILED
        assert "Invalid" in res.error_message

    def test_messenger_adapter_blocks_cold_automated_send(self, session):
        """Honest Meta Messenger policy enforcement: cold API send must return BLOCKED."""
        biz = _create_biz(
            session,
            fb_page_url="https://facebook.com/kafueearthmoving",
            source_provenance={"origin": "scrape"},  # No prior PSID
        )
        adapter = MessengerAdapter(force_opt_in=False)
        job = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status="QUEUED",
            recipient_target=biz.fb_page_url,
            rendered_content="Test message",
            idempotency_key="fb_cold_test_1",
            scheduled_for=dt.now(timezone.utc),
        )

        res = adapter.send(job, biz)

        # Must NOT fake delivery
        assert res.success is False
        assert res.status == DispatchStatus.BLOCKED
        assert "Meta Messenger policy" in res.error_message
        assert res.requires_operator_action is True
        assert res.action_url == "https://m.me/kafueearthmoving"

    def test_messenger_adapter_allows_opted_in_send(self, session):
        biz = _create_biz(
            session,
            fb_page_url="https://facebook.com/kafueearthmoving",
            source_provenance={"messenger_psid": "1234567890"},
        )
        adapter = MessengerAdapter()
        job = DispatchJob(
            business_id=biz.id,
            channel="FB_MESSENGER",
            status="QUEUED",
            recipient_target=biz.fb_page_url,
            rendered_content="Test message",
            idempotency_key="fb_opted_in_test",
            scheduled_for=dt.now(timezone.utc),
        )

        res = adapter.send(job, biz)
        assert res.success is True
        assert res.status == DispatchStatus.SENT

    def test_assisted_adapter_formats_payload_and_confirms_send(self, session):
        biz = _create_biz(
            session,
            company_name="Copperbelt Haulage",
            fb_page_url="https://facebook.com/copperbelthaulage",
            phone_normalized="+260977889900",
        )
        job = DispatchJob(
            business_id=biz.id,
            channel="MANUAL",
            status="QUEUED",
            recipient_target=biz.phone_normalized,
            rendered_content="Sample review message for Copperbelt",
            idempotency_key="assisted_test_1",
            scheduled_for=dt.now(timezone.utc),
        )

        adapter = AssistedAdapter(operator_confirmed=True)
        payload = adapter.prepare_payload(job, biz)

        assert payload["company_name"] == "Copperbelt Haulage"
        assert payload["action_url"] == "https://m.me/copperbelthaulage"
        assert payload["clipboard_content"] == "Sample review message for Copperbelt"
        assert payload["phone"] == "+260977889900"

        # Confirmed execution
        res = adapter.send(job, biz)
        assert res.success is True
        assert res.status == DispatchStatus.SENT
        assert res.external_id.startswith("assisted_")


# =========================================================================== #
#  4. DISPATCHER SAFETY & THROTTLES TESTS
# =========================================================================== #

class TestDispatcherSafetyAndThrottles:
    """Verifies operating hours, daily cap, jitter, idempotency, and conversation handshake."""

    def test_operating_hours_blocks_outside_window(self, session):
        # Sunday at 10:00 CAT
        sunday_morning = dt(2026, 10, 18, 10, 0, tzinfo=CAT_TIMEZONE)
        assert is_within_operating_hours(sunday_morning) is False

        # Tuesday at 19:30 CAT (after 16:30)
        tuesday_night = dt(2026, 10, 13, 19, 30, tzinfo=CAT_TIMEZONE)
        assert is_within_operating_hours(tuesday_night) is False

        # Tuesday at 07:45 CAT (before 08:00)
        tuesday_early = dt(2026, 10, 13, 7, 45, tzinfo=CAT_TIMEZONE)
        assert is_within_operating_hours(tuesday_early) is False

        # Queue a job and run dispatcher simulated at night
        biz = _create_biz(session)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [jobs[0].id])

        worker = DispatchWorker()
        summary = worker.dispatch_queue(session, simulated_time=tuesday_night)

        assert summary.held_by_operating_hours is True
        assert summary.dispatched == 0

    def test_operating_hours_allows_within_window(self, session):
        # Wednesday at 11:30 CAT (within 08:00 - 16:30)
        wednesday_midday = dt(2026, 10, 14, 11, 30, tzinfo=CAT_TIMEZONE)
        assert is_within_operating_hours(wednesday_midday) is True

        biz = _create_biz(session, email="ops@contractor.zm", fb_page_url=None)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [jobs[0].id], channel="EMAIL")

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        summary = worker.dispatch_queue(session, simulated_time=wednesday_midday)

        assert summary.held_by_operating_hours is False
        assert summary.dispatched == 1

    def test_force_flag_bypasses_operating_hours(self, session):
        sunday_night = dt(2026, 10, 18, 22, 0, tzinfo=CAT_TIMEZONE)
        biz = _create_biz(session, email="test@contractor.zm", fb_page_url=None)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [jobs[0].id], channel="EMAIL")

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        summary = worker.dispatch_queue(
            session,
            force=True,
            simulated_time=sunday_night,
        )

        assert summary.dispatched == 1

    def test_daily_cap_enforcement_stops_queue(self, session):
        # Set daily cap to 2
        worker = DispatchWorker(daily_cap=2, min_delay=0.0, max_delay=0.0)

        # Create 4 qualified businesses with emails
        for i in range(4):
            b = _create_biz(
                session,
                company_name=f"Plant Firm {i}",
                email=f"firm{i}@mining.zm",
                fb_page_url=None,
            )
            _create_qual(session, b)

        jobs = draft_outreach_for_qualified(session, limit=10)
        approve_batch(session, [j.id for j in jobs], channel="EMAIL")

        wednesday = dt(2026, 10, 14, 10, 0, tzinfo=CAT_TIMEZONE)
        summary = worker.dispatch_queue(session, simulated_time=wednesday)

        assert summary.dispatched == 2
        assert summary.daily_cap_reached is True

        # Check that remaining jobs are still QUEUED
        queued_remaining = (
            session.query(DispatchJob)
            .filter(DispatchJob.status == DispatchStatus.QUEUED.value)
            .count()
        )
        assert queued_remaining == 2

    def test_idempotency_prevents_duplicate_same_day_outreach(self, session):
        biz = _create_biz(session, email="same.day@contractor.zm", fb_page_url=None)

        now = dt.now(timezone.utc)
        # Manually create a job that was already SENT today
        first_job = DispatchJob(
            business_id=biz.id,
            channel="EMAIL",
            status=DispatchStatus.SENT.value,
            recipient_target=biz.email,
            rendered_content="First dispatch",
            idempotency_key=f"{biz.id}_EMAIL_today_1",
            scheduled_for=now,
            sent_at=now,
        )
        session.add(first_job)

        # Now create a second job for same business that gets queued
        second_job = DispatchJob(
            business_id=biz.id,
            channel="EMAIL",
            status=DispatchStatus.QUEUED.value,
            recipient_target=biz.email,
            rendered_content="Second attempt",
            idempotency_key=f"{biz.id}_EMAIL_today_2",
            scheduled_for=now,
        )
        session.add(second_job)
        session.commit()

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        wednesday = dt(2026, 10, 14, 10, 0, tzinfo=CAT_TIMEZONE)
        summary = worker.dispatch_queue(session, force=True)

        assert summary.dispatched == 0
        assert summary.blocked == 1
        assert second_job.status == DispatchStatus.BLOCKED.value
        assert "Duplicate" in second_job.blocked_reason

    def test_conversation_handshake_creates_awaiting_reply_and_message(self, session):
        biz = _create_biz(
            session,
            company_name="Luanshya Quarrying",
            email="contracts@luanshya-quarry.zm",
            fb_page_url=None,
        )
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [jobs[0].id], channel="EMAIL")

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        summary = worker.dispatch_queue(session, force=True)

        assert summary.dispatched == 1

        # Check Conversation was created
        conv = (
            session.query(Conversation)
            .filter(Conversation.business_id == biz.id)
            .first()
        )
        assert conv is not None
        assert conv.state == ConversationState.AWAITING_REPLY.value
        assert conv.channel == "EMAIL"

        # Check outbound Message was recorded
        msg = (
            session.query(Message)
            .filter(Message.conversation_id == conv.id)
            .first()
        )
        assert msg is not None
        assert msg.direction == MessageDirection.OUTBOUND.value
        assert msg.delivery_status == DeliveryStatus.SENT.value
        assert "3-point capability review" in msg.content
        assert msg.raw_metadata.get("dispatch_job_id") == jobs[0].id

    def test_dispatcher_pacing_jitter_called(self, session):
        sleep_records = []

        def mock_sleep(sec):
            sleep_records.append(sec)

        worker = DispatchWorker(
            min_delay=50.0,
            max_delay=55.0,
            sleep_fn=mock_sleep,
        )

        for i in range(2):
            b = _create_biz(session, email=f"pacing{i}@test.zm", fb_page_url=None)
            _create_qual(session, b)

        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [j.id for j in jobs], channel="EMAIL")

        summary = worker.dispatch_queue(session, force=True)
        assert summary.dispatched == 2
        # Jitter called once between the 2 jobs
        assert len(sleep_records) == 1
        assert 50.0 <= sleep_records[0] <= 55.0

    def test_dispatcher_dry_run_leaves_jobs_unchanged(self, session):
        biz = _create_biz(session, email="dryrun@test.zm", fb_page_url=None)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [jobs[0].id], channel="EMAIL")

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        summary = worker.dispatch_queue(session, force=True, dry_run=True)

        assert summary.dispatched == 1
        # In dry run, status remains QUEUED
        job = session.query(DispatchJob).filter(DispatchJob.id == jobs[0].id).one()
        assert job.status == DispatchStatus.QUEUED.value

        # No conversation created
        conv = session.query(Conversation).filter(Conversation.business_id == biz.id).first()
        assert conv is None

    def test_dispatcher_handles_failed_job_transition(self, session):
        """When an adapter reports failure, dispatcher transitions job to FAILED."""
        biz = _create_biz(session, email="invalid-email-address", fb_page_url=None)
        _create_qual(session, biz)
        jobs = draft_outreach_for_qualified(session)
        approve_batch(session, [jobs[0].id], channel="EMAIL")

        worker = DispatchWorker(min_delay=0.0, max_delay=0.0)
        summary = worker.dispatch_queue(session, force=True)

        assert summary.failed == 1
        assert summary.dispatched == 0
        job = session.query(DispatchJob).filter(DispatchJob.id == jobs[0].id).one()
        assert job.status == DispatchStatus.FAILED.value
        assert job.last_error is not None
        assert "Invalid" in job.last_error


# =========================================================================== #
#  5. ADAPTER EDGE CASES & CLI INTEGRATION TESTS
# =========================================================================== #

class TestAdapterEdgeCasesAndCLI:
    """Verifies adapter edge cases, username extraction, and CLI commands."""

    def test_extract_fb_username_variations(self):
        assert extract_fb_username("https://www.facebook.com/zambeziearthmoving") == "zambeziearthmoving"
        assert extract_fb_username("https://facebook.com/zambeziearthmoving/") == "zambeziearthmoving"
        assert extract_fb_username("https://m.me/mininghaulage") == "mininghaulage"
        assert extract_fb_username("https://facebook.com/profile.php?id=1000998877") == "1000998877"
        assert extract_fb_username("") == ""
        assert extract_fb_username(None) == ""

    def test_assisted_adapter_blocks_suppressed(self, session):
        biz = _create_biz(session, is_suppressed=True)
        job = DispatchJob(
            business_id=biz.id,
            channel="MANUAL",
            status="QUEUED",
            recipient_target=biz.company_name,
            rendered_content="Test message",
            idempotency_key="assisted_suppressed_test",
            scheduled_for=dt.now(timezone.utc),
        )
        adapter = AssistedAdapter(operator_confirmed=True)
        res = adapter.send(job, biz)
        assert res.success is False
        assert res.status == DispatchStatus.BLOCKED
        assert "suppressed" in res.error_message.lower()

    def test_cli_outreach_draft_and_approve_and_dispatch(self, engine, monkeypatch):
        from app.cli import (
            cmd_outreach_draft,
            cmd_outreach_approve,
            cmd_outreach_dispatch,
        )
        import argparse
        from sqlalchemy.orm import sessionmaker

        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        # Seed data
        seed_session = SessionFactory()
        biz = _create_biz(seed_session, email="cli.test@company.zm", fb_page_url=None)
        _create_qual(seed_session, biz)
        seed_session.commit()
        seed_session.close()

        # 1. CLI draft
        args_draft = argparse.Namespace(limit=10)
        cmd_outreach_draft(args_draft)

        check_session1 = SessionFactory()
        jobs = check_session1.query(DispatchJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == DispatchStatus.DRAFTED.value
        job_id = jobs[0].id
        check_session1.close()

        # 2. CLI approve
        args_approve = argparse.Namespace(all=True, job_ids=None, channel="EMAIL")
        cmd_outreach_approve(args_approve)

        check_session2 = SessionFactory()
        job2 = check_session2.query(DispatchJob).filter(DispatchJob.id == job_id).one()
        assert job2.status == DispatchStatus.QUEUED.value
        assert job2.channel == "EMAIL"
        check_session2.close()

        # 3. CLI dispatch with force
        args_dispatch = argparse.Namespace(
            limit=None, channel="EMAIL", force=True, dry_run=False
        )
        cmd_outreach_dispatch(args_dispatch)

        check_session3 = SessionFactory()
        job3 = check_session3.query(DispatchJob).filter(DispatchJob.id == job_id).one()
        assert job3.status == DispatchStatus.SENT.value
        check_session3.close()

    def test_cli_outreach_reject_command(self, engine, monkeypatch):
        from app.cli import cmd_outreach_draft, cmd_outreach_reject
        import argparse
        from sqlalchemy.orm import sessionmaker

        SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        monkeypatch.setattr("app.cli._get_session", lambda: SessionFactory())

        # Seed data
        seed_session = SessionFactory()
        biz = _create_biz(seed_session, email="reject.cli@company.zm", fb_page_url=None)
        _create_qual(seed_session, biz)
        seed_session.commit()
        seed_session.close()

        # Draft
        cmd_outreach_draft(argparse.Namespace(limit=10))

        check_session = SessionFactory()
        jobs = check_session.query(DispatchJob).all()
        assert len(jobs) == 1
        job_id = jobs[0].id
        check_session.close()

        # Reject
        cmd_outreach_reject(argparse.Namespace(job_ids=[job_id], reason="Target entity outside focus territory"))

        check_session2 = SessionFactory()
        job = check_session2.query(DispatchJob).filter(DispatchJob.id == job_id).one()
        assert job.status == DispatchStatus.BLOCKED.value
        assert job.blocked_reason == "Target entity outside focus territory"
        check_session2.close()

    def test_message_compiler_empty_qualification_graceful_handling(self, session):
        biz = _create_biz(session, location_district=None)
        compiler = MessageCompiler()
        # Compile with None qualification
        msg = compiler.compile(biz, None)
        valid, reason = validate_outreach_grammar(msg)
        assert valid is True
        assert len(msg.split()) <= MAX_WORD_COUNT
        assert "Mind if I drop it here?" in msg
