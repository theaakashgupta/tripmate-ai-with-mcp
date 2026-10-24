"""Registry of booking-site adapters.

An adapter is chosen by the kind of purchase it handles ("flight" or
"hotel"). ``BOOKING_FLIGHT_ADAPTER`` / ``BOOKING_HOTEL_ADAPTER`` can point
the app at a different adapter without touching code — handy for testing
against the local mock site.
"""

from __future__ import annotations

import os

from .base import BrowserOffer, CheckoutResult, SiteAdapter
from .booking_com import BookingComAdapter
from .google_flights import GoogleFlightsAdapter

_ADAPTERS = {
    "flight": GoogleFlightsAdapter,
    "hotel": BookingComAdapter,
}

# Friendly aliases so the env var can use a name instead of a class name.
_ALIASES = {
    "google_flights": "flight",
    "googleflights": "flight",
    "gflights": "flight",
    "booking_com": "hotel",
    "bookingcom": "hotel",
    "booking": "hotel",
}


def available_kinds() -> list[str]:
    return sorted(_ADAPTERS)


def get_adapter(kind: str, base_url: str | None = None) -> SiteAdapter:
    """Return the adapter that handles ``kind``."""
    key = str(kind or "").strip().lower()
    key = _ALIASES.get(key, key)
    adapter_cls = _ADAPTERS.get(key)
    if adapter_cls is None:
        raise ValueError(
            f"No browser adapter for '{kind}'. Available: {available_kinds()}"
        )
    return adapter_cls(base_url=base_url)


def _override(kind: str) -> str:
    variable = "BOOKING_FLIGHT_ADAPTER" if kind == "flight" else "BOOKING_HOTEL_ADAPTER"
    return (os.getenv(variable) or "").strip().lower()


def resolve_adapter(kind: str) -> SiteAdapter:
    """Adapter for ``kind``, honouring the env override."""
    key = str(kind or "").strip().lower()
    override = _override(key)
    if override:
        target = _ALIASES.get(override, override)
        if target != key and target in _ADAPTERS:
            kind = target
    return get_adapter(kind)


__all__ = [
    "BrowserOffer",
    "CheckoutResult",
    "SiteAdapter",
    "BookingComAdapter",
    "GoogleFlightsAdapter",
    "available_kinds",
    "get_adapter",
    "resolve_adapter",
]
