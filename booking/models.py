"""Domain models for the approval-gated booking workflow.

Every object that lives inside LangGraph state is a plain dict so the
Postgres checkpointer (JSON serializer) can persist it across interrupts
and resume the same workflow later.
"""

from __future__ import annotations

import os
import time
import uuid
from enum import Enum


class BookingStatus(str, Enum):
    """Explicit states of the booking workflow."""

    DRAFT = "DRAFT"
    OPTIONS_READY = "OPTIONS_READY"
    AWAITING_USER_APPROVAL = "AWAITING_USER_APPROVAL"
    REVALIDATING = "REVALIDATING"
    BOOKING_IN_PROGRESS = "BOOKING_IN_PROGRESS"
    BOOKED = "BOOKED"
    BOOKING_FAILED = "BOOKING_FAILED"
    APPROVAL_REQUIRED_AGAIN = "APPROVAL_REQUIRED_AGAIN"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    # The browser filled in the traveller details and stopped at the payment
    # form. Nothing has been paid yet, so this is not a confirmed booking.
    AWAITING_PAYMENT = "AWAITING_PAYMENT"


# Statuses where money has been committed (or is being committed).
MONEY_COMMITTED_STATUSES = {
    BookingStatus.BOOKING_IN_PROGRESS.value,
    BookingStatus.BOOKED.value,
    BookingStatus.CANCEL_PENDING.value,
}

# Statuses where the workflow is waiting on the human.
WAITING_STATUSES = {
    BookingStatus.AWAITING_USER_APPROVAL.value,
    BookingStatus.CANCEL_PENDING.value,
    BookingStatus.APPROVAL_REQUIRED_AGAIN.value,
    BookingStatus.AWAITING_PAYMENT.value,
}


STATUS_LABELS = {
    BookingStatus.DRAFT.value: "Draft",
    BookingStatus.OPTIONS_READY.value: "Options ready",
    BookingStatus.AWAITING_USER_APPROVAL.value: "Awaiting your approval",
    BookingStatus.REVALIDATING.value: "Revalidating price",
    BookingStatus.BOOKING_IN_PROGRESS.value: "Booking in progress",
    BookingStatus.BOOKED.value: "Booked",
    BookingStatus.BOOKING_FAILED.value: "Booking failed",
    BookingStatus.APPROVAL_REQUIRED_AGAIN.value: "Approval required again",
    BookingStatus.CANCEL_PENDING.value: "Cancellation pending approval",
    BookingStatus.CANCELLED.value: "Cancelled",
    BookingStatus.AWAITING_PAYMENT.value: "Awaiting your payment",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def default_currency() -> str:
    return (os.getenv("DEFAULT_CURRENCY") or "USD").strip().upper()


def approval_ttl_minutes() -> float:
    """Approvals expire after this many minutes unless refreshed."""
    return env_float("APPROVAL_TTL_MINUTES", 15.0)


def price_tolerance_pct() -> float:
    """A price move at or above this percentage is a material change."""
    return env_float("PRICE_CHANGE_TOLERANCE_PCT", 2.0)


def price_tolerance_abs() -> float:
    """A price move at or above this absolute amount is a material change."""
    return env_float("PRICE_CHANGE_TOLERANCE_ABS", 50.0)


def make_traveler(name: str) -> dict:
    return {"name": str(name).strip(), "type": "adult"}


def make_plan(
    destination: str = "",
    origin: str = "",
    start_date: str = "",
    end_date: str = "",
    travelers: list | None = None,
    budget: float | None = None,
    currency: str | None = None,
    summary: str = "",
    booking_intent: bool = False,
    cancel_intent: bool = False,
) -> dict:
    return {
        "destination": (destination or "").strip(),
        "origin": (origin or "").strip(),
        "start_date": (start_date or "").strip(),
        "end_date": (end_date or "").strip(),
        "travelers": travelers or [],
        "budget": budget,
        "currency": currency or default_currency(),
        "summary": (summary or "").strip(),
        "booking_intent": bool(booking_intent),
        "cancel_intent": bool(cancel_intent),
        "updated_at": now(),
    }


def merge_plan(existing: dict, incoming: dict) -> dict:
    """Keep previously known trip details when a later message omits them.

    A follow-up like "book it" carries no dates or traveler names, so the
    plan extracted from the original request must survive.
    """
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key in ("updated_at",):
            continue
        if value in (None, "", [], 0):
            continue
        if key == "travelers" and not isinstance(value, list):
            continue
        if key == "budget" and merged.get("budget"):
            continue
        # Never let a later turn switch off an intent that is still active.
        if key in ("booking_intent", "cancel_intent"):
            merged[key] = bool(merged.get(key)) or bool(value)
            continue
        merged[key] = value
    merged.setdefault("currency", default_currency())
    merged["updated_at"] = now()
    return merged


def nights_between(start_date: str, end_date: str) -> int | None:
    """Nights between two ISO dates, or None when dates are unknown."""
    from datetime import date

    try:
        start = date.fromisoformat(str(start_date)[:10])
        end = date.fromisoformat(str(end_date)[:10])
    except (ValueError, TypeError):
        return None
    delta = (end - start).days
    return delta if delta > 0 else None


def make_option(
    option_id: str,
    kind: str,
    title: str,
    subtitle: str = "",
    details: dict | None = None,
    provider: str = "",
) -> dict:
    return {
        "option_id": option_id,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "details": details or {},
        "provider": provider,
    }


def make_quote(
    quote_id: str,
    kind: str,
    option_id: str,
    provider: str,
    currency: str,
    components: list,
    conditions: dict,
    dates: dict,
    traveler_count: int,
    nights: int | None = None,
    room_type: str = "",
    availability: bool = True,
    price_source: str = "provider_quote",
) -> dict:
    total = round(sum(float(c.get("amount") or 0) for c in components), 2)
    count = max(int(traveler_count or 1), 1)
    return {
        "quote_id": quote_id,
        "kind": kind,
        "option_id": option_id,
        "provider": provider,
        "currency": currency,
        "components": components,
        "total": total,
        "per_traveler": round(total / count, 2),
        "traveler_count": count,
        "nights": nights,
        "room_type": room_type,
        "dates": dates,
        "conditions": conditions,
        "availability": bool(availability),
        "price_source": price_source,
        "quoted_at": now(),
    }


def make_purchase(kind: str, option: dict, quote: dict) -> dict:
    """One financially binding action (a flight purchase or a room purchase)."""
    return {
        "purchase_id": new_id("pu"),
        "kind": kind,
        "option": option,
        "quote": quote,
        "approval": None,
        "decision": None,
        "revalidation": None,
        "result": None,
        "verified": None,
        "history": [],
    }


def purchase_note(purchase: dict, note: str) -> dict:
    history = list(purchase.get("history") or [])
    history.append({"at": now(), "note": note})
    purchase = dict(purchase)
    purchase["history"] = history
    return purchase


def trip_totals(purchases: list) -> dict:
    """Estimated total trip cost across the selected purchases."""
    currency = default_currency()
    total = 0.0
    rows = []
    for purchase in purchases or []:
        quote = purchase.get("quote") or {}
        if not quote:
            continue
        currency = quote.get("currency") or currency
        amount = float(quote.get("total") or 0)
        total += amount
        rows.append(
            {
                "purchase_id": purchase.get("purchase_id"),
                "kind": purchase.get("kind"),
                "label": (purchase.get("option") or {}).get("title", ""),
                "amount": round(amount, 2),
            }
        )
    return {
        "currency": currency,
        "total": round(total, 2),
        "rows": rows,
    }
