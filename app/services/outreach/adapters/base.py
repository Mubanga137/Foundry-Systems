"""
FS-CAE Base Channel Adapter & Result Definitions

Defines the contract for all outbound communication adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.models.business import Business
from app.models.dispatch import DispatchJob
from app.schemas.states import DispatchState, DispatchStatus


@dataclass
class AdapterResult:
    """Standardized execution result returned by all channel adapters."""

    success: bool
    status: DispatchStatus  # SENT, DELIVERED, BLOCKED, or FAILED
    external_id: Optional[str] = None
    error_message: Optional[str] = None
    requires_operator_action: bool = False
    action_url: Optional[str] = None  # e.g., m.me link for assisted mode


class BaseChannelAdapter(ABC):
    """Abstract base class for all outreach channel adapters."""

    @abstractmethod
    def send(self, job: DispatchJob, business: Business) -> AdapterResult:
        """
        Execute dispatch attempt through the underlying communication channel.

        Args:
            job: The DispatchJob instance to execute.
            business: The recipient Business entity.

        Returns:
            AdapterResult with success status, details, and optional action URL.
        """
        pass
