"""Booking.com adapter.

Booking.com serves its search results server side, so the results are
usually present as soon as the document is parsed. The adapter:

1. loads the search page for the destination, dates and guest count,
2. reads the property cards (name, rating, price per night),
3. opens the chosen property, selects a room and fills in the traveller
   details, and
4. stops as soon as the payment form is on screen.

Step 4 is a hard boundary: no card number, expiry or CVC is ever typed.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from .base import BrowserOffer, CheckoutResult, SiteAdapter

DEFAULT_BASE_URL = "https://www.booking.com"

CARD_SELECTORS = (
    "[data-testid='property-card']",
    "[data-tm-property-card]",
    "li[data-testid='property-card']",
    "div[data-testid='property-card']",
)
TITLE_SELECTORS = (
    "[data-testid='title']",
    "[data-tm-property-name]",
    "h3",
)
PRICE_SELECTORS = (
    "[data-testid='price-and-discounted-price']",
    "[data-testid='price']",
    "[data-tm-price]",
    ".bui-price-display__value",
)
REVIEW_SELECTORS = (
    "[data-testid='review-score']",
    "[data-tm-review-score]",
)
ROOM_SELECTORS = (
    "[data-testid='room-name']",
    "[data-tm-room-name]",
    ".h2",
)
# The room table lets the visitor pick how many rooms to book with a
# <select>. Until a room is chosen the "I'll reserve" button refuses to
# continue ("Select your accommodations first").
ROOM_COUNT_SELECTORS = (
    "select[data-testid='select-room-trigger']",
    "select.js-hprt-nos-select",
    "select.hprt-nos-select",
    "select[name^='nr_rooms']",
)
# A property page that links straight to the checkout has no room table.
SIMPLE_RESERVE_SELECTORS = (
    "a[data-testid='reserve']",
    "a[data-tm-reserve]",
)
BOOK_BUTTON_SELECTORS = (
    # The room table CTA on the live site. It has to come before the
    # generic "Reserve" button, which is the one in the site header.
    "button.js-reservation-button",
    "button:has-text(\"I'll reserve\")",
    "[data-testid='reserve']",
    "[data-tm-reserve]",
    "a[data-testid='reserve']",
    "a[data-tm-reserve]",
    "button:has-text('Reserve')",
    "a:has-text('Reserve')",
    "button:has-text('See availability')",
    "a:has-text('See availability')",
)
AVAILABILITY_TAB_SELECTORS = (
    "[data-testid='Property-Header-Nav-Tab-Trigger-availability']",
    "a:has-text('Info & prices')",
)
DIALOG_CONTINUE_SELECTORS = (
    "button:has-text('Continue')",
    "button:has-text('Next step')",
    "[data-testid='dialog-continue-button']",
)
GUEST_NAME_SELECTORS = (
    "input[name='firstname']",
    "input[name='firstName']",
    "[data-tm-firstname]",
)
GUEST_LASTNAME_SELECTORS = (
    "input[name='lastname']",
    "input[name='lastName']",
    "[data-tm-lastname]",
)
GUEST_EMAIL_SELECTORS = (
    "input[name='email']",
    "[data-tm-email]",
)
GUEST_COUNTRY_SELECTORS = (
    "select[name='cc1']",
    "select[name='country']",
    "[data-tm-country]",
)
GUEST_PHONE_SELECTORS = (
    "input[name='phoneNumber']",
    "input[name='telephone']",
    "input[name='phone']",
    "[data-tm-phone]",
)
# Buttons that move the checkout forward to the payment step. The final
# "Complete booking" button is deliberately not in this list: clicking it
# would place the booking.
ADVANCE_SELECTORS = (
    "button:has-text('Next: Final details')",
    "button:has-text('Continue to payment')",
    "button:has-text('Next step')",
    "[data-tm-confirm]",
)


class BookingComAdapter(SiteAdapter):
    name = "Booking.com"
    kind = "hotel"

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url or os.getenv("BOOKING_COM_BASE_URL") or DEFAULT_BASE_URL)

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def build_url(self, plan: dict) -> str:
        destination = (plan.get("destination") or "").strip()
        check_in = str(plan.get("start_date") or "").strip()[:10]
        check_out = str(plan.get("end_date") or "").strip()[:10]
        adults = max(int(plan.get("adults") or len(plan.get("travelers") or [1]) or 1), 1)
        rooms = max(int(plan.get("rooms") or 1), 1)

        currency = str(plan.get("currency") or "USD").strip().upper()

        query = {
            "ss": destination,
            "checkin": check_in,
            "checkout": check_out,
            "group_adults": adults,
            "no_rooms": rooms,
            "group_children": 0,
            # Without this Booking.com localises the price to the server's
            # region, which would not match the currency of the plan.
            "selected_currency": currency,
        }
        parts = "&".join(
            f"{key}={quote_plus(str(value))}"
            for key, value in query.items()
            if value not in ("", None)
        )
        return f"{self.base_url}/searchresults.html?{parts}"

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
        lines = self.card_lines(card)
        prices = self.parse_prices(lines)

        # The price element holds the *current* price, which is not the same
        # as the first price in the card text when the property is reduced.
        element_text = self._card_text(card, PRICE_SELECTORS)
        element_value = self.money_to_float(element_text) if element_text else 0.0

        nights = prices.get("nights")
        per_night = prices.get("per_night")
        total = prices.get("total")

        # The card states the number of nights, so a bare price next to it
        # is the total for the stay. Without it, the price is a nightly
        # rate and the quote multiplies it by the number of nights.
        basis = "total_for_stay" if (nights or total is not None) else "per_night"

        if basis == "total_for_stay":
            price = total if total is not None else element_value
            if not price and per_night is not None and nights:
                price = round(per_night * nights, 2)
            total = price
            if nights and price:
                per_night = round(price / nights, 2)
        else:
            price = per_night if per_night is not None else element_value
            if not price and total is not None:
                price = total
            per_night = price

        if price <= 0:
            return None

        price_text = prices.get("price_text") or element_text

        title = self._card_text(card, TITLE_SELECTORS) or "Hotel"
        review = self._card_text(card, REVIEW_SELECTORS)
        subtitle = review

        link = ""
        try:
            anchor = card.locator("a[href*='/hotel/']").first
            if anchor.count():
                link = anchor.get_attribute("href") or ""
        except Exception:
            link = ""

        return BrowserOffer(
            kind="hotel",
            provider=self.name,
            title=title,
            subtitle=subtitle,
            price=price,
            currency=self.guess_currency(price_text or element_text, "USD"),
            details={
                "review_score": review,
                "property_url": self._absolute(link, search_url),
                "search_url": search_url,
                "price_basis": basis,
                "price_per_night": per_night,
                "price_total": total,
                "nights": nights,
                "price_text": price_text or element_text,
            },
        )

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

    def _absolute(self, href: str, base: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        if not base:
            return href
        root = base.split("/searchresults")[0]
        return f"{root}{href if href.startswith('/') else '/' + href}"

    def _no_results(self, url: str):
        from ...providers import ProviderError

        return ProviderError(
            "Booking.com did not return any readable results. "
            "The page layout may have changed, or the site asked for a "
            "human verification step.",
            transient=True,
            payload={"url": url},
        )

    # ------------------------------------------------------------------
    # checkout
    # ------------------------------------------------------------------

    def open_checkout(self, page, offer: BrowserOffer, travelers: list) -> CheckoutResult:
        steps: list[str] = []
        property_url = (offer.details or {}).get("property_url") or (offer.details or {}).get(
            "search_url", ""
        )
        page.goto(property_url, wait_until="domcontentloaded")
        self.check_blocked(page)
        steps.append("Opened the selected property")

        # The room table lives behind the "Info & prices" tab and is
        # rendered lazily, so the tab is opened and the page is scrolled
        # until the room picker shows up.
        self._open_availability_tab(page)
        room_select = self._wait_for_room_select(page)
        if room_select is not None:
            if self._select_room(room_select, offer):
                steps.append("Selected the booked room")
            else:
                steps.append("Could not pick a room automatically")

        button = self._wait_for_reserve_button(page)
        if button is None:
            raise self._checkout_error(
                "Could not find a 'Reserve' button on the property page.", page
            )
        button.scroll_into_view_if_needed(timeout=5000)
        button.click(timeout=15000)
        steps.append("Started the reservation")
        try:
            page.wait_for_timeout(3000)
        except Exception:
            pass

        # Some room tables open a small dialog before the checkout page.
        self._dismiss_room_dialog(page)
        self._wait_for_checkout(page)

        # Fill the traveller details, then stop at payment.
        filled = self._fill_guest_details(page, travelers)
        missing = self._missing_guest_fields(page)
        if filled:
            steps.append("Filled in the traveller details")
        else:
            steps.append("Traveller form was not reachable — fill it in manually")

        if missing:
            steps.append(
                "Still missing: " + ", ".join(missing) + " — finish the booking yourself"
            )
        elif self._advance_to_payment(page):
            steps.append("Moved on to the payment step")

        if self.check_payment_step(page):
            steps.append("Stopped at the payment form (no payment details were typed)")

        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass

        reference = self._reference(page)
        return CheckoutResult(
            success=True,
            reference=reference,
            status="awaiting_payment",
            url=page.url,
            message=(
                "The reservation is ready for payment. The bot stopped before "
                "the payment form — finish it in a normal browser."
            ),
            steps=steps,
            payment_required=True,
            raw={
                "final_url": page.url,
                "property_url": property_url,
                "guest_filled": filled,
                "missing_guest_fields": missing,
                "travelers": travelers,
            },
        )

    def _wait_for_room_select(self, page, timeout_ms: int = 20000):
        """Return the room picker, or None when the page has none.

        Booking.com renders the room table only once the availability
        section is on screen, so the page is scrolled while we wait.
        """
        deadline = timeout_ms / 1000.0
        waited = 0.0
        while waited < deadline:
            for selector in ROOM_COUNT_SELECTORS:
                try:
                    locator = page.locator(selector)
                    if locator.count():
                        return locator
                except Exception:
                    continue
            # A page that links straight to the checkout has no room table.
            try:
                if page.locator(SIMPLE_RESERVE_SELECTORS[0]).count():
                    return None
            except Exception:
                pass
            try:
                page.mouse.wheel(0, 1200)
            except Exception:
                pass
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            waited += 1.5
        return None

    def _select_room(self, room_select, offer: BrowserOffer) -> bool:
        """Pick the room that matches the quoted price."""
        details = offer.details or {}
        target = float(details.get("price_total") or offer.price or 0)

        fallback = None
        try:
            count = min(room_select.count(), 8)
        except Exception:
            return False

        for index in range(count):
            select = room_select.nth(index)
            try:
                options = select.locator("option")
                option_count = options.count()
            except Exception:
                continue
            for position in range(option_count):
                option = options.nth(position)
                try:
                    value = (option.get_attribute("value") or "").strip()
                    label = option.text_content() or ""
                except Exception:
                    continue
                if value in ("", "0"):
                    continue
                amount = self.money_to_float(label)
                if target and amount and abs(amount - target) < 0.5:
                    return self._apply_room(select, value)
                if fallback is None:
                    fallback = (select, value)

        if fallback is not None:
            select, value = fallback
            return self._apply_room(select, value)
        return False

    def _apply_room(self, select, value: str) -> bool:
        try:
            select.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            select.select_option(value)
        except Exception:
            return False
        try:
            page_wait = select.page
            page_wait.wait_for_timeout(2000)
        except Exception:
            pass
        return True

    def _wait_for_checkout(self, page, timeout_ms: int = 20000) -> bool:
        """Wait until the guest form is on the page."""
        deadline = timeout_ms / 1000.0
        waited = 0.0
        while waited < deadline:
            for selector in GUEST_NAME_SELECTORS:
                try:
                    if page.locator(selector).count():
                        return True
                except Exception:
                    continue
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            waited += 1.0
        return False

    def _fill_guest_details(self, page, travelers: list) -> bool:
        """Type the traveller names and e-mail into the guest form."""
        if not travelers:
            return False
        first = travelers[0] or {}
        full_name = str(first.get("name") or "").strip()
        parts = full_name.split()
        first_name = parts[0] if parts else ""
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

        filled = False
        for selector in GUEST_NAME_SELECTORS:
            if self.fill(page, selector, first_name):
                filled = True
                break
        for selector in GUEST_LASTNAME_SELECTORS:
            if self.fill(page, selector, last_name):
                filled = True
                break

        email = str(first.get("email") or os.getenv("BOOKING_GUEST_EMAIL") or "").strip()
        if email:
            for selector in GUEST_EMAIL_SELECTORS:
                if self.fill(page, selector, email):
                    filled = True
                    break

        country = str(first.get("country") or os.getenv("BOOKING_GUEST_COUNTRY") or "").strip()
        if country and self._select_country(page, country):
            filled = True

        phone = str(first.get("phone") or os.getenv("BOOKING_GUEST_PHONE") or "").strip()
        if phone:
            for selector in GUEST_PHONE_SELECTORS:
                if self.fill(page, selector, phone):
                    filled = True
                    break

        return filled

    def _select_country(self, page, country: str) -> bool:
        """Pick the country/region the guest is in."""
        for selector in GUEST_COUNTRY_SELECTORS:
            try:
                select = page.locator(selector).first
                if not select.count():
                    continue
                try:
                    select.select_option(country.strip().lower())
                    return True
                except Exception:
                    pass
                options = select.locator("option")
                for index in range(min(options.count(), 400)):
                    option = options.nth(index)
                    label = (option.text_content() or "").lower()
                    if country.strip().lower() in label:
                        value = option.get_attribute("value")
                        if value:
                            select.select_option(value)
                            return True
                return False
            except Exception:
                continue
        return False

    def _missing_guest_fields(self, page) -> list[str]:
        """Required guest fields that are still empty."""
        missing: list[str] = []
        try:
            fields = page.locator("input[required], select[required], textarea[required]")
            for index in range(min(fields.count(), 20)):
                field = fields.nth(index)
                try:
                    if not field.is_visible():
                        continue
                    if field.evaluate("node => node.value") not in ("", None):
                        continue
                except Exception:
                    continue
                name = ""
                try:
                    name = (
                        field.get_attribute("name")
                        or field.get_attribute("data-tm-field")
                        or field.get_attribute("id")
                        or "field"
                    )
                except Exception:
                    name = "field"
                if name not in missing:
                    missing.append(name)
        except Exception:
            return missing
        return missing

    def _advance_to_payment(self, page) -> bool:
        """Move to the payment step without ever submitting the booking."""
        if self.check_payment_step(page):
            return False
        for selector in ADVANCE_SELECTORS:
            try:
                button = page.locator(selector).first
                if not button.count() or not button.is_visible():
                    continue
                text = (button.inner_text() or "").strip().lower()
                if any(
                    word in text
                    for word in ("complete booking", "pay now", "confirm booking")
                ):
                    # The final submit button: never click it.
                    continue
                button.click(timeout=10000)
                try:
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    def _click_first(self, page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click()
                    return True
            except Exception:
                continue
        return False

    def _open_availability_tab(self, page) -> None:
        """Open the "Info & prices" tab, which holds the room table."""
        for selector in AVAILABILITY_TAB_SELECTORS:
            try:
                tab = page.locator(selector).first
                if tab.count() and tab.is_visible():
                    tab.click()
                    try:
                        page.wait_for_timeout(2500)
                    except Exception:
                        pass
                    return
            except Exception:
                continue

    def _wait_for_reserve_button(self, page, timeout_ms: int = 25000):
        """Find the reserve button, scrolling the page while we wait.

        Booking.com renders the room table only once the availability
        section is on screen, so the button can appear a few seconds
        after the page has loaded.
        """
        deadline = timeout_ms / 1000.0
        waited = 0.0
        while waited < deadline:
            for selector in BOOK_BUTTON_SELECTORS:
                try:
                    candidates = page.locator(selector)
                    for index in range(min(candidates.count(), 3)):
                        button = candidates.nth(index)
                        if not button.is_visible():
                            continue
                        # A room table CTA that still asks for a room was
                        # not enabled by the selection; clicking it would
                        # only pop up a tooltip.
                        title = (button.get_attribute("data-title") or "").lower()
                        if "select your accommodations" in title or "select a room" in title:
                            continue
                        return button
                except Exception:
                    continue
            try:
                page.mouse.wheel(0, 1200)
            except Exception:
                pass
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            waited += 1.5
        return None

    def _dismiss_room_dialog(self, page) -> None:
        """Close the room-occupancy dialog some room tables open."""
        for selector in DIALOG_CONTINUE_SELECTORS:
            try:
                dialog = page.locator("[role='dialog']")
                if not dialog.count():
                    return
                button = dialog.first.locator(selector).first
                if button.count() and button.is_visible():
                    button.click()
                    try:
                        page.wait_for_timeout(2500)
                    except Exception:
                        pass
                    return
            except Exception:
                continue

    def _checkout_error(self, message: str, page):
        from ...providers import ProviderError

        return ProviderError(
            f"{message} Current URL: {page.url}",
            transient=True,
            payload={"url": page.url},
        )

    def _reference(self, page) -> str:
        """Derive a reference from the booking URL when one is present."""
        match = re.search(r"(?:bkid|reservation)=([A-Za-z0-9_-]+)", page.url or "")
        if match:
            return f"BC-{match.group(1)}"
        try:
            from ...models import new_id

            return new_id("bc")
        except Exception:  # pragma: no cover
            return "bc-pending"
