"""Hermes-style task tracking, intent detection and approval parsing.

The agent decomposes a high-level travel goal into tracked tasks, keeps
their status in state, and exposes short progress lines (never
chain-of-thought or credentials).
"""

from __future__ import annotations

import re

from .models import new_id, now

RESEARCH_TASKS = [
    ("parse_request", "Understand the travel goal"),
    ("search_flights", "Search flights"),
    ("search_hotels", "Search hotels"),
    ("check_weather", "Check weather"),
    ("build_itinerary", "Generate itinerary"),
    ("compare_options", "Compare available options"),
    ("estimate_cost", "Estimate total trip cost"),
]

BOOKING_TASKS = [
    ("prepare_approval", "Prepare selections for approval"),
    ("revalidate", "Revalidate live price & availability"),
    ("execute_booking", "Execute authorized booking"),
    ("verify_booking", "Verify booking with provider"),
    ("save_receipt", "Save references & receipt"),
]

CANCEL_TASKS = [
    ("parse_request", "Understand the cancellation request"),
    ("review_booking", "Review the existing booking"),
    ("cancel_approval", "Request cancellation approval"),
    ("execute_cancel", "Execute authorized cancellation"),
    ("verify_cancel", "Verify cancellation with provider"),
]


def build_tasks(mode: str = "research") -> list[dict]:
    catalog = list(RESEARCH_TASKS)
    if mode == "booking":
        catalog += BOOKING_TASKS
    elif mode == "cancel":
        catalog = list(CANCEL_TASKS)

    return [
        {
            "key": key,
            "label": label,
            "status": "pending",
            "detail": "",
            "updated_at": now(),
        }
        for key, label in catalog
    ]


def _set(tasks: list, key: str, status: str, detail: str = "") -> list:
    updated = []
    for task in tasks or []:
        task = dict(task)
        if task.get("key") == key:
            task["status"] = status
            if detail:
                task["detail"] = detail
            task["updated_at"] = now()
        updated.append(task)
    return updated


def mark_running(tasks, key, detail=""):
    return _set(tasks, key, "in_progress", detail)


def mark_done(tasks, key, detail=""):
    return _set(tasks, key, "done", detail)


def mark_failed(tasks, key, detail=""):
    return _set(tasks, key, "failed", detail)


def task_progress(tasks) -> list[str]:
    """Short progress lines safe to show the user."""
    lines = []
    for task in tasks or []:
        status = task.get("status")
        if status == "done":
            lines.append(f"Done: {task.get('label')}")
        elif status == "in_progress":
            lines.append(f"Working: {task.get('label')}")
        elif status == "failed":
            lines.append(
                f"Failed: {task.get('label')}"
                + (f" ({task.get('detail')})" if task.get("detail") else "")
            )
    return lines


BOOKING_INTENT_RE = re.compile(
    r"\b(book|booking|reserve|reserved|reservation|purchase|buy|checkout|"
    r"confirm(?:\s+the)?\s+(?:booking|trip|flight|hotel)|pay for|"
    r"get\s+me\s+(?:the\s+)?tickets|hold\s+(?:my\s+)?(?:flight|room|hotel))\b",
    re.IGNORECASE,
)

# "do not book", "just plan", "without booking" … the user explicitly
# does NOT want anything financially binding.
BOOKING_NEGATED_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|no|without|never|not)\s+"
    r"(?:book(?:ing)?|reserve|reservation|purchase|buy|checkout|pay)\b"
    r"|\b(?:just|only)\s+(?:a\s+)?(?:plan|itinerary|ideas?|suggestions?)\b"
    r"|\bplan\s+(?:only|just)\b"
    r"|\bno\s+(?:booking|purchases?)\b"
    r"|\b(?:book(?:ing)?|reserve|purchase|buy)\s+nothing\b"
    r"|\bnothing\s+(?:to\s+)?(?:book|reserve|purchase)\b",
    re.IGNORECASE,
)

CANCEL_INTENT_RE = re.compile(
    r"\b(cancel|cancellation|refund|void|call\s+off)\b",
    re.IGNORECASE,
)

CANCEL_NEGATED_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|not)\s+"
    r"(?:cancel|cancellation|refund)\b",
    re.IGNORECASE,
)

RETRY_INTENT_RE = re.compile(
    r"\b(retry|try\s+again|attempt\s+(?:again|once\s+more))\b",
    re.IGNORECASE,
)


def detect_intent(query: str, has_bookings: bool = False) -> dict:
    text = str(query or "")
    cancel_negated = bool(CANCEL_NEGATED_RE.search(text))
    booking = bool(BOOKING_INTENT_RE.search(text)) and not (
        BOOKING_NEGATED_RE.search(text) or cancel_negated
    )
    return {
        "booking": booking,
        "cancel": bool(CANCEL_INTENT_RE.search(text))
        and not cancel_negated
        and has_bookings,
        "keep": cancel_negated and has_bookings,
        "retry": bool(RETRY_INTENT_RE.search(text)),
    }


def missing_requirements(plan: dict) -> list[str]:
    """Information that must exist before anything financially binding."""
    missing = []
    if not (plan or {}).get("destination"):
        missing.append("the destination city")
    if not (plan or {}).get("origin"):
        missing.append("your departure city")
    if not (plan or {}).get("start_date"):
        missing.append("the travel dates")
    if not (plan or {}).get("end_date"):
        missing.append("the return date")
    travelers = (plan or {}).get("travelers") or []
    if not any(str((t or {}).get("name", "")).strip() for t in travelers):
        missing.append("the full name of at least one traveler")
    return missing


def ask_for_missing(missing: list[str]) -> str:
    listed = ", ".join(missing[:-1]) + (" and " + missing[-1] if len(missing) > 1 else missing[0])
    return (
        "Before I can prepare anything for booking I still need "
        f"{listed}.\n\n"
        "I have kept your draft plan — send those details and I will "
        "continue the same workflow (nothing will be purchased without "
        "your explicit approval)."
    )


APPROVE_WORDS = re.compile(
    r"\b(approve|approved|confirm|confirmed|yes|yeah|yep|go ahead|proceed|"
    r"accept|accepted|looks good|do it|book it|ok|okay|sure|continue)\b",
    re.IGNORECASE,
)

REJECT_WORDS = re.compile(
    r"\b(reject|rejected|decline|declined|no|nope|do not|don't|stop|"
    r"not now|keep|skip|abort)\b",
    re.IGNORECASE,
)

MODIFY_WORDS = re.compile(
    r"\b(modify|change|swap|replace|different|another|cheaper|switch|"
    r"instead|rather|update|adjust)\b",
    re.IGNORECASE,
)


def parse_decision(text: str, pending_action: str, purchases: list) -> dict:
    """Turn a free-text reply (or an explicit API call) into decisions.

    Returns {"action": "approve_all"|"reject_all"|"custom"|"modify",
             "decisions": {approval_id: bool}, "modifications": str|None}
    """
    raw = str(text or "").strip()
    lower = raw.lower()

    has_approve = bool(APPROVE_WORDS.search(lower))
    has_reject = bool(REJECT_WORDS.search(lower))
    has_modify = bool(MODIFY_WORDS.search(lower))

    # Inside a cancellation approval the word "cancel" means "yes, cancel it".
    if pending_action == "cancel" and re.search(r"\b(cancel|yes|approve|confirm|go ahead)\b", lower):
        has_approve = True
        has_reject = False
        has_modify = False

    if has_modify and not has_approve:
        return {"action": "modify", "decisions": {}, "modifications": raw}

    kind_filter = None
    if re.search(r"\bflights?\b", lower) and not re.search(r"\bhotels?\b", lower):
        kind_filter = "flight"
    elif re.search(r"\bhotels?\b", lower) and not re.search(r"\bflights?\b", lower):
        kind_filter = "hotel"

    if has_reject and not has_approve:
        decisions = {p.get("approval_id"): False for p in (purchases or []) if p.get("approval_id")}
        return {"action": "reject_all", "decisions": decisions, "modifications": None}

    if has_approve:
        decisions = {}
        for purchase in purchases or []:
            approval_id = purchase.get("approval_id") or (
                (purchase.get("approval") or {}).get("approval_id")
            )
            if not approval_id:
                continue
            if kind_filter is None:
                decisions[approval_id] = True
            else:
                decisions[approval_id] = purchase.get("kind") == kind_filter
        if kind_filter and not any(decisions.values()):
            # e.g. "approve the flight" when only a hotel purchase exists
            decisions = {p.get("approval_id"): True for p in purchases if p.get("approval_id")}
        return {"action": "custom" if kind_filter else "approve_all",
                "decisions": decisions, "modifications": None}

    if has_modify:
        return {"action": "modify", "decisions": {}, "modifications": raw}

    return {"action": "unrecognized", "decisions": {}, "modifications": raw}


def decision_help(pending_action: str) -> str:
    if pending_action == "cancel":
        return (
            "There is a **cancellation waiting for your approval**.\n\n"
            "Reply with **\"approve cancellation\"** to proceed or "
            "**\"keep my booking\"** to leave it as is."
        )
    return (
        "Your selections are **awaiting approval** and nothing has been "
        "purchased yet.\n\n"
        "Reply with:\n"
        "- **\"approve\"** to authorize every listed purchase\n"
        "- **\"approve only the flight\"** / **\"approve only the hotel\"**\n"
        "- **\"modify ...\"** to change the selection\n"
        "- **\"reject\"** to discard the selections"
    )


def new_record(kind: str, reference: str, **fields) -> dict:
    record = {
        "record_id": new_id("bk"),
        "kind": kind,
        "reference": reference,
        "status": "confirmed",
        "created_at": now(),
    }
    record.update(fields)
    return record
