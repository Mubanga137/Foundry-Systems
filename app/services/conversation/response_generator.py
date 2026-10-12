"""
FS-CAE Inbound Objection & Response Generator

Provides structured, high-converting objection handlers and commercial responses
tailored to Zambian contractor psychology.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.models.business import Business

logger = logging.getLogger(__name__)


def generate_pricing_response(business: Business) -> str:
    """
    Handle pricing questions:
    Anchors against standard K35,000+ custom agency builds,
    presents K14,500 capability investment, and highlights K5,500 mobilization.
    """
    return (
        f"Custom enterprise systems for industrial contractors typically run K35,000+. "
        f"Our focused Capability Portal Core for {business.company_name} is packaged at K14,500, "
        f"split across three verified delivery milestones starting with a K5,500 mobilization deposit. "
        f"Mind if I send through the 1-page milestone schedule?"
    )


def generate_existing_website_response(business: Business) -> str:
    """
    Handle 'already have a website' objection:
    Validates presence, shifts focus to interactive fleet spec digitization,
    mobile RFQs for commercial estimators, and procurement verifiability.
    """
    return (
        f"Understood. Most contractors we partner with already have a corporate landing page. "
        f"Our capability portal is purpose-built as a mobile tool for site estimators and procurement officers: "
        f"instant machinery spec verification, WhatsApp RFQ capture, and verified compliance credentials. "
        f"Would you like to review how that integrates alongside your current presence?"
    )


def generate_call_or_meeting_response(business: Business) -> str:
    """Handle requests for phone call or in-person meeting."""
    phone = business.phone_normalized or business.phone_raw or "your line"
    return (
        f"Happy to connect directly. I will arrange a brief 10-minute briefing for {business.company_name}. "
        f"Is {phone} the best direct line for you today?"
    )


def generate_timing_delay_response(business: Business) -> str:
    """Handle requests to check back later / timing delay."""
    return (
        f"Understood, timing is key. We will hold your capability review on file and check back next month. "
        f"All the best with your ongoing project delivery."
    )


def generate_general_question_response(business: Business, question: Optional[str] = None) -> str:
    """Handle general commercial questions."""
    return (
        f"We build dedicated mobile capability portals for commercial contractors across Zambia, "
        f"allowing institutional procurement teams to verify fleet capacity and request RFQs instantly on mobile. "
        f"Happy to share a 3-point review of how your current digital setup compares."
    )


class ResponseGenerator:
    """Class wrapper for generating responses based on intent."""

    def __init__(self):
        pass

    def get_pricing_reply(self, business: Business) -> str:
        return generate_pricing_response(business)

    def get_website_objection_reply(self, business: Business) -> str:
        return generate_existing_website_response(business)

    def get_call_reply(self, business: Business) -> str:
        return generate_call_or_meeting_response(business)

    def get_delay_reply(self, business: Business) -> str:
        return generate_timing_delay_response(business)

    def get_general_reply(self, business: Business, question: Optional[str] = None) -> str:
        return generate_general_question_response(business, question)
