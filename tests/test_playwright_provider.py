"""Fast, browser-free tests for the Playwright provider logic.

The end-to-end suite in ``test_browser_booking`` launches Chromium against
the mock site. These tests stub the browser so the parts that are easy to
get wrong can be checked in milliseconds:

* revalidation must never answer from the search cache,
* quoting reuses a search instead of hitting the site per option,
* a hotel whose site shows a stay total is not multiplied by the nights,
* checkout is approval-gated and stops at the payment step,
* the provider mode env var selects the right implementation.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import booking.providers as providers  # noqa: E402
from booking.browser import BrowserOffer, CheckoutResult  # noqa: E402
from booking.models import make_option, make_traveler  # noqa: E402
from booking.playwright_provider import PlaywrightBookingProvider  # noqa: E402


class FakeSession:
    """Runs browser callbacks immediately, with no browser involved."""

    def __init__(self):
        self.closed = False
        self.screenshots = 0

    def run(self, fn, *args, **kwargs):
        return fn(None, *args, **kwargs)

    def screenshot(self, page, name):
        self.screenshots += 1
        return ""

    def close(self):
        self.closed = True


class FakeAdapter:
    """Deterministic stand-in for a booking-site adapter."""

    def __init__(self, kind: str):
        self.kind = kind
        self.name = f"Fake {kind.title()} Site"
        self.searches = 0
        self.checkouts = 0

    def search(self, page, plan, travelers, limit=5):
        self.searches += 1
        # Every search is a little more expensive, so a cached answer is
        # easy to tell apart from a fresh one.
        price = 100.0 + self.searches
        details = {"airline": "Fake Air"} if self.kind == "flight" else {}
        if self.kind == "hotel":
            details = {
                "price_basis": "total_for_stay",
                "price_total": price,
                "price_per_night": round(price / 3, 2),
                "nights": 3,
            }
        offers = [
            BrowserOffer(
                kind=self.kind,
                provider=self.name,
                title="Fake Air" if self.kind == "flight" else "Fake Hotel",
                subtitle="DAC → NRT" if self.kind == "flight" else "Central",
                price=price,
                currency="USD",
                details=details,
            )
        ]
        return offers[:limit]

    def open_checkout(self, page, offer, travelers):
        self.checkouts += 1
        return CheckoutResult(
            success=True,
            reference="fk_test123",
            status="awaiting_payment",
            url="https://example.test/checkout",
            message="stopped before payment",
            steps=["Started the reservation", "Stopped at the payment form"],
            payment_required=True,
        )


class PlaywrightProviderLogicTest(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "origin": "DAC",
            "destination": "Tokyo",
            "start_date": "2026-11-10",
            "end_date": "2026-11-13",
            "currency": "USD",
            "travelers": [make_traveler("Aakash Rahman")],
        }
        self.session = FakeSession()
        self.adapters = {
            "flight": FakeAdapter("flight"),
            "hotel": FakeAdapter("hotel"),
        }
        self.resolver = patch(
            "booking.playwright_provider.resolve_adapter",
            side_effect=lambda kind: self.adapters[kind],
        )
        self.resolver.start()
        self.provider = PlaywrightBookingProvider(session=self.session)

    def tearDown(self):
        self.provider.close()
        self.resolver.stop()

    def flight_option(self):
        return make_option(
            option_id="fl_1",
            kind="flight",
            title="Fake Air",
            subtitle="DAC → NRT",
            details={"airline": "Fake Air", "fare_brand": "Standard"},
            provider="Fake Flight Site",
        )

    def hotel_option(self):
        return make_option(
            option_id="ho_1",
            kind="hotel",
            title="Fake Hotel",
            subtitle="Central",
            details={"room_type": "Standard Double"},
            provider="Fake Hotel Site",
        )

    # ------------------------------------------------------------------

    def test_quote_reuses_the_cached_search(self):
        self.provider.quote_flight(self.flight_option(), self.plan, 1)
        self.provider.quote_flight(self.flight_option(), self.plan, 1)
        self.assertEqual(self.adapters["flight"].searches, 1)

    def test_revalidation_always_hits_the_site(self):
        quote = self.provider.quote_flight(self.flight_option(), self.plan, 1)
        self.assertEqual(quote["total"], 101.0)
        self.assertEqual(self.adapters["flight"].searches, 1)

        first = self.provider.revalidate(quote)
        self.assertEqual(first["quote"]["total"], 102.0)
        self.assertEqual(first["quote"]["price_source"], "browser_revalidated")

        # A second immediate revalidation must not reuse the cache either.
        second = self.provider.revalidate(quote)
        self.assertEqual(second["quote"]["total"], 103.0)
        self.assertEqual(self.adapters["flight"].searches, 3)

    def test_hotel_stay_total_is_not_multiplied_by_nights(self):
        quote = self.provider.quote_hotel(self.hotel_option(), self.plan, 1)
        # The site quoted 101 for the whole 3-night stay.
        self.assertEqual(quote["total"], 101.0)
        self.assertEqual(quote["nights"], 3)
        self.assertEqual(quote["conditions"]["live_rate_per_night"], 33.67)

    def test_checkout_stops_at_payment_and_is_approval_gated(self):
        quote = self.provider.quote_hotel(self.hotel_option(), self.plan, 1)

        with self.assertRaises(providers.ProviderError):
            self.provider.create_booking(quote, self.plan["travelers"], "")

        result = self.provider.create_booking(
            quote, self.plan["travelers"], "apr_test"
        )
        self.assertEqual(result["status"], "awaiting_payment")
        self.assertEqual(result["approval_id"], "apr_test")
        self.assertTrue(result["raw"]["payment_required"])
        self.assertEqual(self.adapters["hotel"].checkouts, 1)
        self.assertEqual(self.provider.get_booking_status("bk_1")["status"],
                         "awaiting_payment")

    def test_quote_without_live_offer_is_unavailable(self):
        with patch.object(self.adapters["hotel"], "search", return_value=[]):
            quote = self.provider.quote_hotel(self.hotel_option(), self.plan, 1)
        self.assertFalse(quote["availability"])
        self.assertEqual(quote["price_source"], "browser_unavailable")
        self.assertIn("unavailable_reason", quote["browser"])


class ProviderModeTest(unittest.TestCase):
    def tearDown(self):
        providers.close_provider()

    def test_default_is_simulated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.provider_mode(), "simulated")
            self.assertIsInstance(
                providers.get_provider(), providers.SimulatedBookingProvider
            )

    def test_explicit_playwright_mode(self):
        with patch.dict(
            os.environ, {"BOOKING_PROVIDER_MODE": "playwright"}, clear=True
        ):
            self.assertEqual(providers.provider_mode(), "playwright")
            self.assertIsInstance(
                providers.get_provider(), PlaywrightBookingProvider
            )

    def test_base_url_implies_http_mode(self):
        with patch.dict(
            os.environ,
            {"BOOKING_PROVIDER_BASE_URL": "https://api.example.test"},
            clear=True,
        ):
            self.assertEqual(providers.provider_mode(), "http")
            self.assertIsInstance(
                providers.get_provider(), providers.HttpBookingProvider
            )

    def test_close_provider_clears_the_cache(self):
        with patch.dict(
            os.environ, {"BOOKING_PROVIDER_MODE": "playwright"}, clear=True
        ):
            provider = providers.get_provider()
            providers.close_provider()
            self.assertEqual(providers._provider_cache, {})
            self.assertTrue(provider._session._closed)

            # A second call creates a new instance rather than reusing the
            # closed one.
            self.assertIsNot(providers.get_provider(), provider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
