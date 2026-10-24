"""End-to-end test of the browser automation layer.

The real Google Flights and Booking.com adapters are pointed at the local
mock site, so the whole flow runs offline and deterministically:

    search → quote → revalidate → approval → checkout (stop before payment)

What the test proves:

* the adapter reads the live offers out of the rendered page,
* the quote is built from the live price, not from a guess,
* the traveller details really are typed into the guest form,
* no payment data is typed and the payment form is never submitted.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from booking.browser import BrowserSession, resolve_adapter  # noqa: E402
from booking.models import make_option, make_traveler  # noqa: E402
from booking.playwright_provider import PlaywrightBookingProvider  # noqa: E402
from mock_site.server import MockTravelSite  # noqa: E402


class BrowserBookingFlowTest(unittest.TestCase):
    """Drives the real adapters against the mock travel site."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.site = MockTravelSite()
        cls.base_url = cls.site.start()

        # Point the adapters at the mock site instead of the live sites.
        os.environ["GOOGLE_FLIGHTS_BASE_URL"] = cls.base_url
        os.environ["BOOKING_COM_BASE_URL"] = cls.base_url
        os.environ["BROWSER_HEADLESS"] = "true"
        os.environ["BROWSER_TIMEOUT_MS"] = "20000"

        cls.plan = {
            "origin": "DAC",
            "destination": "Tokyo",
            "start_date": "2026-11-10",
            "end_date": "2026-11-13",
            "currency": "USD",
            "travelers": [make_traveler("Aakash Rahman")],
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.site.stop()

    # ------------------------------------------------------------------

    def test_flight_search_reads_live_offers(self):
        adapter = resolve_adapter("flight")
        session = BrowserSession()
        try:
            offers = session.run(
                lambda page: adapter.search(
                    page, self.plan, self.plan["travelers"], limit=3
                )
            )
        finally:
            session.close()

        self.assertEqual(len(offers), 3)
        titles = [offer.title for offer in offers]
        self.assertIn("Biman Bangladesh Airlines", titles)
        self.assertIn("Turkish Airlines", titles)

        cheapest = min(offers, key=lambda offer: offer.price)
        self.assertEqual(cheapest.price, 412.0)
        self.assertEqual(cheapest.currency, "USD")

    def test_hotel_search_reads_live_offers(self):
        adapter = resolve_adapter("hotel")
        session = BrowserSession()
        try:
            offers = session.run(
                lambda page: adapter.search(
                    page, self.plan, self.plan["travelers"], limit=3
                )
            )
        finally:
            session.close()

        self.assertEqual(len(offers), 3)
        names = [offer.title for offer in offers]
        self.assertIn("Grand Tokyo Hotel", names)
        self.assertIn("Shinjuku Business Inn", names)

    def test_quote_uses_the_live_price(self):
        provider = PlaywrightBookingProvider()
        option = make_option(
            option_id="ho_1",
            kind="hotel",
            title="Grand Tokyo Hotel",
            subtitle="Shinjuku",
            details={"room_type": "Standard Double", "free_cancellation": True},
            provider="Booking.com",
        )
        quote = provider.quote_hotel(option, self.plan, 1)

        # 3 nights at the live rate of 142.
        self.assertEqual(quote["total"], 426.0)
        self.assertEqual(quote["price_source"], "browser_quote")
        self.assertTrue(quote["availability"])
        self.assertEqual(quote["browser"]["offer"]["title"], "Grand Tokyo Hotel")
        provider.close()

    def test_unknown_option_is_reported_unavailable(self):
        provider = PlaywrightBookingProvider()
        option = make_option(
            option_id="ho_9",
            kind="hotel",
            title="Nowhere Land Resort",
            details={"room_type": "Standard Double"},
            provider="Booking.com",
        )
        quote = provider.quote_hotel(option, self.plan, 1)
        self.assertFalse(quote["availability"])
        self.assertEqual(quote["price_source"], "browser_unavailable")
        provider.close()

    def test_checkout_fills_guest_details_and_stops_before_payment(self):
        provider = PlaywrightBookingProvider()
        option = make_option(
            option_id="ho_1",
            kind="hotel",
            title="Grand Tokyo Hotel",
            details={"room_type": "Standard Double"},
            provider="Booking.com",
        )
        quote = provider.quote_hotel(option, self.plan, 1)
        self.assertTrue(quote["availability"])

        result = provider.create_booking(
            quote, self.plan["travelers"], "apr_test_approval"
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "awaiting_payment")
        self.assertTrue(result["raw"]["payment_required"])
        self.assertIn("Started the reservation", result["raw"]["checkout"]["steps"])
        self.assertIn(
            "Stopped at the payment form (no payment details were typed)",
            result["raw"]["checkout"]["steps"],
        )

        # The payment form must never have been submitted.
        submissions = self.site.state["submissions"]
        self.assertEqual(
            submissions, [], f"The bot submitted a payment form: {submissions}"
        )

        # The guest fields were filled in on the rendered page. The mock
        # site records submissions, so we check the page state instead by
        # re-running the checkout and reading the form values.
        self.assertIn(
            "checkout", result["raw"]["checkout"]["raw"]["final_url"]
        )

    def test_guest_form_is_actually_filled(self):
        """Walk the booking.com flow by hand and read the form values."""
        adapter = resolve_adapter("hotel")
        session = BrowserSession()

        def flow(page):
            offers = adapter.search(page, self.plan, self.plan["travelers"], limit=1)
            outcome = adapter.open_checkout(page, offers[0], self.plan["travelers"])
            return (
                outcome,
                page.locator("input[name='firstname']").first.input_value(),
                page.locator("input[name='lastname']").first.input_value(),
                page.locator("input[name='cardnumber']").first.input_value(),
            )

        try:
            result, first, last, card = session.run(flow)
        finally:
            session.close()

        self.assertTrue(result.success)
        self.assertTrue(result.payment_required)
        self.assertEqual(first, "Aakash")
        self.assertEqual(last, "Rahman")
        self.assertEqual(card, "", "The bot must never type card details")

    def test_revalidation_reports_the_live_price(self):
        provider = PlaywrightBookingProvider()
        option = make_option(
            option_id="fl_1",
            kind="flight",
            title="Turkish Airlines",
            details={"airline": "Turkish Airlines", "fare_brand": "Standard"},
            provider="Google Flights",
        )
        quote = provider.quote_flight(option, self.plan, 1)
        self.assertEqual(quote["total"], 588.0)

        result = provider.revalidate(quote)
        self.assertTrue(result["available"])
        self.assertEqual(result["quote"]["total"], 588.0)
        self.assertEqual(result["quote"]["price_source"], "browser_revalidated")
        provider.close()

    def test_booking_requires_an_approval_id(self):
        provider = PlaywrightBookingProvider()
        with self.assertRaises(Exception):
            provider.create_booking({"kind": "flight"}, self.plan["travelers"], "")
        provider.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
