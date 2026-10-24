"""A tiny mock travel site used to test the browser automation offline.

It mimics the parts of Google Flights and Booking.com that the adapters
read:

* ``/flights``  - flight cards with the ``data-tm-*`` hooks the Google
  Flights adapter looks for, and a review page that links out to an airline.
* ``/searchresults.html`` - property cards with the ``data-testid`` hooks
  the Booking.com adapter looks for.
* ``/hotel/<slug>`` - a property page with a "Reserve" button.
* ``/checkout/<id>`` - the guest-details form *and* the payment section.
* ``/pay/<id>``    - the payment submit. The bot must never reach it.

``GET /__state`` returns what the site has recorded, which is how the test
proves the traveller details were typed and the card fields were not.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FLIGHTS = [
    {
        "id": "gf-1",
        "airline": "Biman Bangladesh Airlines",
        "departure": "08:30",
        "arrival": "18:45",
        "stops": "1 stop",
        "price": "US$412",
    },
    {
        "id": "gf-2",
        "airline": "Turkish Airlines",
        "departure": "21:15",
        "arrival": "14:05",
        "stops": "Nonstop",
        "price": "US$588",
    },
    {
        "id": "gf-3",
        "airline": "Qatar Airways",
        "departure": "05:40",
        "arrival": "16:20",
        "stops": "1 stop",
        "price": "US$655",
    },
]

HOTELS = [
    {
        "id": "ho-1",
        "name": "Grand Tokyo Hotel",
        "review": "9.1 Excellent",
        "price": "US$142",
        "slug": "grand-tokyo-hotel",
    },
    {
        "id": "ho-2",
        "name": "Shinjuku Business Inn",
        "review": "8.4 Very good",
        "price": "US$98",
        "slug": "shinjuku-business-inn",
    },
    {
        "id": "ho-3",
        "name": "Asakusa Riverside Ryokan",
        "review": "8.9 Good",
        "price": "US$176",
        "slug": "asakusa-riverside-ryokan",
    },
]


def _flights_page() -> str:
    cards = []
    for flight in FLIGHTS:
        cards.append(
            f"""
            <li class="pIav2d" data-tm-flight-card="{flight['id']}">
              <div data-tm-airline>{flight['airline']}</div>
              <div class="wIjUOb" data-tm-departure>{flight['departure']}</div>
              <div class="ZwBxb" data-tm-arrival>{flight['arrival']}</div>
              <div class="gvkrdb" data-tm-stops>{flight['stops']}</div>
              <div class="YMlIz" data-tm-price>{flight['price']}</div>
              <a data-tm-select href="/flights/review?id={flight['id']}">Select</a>
            </li>"""
        )
    return f"""
    <html><head><title>Flights</title></head>
    <body>
      <h1>Flights</h1>
      <ul id="flight-list">{''.join(cards)}</ul>
    </body></html>
    """


def _flights_review_page(flight_id: str) -> str:
    flight = next((f for f in FLIGHTS if f["id"] == flight_id), FLIGHTS[0])
    return f"""
    <html><head><title>Review</title></head>
    <body>
      <h2>{flight['airline']}</h2>
      <p>{flight['departure']} → {flight['arrival']} · {flight['stops']}</p>
      <p>{flight['price']}</p>
      <a data-tm-airline-url href="/airline/checkout?ref={flight['id']}">
        Book with {flight['airline']}
      </a>
    </body></html>
    """


def _nights(query: dict) -> int:
    """How many nights the search was for."""
    from datetime import date

    check_in = (query.get("checkin") or [""])[0]
    check_out = (query.get("checkout") or [""])[0]
    try:
        start = date.fromisoformat(check_in)
        end = date.fromisoformat(check_out)
    except ValueError:
        return 1
    return max((end - start).days, 1)


def _hotels_page(query: dict) -> str:
    nights = _nights(query)
    cards = []
    for hotel in HOTELS:
        link = f"/hotel/{hotel['slug']}?checkin={query.get('checkin', [''])[0]}&checkout={query.get('checkout', [''])[0]}"
        cards.append(
            f"""
            <div data-testid="property-card" data-tm-property-card="{hotel['id']}">
              <h3 data-testid="title">{hotel['name']}</h3>
              <div data-testid="review-score">{hotel['review']}</div>
              <div data-testid="price-and-discounted-price">{hotel['price']}</div>
              <a href="{link}">See availability</a>
            </div>"""
        )
    return f"""
    <html><head><title>Stays</title></head>
    <body>
      <h1>Stays</h1>
      <div id="property-list">{''.join(cards)}</div>
    </body></html>
    """


def _hotel_page(hotel: dict, query: dict) -> str:
    """A property page with a room picker, like the real Booking.com."""
    nights = _nights(query)
    nightly = float(hotel["price"].replace("US$", ""))
    total = nightly * nights
    total_text = f"US${total:,.0f}"
    return f"""
    <html><head><title>{hotel['name']}</title></head>
    <body>
      <h1>{hotel['name']}</h1>
      <p data-testid="review-score">{hotel['review']}</p>
      <div data-testid="room-name">Standard Double</div>
      <p data-testid="price-and-discounted-price">{hotel['price']} per night</p>
      <table id="room-table">
        <tr>
          <td>
            <label for="hprt_nos_select_{hotel['id']}">Select Rooms</label>
            <select class="hprt-nos-select js-hprt-nos-select"
                    name="nr_rooms_{hotel['id']}"
                    data-testid="select-room-trigger"
                    id="hprt_nos_select_{hotel['id']}"
                    onchange="document.querySelector('.js-reservation-button').removeAttribute('data-title')">
              <option value="0">0</option>
              <option value="1">1 ({total_text})</option>
              <option value="2">2 ({total_text})</option>
            </select>
          </td>
        </tr>
      </table>
      <button class="js-reservation-button"
              data-title="Select your accommodations first"
              onclick="location.href='/checkout/{hotel['id']}'">I'll reserve</button>
    </body></html>
    """


def _checkout_page(booking_id: str) -> str:
    """Guest details plus the payment section the bot must stop at."""
    return f"""
    <html><head><title>Checkout</title></head>
    <body>
      <h1>Complete your booking</h1>
      <form id="guest-form" method="post" action="/pay/{booking_id}">
        <label>First name
          <input name="firstname" type="text" />
        </label>
        <label>Last name
          <input name="lastname" type="text" />
        </label>
        <label>E-mail
          <input name="email" type="email" />
        </label>
        <fieldset id="payment-method">
          <legend>Payment</legend>
          <label>Card number
            <input name="cardnumber" type="text" autocomplete="cc-number" />
          </label>
          <label>Expiry
            <input name="cc-exp" type="text" autocomplete="cc-exp" />
          </label>
          <label>CVC
            <input name="cc-csc" type="text" autocomplete="cc-csc" />
          </label>
        </fieldset>
        <button type="submit" data-tm-confirm>Pay now</button>
      </form>
    </body></html>
    """


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockTravelSite/1.0"

    # -- helpers -------------------------------------------------------

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: dict) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    # -- routes --------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        state = self.server.state

        state["requests"].append({"method": "GET", "path": path})

        if path == "/__state":
            self._send_json(state)
            return
        if path == "/flights":
            self._send(_flights_page())
            return
        if path == "/flights/review":
            flight_id = (query.get("id") or ["gf-1"])[0]
            self._send(_flights_review_page(flight_id))
            return
        if path == "/searchresults.html":
            self._send(_hotels_page(query))
            return
        if path.startswith("/hotel/"):
            slug = path.rsplit("/", 1)[-1]
            hotel = next((h for h in HOTELS if h["slug"] == slug), HOTELS[0])
            self._send(_hotel_page(hotel, query))
            return
        if path.startswith("/checkout/"):
            self._send(_checkout_page(path.rsplit("/", 1)[-1]))
            return
        if path.startswith("/airline/checkout"):
            self._send(
                "<html><body><h1>Airline checkout</h1>"
                "<p>Complete the payment on the airline site.</p></body></html>"
            )
            return
        self._send("<html><body><h1>Not found</h1></body></html>", status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        fields = {key: values[0] for key, values in parse_qs(raw).items()}

        state = self.server.state
        state["requests"].append({"method": "POST", "path": parsed.path})
        state["submissions"].append({"path": parsed.path, "fields": fields})

        self._send(
            "<html><body><h1>Payment received</h1>"
            "<p>Mock confirmation.</p></body></html>"
        )


class MockTravelSite:
    """Runs the mock site on a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self._server = None
        self._thread = None

    def start(self) -> str:
        self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._server.state = {"requests": [], "submissions": []}
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        self.port = self._server.server_address[1]
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def state(self) -> dict:
        return self._server.state

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


if __name__ == "__main__":  # pragma: no cover - manual use
    site = MockTravelSite(port=8099)
    print("Mock travel site on", site.start())
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        site.stop()
