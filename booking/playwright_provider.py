"""Browser-automation booking provider.

This provider plugs a real browser into the existing approval-gated booking
workflow. It implements the same interface as the simulated and HTTP
providers, so the graph does not need to know which one is active.

Safety rules that this provider never breaks:

* it is only ever called after the graph validated a granted approval,
* it requires a non-empty ``approval_id`` for every purchase,
* it never types payment details — the flow stops at the payment form,
* it never attempts to solve or bypass a CAPTCHA or bot challenge.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Iterable

from .browser import BrowserOffer, BrowserSession, resolve_adapter
from .models import (
    default_currency,
    env_float,
    make_quote,
    new_id,
    nights_between,
)
from .providers import ProviderError, fare_brand_conditions

# Search results are cached briefly: the options agent asks for a quote per
# option, and there is no point hitting the site once per option.
SEARCH_CACHE_TTL_SECONDS = env_float("BROWSER_SEARCH_CACHE_TTL", 180.0)

# How close a live offer must be to the researched option's price to be
# considered the same option when the names do not match.
PRICE_MATCH_TOLERANCE_PCT = env_float("BROWSER_PRICE_MATCH_TOLERANCE_PCT", 15.0)


def _normalize(value: str) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum() or char == " ")


def _tokens(value: str) -> set[str]:
    return {token for token in _normalize(value).split() if len(token) > 2}


def _overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


class PlaywrightBookingProvider:
    """Books flights and hotels by driving a real browser."""

    mode = "playwright"

    def __init__(self, session: BrowserSession | None = None):
        self._session = session or BrowserSession()
        self._cache: dict = {}
        self._cache_lock = threading.Lock()

    @property
    def name(self) -> str:
        return os.getenv("BOOKING_PROVIDER_NAME") or "TripMate Browser Automation"

    # ------------------------------------------------------------------
    # searching
    # ------------------------------------------------------------------

    def _search(
        self,
        kind: str,
        plan: dict,
        travelers: list,
        limit: int = 5,
        fresh: bool = False,
    ) -> list[BrowserOffer]:
        """Run a browser search, with a short-lived cache.

        ``fresh`` skips the cache entirely: revalidation has to look at the
        site again, otherwise the "live" price it reports could be minutes
        old. The fresh result replaces the cached one.
        """
        key = self._cache_key(kind, plan, travelers)
        now = time.time()
        wanted = max(int(limit or 0), 5)

        if not fresh:
            with self._cache_lock:
                cached = self._cache.get(key)
                if (
                    cached
                    and now - cached["at"] < SEARCH_CACHE_TTL_SECONDS
                    and cached.get("limit", 0) >= wanted
                ):
                    return list(cached["offers"])[:limit]

        adapter = resolve_adapter(kind)
        # Read a few extra offers once so a later quote (which asks for the
        # default limit) is still served from this result.
        offers = self._session.run(
            lambda page: adapter.search(page, plan, travelers, limit=wanted)
        )

        with self._cache_lock:
            self._cache[key] = {"at": now, "offers": offers, "limit": wanted}
        return list(offers)[:limit]

    def search_offers(
        self, kind: str, plan: dict, travelers: list, limit: int = 5
    ) -> list[BrowserOffer]:
        """Live results for a plan.

        The graph uses this to show the user what the booking site
        actually has right now instead of prices found on the open web.
        """
        return self._search(kind, plan, travelers, limit=limit)

    def _cache_key(self, kind: str, plan: dict, travelers: list) -> str:
        return "|".join(
            [
                kind,
                str(plan.get("origin") or ""),
                str(plan.get("destination") or ""),
                str(plan.get("start_date") or ""),
                str(plan.get("end_date") or ""),
                str(plan.get("currency") or ""),
                str(len(travelers or [])),
            ]
        ).lower()

    def _match_offer(
        self, kind: str, option: dict, offers: Iterable[BrowserOffer]
    ) -> BrowserOffer | None:
        """Find the live offer that corresponds to the researched option.

        The match is deliberately strict: the bot must never quietly book a
        different flight or a different hotel than the one the user
        approved. When nothing matches, the option is reported as
        unavailable instead of being substituted.
        """
        offers = list(offers or [])
        if not offers:
            return None

        details = option.get("details") or {}
        option_title = str(option.get("title") or "")

        # The option was built from this very offer: match it exactly.
        live_id = str(details.get("live_offer_id") or "")
        if live_id:
            for offer in offers:
                if offer.offer_id == live_id:
                    return offer

        if kind == "flight":
            airline = str(details.get("airline") or "").strip()
            if not airline and "·" in option_title:
                airline = option_title.split("·")[0].strip()
            candidates = [
                offer
                for offer in offers
                if airline
                and (
                    _normalize(airline) in _normalize(offer.title)
                    or _normalize(offer.title) in _normalize(airline)
                )
            ]
            if not candidates:
                # Fall back to the route: same origin/destination pair.
                route = str(details.get("route") or "").strip()
                if route:
                    candidates = [
                        offer
                        for offer in offers
                        if _overlap(route, offer.subtitle) > 0.5
                    ]
            if candidates:
                return min(candidates, key=lambda offer: offer.price)
            return None

        # Hotel: match on the property name.
        candidates = [
            offer for offer in offers if _overlap(option_title, offer.title) >= 0.5
        ]
        if candidates:
            return min(candidates, key=lambda offer: offer.price)
        return None

    # ------------------------------------------------------------------
    # quoting
    # ------------------------------------------------------------------

    def quote_flight(self, option: dict, plan: dict, traveler_count: int) -> dict:
        travelers = plan.get("travelers") or []
        offers = self._search("flight", plan, travelers)
        offer = self._match_offer("flight", option, offers)
        details = option.get("details") or {}

        if offer is None:
            return self._unavailable_quote(
                kind="flight",
                option=option,
                plan=plan,
                traveler_count=traveler_count,
                conditions=fare_brand_conditions(details.get("fare_brand") or "Standard"),
                reason=(
                    "The browser could not find this exact flight on Google Flights."
                ),
            )

        total = float(offer.price)
        currency = offer.currency or default_currency()
        conditions = fare_brand_conditions(details.get("fare_brand") or "Standard")
        conditions["live_source"] = f"{self.name}: {offer.provider}"
        conditions["live_offer_id"] = offer.offer_id

        quote = make_quote(
            quote_id=new_id("qt"),
            kind="flight",
            option_id=option.get("option_id", ""),
            provider=offer.provider,
            currency=currency,
            components=[
                {
                    "label": (
                        f"Live fare — {offer.title}"
                        f"{' · ' + offer.subtitle if offer.subtitle else ''}"
                    ),
                    "amount": round(total, 2),
                }
            ],
            conditions=conditions,
            dates={
                "departure": str(plan.get("start_date") or ""),
                "return": str(plan.get("end_date") or ""),
                "depart_time": offer.details.get("departure") or "",
                "return_time": offer.details.get("arrival") or "",
            },
            traveler_count=traveler_count,
            price_source="browser_quote",
        )
        quote["browser"] = self._browser_state(offer, plan)
        return quote

    def quote_hotel(self, option: dict, plan: dict, traveler_count: int) -> dict:
        travelers = plan.get("travelers") or []
        offers = self._search("hotel", plan, travelers)
        offer = self._match_offer("hotel", option, offers)
        details = option.get("details") or {}
        room_type = details.get("room_type") or "Standard Double"
        nights = nights_between(plan.get("start_date"), plan.get("end_date")) or 0

        if offer is None:
            return self._unavailable_quote(
                kind="hotel",
                option=option,
                plan=plan,
                traveler_count=traveler_count,
                conditions={
                    "room_type": room_type,
                    "board": details.get("board") or "Room only",
                    "cancellation_policy": "Free cancellation until 48 hours before check-in",
                    "changes": "Date changes subject to availability and rate difference",
                    "refundable": bool(details.get("free_cancellation", True)),
                },
                reason=(
                    "The browser could not find this exact property on Booking.com."
                ),
            )

        nightly, total = self._stay_price(offer, nights)
        currency = offer.currency or default_currency()

        conditions = {
            "room_type": room_type,
            "board": details.get("board") or "Room only",
            "cancellation_policy": "Free cancellation until 48 hours before check-in",
            "changes": "Date changes subject to availability and rate difference",
            "refundable": bool(details.get("free_cancellation", True)),
            "occupancy": f"{max(int(traveler_count or 1), 1)} guest(s)",
            "live_source": f"{self.name}: {offer.provider}",
            "live_offer_id": offer.offer_id,
            "live_rate_per_night": round(nightly, 2),
        }
        if nights:
            components = [
                {
                    "label": f"{room_type} × {nights} night(s) — live rate",
                    "amount": total,
                }
            ]
        else:
            components = [
                {
                    "label": f"{room_type} — live rate per night (dates not set)",
                    "amount": round(nightly, 2),
                }
            ]

        quote = make_quote(
            quote_id=new_id("qt"),
            kind="hotel",
            option_id=option.get("option_id", ""),
            provider=offer.provider,
            currency=currency,
            components=components,
            conditions=conditions,
            dates={
                "check_in": str(plan.get("start_date") or ""),
                "check_out": str(plan.get("end_date") or ""),
            },
            traveler_count=traveler_count,
            nights=nights or None,
            room_type=room_type,
            price_source="browser_quote",
        )
        quote["browser"] = self._browser_state(offer, plan)
        return quote

    def _stay_price(self, offer: BrowserOffer, nights: int) -> tuple[float, float]:
        """Return (price per night, total for the stay).

        Booking sites show either a nightly rate or the total for the
        whole stay. Multiplying a total by the number of nights would
        inflate the price, so the offer's price basis decides.
        """
        basis = (offer.details or {}).get("price_basis")
        if basis == "total_for_stay" and nights:
            total = round(float(offer.price), 2)
            return round(total / nights, 2), total
        nightly = round(float(offer.price), 2)
        total = round(nightly * nights, 2) if nights else nightly
        return nightly, total

    def _unavailable_quote(
        self,
        kind: str,
        option: dict,
        plan: dict,
        traveler_count: int,
        conditions: dict,
        reason: str,
    ) -> dict:
        """A quote for an option the browser could not find.

        It is deliberately marked unavailable: the revalidation step turns
        that into a material change, so the user is asked again instead of
        the bot booking something else.
        """
        quote = make_quote(
            quote_id=new_id("qt"),
            kind=kind,
            option_id=option.get("option_id", ""),
            provider=self.name,
            currency=plan.get("currency") or default_currency(),
            components=[
                {"label": "No live price available for this option", "amount": 0.0}
            ],
            conditions=dict(conditions),
            dates={},
            traveler_count=traveler_count,
            availability=False,
            price_source="browser_unavailable",
        )
        quote["browser"] = {"unavailable_reason": reason}
        return quote

    def _browser_state(self, offer: BrowserOffer, plan: dict | None = None) -> dict:
        plan = plan or {}
        state = {
            "offer_id": offer.offer_id,
            "provider": offer.provider,
            "title": offer.title,
            "subtitle": offer.subtitle,
            "price": offer.price,
            "currency": offer.currency,
            "captured_at": time.time(),
            "offer": offer.to_dict(),
        }
        if plan:
            state["origin"] = str(plan.get("origin") or "")
            state["destination"] = str(plan.get("destination") or "")
            state["travelers"] = list(plan.get("travelers") or [])
        for key in ("search_url", "property_url", "airline_url", "departure", "arrival"):
            if key in (offer.details or {}):
                state[key] = offer.details[key]
        return state

    # ------------------------------------------------------------------
    # revalidation
    # ------------------------------------------------------------------

    def revalidate(self, quote: dict) -> dict:
        """Re-check the live price right before the purchase is made."""
        browser_state = quote.get("browser") or {}
        kind = quote.get("kind") or "flight"
        offer_id = browser_state.get("offer_id")

        plan = self._plan_from_quote(quote)
        travelers = plan.get("travelers") or []

        # Always hit the site: the whole point of revalidation is that the
        # price shown here is the one the purchase will use.
        offers = self._search(kind, plan, travelers, fresh=True)

        offer = None
        for candidate in offers:
            if candidate.offer_id == offer_id:
                offer = candidate
                break
        if offer is None and browser_state.get("title"):
            title = browser_state["title"]
            matches = [item for item in offers if _overlap(title, item.title) >= 0.6]
            offer = matches[0] if matches else None

        if offer is None:
            new_quote = dict(quote)
            new_quote["availability"] = False
            new_quote["price_source"] = "browser_unavailable"
            return {
                "checked_at": time.time(),
                "available": False,
                "quote": new_quote,
            }

        new_quote = self._reprice(quote, offer)
        return {
            "checked_at": time.time(),
            "available": True,
            "quote": new_quote,
        }

    def _reprice(self, quote: dict, offer: BrowserOffer) -> dict:
        """Rebuild the quote with the freshly observed live price."""
        new_quote = dict(quote)
        new_quote["provider"] = offer.provider
        new_quote["currency"] = offer.currency or quote.get("currency") or default_currency()

        if quote.get("kind") == "hotel":
            nights = int(quote.get("nights") or 0)
            _, total = self._stay_price(offer, nights)
            label = (quote.get("components") or [{}])[0].get(
                "label", "Live rate"
            )
        else:
            total = round(offer.price, 2)
            label = (quote.get("components") or [{}])[0].get("label", "Live fare")

        new_quote["components"] = [{"label": label, "amount": total}]
        new_quote["total"] = total
        count = max(int(quote.get("traveler_count") or 1), 1)
        new_quote["per_traveler"] = round(total / count, 2)
        new_quote["availability"] = True
        new_quote["quoted_at"] = time.time()
        new_quote["price_source"] = "browser_revalidated"

        state = dict(new_quote.get("browser") or {})
        state.update(self._browser_state(offer, self._plan_from_quote(quote)))
        new_quote["browser"] = state
        return new_quote

    def _plan_from_quote(self, quote: dict) -> dict:
        """Rebuild a minimal plan so the browser can repeat the search."""
        browser_state = quote.get("browser") or {}
        dates = quote.get("dates") or {}
        if quote.get("kind") == "hotel":
            start = dates.get("check_in") or ""
            end = dates.get("check_out") or ""
        else:
            start = dates.get("departure") or ""
            end = dates.get("return") or ""

        destination = browser_state.get("destination") or quote.get("destination") or ""
        origin = browser_state.get("origin") or quote.get("origin") or ""
        return {
            "origin": origin,
            "destination": destination,
            "start_date": start,
            "end_date": end,
            "currency": quote.get("currency") or default_currency(),
            "travelers": browser_state.get("travelers") or [],
        }

    # ------------------------------------------------------------------
    # booking
    # ------------------------------------------------------------------

    def create_booking(self, quote: dict, travelers: list, approval_id: str) -> dict:
        """Drive the browser to the checkout page and stop before payment."""
        if not approval_id:
            raise ProviderError("An approval reference is required to book.")

        kind = quote.get("kind") or "flight"
        browser_state = quote.get("browser") or {}
        offer_data = browser_state.get("offer") or {}
        if not offer_data:
            raise ProviderError(
                "This quote has no live browser offer attached, so it cannot "
                "be booked automatically.",
                transient=False,
            )

        offer = BrowserOffer(
            kind=kind,
            provider=browser_state.get("provider") or offer_data.get("provider") or self.name,
            title=offer_data.get("title") or "",
            subtitle=offer_data.get("subtitle") or "",
            price=float(offer_data.get("price") or 0),
            currency=offer_data.get("currency") or quote.get("currency") or default_currency(),
            details=dict(offer_data.get("details") or {}),
        )
        for key in ("search_url", "property_url"):
            if browser_state.get(key):
                offer.details[key] = browser_state[key]

        if kind == "hotel":
            # The room picker on the property page is priced for the whole
            # stay, so the checkout has to look for the same total.
            nights = int(quote.get("nights") or 0)
            _, total = self._stay_price(offer, nights)
            offer.details["price_total"] = total
            offer.details["stay_total"] = total

        adapter = resolve_adapter(kind)

        def _checkout(page):
            outcome = adapter.open_checkout(page, offer, travelers or [])
            outcome.raw["screenshot"] = self._session.screenshot(
                page, f"{kind}-{offer.offer_id}"
            )
            return outcome

        result = self._session.run(_checkout)

        if not result.success:
            raise ProviderError(
                result.message or "The browser could not reach the checkout page.",
                transient=True,
                payload=result.to_dict(),
            )

        return {
            "success": True,
            "reference": result.reference or new_id("bk"),
            "status": result.status or "awaiting_payment",
            "provider": self.name,
            "approval_id": approval_id,
            "created_at": time.time(),
            "raw": {
                "checkout": result.to_dict(),
                "payment_required": result.payment_required,
            },
        }

    def get_booking_status(self, reference: str) -> dict:
        """Browser bookings stay 'awaiting_payment' until a human pays."""
        if not reference:
            raise ProviderError("Missing booking reference.", transient=True)
        return {
            "reference": reference,
            "status": "awaiting_payment",
            "raw": {
                "message": (
                    "The browser filled in the traveller details and stopped at "
                    "the payment form. No payment was made by the bot."
                )
            },
        }

    # ------------------------------------------------------------------
    # cancellation
    # ------------------------------------------------------------------

    def cancellation_estimate(self, quote: dict) -> dict:
        total = float(quote.get("total") or 0)
        conditions = quote.get("conditions") or {}
        refundable = bool(conditions.get("refundable"))
        if refundable:
            fee = 0.0
        elif quote.get("kind") == "hotel":
            fee = round(total * 0.20, 2)
        else:
            fee = round(total * 0.10, 2)
        return {
            "amount_paid": round(total, 2),
            "cancellation_fee": fee,
            "refund": round(max(total - fee, 0.0), 2),
            "currency": quote.get("currency") or default_currency(),
        }

    def cancel_booking(self, quote: dict, reference: str) -> dict:
        """Cancellations are done on the provider's site, not by the bot."""
        raise ProviderError(
            "A browser-automated booking has to be cancelled on the provider's "
            "own site. Open the confirmation link from the receipt and cancel "
            "it there.",
            transient=False,
            payload={"reference": reference},
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass
