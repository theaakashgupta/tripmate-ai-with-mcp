# ✈️ TripMate AI — A Multi-Agent Travel Planner with MCP

An open-source AI travel planner that turns a natural-language trip request into a practical travel plan with flight suggestions, hotel ideas, weather details, and a day-by-day itinerary. The project uses a multi-agent workflow built with LangGraph, LangChain, FastAPI, and MCP tooling.

## Why this project?

Planning a trip usually means jumping between multiple websites, tools, and spreadsheets. This project brings that flow into one experience by combining:

- a flight-search agent,
- a hotel-research agent,
- a weather agent,
- an itinerary-planning agent, and
- a final response agent,

all coordinated through a LangGraph workflow with MCP-based tool integrations.

## Features

- ✈️ Flight research using AviationStack
- 🏨 Hotel suggestions using Tavily search
- 🌤 Weather lookup via a custom MCP tool
- 🧠 Multi-agent orchestration with LangGraph and MCP
- 📝 Structured travel itinerary generation
- 🌐 FastAPI backend with a simple web interface
- 💾 Conversation state persistence using PostgreSQL
- ⚡ LLM-powered responses with Groq
- 🤖 Optional browser automation (Playwright) for live prices and checkout

## Tech Stack

- Python 3.10+
- FastAPI
- Jinja2 + HTML/CSS/JavaScript frontend
- LangGraph
- LangChain
- Groq LLMs
- PostgreSQL
- Tavily API
- AviationStack API
- MCP via `langchain-mcp-adapters` and `mcp`

## State and MCP Integration

This project integrates MCP in several places:

- `Tavily` search uses a remote MCP endpoint at `https://mcp.tavily.com/mcp/`
- `AviationStack` uses a local stdio MCP command: `uvx aviationstack-mcp`
- `Weather` is implemented with a custom local MCP server in `custom_weather_mcp_server.py`

The MCP client is defined in `mcp_client.py`, which exposes async helper functions for:

- `tavily_mcp_search`
- `aviation_mcp_call`
- `weather_mcp_search`
- `forecast_mcp_search`
- `extract_destination`

The main travel workflow in `backend.py` calls these helpers from the flight, hotel, and weather agents.

## Project Structure

```text
.
├── app.py                      # FastAPI app entry point
├── backend.py                  # LangGraph travel workflow
├── mcp_client.py               # MCP client and tool integration
├── custom_weather_mcp_server.py# Local weather MCP server
├── requirements.txt            # Python dependencies
├── static/                     # Static frontend assets
├── templates/                  # HTML templates
├── tools/                      # Flight and web search integrations
├── booking/                    # Approval-gated booking workflow
│   ├── browser/                # Playwright session + one adapter per site
│   └── playwright_provider.py  # Browser-driven booking provider
└── tests/                      # Offline browser suite + mock travel site
```

## Prerequisites

Before running the project locally, make sure you have:

