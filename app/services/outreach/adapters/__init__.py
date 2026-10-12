"""FS-CAE Channel Adapters Package."""

from app.services.outreach.adapters.base import AdapterResult, BaseChannelAdapter
from app.services.outreach.adapters.email_adapter import EmailAdapter
from app.services.outreach.adapters.messenger_adapter import MessengerAdapter
from app.services.outreach.adapters.assisted_adapter import AssistedAdapter

__all__ = [
    "AdapterResult",
    "BaseChannelAdapter",
    "EmailAdapter",
    "MessengerAdapter",
    "AssistedAdapter",
]
