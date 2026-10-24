"""Base types shared by every booking-site adapter.

An adapter knows two things about one site:

* how to turn a trip plan into a list of live offers, and
* how to walk from a chosen offer to the checkout page, filling in the
  traveller details and stopping right before payment.

It never types payment details. ``CheckoutResult`` makes that explicit so
the caller can report honestly what happened.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# --- text parsing helpers -------------------------------------------------
# Booking sites render their result cards in wildly different markup, but
# the *text* of a card is much more consistent than its class names. These
# patterns pull a price, the times and the airline out of a card's text.

TIME_RE = re.compile(r"^\d{1,2}:\d{2}(\s?[AP]M)?(\s*\+\d)?$", re.IGNORECASE)

# A line that is only a price: "$788", "US$412", "1,234".
CURRENCY_LINE_RE = re.compile(
    r"^(?:[\$€£¥₹₩₽₪₺]|(?:(?:US|CA|AU|NZ|SG|HK)?\$)|USD|EUR|GBP|JPY|CNY|RMB|"
    r"KRW|INR|THB|SGD|HKD|AED|TRY|BRL|CHF|Rs\.?)"
    r"\s?\d[\d.,\s]*(?:per night|/ ?night|total|round trip|one way|for \d+ nights)?$",
    re.IGNORECASE,
)
NUMERIC_LINE_RE = re.compile(r"^[\d.,\s]+$")

# Lines that are never the airline name.
NOISE_LINE_RE = re.compile(
    r"stop|hr\b|min\b|co2|emission|tree|round trip|one way|nonstop|layover|"
    r"transfer|kg\b|taxi|self transfer|\u2013|\u2014|overnight|direct flight|"
    r"best flight|no flights|only \d|available|select|choose|view details|"
    r"including taxes|per person|per traveller|\bday\b|flight duration|"
    r"^on\s+\w+day,|^\w+day,\s+\w+\s+\d+$",
    re.IGNORECASE,
)


@dataclass
class BrowserOffer:
    """One live result scraped from a booking site."""

    kind: str                       # "flight" | "hotel"
    provider: str
    title: str
    subtitle: str = ""
    price: float = 0.0
    currency: str = "USD"
    details: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def offer_id(self) -> str:
        """Stable id so a quote can be matched back to this offer later."""
        seed = "|".join(
            [
                self.kind,
                self.provider,
                self.title,
                self.subtitle,
                f"{self.price:.2f}",
                str(self.details.get("departure", "")),
                str(self.details.get("arrival", "")),
            ]
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict:
        return {
            "offer_id": self.offer_id,
            "kind": self.kind,
            "provider": self.provider,
            "title": self.title,
            "subtitle": self.subtitle,
            "price": self.price,
            "currency": self.currency,
            "details": dict(self.details),
        }


@dataclass
class CheckoutResult:
    """What happened while walking an offer to the checkout page."""

    success: bool
    reference: str = ""
    status: str = "unknown"
    url: str = ""
    message: str = ""
    steps: list = field(default_factory=list)
    payment_required: bool = False
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "reference": self.reference,
            "status": self.status,
            "url": self.url,
            "message": self.message,
            "steps": list(self.steps),
            "payment_required": self.payment_required,
            "raw": dict(self.raw),
        }


class SiteAdapter:
    """Interface every booking-site adapter implements."""

    #: Human readable site name, e.g. "Google Flights".
    name = ""
    #: What this adapter books: "flight" or "hotel".
    kind = ""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or "").rstrip("/")

    # ------------------------------------------------------------------

    def search(self, page, plan: dict, travelers: list, limit: int = 5) -> list[BrowserOffer]:
        """Return up to ``limit`` live offers for ``plan``."""
        raise NotImplementedError

    def open_checkout(self, page, offer: BrowserOffer, travelers: list) -> CheckoutResult:
        """Walk the offer to the checkout page, stopping before payment."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # helpers shared by the concrete adapters
    # ------------------------------------------------------------------

    @staticmethod
    def check_blocked(page) -> None:
        """Stop the run when the site asks for a human verification."""
        from ..session import BrowserSession

        BrowserSession.check_blocked(page)

    @staticmethod
    def check_payment_step(page) -> bool:
        """True once the page shows a payment form."""
        from ..session import BrowserSession

        return BrowserSession.check_payment_step(page)

    @staticmethod
    def fill(page, selector: str, value: str, timeout: int = 8000) -> bool:
        """Type ``value`` into the first visible matching field.

        Checkout pages often render the same input twice (once for the
        desktop layout, once for the mobile one) and only one of them is
        visible, so every match is tried until one accepts the text.
        """
        if not value:
            return False
        try:
            fields = page.locator(selector)
            total = fields.count()
        except Exception:
            return False
        for index in range(min(total, 4)):
            try:
                field = fields.nth(index)
                field.wait_for(state="visible", timeout=timeout)
                field.fill("")
                field.fill(value)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def first_visible(page, selectors: list[str]) -> str:
        """Return the first selector that is visible on the page."""
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return selector
            except Exception:
                continue
        return ""

    @staticmethod
    def text(page, selector: str, default: str = "") -> str:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                return default
            value = (locator.inner_text() or "").strip()
            return value or default
        except Exception:
            return default

    @staticmethod
    def money_to_float(text: str) -> float:
        """Parse 'US$1,234.50' / '₹ 99,131' / '1 234' into a float.

        Booking sites mix currency symbols, ISO codes, non-breaking spaces
        and either separator convention, so the number is pulled out of the
        text rather than the whole string being cleaned up.
        """
        if not text:
            return 0.0

        raw = re.sub(r"\s", "", str(text))
        # Keep only the first price in the text: a discounted card can read
        # "US$837US$510" (original and current price side by side).
        match = re.search(
            r"(?:US\$|CA\$|AU\$|NZ\$|SG\$|HK\$|R\$|[$€£¥₹₩₽₪₺₦₱]"
            r"|USD|EUR|GBP|JPY|CNY|RMB|KRW|INR|THB|SGD|HKD|AED|TRY|BRL|MXN|"
            r"CAD|AUD|CHF|PLN|CZK|SEK|NOK|DKK|ZAR)\s?(\d[\d.,']*)",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            number = match.group(1)
        else:
            # No currency marker: take the first number in the text.
            match = re.search(r"\d[\d.,']*", raw)
            number = match.group(0) if match else ""

        number = number.replace("'", "")
        if "," in number and "." in number:
            # The last separator is the decimal one.
            if number.rfind(",") > number.rfind("."):
                number = number.replace(".", "").replace(",", ".")
            else:
                number = number.replace(",", "")
        elif "," in number:
            # "1,044" is a thousands separator, "12,50" a decimal comma.
            if re.search(r",\d{2}$", number) and not re.search(r",\d{3}", number):
                number = number.replace(",", ".")
            else:
                number = number.replace(",", "")
        elif "." in number and len(number.split(".")[-1]) == 3:
            # "1.044" is a thousands separator in most European locales.
            if number.count(".") == 1 and len(number.split(".")[0]) <= 3:
                number = number.replace(".", "")

        try:
            return float(number) if number else 0.0
        except ValueError:
            return 0.0

    @staticmethod
    def guess_currency(text: str, default: str = "USD") -> str:
        """Best guess at the currency of a price string."""
        raw = str(text or "")
        upper = raw.upper()
        for code in ("EUR", "GBP", "JPY", "CNY", "RMB", "KRW", "INR", "THB",
                     "SGD", "HKD", "AED", "TRY", "BRL", "MXN", "ZAR", "CHF"):
            if code in upper:
                return code
        if "€" in raw:
            return "EUR"
        if "£" in raw:
            return "GBP"
        if "¥" in raw:
            return "JPY"
        if "₹" in raw:
            return "INR"
        if "₩" in raw:
            return "KRW"
        if "₽" in raw:
            return "RUB"
        if "R$" in raw:
            return "BRL"
        if "$" in raw:
            return "USD"
        return default

    # ------------------------------------------------------------------
    # card text parsing
    # ------------------------------------------------------------------

    @staticmethod
    def card_lines(card) -> list[str]:
        """The visible text of a card, one entry per line."""
        try:
            text = card.inner_text() or ""
        except Exception:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    @classmethod
    def parse_card_text(cls, lines: list[str]) -> dict:
        """Pull the interesting values out of a result card's text."""
        result = {
            "price_text": "",
            "times": [],
            "airline": "",
            "stops": "",
            "duration": "",
        }
        if not lines:
            return result

        # Price: a line that is only a price, currency first.
        for line in lines:
            if CURRENCY_LINE_RE.match(line):
                result["price_text"] = line
                break
        if not result["price_text"]:
            for line in lines:
                if NUMERIC_LINE_RE.match(line) and (
                    "," in line or "." in line
                ):
                    result["price_text"] = line
                    break

        # Times.
        for line in lines:
            if TIME_RE.match(line):
                result["times"].append(line)
            if len(result["times"]) == 2:
                break

        # Stops and duration.
        for line in lines:
            if not result["stops"] and re.search(r"\bstop", line, re.IGNORECASE):
                result["stops"] = line
            if not result["duration"] and re.search(
                r"\d+\s*(?:hr|h|hour)s?", line, re.IGNORECASE
            ):
                result["duration"] = line

        # Airline: the first line that is not a time, a price or noise.
        skip = set(result["times"])
        if result["price_text"]:
            skip.add(result["price_text"])
        for line in lines:
            if line in skip:
                continue
            if NOISE_LINE_RE.search(line):
                continue
            if len(line) < 3 or not any(char.isalpha() for char in line):
                continue
            result["airline"] = line
            break

        return result

    @staticmethod
    def aria_labels(card) -> list[str]:
        """Every aria-label inside a card."""
        try:
            return [
                (value or "").strip()
                for value in card.locator("[aria-label]").evaluate_all(
                    "nodes => nodes.map(n => n.getAttribute('aria-label'))"
                )
                if (value or "").strip()
            ]
        except Exception:
            return []

    @staticmethod
    def aria_match(labels: list[str], pattern: str) -> str:
        """First aria-label matching ``pattern`` (a regex)."""
        matcher = re.compile(pattern, re.IGNORECASE)
        for label in labels:
            if matcher.search(label):
                return label
        return ""

    @classmethod
    def parse_prices(cls, lines: list[str]) -> dict:
        """Work out which price a card shows and what it applies to.

        Hotel cards are ambiguous: the same card can show a nightly rate,
        a total for the whole stay, or both. The result says which is which
        so the quote never multiplies a total by the number of nights.
        """
        result = {
            "price": 0.0,
            "price_text": "",
            "basis": "per_night",
            "per_night": None,
            "total": None,
            "nights": None,
        }

        per_night = None
        total = None
        plain = None

        for line in lines:
            if not CURRENCY_LINE_RE.match(line):
                continue
            value = cls.money_to_float(line)
            if value <= 0:
                continue
            lowered = line.lower()
            if "per night" in lowered or "/night" in lowered:
                per_night = value
                result["price_text"] = line
            elif "total" in lowered:
                total = value
                result["price_text"] = line
            elif plain is None:
                plain = value
                result["price_text"] = line

        # How many nights does the card refer to?
        for line in lines:
            match = re.search(r"(\d+)\s*nights?", line, re.IGNORECASE)
            if match:
                result["nights"] = int(match.group(1))
                break

        if per_night is not None:
            result["per_night"] = per_night
            result["price"] = total if total is not None else per_night
            result["basis"] = "total_for_stay" if total is not None else "per_night"
            if total is not None:
                result["total"] = total
        elif total is not None:
            result["total"] = total
            result["price"] = total
            result["basis"] = "total_for_stay"
        elif plain is not None:
            result["price"] = plain
            if result["nights"]:
                # The card states the number of nights, so the bare price
                # next to it is the total for the stay.
                result["total"] = plain
                result["basis"] = "total_for_stay"
            else:
                result["per_night"] = plain
                result["basis"] = "per_night"

        return result