- Python 3.10 or newer installed
- PostgreSQL running and accessible
- API keys for:
  - Groq
  - Tavily
  - AviationStack
  - OpenWeather (optional — the weather MCP server falls back to
    [Open-Meteo](https://open-meteo.com/) when the key is missing or invalid)
- `uvx` for local `aviationstack-mcp` usage. It is installed with the Python
  dependencies (`uv` is pinned in `requirements.txt`); keep `.venv/bin` on
  `PATH` so the MCP client can find it.

## Environment Variables

Create a `.env` file in the project root with the following variables:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/travel_db
GROQ_API_KEY=your_groq_api_key
AVIATIONSTACK_API_KEY=your_aviationstack_api_key
TAVILY_API_KEY=your_tavily_api_key
OPENWEATHER_API_KEY=your_openweather_api_key
DEFAULT_ORIGIN_IATA=DAC
```

`OPENWEATHER_API_KEY` may be left as a placeholder: the weather MCP server
then serves the same current-conditions and forecast data from Open-Meteo,
which needs no key.

Set `BOOKING_PROVIDER_MODE=playwright` to replace the simulated prices with
live ones from Google Flights and Booking.com — see
[Browser automation](#browser-automation-optional).

## Installation

```bash
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running the App

Start the FastAPI server:

```bash
python app.py
```

Then open your browser at:

```text
http://127.0.0.1:8000/
```

## Using MCP Tools

The app uses MCP behind the scenes, so there is no separate frontend change required after the environment is set up.

If you need to customize the weather MCP server command, edit `mcp_client.py` and update the `weather` tool path to your local Python environment.

To check that every MCP server connects and lists its tools:

```bash
PATH="$PWD/.venv/bin:$PATH" python test.py
```

Notes:

- **Tavily** is a remote MCP endpoint and works with a valid `TAVILY_API_KEY`.
- **AviationStack** is launched with `uvx aviationstack-mcp`. Its data calls
  fail with `usage_limit_reached` once the account's monthly quota is spent;
  renew the plan or use a different key.
- **Weather** starts even with a placeholder OpenWeather key and returns
  Open-Meteo data (`"source": "open-meteo"`).

## API Endpoints

- GET /health - Health check
- POST /api/travel - Submit a travel request

Example request:

```bash
curl -X POST http://127.0.0.1:8000/api/travel \
  -H "Content-Type: application/json" \
  -d '{"message":"Plan a 3-day trip to Tokyo with a budget of $1200"}'
```

## How the Workflow Works

1. The user submits a travel request.
2. The flight agent uses MCP-backed AviationStack data.
3. The hotel agent uses a remote Tavily MCP search.
4. The weather agent calls the custom weather MCP server.
5. The itinerary agent creates a practical travel plan.
6. The final response is returned through the web API.

## Browser automation (optional)

TripMate can drive a real browser instead of returning simulated prices.
With the browser provider switched on, the agents read **live** fares from
Google Flights and **live** rates from Booking.com, and the approval
"book it" step walks the chosen offer all the way to the payment page.

### Install

```bash
pip install -r requirements.txt
python -m playwright install chromium     # the browser itself
```

### Switch it on

```env
# .env
BOOKING_PROVIDER_MODE=playwright          # simulated (default) | playwright | http
```

`simulated` keeps the original behaviour (invented prices, no browser).
`playwright` uses the real sites. Restart the server afterwards.

### What it does

1. **Search** – the adapters read the live result cards (airline, times,
   stops, price / property name, rating, price per night or total).
2. **Quote** – every option in the approval screen is priced from the live
   page, so the number you approve is the number the site shows.
3. **Revalidate** – the price is looked up again right before the
   purchase. A material change sends the request back for a fresh approval.
4. **Checkout** – the browser opens the property page, picks the room that
   matches the quoted total, clicks *I'll reserve*, fills in the guest
   details (name, e-mail, country, phone) and stops.
5. **Stop** – the bot stops on the payment form. No card number, expiry or
   CVC is ever typed, so the booking status becomes `AWAITING_PAYMENT`
   and the receipt links you to the page where you pay yourself.

Google Flights is a metasearch engine: the flight is selected on Google and
the purchase happens on the airline's own site, so the bot stops on the
itinerary page with the airline link.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOOKING_PROVIDER_MODE` | `simulated` | `simulated`, `playwright` or `http` |
| `BROWSER_HEADLESS` | `true` | `false` shows the browser window (needs a display) |
| `BROWSER_TIMEOUT_MS` | `45000` | Timeout for a single browser step |
| `BROWSER_SLOW_MO_MS` | `0` | Slows the browser down for debugging |
| `BROWSER_ARTIFACT_DIR` | – | Screenshots of every checkout step |
| `BROWSER_USER_DATA_DIR` | – | Persistent Chromium profile (keeps a login) |
| `BROWSER_STORAGE_STATE` | – | Playwright storage-state file (cookies/localStorage) |
| `BROWSER_USER_AGENT` | Chrome UA | Overrides the user agent |
| `BROWSER_LOCALE` | `en-US` | Locale sent to the site |
| `BROWSER_PROXY` | – | Proxy URL for the browser |
| `BOOKING_GUEST_EMAIL` | – | E-mail typed into the guest form |
| `BOOKING_GUEST_PHONE` | – | Phone number typed into the guest form |
| `BOOKING_GUEST_COUNTRY` | – | Two-letter country code (e.g. `BD`) |
| `BROWSER_SEARCH_CACHE_TTL` | `180` | Seconds a live search is reused |
| `BOOKING_FLIGHT_ADAPTER` | – | Use another adapter for flights |
| `BOOKING_HOTEL_ADAPTER` | – | Use another adapter for hotels |

The guest details are only used for the traveller fields of the checkout
form. Leave them empty and the bot fills in the name only, then reports
which fields are still missing.

### Live endpoints

```bash
curl http://127.0.0.1:8000/api/browser/status
```

```bash
curl -X POST http://127.0.0.1:8000/api/browser/search \
  -H "Content-Type: application/json" \
  -d '{"kind":"hotel","destination":"Tokyo","start_date":"2026-11-10",
       "end_date":"2026-11-13","currency":"USD","limit":3}'
```

`kind` is `flight` or `hotel`. The search endpoint never books anything.

### Adding another site

Every site lives behind one adapter
(`booking/browser/adapters/<site>.py`) that implements two methods:

```python
class MySiteAdapter(SiteAdapter):
    def search(self, page, plan, travelers, limit=5) -> list[BrowserOffer]:
        ...          # read the result cards of the site

    def open_checkout(self, page, offer, travelers) -> CheckoutResult:
        ...          # walk to the payment form and stop there
```

Register it in `booking/browser/adapters/__init__.py` and it becomes
available to the provider. The approval gate, the revalidation step and
the receipt work for every adapter without changes.

### Tests

The offline suite drives the adapters against a local mock site that
mirrors the real DOM, so no network access is needed:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

`tests/test_playwright_provider.py` covers the provider logic without a
browser (cache, revalidation, room pricing, mode selection), while
`tests/test_browser_booking.py` runs the full search → quote → revalidate →
checkout flow in Chromium.

### Boundaries

* The bot stops before payment. It never types card details and never
  charges anything.
* No CAPTCHA or bot challenge is solved, bypassed or worked around. When a
  site shows one, the run fails and says so.
* Automating a booking site may break its terms of service. Use this on
  sites and accounts you are allowed to automate, and keep the request
  rate low.

## Contributing

Contributions are welcome. If you want to improve the app, add new travel features, or fix issues:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Open a pull request

## Acknowledgments

This project is built with the help of modern LLM tooling, MCP integrations, and travel APIs. It is intended as a practical example of combining LangGraph agents with real-world applications.
