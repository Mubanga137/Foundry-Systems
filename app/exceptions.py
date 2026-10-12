"""
FS-CAE Custom Exceptions

Business-rule exceptions for state machine enforcement and financial integrity.
"""


class FSCAEError(Exception):
    """Base exception for all FS-CAE errors."""
    pass


class InvalidStateTransitionError(FSCAEError):
    """Raised when a state transition violates the defined state machine rules."""

    def __init__(self, entity_type: str, current_state: str, requested_state: str, reason: str = ""):
        self.entity_type = entity_type
        self.current_state = current_state
        self.requested_state = requested_state
        detail = f"{entity_type}: cannot transition from '{current_state}' to '{requested_state}'"
        if reason:
            detail += f" — {reason}"
        super().__init__(detail)


class UnmetMilestoneError(FSCAEError):
    """Raised when a project activation is attempted without a verified paid mobilization invoice."""

    def __init__(self, detail: str = "Mobilization invoice must be PAID before project activation."):
        super().__init__(detail)


class DuplicatePaymentError(FSCAEError):
    """Raised when a duplicate transaction_reference is detected on PaymentEvent."""

    def __init__(self, transaction_reference: str):
        super().__init__(
            f"Duplicate transaction reference: '{transaction_reference}'"
        )
