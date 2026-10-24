"""Google Flights adapter.

Google Flights renders its result list client side, so the adapter waits for
the flight cards to appear before reading them. Every selector has more than
one fallback because Google rotates its class names regularly; the
``data-tm-*`` hooks are the stable ones used by the local test site.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from .base import BrowserOffer, CheckoutResult, SiteAdapter

DEFAULT_BASE_URL = "https://www.google.com/travel"

# Selector families, tried in order. The first match wins. The ``data-tm-*``
# hooks belong to the local test site.
CARD_SELECTORS = (
    "[data-tm-flight-card]",
    "li.pIav2d",
    "div[data-testid='flight-card']",
)
SELECT_BUTTON_SELECTORS = (
    "[data-tm-select]",
    "button[aria-label='Select flight']",
)
CONTINUE_BUTTON_SELECTORS = (
    "[data-tm-continue]",
    "button:has-text('Continue')",
    "button:has-text('Done')",
)


class GoogleFlightsAdapter(SiteAdapter):
    name = "Google Flights"
    kind = "flight"

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url or os.getenv("GOOGLE_FLIGHTS_BASE_URL") or DEFAULT_BASE_URL)

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def build_url(self, plan: dict) -> str:
        """Google Flights understands a natural-language 'q' query."""
        origin = (plan.get("origin_iata") or plan.get("origin") or "").strip()
        destination = (
            plan.get("destination_iata") or plan.get("destination") or ""
        ).strip()
        start = str(plan.get("start_date") or "").strip()[:10]
        end = str(plan.get("end_date") or "").strip()[:10]

        parts = []
        if destination:
            parts.append(f"Flights to {destination}")
        if origin:
            parts.append(f"from {origin}")
        if start:
            if end:
                parts.append(f"on {start} through {end}")
            else:
                parts.append(f"on {start}")
        query = " ".join(parts) or "flights"

        currency = (plan.get("currency") or "USD").upper()
        return (
            f"{self.base_url}/flights?q={quote_plus(query)}"
            f"&curr={quote_plus(currency)}&hl=en"
        )

    def search(self, page, plan: dict, travelers: list, limit: int = 5) -> list[BrowserOffer]:
        url = self.build_url(plan)
        page.goto(url, wait_until="domcontentloaded")
        self.check_blocked(page)

        cards = self._wait_for_cards(page)
        if not cards:
            raise self._no_results(url)

        offers: list[BrowserOffer] = []
        for card in cards[:limit]:
            offer = self._read_card(card, url)
            if offer and offer.price > 0:
                offers.append(offer)
        if not offers:
            raise self._no_results(url)
        return offers

    def _wait_for_cards(self, page) -> list:
        for selector in CARD_SELECTORS:
            try:
                locator = page.locator(selector)
                locator.first.wait_for(state="visible", timeout=15000)
                return locator.all()
            except Exception:
                continue
        # Nothing matched: give the page a moment in case it is still loading.
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass
        for selector in CARD_SELECTORS:
            try:
                locator = page.locator(selector)
                if locator.count():
                    return locator.all()
            except Exception:
                continue
        return []

    def _read_card(self, card, search_url: str = "") -> BrowserOffer | None:
        """Read one flight card.

        Google Flights rotates its class names, but it keeps a set of
        meaningful ``aria-label`` attributes, so those are the primary
        source and the visible text is the fallback.
        """
        labels = self.aria_labels(card)
        lines = self.card_lines(card)
        parsed = self.parse_card_text(lines)

        price_text = parsed["price_text"]
        if not price_text:
            aria_price = self.aria_match(
                labels, r"([\d][\d.,]*)\s*(?:us dollars|dollars|euros|pounds|yuan|yen)"
            )
            price_text = aria_price
        price = self.money_to_float(price_text)
        if price <= 0:
            return None

        departure = self._aria_value(labels, r"^Departure time:\s*(.+)$")
        arrival = self._aria_value(labels, r"^Arrival time:\s*(.+)$")
        if not departure and parsed["times"]:
            departure = parsed["times"][0]
        if not arrival and len(parsed["times"]) > 1:
            arrival = parsed["times"][1]

        duration = self._aria_value(labels, r"^Total duration\s*(.+)$") or parsed["duration"]
        stops = self._aria_value(labels, r"^(\d+\s+stop.*|nonstop.*)$") or parsed["stops"]

        airline = parsed["airline"]
        title = airline or "Flight"
        subtitle = " → ".join(part for part in (departure, arrival) if part)
        if not subtitle:
            subtitle = " · ".join(part for part in (duration, stops) if part)

        return BrowserOffer(
            kind="flight",
            provider=self.name,
            title=title,
            subtitle=subtitle,
            price=price,
            currency=self.guess_currency(price_text, "USD"),
            details={
                "airline": airline,
                "departure": departure,
                "arrival": arrival,
                "duration": duration,
                "stops": stops,
                "search_url": search_url,
                "raw_label": self._aria_label(card),
            },
        )

    def _aria_value(self, labels: list[str], pattern: str) -> str:
        """Extract the captured group of the first matching aria-label."""
        import re

        matcher = re.compile(pattern, re.IGNORECASE)
        for label in labels:
            match = matcher.search(label)
            if match:
                value = (match.group(1) or "").strip()
                return re.sub(r"\s+flight\.?$", "", value, flags=re.IGNORECASE)
        return ""

    def _card_text(self, card, selectors: list[str]) -> str:
        for selector in selectors:
            try:
                locator = card.locator(selector)
                if locator.count():
                    value = (locator.first.inner_text() or "").strip()
                    if value:
                        return value
            except Exception:
                continue
        return ""

    def _aria_label(self, card) -> str:
        try:
            return (card.get_attribute("aria-label") or "").strip()
        except Exception:
            return ""

    def _no_results(self, url: str):
        from ...providers import ProviderError

        return ProviderError(
            "Google Flights did not return any readable results. "
            "The page layout may have changed, or the site asked for a "
            "human verification step.",
            transient=True,
            payload={"url": url},
        )

    # ------------------------------------------------------------------
    # checkout
    # ------------------------------------------------------------------

    def open_checkout(self, page, offer: BrowserOffer, travelers: list) -> CheckoutResult:
        """Open the chosen flight and walk up to the airline's booking page.

        Google Flights is a metasearch engine: the actual payment always
        happens on the airline's own site. The bot therefore selects the
        approved flight (and, for a round trip, the return flight), stops
        on the booking page and hands the user the link to finish with.
        """
        steps: list[str] = []
        search_url = (offer.details or {}).get("search_url") or self.build_url({})
        page.goto(search_url, wait_until="domcontentloaded")
        self.check_blocked(page)
        steps.append("Opened the flight search results")

        card = self._find_offer_card(page, offer)
        if card is not None and self._click_select(page, card):
            steps.append("Selected the approved flight")
        else:
            steps.append("Could not find the approved flight in the results")

        # A round trip asks for the return flight after the outbound one.
        return_card = self._wait_for_return_card(page, offer)
        if return_card is not None and self._click_select(page, return_card):
            steps.append("Selected the return flight")

        self._wait_for_booking_page(page)
        itinerary = self._itinerary(page)

        # The booking options sheet offers a link out to the airline.
        airline_url = ""
        for selector in (
            "[data-tm-airline-url]",
            "a[href*='/flights/booking']",
            "a[href*='/travel/flights/booking']",
            "a[href*='book']:has-text('Book')",
            "a:has-text('Book with')",
        ):
            try:
                link = page.locator(selector).first
                if link.count():
                    airline_url = link.get_attribute("href") or ""
                    if airline_url:
                        break
            except Exception:
                continue
        if airline_url:
            steps.append("Located the airline booking link")
            if airline_url.startswith("/"):
                airline_url = f"{self.base_url}{airline_url}"

        payment_visible = self.check_payment_step(page)
        reference = self._reference(page)

        return CheckoutResult(
            success=True,
            reference=reference,
            status="awaiting_payment",
            url=airline_url or page.url,
            message=(
                "Google Flights is a search engine: finish the purchase on the "
                "airline site. The bot stopped before any payment step."
            ),
            steps=steps,
            payment_required=True,
            raw={
                "airline_url": airline_url,
                "final_url": page.url,
                "search_url": search_url,
                "itinerary": itinerary,
                "payment_visible": payment_visible,
                "travelers": travelers,
            },
        )

    # ------------------------------------------------------------------
    # checkout helpers
    # ------------------------------------------------------------------

    def _card_label(self, card) -> str:
        """The aria-label that describes a flight card."""
        try:
            nodes = card.locator("[aria-label]")
            for index in range(min(nodes.count(), 6)):
                label = (nodes.nth(index).get_attribute("aria-label") or "").strip()
                if label and "select flight" in label.lower():
                    return label
        except Exception:
            pass
        try:
            return (card.get_attribute("aria-label") or "").strip()
        except Exception:
            return ""

    def _find_offer_card(self, page, offer: BrowserOffer):
        """Find the card of the approved flight."""
        details = offer.details or {}
        target_label = str(details.get("raw_label") or "")
        airline = str(details.get("airline") or offer.title or "").strip().lower()
        price = float(offer.price or 0)

        cards = None
        for selector in CARD_SELECTORS:
            try:
                locator = page.locator(selector)
                if locator.count():
                    cards = locator
                    break
            except Exception:
                continue
        if cards is None:
            return None
        try:
            count = min(cards.count(), 40)
        except Exception:
            return None

        fallback = None
        for index in range(count):
            card = cards.nth(index)
            label = self._card_label(card)
            if not label:
                continue
            if target_label and label == target_label:
                return card
            if airline and airline in label.lower():
                if not price or f"{price:.0f} US dollars" in label:
                    return card
                fallback = fallback or card
        return fallback

    def _click_select(self, page, card) -> bool:
        """Click the "Select flight" control of a card.

        Google Flights keeps that button hidden until the card is
        expanded, so the click is dispatched in the page when the button
        is not visible.
        """
        for selector in SELECT_BUTTON_SELECTORS:
            try:
                button = card.locator(selector).first
                if not button.count():
                    continue
                if button.is_visible():
                    button.click(timeout=10000)
                else:
                    button.evaluate("node => node.click()")
                try:
                    page.wait_for_timeout(4000)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    def _wait_for_return_card(self, page, offer: BrowserOffer, timeout_ms: int = 20000):
        """Wait for the return-flight list of a round trip."""
        deadline = timeout_ms / 1000.0
        waited = 0.0
        while waited < deadline:
            try:
                if "/flights/search" in page.url:
                    card = self._find_offer_card(page, offer)
                    if card is not None:
                        return card
                    cards = page.locator(CARD_SELECTORS[1])
                    if cards.count():
                        return cards.first
                elif (
                    page.locator("button[aria-label='Select flight']").count() == 0
                    and page.locator("[data-tm-select]").count() == 0
                ):
                    # There is no flight list on this page any more, so
                    # there is no return flight to pick.
                    return None
            except Exception:
                pass
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            waited += 1.0
        return None

    def _wait_for_booking_page(self, page, timeout_ms: int = 20000) -> bool:
        """Wait for the booking page that lists the airline options."""
        deadline = timeout_ms / 1000.0
        waited = 0.0
        while waited < deadline:
            try:
                if "/flights/booking" in page.url:
                    return True
            except Exception:
                pass
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            waited += 1.0
        return False

    def _itinerary(self, page) -> str:
        """The selected itinerary, as shown on the booking page."""
        try:
            summary = page.locator("[aria-label='Itinerary summary'], [role='main']")
            if summary.count():
                text = (summary.first.inner_text() or "").strip()
                if text:
                    return " ".join(text.split())[:600]
        except Exception:
            pass
        try:
            body = page.inner_text("body") or ""
        except Exception:
            return ""
        marker = body.find("Selected flights")
        if marker < 0:
            return ""
        return " ".join(body[marker : marker + 600].split())

    def _reference(self, page) -> str:
        """Build a stable reference from the current URL."""
        try:
            from ...models import new_id

            return new_id("gf")
        except Exception:  # pragma: no cover
            return "gf-pending"
