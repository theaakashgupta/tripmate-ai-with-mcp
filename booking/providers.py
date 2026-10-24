"""Provider adapters that turn a prepared selection into live quotes,
bookings, verification responses and cancellations.

Two implementations:

* ``SimulatedBookingProvider`` - deterministic, offline, used when no
  provider API is configured (this project ships without one).
* ``HttpBookingProvider`` - talks to a real provider endpoint when
  ``BOOKING_PROVIDER_BASE_URL`` is set.

Nothing in this module ever spends money on its own: it is only ever
called after the graph has validated an explicit user approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import requests

from .models import (
    default_currency,
    env_float,
    make_quote,
    new_id,
    nights_between,
)


class ProviderError(Exception):
    """Provider call failed. ``transient`` marks errors worth retrying."""

    def __init__(self, message: str, transient: bool = False, payload: dict | None = None):
        super().__init__(message)
        self.transient = transient
        self.payload = payload or {}


def _seed(*parts) -> int:
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


def _seeded_range(seed: int, low: int, high: int) -> float:
    return float(low + seed % (high - low + 1))


def _pct(value: float, pct: float) -> float:
    return round(value * pct / 100.0, 2)


# Fare-brand rules shared by every provider so a quote says the same thing
# about baggage and cancellation no matter where the price came from.
FARE_BRAND_CONDITIONS = {
    "Basic": {
        "baggage": "1 cabin bag (7kg) only",
        "cancellation_policy": "Non-refundable",
        "changes": "No changes permitted",
        "refundable": False,
        "fee_per_traveler": 12.0,
    },
    "Standard": {
        "baggage": "1 cabin bag (7kg) + 1 checked bag (23kg)",
        "cancellation_policy": "Refundable with a fee up to 24h before departure",
        "changes": "Changes allowed, fare difference applies",
        "refundable": False,
        "fee_per_traveler": 24.0,
    },
    "Flex": {
        "baggage": "1 cabin bag (7kg) + 2 checked bags (23kg each)",
        "cancellation_policy": "Fully refundable until 24h before departure",
        "changes": "Free changes, no fare difference on the same cabin",
        "refundable": True,
        "fee_per_traveler": 39.0,
    },
}


def fare_brand_conditions(brand: str | None) -> dict:
    """Baggage/cancellation rules for a fare brand."""
    key = str(brand or "Standard").strip().title()
    config = FARE_BRAND_CONDITIONS.get(key, FARE_BRAND_CONDITIONS["Standard"])
    return {
        "baggage": config["baggage"],
        "cancellation_policy": config["cancellation_policy"],
        "changes": config["changes"],
        "refundable": config["refundable"],
    }


class SimulatedBookingProvider:
    """Deterministic stand-in for a booking provider API."""

    name = "TripMate Simulated Provider"
    mode = "simulated"

    # ---------- quoting ----------

    def quote_flight(self, option: dict, plan: dict, traveler_count: int) -> dict:
        details = option.get("details") or {}
        seed = _seed(
            plan.get("origin"),
            plan.get("destination"),
            plan.get("start_date"),
            details.get("airline") or option.get("title"),
            details.get("fare_brand"),
        )
        count = max(int(traveler_count or 1), 1)
        brand = details.get("fare_brand") or "Standard"
        base_per_traveler = _seeded_range(seed, 165, 640)

        config = FARE_BRAND_CONDITIONS.get(brand, FARE_BRAND_CONDITIONS["Standard"])

        taxes = _pct(base_per_traveler, 12.0)
        fees = float(config["fee_per_traveler"])
        base = round(base_per_traveler * count, 2)

        components = [
            {"label": f"Base fare × {count} traveler(s)", "amount": base},
            {"label": "Taxes (12%)", "amount": round(taxes * count, 2)},
            {"label": "Airline & booking fees", "amount": round(fees * count, 2)},
        ]

        if "checked bag" not in config["baggage"].lower():
            components.append(
                {"label": "Checked baggage (optional add-on)", "amount": 0.0}
            )

        dates = {
            "departure": plan.get("start_date") or "",
            "return": plan.get("end_date") or "",
            "depart_time": details.get("depart") or "",
            "return_time": details.get("arrive") or "",
        }

        return make_quote(
            quote_id=new_id("qt"),
            kind="flight",
            option_id=option.get("option_id", ""),
            provider=self.name,
            currency=(plan.get("currency") or default_currency()),
            components=components,
            conditions={
                "fare_brand": brand,
                "baggage": config["baggage"],
                "cancellation_policy": config["cancellation_policy"],
                "changes": config["changes"],
                "refundable": config["refundable"],
                "seat_selection": "Advance seat selection available for a fee",
            },
            dates=dates,
            traveler_count=count,
        )

    def quote_hotel(self, option: dict, plan: dict, traveler_count: int) -> dict:
        details = option.get("details") or {}
        room_type = details.get("room_type") or "Standard Double"
        seed = _seed(
            plan.get("destination"),
            option.get("title"),
            plan.get("start_date"),
            room_type,
        )
        nights = nights_between(plan.get("start_date"), plan.get("end_date")) or 0
        nightly = _seeded_range(seed, 48, 310)
        room_total = round(nightly * max(nights, 0), 2)
        service = 6.0 * max(nights, 0)
        taxes = _pct(room_total, 12.0)
        free_cancellation = bool(details.get("free_cancellation", True))

        if nights > 0:
            components = [
                {
                    "label": f"{room_type} × {nights} night(s)",
                    "amount": room_total,
                },
                {
                    "label": "Taxes & service charges (12%)",
                    "amount": round(taxes + service, 2),
                },
            ]
        else:
            # Travel dates unknown: quote a per-night estimate instead of 0.
            components = [
                {
                    "label": (
                        f"{room_type} — per night estimate "
                        "(travel dates not set yet)"
                    ),
                    "amount": round(nightly, 2),
                }
            ]

        conditions = {
            "room_type": room_type,
            "board": details.get("board") or "Room only",
            "cancellation_policy": (
                "Free cancellation until 48 hours before check-in"
                if free_cancellation
                else "Non-refundable, prepaid rate"
            ),
            "changes": "Date changes subject to availability and rate difference",
            "refundable": free_cancellation,
            "occupancy": f"{max(int(traveler_count or 1), 1)} guest(s)",
        }

        return make_quote(
            quote_id=new_id("qt"),
            kind="hotel",
            option_id=option.get("option_id", ""),
            provider=self.name,
            currency=(plan.get("currency") or default_currency()),
            components=components,
            conditions=conditions,
            dates={
                "check_in": plan.get("start_date") or "",
                "check_out": plan.get("end_date") or "",
            },
            traveler_count=traveler_count or 1,
            nights=nights if nights > 0 else None,
            room_type=room_type,
        )

    # ---------- revalidation ----------

    def revalidate(self, quote: dict) -> dict:
        """Fetch live availability + price immediately before booking."""
        drift_pct = env_float("SIMULATED_PRICE_DRIFT_PCT", 1.0)
        forced = str(os.getenv("SIMULATED_MATERIAL_CHANGE") or "").strip() in {
            "1",
            "true",
            "yes",
        }
        sold_out = str(os.getenv("SIMULATED_SOLD_OUT") or "").strip() in {
            "1",
            "true",
            "yes",
        }

        seed = _seed(quote.get("quote_id"), quote.get("option_id"))
        direction = -1.0 if seed % 2 else 1.0

        factor = 1.0 + (direction * drift_pct / 100.0)
        if forced:
            factor = 1.13 if direction > 0 else 0.87

        old_total = float(quote.get("total") or 0)
        scale = factor if old_total else 1.0

        new_components = []
        for component in quote.get("components") or []:
            amount = round(float(component.get("amount") or 0) * scale, 2)
            new_components.append(
                {"label": component.get("label", ""), "amount": amount}
            )

        new_quote = dict(quote)
        new_quote["components"] = new_components
        new_quote["total"] = round(
            sum(float(c["amount"]) for c in new_components), 2
        )
        count = max(int(quote.get("traveler_count") or 1), 1)
        new_quote["per_traveler"] = round(new_quote["total"] / count, 2)
        new_quote["availability"] = not sold_out
        new_quote["quoted_at"] = time.time()
        new_quote["price_source"] = "provider_revalidated"

        if forced:
            conditions = dict(new_quote.get("conditions") or {})
            conditions["cancellation_policy"] = (
                "Non-refundable (policy tightened by the provider)"
            )
            conditions["refundable"] = False
            new_quote["conditions"] = conditions

        return {
            "checked_at": time.time(),
            "available": new_quote["availability"],
            "quote": new_quote,
        }

    # ---------- booking ----------

    def create_booking(self, quote: dict, travelers: list, approval_id: str) -> dict:
        if not approval_id:
            raise ProviderError("An approval reference is required to book.")
        if not quote.get("availability", True):
            raise ProviderError("The option is no longer available.", transient=False)

        reference = "TRP-" + hashlib.sha256(
            f"{quote.get('quote_id')}|{approval_id}|{time.time()}".encode()
        ).hexdigest()[:8].upper()

        return {
            "success": True,
            "reference": reference,
            "status": "confirmed",
            "provider": self.name,
            "approval_id": approval_id,
            "created_at": time.time(),
            "raw": {
                "confirmation": reference,
                "kind": quote.get("kind"),
                "total": quote.get("total"),
                "currency": quote.get("currency"),
                "message": "Provider accepted the booking.",
            },
        }

    def get_booking_status(self, reference: str) -> dict:
        if not reference:
            raise ProviderError("Missing booking reference.", transient=True)

        if str(os.getenv("SIMULATED_VERIFY_FAIL") or "").strip() in {"1", "true", "yes"}:
            return {
                "reference": reference,
                "status": "not_found",
                "raw": {"message": "Provider has no record of this reference."},
            }

        return {
            "reference": reference,
            "status": "confirmed",
            "raw": {"message": "Provider confirmed the booking."},
        }

    def cancellation_estimate(self, quote: dict) -> dict:
        """Fee/refund preview shown to the user before they approve a cancel."""
        total = float(quote.get("total") or 0)
        conditions = quote.get("conditions") or {}
        refundable = bool(conditions.get("refundable"))

        if refundable:
            fee = 0.0
        elif quote.get("kind") == "hotel":
            fee = round(total * 0.20, 2)
        else:
            fee = round(total * 0.10, 2)
        refund = round(max(total - fee, 0.0), 2)

        return {
            "amount_paid": round(total, 2),
            "cancellation_fee": fee,
            "refund": refund,
            "currency": quote.get("currency") or default_currency(),
        }

    def cancel_booking(self, quote: dict, reference: str) -> dict:
        estimate = self.cancellation_estimate(quote)

        return {
            "success": True,
            "reference": reference,
            "status": "cancelled",
            "provider": self.name,
            "cancellation_fee": estimate["cancellation_fee"],
            "refund": estimate["refund"],
            "currency": estimate["currency"],
            "created_at": time.time(),
            "raw": {
                "message": "Provider cancelled the booking.",
                "confirmation": f"CXL-{reference}",
            },
        }


class HttpBookingProvider:
    """Talks to a configured provider REST API.

    Expected endpoints (all JSON):
      POST {base}/quotes/revalidate   {quote} -> {available, quote}
      POST {base}/bookings            {quote, travelers, approval_id} -> booking
      GET  {base}/bookings/{ref}      -> {status, raw}
      POST {base}/bookings/{ref}/cancel {quote} -> cancellation
    """

    mode = "http"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # Reuse the simulated provider for quote construction: the provider
        # API is only contacted for live revalidation/booking/status calls.
        self._quoting = SimulatedBookingProvider()

    @property
    def name(self) -> str:
        return os.getenv("BOOKING_PROVIDER_NAME") or self.base_url

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers(),
                json=payload or {},
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise ProviderError(
                f"Provider unreachable: {error}", transient=True
            ) from error

        if response.status_code in (429, 500, 502, 503, 504):
            raise ProviderError(
                f"Provider transient error HTTP {response.status_code}",
                transient=True,
                payload={"status_code": response.status_code},
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"Provider rejected the request (HTTP {response.status_code}).",
                transient=False,
                payload={"body": response.text[:500]},
            )
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError("Provider returned invalid JSON.", transient=True) from error

    def quote_flight(self, option: dict, plan: dict, traveler_count: int) -> dict:
        return self._quoting.quote_flight(option, plan, traveler_count)

    def quote_hotel(self, option: dict, plan: dict, traveler_count: int) -> dict:
        return self._quoting.quote_hotel(option, plan, traveler_count)

    def revalidate(self, quote: dict) -> dict:
        data = self._request("POST", "/quotes/revalidate", {"quote": quote})
        return {
            "checked_at": time.time(),
            "available": bool(data.get("available", True)),
            "quote": data.get("quote") or quote,
        }

    def create_booking(self, quote: dict, travelers: list, approval_id: str) -> dict:
        if not approval_id:
            raise ProviderError("An approval reference is required to book.")
        data = self._request(
            "POST",
            "/bookings",
            {"quote": quote, "travelers": travelers, "approval_id": approval_id},
        )
        return {
            "success": bool(data.get("success", True)),
            "reference": data.get("reference") or data.get("confirmation") or "",
            "status": data.get("status") or "pending",
            "provider": self.name,
            "approval_id": approval_id,
            "created_at": time.time(),
            "raw": data,
        }

    def get_booking_status(self, reference: str) -> dict:
        data = self._request("GET", f"/bookings/{reference}")
        return {
            "reference": reference,
            "status": data.get("status") or "unknown",
            "raw": data,
        }

    def cancellation_estimate(self, quote: dict) -> dict:
        try:
            data = self._request("POST", "/quotes/cancellation-estimate", {"quote": quote})
            return {
                "amount_paid": float(data.get("amount_paid") or quote.get("total") or 0),
                "cancellation_fee": float(data.get("cancellation_fee") or 0),
                "refund": float(data.get("refund") or 0),
                "currency": data.get("currency") or quote.get("currency") or default_currency(),
            }
        except ProviderError:
            # Fall back to the local estimate so the user can still decide.
            return self._quoting.cancellation_estimate(quote)

    def cancel_booking(self, quote: dict, reference: str) -> dict:
        data = self._request(
            "POST", f"/bookings/{reference}/cancel", {"quote": quote}
        )
        return {
            "success": bool(data.get("success", True)),
            "reference": reference,
            "status": data.get("status") or "cancelled",
            "provider": self.name,
            "cancellation_fee": float(data.get("cancellation_fee") or 0),
            "refund": float(data.get("refund") or 0),
            "currency": data.get("currency") or quote.get("currency") or default_currency(),
            "created_at": time.time(),
            "raw": data,
        }


_provider_cache: dict = {}


def provider_mode() -> str:
    """Which provider to use.

    * ``browser`` / ``playwright`` - drive a real browser (needs Playwright)
    * ``http``                     - talk to a provider REST API
    * ``simulated`` (default)      - the offline stand-in
    """
    explicit = (os.getenv("BOOKING_PROVIDER_MODE") or "").strip().lower()
    if explicit:
        if explicit in ("browser", "playwright", "pw"):
            return "playwright"
        if explicit in ("http", "rest", "api"):
            return "http"
        if explicit in ("simulated", "sim", "offline", "fake"):
            return "simulated"
    if (os.getenv("BOOKING_PROVIDER_BASE_URL") or "").strip():
        return "http"
    return "simulated"


def get_provider():
    """The configured provider.

    The Playwright provider is imported lazily so the rest of the app keeps
    working when Playwright is not installed.
    """
    mode = provider_mode()
    cache_key = mode
    if mode == "http":
        cache_key = f"http:{os.getenv('BOOKING_PROVIDER_BASE_URL')}"
    if cache_key in _provider_cache:
        return _provider_cache[cache_key]

    if mode == "playwright":
        try:
            from .playwright_provider import PlaywrightBookingProvider
        except ImportError as error:
            raise ProviderError(
                "Browser automation is selected but Playwright is not installed. "
                "Run: pip install playwright && python -m playwright install chromium",
                transient=False,
            ) from error
        provider = PlaywrightBookingProvider()
    elif mode == "http":
        provider = HttpBookingProvider(
            (os.getenv("BOOKING_PROVIDER_BASE_URL") or "").strip(),
            (os.getenv("BOOKING_PROVIDER_API_KEY") or "").strip(),
        )
    else:
        provider = SimulatedBookingProvider()

    _provider_cache[cache_key] = provider
    return provider


def close_provider() -> None:
    """Close every cached provider and forget it.

    The Playwright provider owns a browser process, so the app calls this on
    shutdown; otherwise Chromium outlives the server. Errors are swallowed:
    teardown must never take the process down with it.
    """
    for provider in list(_provider_cache.values()):
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    _provider_cache.clear()


def with_retries(fn, attempts: int = 2, logger=print):
    """Recover from transient provider/network errors before giving up."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except ProviderError as error:
            last_error = error
            if not error.transient or attempt == attempts:
                raise
            if logger:
                logger(f"[provider] transient error (attempt {attempt}): {error}")
            time.sleep(1.0 * attempt)
        except (requests.RequestException,) as error:
            last_error = ProviderError(str(error), transient=True)
            if attempt == attempts:
                raise last_error from error
            if logger:
                logger(f"[provider] network error (attempt {attempt}): {error}")
            time.sleep(1.0 * attempt)
    raise last_error
