"""Browser automation layer for the booking workflow."""

from .session import BrowserSession, PAYMENT_SELECTORS
from .adapters import (
    BrowserOffer,
    CheckoutResult,
    SiteAdapter,
    available_kinds,
    get_adapter,
    resolve_adapter,
)

__all__ = [
    "BrowserSession",
    "PAYMENT_SELECTORS",
    "BrowserOffer",
    "CheckoutResult",
    "SiteAdapter",
    "available_kinds",
    "get_adapter",
    "resolve_adapter",
]
