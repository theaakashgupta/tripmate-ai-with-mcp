"""Approval tokens bound to the exact purchase they authorize.

An approval is only valid when every detail it was issued for still
matches: traveler, itinerary, provider, price, dates, fare/room
conditions and the booking action. Any mismatch or expiry forces the
workflow back into APPROVAL_REQUIRED_AGAIN.
"""

from __future__ import annotations

import hashlib
import json

from .models import (
    approval_ttl_minutes,
    default_currency,
    new_id,
    now,
    price_tolerance_abs,
    price_tolerance_pct,
)


def _stable(value):
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, dict):
        return {k: _stable(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def canonical_binding(
    action: str,
    plan: dict,
    purchase: dict,
    extra: dict | None = None,
) -> dict:
    """The exact facts an approval is bound to."""
    quote = purchase.get("quote") or {}
    option = purchase.get("option") or {}
    conditions = quote.get("conditions") or {}

    binding = {
        "action": action,
        "travelers": sorted(
            str((t or {}).get("name", "")).strip().lower()
            for t in (plan.get("travelers") or [])
            if str((t or {}).get("name", "")).strip()
        ),
        "itinerary": {
            "origin": (plan.get("origin") or "").strip().lower(),
            "destination": (plan.get("destination") or "").strip().lower(),
            "start_date": plan.get("start_date") or "",
            "end_date": plan.get("end_date") or "",
        },
        "provider": quote.get("provider") or option.get("provider") or "",
        "kind": purchase.get("kind") or "",
        "option_id": option.get("option_id") or "",
        "dates": _stable(quote.get("dates") or {}),
        "room_or_fare_conditions": _stable(
            {
                "room_type": quote.get("room_type") or "",
                "baggage": conditions.get("baggage") or "",
                "cancellation_policy": conditions.get("cancellation_policy") or "",
                "changes": conditions.get("changes") or "",
                "refundable": bool(conditions.get("refundable")),
            }
        ),
        "price": {
            "currency": quote.get("currency") or default_currency(),
            "total": round(float(quote.get("total") or 0), 2),
        },
    }
    if extra:
        binding["extra"] = _stable(extra)
    return binding


def binding_hash(binding: dict) -> str:
    payload = json.dumps(_stable(binding), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def issue_approval(action: str, binding: dict, ttl_minutes: float | None = None) -> dict:
    ttl = approval_ttl_minutes() if ttl_minutes is None else float(ttl_minutes)
    issued_at = now()
    return {
        "approval_id": new_id("apr"),
        "action": action,
        "binding": _stable(binding),
        "binding_hash": binding_hash(binding),
        "issued_at": issued_at,
        "expires_at": issued_at + ttl * 60.0,
        "ttl_minutes": ttl,
        "granted_by": None,
        "granted_at": None,
    }


def validate_approval(approval: dict | None, expected_binding: dict) -> tuple[bool, str]:
    """True only while the approval exists, has not expired and still matches."""
    if not approval:
        return False, "No approval was issued for this purchase."

    expected_hash = binding_hash(expected_binding)
    if approval.get("binding_hash") != expected_hash:
        return False, "The approved details changed (traveler, price, dates or conditions)."

    expires_at = float(approval.get("expires_at") or 0)
    if not expires_at:
        return False, "The approval has no expiry and cannot be used."
    if now() > expires_at:
        return False, "The approval expired before it was used."

    return True, ""


def grant_approval(approval: dict, granted_by: str = "user") -> dict:
    updated = dict(approval)
    updated["granted_by"] = granted_by
    updated["granted_at"] = now()
    return updated


def material_changes(old_quote: dict, new_quote: dict) -> list[str]:
    """Human-readable list of material differences between two quotes."""
    changes: list[str] = []

    if not new_quote.get("availability", True):
        changes.append("The option is no longer available.")

    old_total = float(old_quote.get("total") or 0)
    new_total = float(new_quote.get("total") or 0)
    delta = round(new_total - old_total, 2)
    if delta:
        pct = (abs(delta) / old_total * 100.0) if old_total else 100.0
        if pct >= price_tolerance_pct() or abs(delta) >= price_tolerance_abs():
            currency = new_quote.get("currency") or default_currency()
            changes.append(
                f"Price changed from {old_total:.2f} to {new_total:.2f} "
                f"{currency} ({delta:+.2f}, {pct:.1f}%)."
            )

    if (old_quote.get("currency") or "") != (new_quote.get("currency") or ""):
        changes.append("Currency changed.")

    if (old_quote.get("room_type") or "") != (new_quote.get("room_type") or ""):
        changes.append(
            f"Room type changed from '{old_quote.get('room_type') or '-'}' "
            f"to '{new_quote.get('room_type') or '-'}'."
        )

    old_dates = old_quote.get("dates") or {}
    new_dates = new_quote.get("dates") or {}
    for key in sorted(set(old_dates) | set(new_dates)):
        if (old_dates.get(key) or "") != (new_dates.get(key) or ""):
            changes.append(
                f"Dates changed: {key} {old_dates.get(key) or '-'} → "
                f"{new_dates.get(key) or '-'} ."
            )

    old_conditions = old_quote.get("conditions") or {}
    new_conditions = new_quote.get("conditions") or {}
    for key in ("baggage", "cancellation_policy", "changes", "refundable"):
        if old_conditions.get(key) != new_conditions.get(key):
            changes.append(
                f"Fare/room conditions changed ({key.replace('_', ' ')})."
            )

    old_option = (old_quote.get("option_id") or "")
    new_option = (new_quote.get("option_id") or "")
    if old_option and new_option and old_option != new_option:
        changes.append("A different option was substituted by the provider.")

    return changes


def describe_selection(purchases: list, plan: dict) -> str:
    """Compact markdown summary of what the user is being asked to approve."""
    lines = []
    travelers = ", ".join(
        t.get("name", "") for t in (plan.get("travelers") or []) if t.get("name")
    ) or "not specified"
    lines.append(f"**Traveler(s):** {travelers}")
    if plan.get("origin") or plan.get("destination"):
        lines.append(
            f"**Route:** {plan.get('origin') or '—'} ✈ {plan.get('destination') or '—'}"
        )
    if plan.get("start_date") or plan.get("end_date"):
        lines.append(
            f"**Dates:** {plan.get('start_date') or '—'} → {plan.get('end_date') or '—'}"
        )

    for purchase in purchases or []:
        option = purchase.get("option") or {}
        quote = purchase.get("quote") or {}
        conditions = quote.get("conditions") or {}
        label = "Flight" if purchase.get("kind") == "flight" else "Hotel"
        lines.append("")
        lines.append(f"### {label}: {option.get('title', '—')}")
        if option.get("subtitle"):
            lines.append(option["subtitle"])
        if quote.get("room_type"):
            lines.append(f"- **Room type:** {quote['room_type']}")
        if conditions.get("fare_brand"):
            lines.append(f"- **Fare brand:** {conditions['fare_brand']}")
        if conditions.get("baggage"):
            lines.append(f"- **Baggage:** {conditions['baggage']}")
        if conditions.get("cancellation_policy"):
            lines.append(f"- **Cancellation:** {conditions['cancellation_policy']}")
        if conditions.get("changes"):
            lines.append(f"- **Changes:** {conditions['changes']}")
        lines.append(
            f"- **Price source:** {quote.get('price_source', 'quote').replace('_', ' ')}"
        )
        lines.append("")
        lines.append("| Item | Amount |")
        lines.append("| --- | ---: |")
        for component in quote.get("components") or []:
            lines.append(
                f"| {component.get('label', '')} | "
                f"{component.get('amount', 0):.2f} {quote.get('currency', '')} |"
            )
        lines.append(
            f"| **Total** | **{float(quote.get('total') or 0):.2f} "
            f"{quote.get('currency', '')}** |"
        )

    return "\n".join(lines)
