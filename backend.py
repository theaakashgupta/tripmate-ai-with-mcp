import os
import re
import datetime
from urllib.parse import urlparse
import certifi
from dotenv import load_dotenv

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from typing import TypedDict, Annotated
import operator
import json
import uuid
import asyncio
import threading
import psycopg
from psycopg.rows import dict_row

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command, interrupt
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    AIMessage,
    SystemMessage,
)
from langchain_groq import ChatGroq
from urllib.parse import quote_plus
# from tools.tavily_tool import tavily_search
# from tools.flight_tool import search_flights
from mcp_client import tavily_mcp_search, aviation_mcp_call, extract_destination, forecast_mcp_search, weather_mcp_search
from booking import (
    BookingStatus,
    CANCEL_NEGATED_RE,
    STATUS_LABELS,
    ProviderError,
    ask_for_missing,
    build_tasks,
    canonical_binding,
    decision_help,
    describe_selection,
    detect_intent,
    get_provider,
    grant_approval,
    issue_approval,
    make_option,
    make_purchase,
    make_traveler,
    mark_done,
    mark_failed,
    mark_running,
    material_changes,
    merge_plan,
    missing_requirements,
    new_record,
    parse_decision,
    task_progress,
    trip_totals,
    validate_approval,
    with_retries,
)



def get_database_url():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError(
            "DATABASE_URL is missing. Please add your Render PostgreSQL External Database URL to .env"
        )

    parsed = urlparse(database_url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}

    if "sslmode=" not in database_url:
        default_sslmode = "disable" if parsed.hostname in local_hosts else "require"
        sslmode = os.getenv("DB_SSLMODE", default_sslmode)
        separator = "&" if parsed.query else "?"
        database_url = f"{database_url}{separator}sslmode={sslmode}"

    return database_url


GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing. Please add it to your .env file.")


# =========================
# LLM
# =========================

llm = ChatGroq(
    model="openai/gpt-oss-120b",
    api_key=GROQ_API_KEY,
    max_retries=3,
    # Safety caps: gpt-oss defaults to 3072+ completion tokens (incl. reasoning),
    # which Groq reserves against the 8000 TPM limit and triggers 413 errors.
    max_tokens=700,
    reasoning_effort="low"
)


def clip(text, limit=1200):
    """Keep prompts small enough to stay under Groq's 8000 TPM limit."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


# Groq's free tier: 8000 TPM. A 413 fires when ONE request
# (prompt tokens + max_tokens reservation) exceeds that ceiling.
TPM_LIMIT = 8000
REQUEST_BUDGET = 7400


def _estimate_tokens(text):
    # ~4 chars per token; +1 for safety.
    return len(str(text or "")) // 4 + 1


def _fit_request(system_prompt, user_prompt, max_tokens):
    """Shrink the prompt until prompt + max_tokens fits under TPM_LIMIT."""
    char_budget = max((REQUEST_BUDGET - max_tokens) * 4, 400)
    system_chars = len(str(system_prompt or ""))
    limit = char_budget - system_chars
    if len(user_prompt) <= limit:
        return user_prompt
    return user_prompt[:max(limit, 200)] + "\n...[truncated]"


def _wait_seconds(error_message):
    """Parse Groq's 'try again in Xs' hint; default to 30s."""
    import re

    match = re.search(r"try again in ([\d.]+)s", error_message)
    if match:
        return min(float(match.group(1)), 60.0)
    return 30.0


def ask(system_prompt, user_prompt, max_tokens=500):
    """
    Invoke the LLM with a request that is guaranteed to fit Groq's
    per-request TPM ceiling, retrying waitable 429s and shrinking
    on a 413 (waiting never fixes a 413).
    """
    import time

    user_prompt = _fit_request(system_prompt, user_prompt, max_tokens)

    last_error = None
    for attempt in range(3):
        try:
            response = llm.bind(max_tokens=max_tokens).invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt)
            ])
            usage = getattr(response, "usage_metadata", None)
            if usage:
                print(
                    f"[llm] tokens in={usage.get('input_tokens')} "
                    f"out={usage.get('output_tokens')} max_tokens={max_tokens}"
                )
            return response.content
        except Exception as error:
            message = str(error).lower()
            if "request too large" in message or "413" in message:
                # Per-request ceiling exceeded: waiting cannot help,
                # only a smaller request can.
                if max_tokens > 200:
                    max_tokens = max(max_tokens // 2, 200)
                    user_prompt = _fit_request(
                        system_prompt, user_prompt, max_tokens
                    )
                    print(f"[llm] 413 — retrying with max_tokens={max_tokens}")
                    continue
                raise RuntimeError(
                    "Groq rejected the request as larger than its "
                    "per-minute token limit even after trimming. "
                    "Reduce prompt size or upgrade the Groq tier."
                ) from error
            if "429" in message or "rate_limit" in message or "tokens per minute" in message:
                # Rolling-window throttling: wait it out.
                last_error = error
                wait = _wait_seconds(message)
                print(f"[llm] rate limited — sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            raise

    raise last_error


def _parse_json(text):
    """Pull the first JSON object out of an LLM reply."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None


def ask_json(system_prompt, user_prompt, max_tokens=350, fallback=None):
    """Structured LLM call that never raises: falls back on any failure."""
    try:
        text = ask(system_prompt, user_prompt, max_tokens=max_tokens)
    except Exception as error:
        print(f"[json] llm call failed: {error}")
        return fallback
    data = _parse_json(text)
    if data is None:
        print("[json] unparseable LLM reply — using fallback")
        return fallback
    return data


# =========================
# State
# =========================

class TravelState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    user_query: str
    flight_results: str
    hotel_results: str
    itinerary: str
    llm_calls: int
    weather_results: str
    # ---- booking workflow ----
    thread_id: str
    plan: dict
    trip_options: list
    purchases: list
    booking_status: str
    pending_action: str
    approval_decision: dict
    changes_summary: list
    booking_records: list
    cancel_target: dict
    cancel_context: dict
    receipt: str
    tasks: list
    progress: list


# =========================
# Flight Agent
# =========================

# def flight_agent(state: TravelState):
#     query = state["user_query"]
#     flight_data = search_flights(query)

#     return {
#         "flight_results": flight_data,
#         "messages": [
#             AIMessage(content="Flight results fetched.")
#         ],
#         "llm_calls": state.get("llm_calls", 0) + 1
#     }




# Flight Tool Router Prompt
FLIGHT_AGENT_PROMPT = """
You are a travel flight expert.

User Query:
{query}

Airport Information:
{airport_data}

Airline Information:
{airline_data}

Generate:

1. Likely departure airport
2. Likely arrival airport
3. Airlines serving this route
4. Typical flight duration
5. Estimated airfare range
6. Peak season pricing warning
7. Booking advice

Return concise travel guidance.
"""




# Flight Agent
def _track(state, key, ok, detail, line):
    """Mark a Hermes task and append a short progress line."""
    tasks = state.get("tasks") or build_tasks("research")
    tasks = (mark_done if ok else mark_failed)(tasks, key, detail)
    progress = list(state.get("progress") or [])
    if line:
        progress.append(line)
    return tasks, progress


def flight_agent(state: TravelState):
    print("\nINSIDE FLIGHT AGENT\n")

    query = state["user_query"]

    try:

        airports = asyncio.run(
            aviation_mcp_call(
                "list_airports"
            )
        )

        airlines = asyncio.run(
            aviation_mcp_call(
                "list_airlines"
            )
        )


        print("\nAIRPORTS:", airports)
        print("\nAIRLINES:", airlines)

        prompt = FLIGHT_AGENT_PROMPT.format(
            query=clip(query, 400),
            airport_data=clip(airports, 800),
            airline_data=clip(airlines, 800)
        )

        flight_data = ask(
            "You are an expert travel flight planner.",
            prompt,
            max_tokens=350
        )

        tasks, progress = _track(
            state, "search_flights", True, "Flight research complete",
            "Flight search complete.",
        )

    except Exception as e:

        flight_data = f"Flight information unavailable: {str(e)}"
        tasks, progress = _track(
            state, "search_flights", False, str(e)[:120],
            "Flight search failed — continuing with alternatives.",
        )

    return {
        "flight_results": flight_data,
        "tasks": tasks,
        "progress": progress,
        "messages": [
            AIMessage(
                content="Flight recommendations generated"
            )
        ],
        "llm_calls": state.get("llm_calls", 0) + 1
    }





# =========================
# Hotel Agent
# =========================

def hotel_agent(state: TravelState):
    query = f"Best hotels for {state['user_query']}"
    # hotel_results = tavily_search(query)
    try:
        hotel_results = clip(asyncio.run(tavily_mcp_search(query)), 1400)
        tasks, progress = _track(
            state, "search_hotels", True, "Hotel research complete",
            "Hotel search complete.",
        )
    except Exception as error:
        hotel_results = f"Hotel information unavailable: {error}"
        tasks, progress = _track(
            state, "search_hotels", False, str(error)[:120],
            "Hotel search failed — continuing with alternatives.",
        )

    return {
        "hotel_results": hotel_results,
        "tasks": tasks,
        "progress": progress,
        "messages": [
            AIMessage(content="Hotel information fetched.")
        ],
        "llm_calls": state.get("llm_calls", 0) + 1
    }




# =========================
# Weather Agent
# =========================

def weather_agent(state: TravelState):

    city = extract_destination(state["user_query"])

    try:
        weather_data = asyncio.run(
            weather_mcp_search(city)
        )

        forecast_data = asyncio.run(
            forecast_mcp_search(city)
        )

        weather_results = clip(f"""
        Current Weather:
        {weather_data}

        Forecast:
        {forecast_data}
        """, 1200)
        tasks, progress = _track(
            state, "check_weather", True, city,
            f"Weather checked for {city}.",
        )
    except Exception as error:
        weather_results = f"Weather unavailable: {error}"
        tasks, progress = _track(
            state, "check_weather", False, str(error)[:120],
            "Weather lookup failed — continuing.",
        )

    return {
        "weather_results": weather_results,
        "tasks": tasks,
        "progress": progress,
        "messages": [
            AIMessage(
                content="Weather information fetched"
            )
        ],
        "llm_calls": state.get("llm_calls", 0) + 1
    }




# =========================
# Itinerary Agent
# =========================

def itinerary_agent(state: TravelState):
    prompt = f"""
Create a complete travel itinerary.

User Query:
{clip(state['user_query'], 300)}

Flight Results:
{clip(state['flight_results'], 700)}

Hotel Results:
{clip(state['hotel_results'], 800)}

Weather Results:
{clip(state['weather_results'], 400)}

Make the itinerary practical, budget-aware, and easy to follow.
"""

    itinerary = ask(
        "You are an expert travel planner.",
        prompt,
        max_tokens=450
    )

    tasks, progress = _track(
        state, "build_itinerary", True, "Itinerary generated",
        "Itinerary generated.",
    )

    return {
        "itinerary": itinerary,
        "tasks": tasks,
        "progress": progress,
        "messages": [AIMessage(content=itinerary)],
        "llm_calls": state.get("llm_calls", 0) + 1
    }



# =========================
# Final Response Agent
# =========================

def final_agent(state: TravelState):
    final_prompt = f"""
Generate the final travel response for the user.

User Request:
{clip(state['user_query'], 300)}

Flights:
{clip(state['flight_results'], 600)}

Hotels:
{clip(state['hotel_results'], 600)}

Weather:
{clip(state['weather_results'], 400)}

Itinerary:
{clip(state['itinerary'], 1200)}

Booking Status:
{state.get('booking_status') or 'DRAFT'}

Receipt / Confirmation:
{clip(state.get('receipt', ''), 700)}

Format the final answer beautifully using these sections:

1. Trip Summary
2. Flight Information
3. Hotel Suggestions
4. Weather Information
5. Day-by-Day Itinerary
6. Estimated Budget
7. Booking Status & Receipt (only if a booking exists)
8. Final Recommendations


Important:
- Be clear and practical.
- Mention that live flight API may not provide ticket prices if pricing is unavailable.
- Include weather-based travel advice.
- Keep the response useful for real travel planning.
- Never claim a booking was made unless the Booking Status says BOOKED.
"""

    answer = ask(
        "You are a professional AI travel booking assistant.",
        final_prompt,
        max_tokens=600
    )

    receipt = clip(state.get("receipt", ""), 1500)
    if receipt:
        answer = f"{answer}\n\n---\n\n{receipt}"

    return {
        "messages": [AIMessage(content=answer)],
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# =========================
# Booking workflow (approval-gated)
#
# DRAFT → OPTIONS_READY → AWAITING_USER_APPROVAL → REVALIDATING →
# BOOKING_IN_PROGRESS → BOOKED / BOOKING_FAILED
# Any material change sends the flow back to
# APPROVAL_REQUIRED_AGAIN, and cancellations go through
# CANCEL_PENDING → CANCELLED.
# =========================

BOOK_STATUS = BookingStatus

_STOP_DESTINATION_WORDS = {
    "it", "this", "that", "them", "there", "trip", "vacation", "holiday",
    "please", "booking", "hotel", "flight", "somewhere", "anywhere",
}

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _clean_dates(value) -> str:
    match = _ISO_DATE_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _clean_name(value) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 60:
        return ""
    if text.lower() in _STOP_DESTINATION_WORDS:
        return ""
    return text


def _fallback_plan(query: str) -> dict:
    """Deterministic extraction used when the LLM call fails."""

    incoming = {
        "destination": "",
        "origin": "",
        "start_date": "",
        "end_date": "",
        "travelers": [],
        "budget": None,
        "currency": "",
        "summary": "",
    }

    try:
        destination = _clean_name(extract_destination(query))
        if destination and destination.lower() not in _STOP_DESTINATION_WORDS:
            incoming["destination"] = destination
    except Exception:
        pass

    origin = _extract_origin(query)
    if origin:
        incoming["origin"] = origin

    dates = _ISO_DATE_RE.findall(str(query or ""))
    if dates:
        incoming["start_date"] = dates[0]
        if len(dates) > 1:
            incoming["end_date"] = dates[1]

    names = re.findall(r"\bfor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", str(query or ""))
    incoming["travelers"] = [make_traveler(n) for n in names[:4]]

    budget_match = re.search(
        r"\b(?:under|below|within|budget(?:\s+of)?)\s+([\d][\d,\.]*)",
        str(query or ""),
        re.IGNORECASE,
    )
    if budget_match:
        try:
            incoming["budget"] = float(budget_match.group(1).replace(",", ""))
        except ValueError:
            pass
    if re.search(r"\b(lakh|lakhs|crore|taka|bdt)\b", str(query or ""), re.IGNORECASE):
        incoming["currency"] = "BDT"

    return incoming


def _extract_plan(query: str) -> dict:
    prompt = f"""
Extract trip details from the request. Return JSON only, no prose.

Fields:
- destination (city or country, "")
- origin (departure city or country, "")
- start_date (YYYY-MM-DD or "")
- end_date (YYYY-MM-DD or "")
- travelers: array of {{"name": "Full Name"}} — only real person names
- budget (number or null)
- currency (ISO code like USD/BDT, or "")
- summary (max 12 words)

Today is {datetime.date.today().isoformat()}.
Use "" / null when a detail is not stated. Never invent traveler names.

Request: {query}
"""
    data = ask_json(
        "You extract trip details as strict JSON.",
        prompt,
        max_tokens=260,
        fallback=None,
    ) or {}

    incoming = {
        "destination": _clean_name(data.get("destination"))
        if str(data.get("destination") or "").lower() not in _STOP_DESTINATION_WORDS
        else "",
        "origin": _clean_name(data.get("origin")),
        "start_date": _clean_dates(data.get("start_date")),
        "end_date": _clean_dates(data.get("end_date")),
        "travelers": [],
        "budget": data.get("budget"),
        "currency": str(data.get("currency") or "").strip().upper(),
        "summary": str(data.get("summary") or "").strip()[:120],
    }

    for entry in data.get("travelers") or []:
        name = _clean_name((entry or {}).get("name") if isinstance(entry, dict) else entry)
        if name:
            incoming["travelers"].append(make_traveler(name))

    if not incoming["budget"]:
        incoming["budget"] = None

    fallback = _fallback_plan(query)
    for key, value in fallback.items():
        if not incoming.get(key):
            incoming[key] = value

    return incoming


def plan_agent(state: TravelState):
    """Decompose the goal, keep persistent state, detect missing info."""
    query = state.get("user_query", "")
    records = state.get("booking_records") or []
    intent = detect_intent(query, has_bookings=bool(records))
    plan = state.get("plan") or {}

    if intent.get("keep") and records:
        # "do not cancel" — reassure; never touch the cancellation workflow.
        active = [
            r for r in records
            if r.get("status") in ("confirmed", "pending")
        ]
        closed = [
            r for r in records
            if r.get("status") in ("cancelled", "failed")
        ]
        lines = ["Nothing was cancelled — your bookings are unchanged.", ""]
        for r in active:
            lines.append(
                f"- `{r.get('reference')}` ({r.get('kind')}): "
                f"{r.get('title') or 'booking'} — **{r.get('status')}**"
            )
        for r in closed:
            lines.append(
                f"- `{r.get('reference')}` ({r.get('kind')}): "
                f"{r.get('title') or 'booking'} — {r.get('status')}"
            )
        if not active and not closed:
            lines.append("There are no bookings on this trip yet.")
        return {
            "pending_action": "status",
            "progress": ["Reassured the user — no cancellation was started."],
            "messages": [AIMessage(content="\n".join(lines))],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    if intent.get("cancel"):
        target = next(
            (
                r for r in records
                if r.get("reference") in str(query)
                and r.get("status") in ("confirmed", "pending")
            ),
            None,
        )
        if target is None:
            target = next(
                (r for r in records if r.get("status") in ("confirmed", "pending")),
                None,
            )

        if target:
            plan = dict(plan)
            plan["cancel_intent"] = True
            return {
                "plan": plan,
                "pending_action": "cancel",
                "booking_status": BOOK_STATUS.CANCEL_PENDING.value,
                "tasks": mark_running(build_tasks("cancel"), "review_booking",
                                      f"Booking {target.get('reference')}"),
                "progress": ["Cancellation requested — waiting for your approval."],
                "messages": [AIMessage(content="Cancellation prepared for approval.")],
                "cancel_target": target,
                "llm_calls": state.get("llm_calls", 0) + 1,
            }

    # Never run extraction over a live approval/cancel workflow.
    in_flight = state.get("booking_status") in (
        BOOK_STATUS.AWAITING_USER_APPROVAL.value,
        BOOK_STATUS.CANCEL_PENDING.value,
        BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
        BOOK_STATUS.REVALIDATING.value,
        BOOK_STATUS.BOOKING_IN_PROGRESS.value,
    )
    incoming = {} if in_flight else _extract_plan(query)
    plan = merge_plan(plan, incoming)

    intent = detect_intent(query, has_bookings=bool(records))
    plan["booking_intent"] = bool(plan.get("booking_intent")) or intent.get("booking")

    if plan.get("booking_intent"):
        missing = missing_requirements(plan)
        if missing:
            return {
                "plan": plan,
                "booking_status": BOOK_STATUS.DRAFT.value,
                "pending_action": "ask",
                "tasks": mark_failed(
                    build_tasks("booking"), "parse_request",
                    f"missing: {', '.join(missing)}",
                ),
                "progress": ["Missing trip details — waiting for your reply."],
                "messages": [AIMessage(content=ask_for_missing(missing))],
                "llm_calls": state.get("llm_calls", 0) + 1,
            }
        tasks = build_tasks("booking")
        progress = ["Travel details complete — starting research."]
        status = BOOK_STATUS.DRAFT.value
    else:
        tasks = build_tasks("research")
        progress = ["Understanding your travel goal."]
        status = BOOK_STATUS.DRAFT.value

    return {
        "plan": plan,
        "tasks": mark_done(tasks, "parse_request", "Trip details extracted"),
        "progress": progress,
        "booking_status": status,
        "pending_action": "",
        "messages": [AIMessage(content="Research started.")],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


def options_agent(state: TravelState):
    """Compare available options, quote them, estimate the trip cost."""
    plan = state.get("plan") or {}
    provider = get_provider()
    tasks = state.get("tasks") or build_tasks("research")
    progress = state.get("progress") or []

    raw = ask_json(
        "You extract travel options as strict JSON.",
        f"""
Extract the best options from this research. Return JSON only with top level keys
"flights" (array with airline, route, depart, arrive, duration, stops, fare_brand)
and "hotels" (array with name, area, room_type, board, free_cancellation).
Max 3 flights and 3 hotels. Use empty string for unknown fields.
fare_brand must be Basic, Standard or Flex. free_cancellation is boolean.

Flight research:
{clip(state.get('flight_results', ''), 600)}

Hotel research:
{clip(state.get('hotel_results'), 600)}
""",
        max_tokens=340,
        fallback=None,
    ) or {}

    flights_raw = raw.get("flights") if isinstance(raw.get("flights"), list) else []
    hotels_raw = raw.get("hotels") if isinstance(raw.get("hotels"), list) else []

    if not flights_raw:
        flights_raw = [{
            "airline": "Best available flight",
            "route": f"{plan.get('origin') or '—'} → {plan.get('destination') or '—'}",
            "depart": "",
            "arrive": "",
            "duration": "",
            "stops": "",
            "fare_brand": "Standard",
        }]
    if not hotels_raw:
        hotels_raw = [{
            "name": f"Recommended stay in {plan.get('destination') or 'your destination'}",
            "area": "",
            "room_type": "Standard Double",
            "board": "Room only",
            "free_cancellation": True,
        }]

    travelers = plan.get("travelers") or [make_traveler("Guest")]
    traveler_count = max(len(travelers), 1)

    options = []
    quotes = {}

    for index, item in enumerate(flights_raw[:3], start=1):
        item = item if isinstance(item, dict) else {}
        brand = str(item.get("fare_brand") or "Standard").title()
        if brand not in ("Basic", "Standard", "Flex"):
            brand = "Standard"
        option = make_option(
            option_id=f"fl_{index}",
            kind="flight",
            title=str(item.get("airline") or "Flight option")
            + (f" · {item['route']}" if item.get("route") else ""),
            subtitle=" · ".join(
                p for p in [
                    str(item.get("duration") or ""),
                    f"{item.get('stops')} stop(s)" if item.get("stops") not in ("", None) else "",
                    f"departs {item.get('depart')}" if item.get("depart") else "",
                ] if p
            ),
            details={
                "airline": item.get("airline") or "",
                "route": item.get("route") or "",
                "depart": item.get("depart") or "",
                "arrive": item.get("arrive") or "",
                "duration": item.get("duration") or "",
                "stops": item.get("stops"),
                "fare_brand": brand,
            },
            provider=provider.name,
        )
        quote = provider.quote_flight(option, plan, traveler_count)
        option["quote"] = quote
        options.append(option)
        quotes[option["option_id"]] = quote

    for index, item in enumerate(hotels_raw[:3], start=1):
        item = item if isinstance(item, dict) else {}
        option = make_option(
            option_id=f"ho_{index}",
            kind="hotel",
            title=str(item.get("name") or "Hotel option"),
            subtitle=str(item.get("area") or ""),
            details={
                "area": item.get("area") or "",
                "room_type": item.get("room_type") or "Standard Double",
                "board": item.get("board") or "Room only",
                "free_cancellation": bool(item.get("free_cancellation", True)),
            },
            provider=provider.name,
        )
        quote = provider.quote_hotel(option, plan, traveler_count)
        option["quote"] = quote
        options.append(option)
        quotes[option["option_id"]] = quote

    # Compare: cheapest flight + cheapest hotel, budget noted.
    flight_options = [o for o in options if o["kind"] == "flight"]
    hotel_options = [o for o in options if o["kind"] == "hotel"]
    best_flight = min(flight_options, key=lambda o: o["quote"]["total"]) if flight_options else None
    best_hotel = min(hotel_options, key=lambda o: o["quote"]["total"]) if hotel_options else None

    selected = [o for o in (best_flight, best_hotel) if o]
    purchases = [make_purchase(o["kind"], o, o["quote"]) for o in selected]
    totals = trip_totals(purchases)

    notes = []
    budget = plan.get("budget")
    if budget and totals["total"] and totals["total"] > float(budget):
        notes.append(
            f"Estimated total {totals['total']:.2f} {totals['currency']} exceeds "
            f"the stated budget of {float(budget):.2f} {totals['currency']}."
        )

    return {
        "trip_options": options,
        "purchases": purchases,
        "booking_status": BOOK_STATUS.OPTIONS_READY.value,
        "tasks": mark_done(
            mark_done(tasks, "compare_options", f"{len(options)} options"),
            "estimate_cost",
            f"{totals['total']:.2f} {totals['currency']}",
        ),
        "progress": progress + [
            f"Compared {len(options)} options — estimated total "
            f"{totals['total']:.2f} {totals['currency']}."
        ] + notes,
        "messages": [AIMessage(content="Options compared and priced.")],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


def prepare_approval(state: TravelState):
    """Issue a fresh, tightly-bound approval token per purchase."""
    plan = state.get("plan") or {}
    changes = list(state.get("changes_summary") or [])
    progress = list(state.get("progress") or [])

    pending = [
        p for p in (state.get("purchases") or [])
        if (p.get("decision") or {}).get("action") != "reject"
    ]

    refreshed = []
    for purchase in pending:
        binding = canonical_binding("BOOK", plan, purchase)
        purchase = dict(purchase)
        purchase["approval"] = issue_approval("BOOK", binding)
        purchase["decision"] = None
        refreshed.append(purchase)

    if changes:
        progress = progress + ["Previous approval is stale — asking again."]
    progress = progress + [
        f"Waiting for explicit approval of {len(refreshed)} purchase(s)."
    ]

    return {
        "purchases": refreshed,
        "booking_status": (
            BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value
            if changes
            else BOOK_STATUS.AWAITING_USER_APPROVAL.value
        ),
        "pending_action": "book",
        "changes_summary": changes,
        "tasks": mark_done(
            state.get("tasks") or build_tasks("booking"),
            "prepare_approval",
            f"{len(refreshed)} purchase(s) awaiting approval",
        ),
        "progress": progress,
    }


def approval_gate(state: TravelState):
    """LangGraph interrupt: the graph stops here until the user decides."""
    purchases = state.get("purchases") or []
    plan = state.get("plan") or {}

    payload = {
        "type": "purchase_approval",
        "action": "book",
        "booking_status": BOOK_STATUS.AWAITING_USER_APPROVAL.value,
        "reapproval_required": bool(state.get("changes_summary")),
        "reasons": list(state.get("changes_summary") or []),
        "help": decision_help("book"),
        "plan": {
            "origin": plan.get("origin", ""),
            "destination": plan.get("destination", ""),
            "start_date": plan.get("start_date", ""),
            "end_date": plan.get("end_date", ""),
            "travelers": plan.get("travelers", []),
        },
        "purchases": [
            {
                "purchase_id": p.get("purchase_id"),
                "kind": p.get("kind"),
                "option": p.get("option"),
                "quote": p.get("quote"),
                "approval": p.get("approval"),
            }
            for p in purchases
        ],
        "total": trip_totals(purchases),
        "summary_markdown": describe_selection(purchases, plan),
    }

    decision = interrupt(payload)
    return {"approval_decision": decision}


def _apply_modification(state: TravelState, modification_text: str) -> list:
    """Swap the selection for a different option the user asked for."""
    plan = state.get("plan") or {}
    provider = get_provider()
    options = state.get("trip_options") or []
    current_ids = {
        (p.get("option") or {}).get("option_id")
        for p in (state.get("purchases") or [])
    }
    alternatives = [o for o in options if o.get("option_id") not in current_ids]
    if not alternatives:
        return state.get("purchases") or []

    picked = ask_json(
        "You pick travel options as strict JSON.",
        f"""
Pick options that satisfy the user's modification request.
Return JSON only: {{"option_ids": ["..."]}} with up to one flight and one hotel.

Request: {modification_text}
Options:
""" + clip(
            "\n".join(
                f"- {o['option_id']} | {o['kind']} | {o['title']} | "
                f"{o.get('quote', {}).get('total')} {o.get('quote', {}).get('currency')}"
                for o in alternatives
            ),
            700,
        ),
        max_tokens=120,
        fallback=None,
    ) or {}

    allowed = {o["option_id"] for o in alternatives}
    chosen_ids = [
        cid for cid in (picked.get("option_ids") or []) if cid in allowed
    ]

    chosen = [o for o in alternatives if o["option_id"] in chosen_ids]
    has_flight = any(o["kind"] == "flight" for o in chosen)
    has_hotel = any(o["kind"] == "hotel" for o in chosen)
    for option in alternatives:
        if option["kind"] == "flight" and not has_flight:
            chosen.append(option)
            has_flight = True
        elif option["kind"] == "hotel" and not has_hotel:
            chosen.append(option)
            has_hotel = True
        if has_flight and has_hotel:
            break

    travelers = plan.get("travelers") or [make_traveler("Guest")]
    purchases = []
    for option in chosen[:2]:
        if option["kind"] == "flight":
            quote = provider.quote_flight(option, plan, len(travelers))
        else:
            quote = provider.quote_hotel(option, plan, len(travelers))
        option = dict(option)
        option["quote"] = quote
        purchases.append(make_purchase(option["kind"], option, quote))
    return purchases


def process_decision(state: TravelState):
    """Validate the resumed approval against the binding it was issued for."""
    decision = state.get("approval_decision") or {}
    plan = state.get("plan") or {}
    purchases = state.get("purchases") or []
    action = decision.get("action")
    progress = list(state.get("progress") or [])

    if action == "modify":
        modified = _apply_modification(state, decision.get("modifications") or "")
        progress.append("Selection updated from your modification request.")
        return {
            "purchases": modified,
            "booking_status": BOOK_STATUS.OPTIONS_READY.value,
            "pending_action": "modified",
            "changes_summary": [],
            "progress": progress,
            "messages": [AIMessage(content="Selection updated — re-issuing approval.")],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    if action == "unrecognized":
        return {
            "booking_status": BOOK_STATUS.AWAITING_USER_APPROVAL.value,
            "progress": progress + ["Waiting for a clear approve / reject / modify reply."],
        }

    decisions = dict(decision.get("decisions") or {})
    if action == "approve_all":
        decisions = {
            (p.get("approval") or {}).get("approval_id"): True for p in purchases
        }
    elif action == "reject_all":
        decisions = {
            (p.get("approval") or {}).get("approval_id"): False for p in purchases
        }

    approved = []
    stale = []
    rejected = 0

    for purchase in purchases:
        approval = purchase.get("approval") or {}
        approval_id = approval.get("approval_id")
        granted = bool(decisions.get(approval_id))

        if not granted:
            rejected += 1
            progress.append(
                f"Rejected: {(purchase.get('option') or {}).get('title', purchase.get('kind'))}"
            )
            continue

        binding = canonical_binding("BOOK", plan, purchase)
        valid, reason = validate_approval(approval, binding)
        if not valid:
            stale.append(reason)
            approved.append(purchase)
            continue

        purchase = dict(purchase)
        purchase["approval"] = grant_approval(approval)
        purchase["decision"] = {"action": "approve", "at": datetime.datetime.now().isoformat()}
        approved.append(purchase)

    if stale:
        reasons = sorted(set(stale))
        progress.append("Approval no longer valid — requesting approval again.")
        return {
            "purchases": approved,
            "booking_status": BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
            "changes_summary": reasons,
            "progress": progress,
            "messages": [AIMessage(content="Approval requires renewal.")],
        }

    if not approved:
        return {
            "purchases": [],
            "booking_status": BOOK_STATUS.OPTIONS_READY.value,
            "pending_action": "",
            "changes_summary": [],
            "progress": progress + ["All purchases rejected — nothing will be booked."],
            "messages": [AIMessage(content="Selections declined.")],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    progress.append(f"{len(approved)} purchase(s) approved by the user.")
    return {
        "purchases": approved,
        "booking_status": BOOK_STATUS.OPTIONS_READY.value,
        "pending_action": "approved",
        "changes_summary": [],
        "progress": progress,
    }


def revalidate_agent(state: TravelState):
    """Re-check live price/availability immediately before booking."""
    provider = get_provider()
    purchases = state.get("purchases") or []
    plan = state.get("plan") or {}
    progress = list(state.get("progress") or [])
    tasks = state.get("tasks") or build_tasks("booking")
    tasks = mark_running(tasks, "revalidate")

    updated = []
    reapproval_reasons = []
    dropped = 0

    for purchase in purchases:
        old_quote = purchase.get("quote") or {}
        try:
            result = with_retries(lambda p=old_quote: provider.revalidate(p))
        except ProviderError as error:
            progress.append(f"Live revalidation failed: {error}")
            return {
                "booking_status": BOOK_STATUS.BOOKING_FAILED.value,
                "tasks": mark_failed(tasks, "revalidate", str(error)),
                "progress": progress,
                "changes_summary": [
                    "Live availability could not be verified. No purchase was made."
                ],
                "messages": [
                    AIMessage(
                        content=(
                            "I could not revalidate live price and availability, "
                            "so I stopped before booking. Nothing was charged. "
                            "Reply 'retry' to try again."
                        )
                    )
                ],
                "llm_calls": state.get("llm_calls", 0) + 1,
            }

        new_quote = result.get("quote") or old_quote
        changes = material_changes(old_quote, new_quote)
        if not result.get("available", True):
            changes.append("The provider reports this option as unavailable.")

        purchase = dict(purchase)
        purchase["revalidation"] = {
            "checked_at": result.get("checked_at"),
            "available": bool(result.get("available", True)),
            "changes": changes,
            "quote": new_quote,
        }

        if changes:
            # Material change: adopt the live quote and force re-approval.
            purchase["quote"] = new_quote
            purchase["option"] = dict(
                purchase.get("option") or {}, quote=new_quote
            )
            reapproval_reasons.extend(changes)
            progress.append(
                "Material change detected — re-approval required: "
                + " ".join(changes)
            )
        else:
            # Immaterial drift: the approval still stands; the live quote is
            # kept separately and used for the actual booking.
            progress.append(
                f"Price confirmed at {float(new_quote.get('total') or 0):.2f} "
                f"{new_quote.get('currency', '')} (within tolerance)."
            )
        updated.append(purchase)

    if reapproval_reasons:
        return {
            "purchases": updated,
            "booking_status": BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
            "changes_summary": sorted(set(reapproval_reasons)),
            "tasks": mark_failed(tasks, "revalidate", "material change"),
            "progress": progress,
        }

    progress.append("Live price and availability revalidated.")
    return {
        "purchases": updated,
        "booking_status": BOOK_STATUS.REVALIDATING.value,
        "tasks": mark_done(tasks, "revalidate", "Live check passed"),
        "progress": progress,
    }


def execute_booking_agent(state: TravelState):
    """Spend money — only ever reached with a valid, granted approval."""
    provider = get_provider()
    plan = state.get("plan") or {}
    purchases = state.get("purchases") or []
    travelers = plan.get("travelers") or [make_traveler("Guest")]
    progress = list(state.get("progress") or [])
    tasks = state.get("tasks") or build_tasks("booking")

    # Defense in depth: re-check every approval immediately before spending.
    for purchase in purchases:
        approval = purchase.get("approval") or {}
        binding = canonical_binding("BOOK", plan, purchase)
        valid, reason = validate_approval(approval, binding)
        if not valid or not approval.get("granted_at"):
            progress.append(f"Approval invalid before booking: {reason}")
            return {
                "booking_status": BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
                "changes_summary": [reason or "Approval missing."],
                "tasks": tasks,
                "progress": progress,
                "messages": [AIMessage(content="Approval required again.")],
            }

        live_quote = (purchase.get("revalidation") or {}).get("quote") or {}
        drift = material_changes(purchase.get("quote") or {}, live_quote)
        if drift:
            progress.append("Live price moved materially before booking.")
            return {
                "booking_status": BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
                "changes_summary": drift,
                "tasks": tasks,
                "progress": progress,
                "messages": [AIMessage(content="Approval required again.")],
            }

    tasks = mark_running(tasks, "execute_booking")
    updated = []
    failures = 0

    for purchase in purchases:
        approval_id = (purchase.get("approval") or {}).get("approval_id")
        purchase = dict(purchase)
        # Book at the live revalidated price; the approved price sits inside
        # the materiality tolerance, otherwise we would not be here.
        quote = (purchase.get("revalidation") or {}).get("quote") or (
            purchase.get("quote") or {}
        )
        purchase["booked_quote"] = quote
        try:
            result = with_retries(
                lambda q=quote, a=approval_id: provider.create_booking(
                    q, travelers, a
                )
            )
        except ProviderError as error:
            failures += 1
            purchase["result"] = {"success": False, "error": str(error)}
            progress.append(f"Provider rejected the purchase: {error}")
        else:
            purchase["result"] = result
            if not result.get("success"):
                failures += 1
                progress.append("Provider did not confirm the purchase.")
        updated.append(purchase)

    return {
        "purchases": updated,
        "booking_status": BOOK_STATUS.BOOKING_IN_PROGRESS.value,
        "tasks": tasks,
        "progress": progress + [f"Executed {len(updated) - failures} purchase(s)."],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


def verify_booking_agent(state: TravelState):
    """Confirm the real booking status from the provider response."""
    provider = get_provider()
    purchases = state.get("purchases") or []
    progress = list(state.get("progress") or [])
    tasks = state.get("tasks") or build_tasks("booking")

    verified_ok = 0
    updated = []

    for purchase in purchases:
        result = purchase.get("result") or {}
        reference = result.get("reference")
        purchase = dict(purchase)

        if result.get("success") and reference:
            try:
                status = with_retries(lambda ref=reference: provider.get_booking_status(ref))
            except ProviderError as error:
                status = {"reference": reference, "status": "unknown", "raw": {"error": str(error)}}
            purchase["verified"] = status
            ok = str(status.get("status", "")).lower() in {
                "confirmed", "ok", "booked", "ticketed", "successful", "success",
            }
            if ok:
                verified_ok += 1
                progress.append(f"Verified booking {reference} with the provider.")
            else:
                progress.append(
                    f"Provider could not confirm {reference} "
                    f"(status: {status.get('status')})."
                )
        updated.append(purchase)

    all_ok = verified_ok == len(purchases) and len(purchases) > 0
    tasks = mark_done(
        mark_done(
            tasks,
            "execute_booking",
            f"{verified_ok}/{len(purchases)} confirmed",
        ),
        "verify_booking",
        "Provider responses checked",
    )

    status = (
        BOOK_STATUS.BOOKED.value
        if all_ok
        else BOOK_STATUS.BOOKING_FAILED.value
    )
    if not all_ok and verified_ok:
        progress.append(
            f"{verified_ok} of {len(purchases)} purchase(s) confirmed; "
            "the rest failed and were not booked."
        )
    elif not all_ok:
        progress.append("No purchase could be confirmed.")

    return {
        "purchases": updated,
        "booking_status": status,
        "tasks": tasks,
        "progress": progress,
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


def receipt_agent(state: TravelState):
    """Persist booking references and build the receipt."""
    purchases = state.get("purchases") or []
    plan = state.get("plan") or {}
    thread_id = state.get("thread_id") or "unknown"
    records = list(state.get("booking_records") or [])
    lines = [
        "### Booking Receipt",
        "",
        f"**Traveler(s):** "
        + (", ".join(t.get("name", "") for t in plan.get("travelers") or []) or "—"),
        f"**Dates:** {plan.get('start_date') or '—'} → {plan.get('end_date') or '—'}",
        "",
        "| Item | Reference | Status | Paid |",
        "| --- | --- | --- | ---: |",
    ]

    total = 0.0
    currency = ""
    for purchase in purchases:
        result = purchase.get("result") or {}
        verified = purchase.get("verified") or {}
        quote = purchase.get("booked_quote") or purchase.get("quote") or {}
        if not result.get("success"):
            continue
        reference = result.get("reference", "")
        currency = quote.get("currency") or currency
        amount = float(quote.get("total") or 0)
        total += amount
        label = (
            "Flight" if purchase.get("kind") == "flight" else "Hotel"
        ) + f": {(purchase.get('option') or {}).get('title', '')}"
        lines.append(
            f"| {label} | `{reference}` | "
            f"{verified.get('status') or result.get('status')} | "
            f"{amount:.2f} {quote.get('currency', '')} |"
        )
        record = new_record(
            purchase.get("kind") or "booking",
            reference,
            status="confirmed",
            approval_id=(purchase.get("approval") or {}).get("approval_id"),
            provider=quote.get("provider") or "",
            title=(purchase.get("option") or {}).get("title", ""),
            quote=quote,
            plan_dates={
                "start_date": plan.get("start_date", ""),
                "end_date": plan.get("end_date", ""),
            },
            travelers=plan.get("travelers") or [],
        )
        if not any(r.get("reference") == reference for r in records):
            records.append(record)
            _save_booking_record(thread_id, record)

    lines += ["", f"**Total paid:** {total:.2f} {currency}".rstrip()]
    receipt = "\n".join(lines)

    tasks = mark_done(
        state.get("tasks") or build_tasks("booking"),
        "save_receipt",
        f"{len(records)} record(s) stored",
    )

    return {
        "booking_records": records,
        "receipt": receipt,
        "tasks": tasks,
        "progress": (state.get("progress") or []) + ["Receipt saved."],
    }


# ------------------------
# Cancellation flow
# ------------------------

def prepare_cancel(state: TravelState):
    target = state.get("cancel_target") or {}
    provider = get_provider()

    quote = target.get("quote") or {}
    pseudo_purchase = {
        "kind": target.get("kind") or "booking",
        "option": {
            "option_id": target.get("reference") or "",
            "title": target.get("title") or target.get("reference") or "Booking",
        },
        "quote": quote,
    }
    binding = canonical_binding("CANCEL", state.get("plan") or {}, pseudo_purchase,
                                extra={"reference": target.get("reference")})
    approval = issue_approval("CANCEL", binding)
    estimate = provider.cancellation_estimate(quote) if quote else {}

    return {
        "pending_action": "cancel",
        "booking_status": BOOK_STATUS.CANCEL_PENDING.value,
        "tasks": mark_done(
            mark_running(build_tasks("cancel"), "cancel_approval"),
            "review_booking",
            target.get("reference", ""),
        ),
        "progress": (state.get("progress") or [])
        + ["Cancellation prepared — waiting for your approval."],
        "cancel_context": {
            "target": target,
            "approval": approval,
            "estimate": estimate,
        },
    }


def cancel_gate(state: TravelState):
    context = state.get("cancel_context") or {}
    target = context.get("target") or {}
    payload = {
        "type": "cancellation_approval",
        "action": "cancel",
        "booking_status": BOOK_STATUS.CANCEL_PENDING.value,
        "reapproval_required": False,
        "reasons": [],
        "help": decision_help("cancel"),
        "booking": target,
        "estimate": context.get("estimate") or {},
        "approval": context.get("approval"),
    }
    decision = interrupt(payload)
    return {"approval_decision": decision}


def process_cancel_decision(state: TravelState):
    decision = state.get("approval_decision") or {}
    context = state.get("cancel_context") or {}
    approval = context.get("approval") or {}
    action = decision.get("action")
    progress = list(state.get("progress") or [])

    approved = action == "approve_all" or bool(
        (decision.get("decisions") or {}).get(approval.get("approval_id"))
    )

    if action == "unrecognized" or action == "modify":
        return {
            "booking_status": BOOK_STATUS.CANCEL_PENDING.value,
            "progress": progress + ["Waiting for a clear cancellation decision."],
        }

    if not approved:
        return {
            "booking_status": BOOK_STATUS.BOOKED.value,
            "pending_action": "",
            "progress": progress + ["Cancellation declined — booking kept."],
            "messages": [AIMessage(content="Cancellation declined. Your booking stays active.")],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    plan = state.get("plan") or {}
    target = context.get("target") or {}
    pseudo_purchase = {
        "kind": target.get("kind") or "booking",
        "option": {
            "option_id": target.get("reference") or "",
            "title": target.get("title") or target.get("reference") or "Booking",
        },
        "quote": target.get("quote") or {},
    }
    expected_binding = canonical_binding(
        "CANCEL", plan, pseudo_purchase,
        extra={"reference": target.get("reference")},
    )
    valid, reason = validate_approval(approval, expected_binding)

    if not valid:
        return {
            "booking_status": BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
            "changes_summary": [reason or "Cancellation approval is stale."],
            "progress": progress + ["Cancellation approval stale — re-issuing."],
        }

    return {
        "cancel_context": {**context, "approval": grant_approval(approval)},
        "booking_status": BOOK_STATUS.CANCEL_PENDING.value,
        "progress": progress + ["Cancellation approved by the user."],
    }


def execute_cancel_agent(state: TravelState):
    provider = get_provider()
    context = state.get("cancel_context") or {}
    target = context.get("target") or {}
    approval = context.get("approval") or {}

    if not approval.get("granted_at"):
        return {
            "booking_status": BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value,
            "changes_summary": ["Cancellation approval missing."],
            "messages": [AIMessage(content="Cancellation approval required.")],
        }

    quote = target.get("quote") or {}
    reference = target.get("reference") or ""
    try:
        result = with_retries(lambda: provider.cancel_booking(quote, reference))
    except ProviderError as error:
        return {
            "booking_status": BOOK_STATUS.BOOKING_FAILED.value,
            "progress": (state.get("progress") or [])
            + [f"Cancellation failed: {error}"],
            "messages": [
                AIMessage(
                    content=f"The provider rejected the cancellation: {error}. "
                    "Your booking is unchanged."
                )
            ],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    context = dict(context)
    context["cancel_result"] = result
    return {
        "cancel_context": context,
        "booking_status": BOOK_STATUS.CANCEL_PENDING.value,
        "progress": (state.get("progress") or []) + ["Cancellation sent to the provider."],
    }


def verify_cancel_agent(state: TravelState):
    provider = get_provider()
    context = state.get("cancel_context") or {}
    target = context.get("target") or {}
    result = context.get("cancel_result") or {}
    reference = target.get("reference") or ""
    thread_id = state.get("thread_id") or "unknown"

    try:
        status = with_retries(lambda: provider.get_booking_status(reference))
    except ProviderError as error:
        status = {"reference": reference, "status": "unknown", "raw": {"error": str(error)}}

    confirmed = str(status.get("status", "")).lower() in {
        "cancelled", "canceled", "void", "refunded",
    } or str(result.get("status", "")).lower() in {"cancelled", "canceled"}

    records = list(state.get("booking_records") or [])
    for index, record in enumerate(records):
        if record.get("reference") == reference:
            updated = dict(record)
            updated["status"] = "cancelled" if confirmed else "confirmed"
            updated["cancellation"] = result
            records[index] = updated
            _save_booking_record(thread_id, updated)

    tasks = mark_done(
        mark_done(state.get("tasks") or build_tasks("cancel"),
                  "execute_cancel", result.get("status", "")),
        "verify_cancel",
        status.get("status", ""),
    )

    receipt = ""
    if confirmed:
        receipt = (
            "### Cancellation Receipt\n\n"
            f"- **Booking:** `{reference}`\n"
            f"- **Status:** CANCELLED\n"
            f"- **Cancellation fee:** {result.get('cancellation_fee', 0):.2f} "
            f"{result.get('currency', '')}\n"
            f"- **Refund due:** {result.get('refund', 0):.2f} "
            f"{result.get('currency', '')}\n"
            f"- **Confirmation:** "
            f"{(result.get('raw') or {}).get('confirmation', reference)}\n"
        )

    status_value = (
        BOOK_STATUS.CANCELLED.value if confirmed else BOOK_STATUS.BOOKED.value
    )
    message = (
        "Cancellation confirmed by the provider. "
        f"Refund due: {result.get('refund', 0):.2f} {result.get('currency', '')} "
        f"(fee {result.get('cancellation_fee', 0):.2f})."
        if confirmed
        else "The provider did not confirm the cancellation — your booking is still active."
    )

    return {
        "booking_status": status_value,
        "booking_records": records,
        "receipt": receipt or state.get("receipt", ""),
        "tasks": tasks,
        "pending_action": "",
        "progress": (state.get("progress") or []) + [message],
        "messages": [AIMessage(content=message)],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }


# ------------------------
# Routers
# ------------------------

def route_after_options(state: TravelState) -> str:
    if (state.get("plan") or {}).get("booking_intent"):
        return "approval"
    return "final"


def route_after_decision(state: TravelState) -> str:
    status = state.get("booking_status")
    if status == BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value:
        return "prepare"
    if state.get("pending_action") == "modified":
        return "prepare"          # new selection → fresh approval round
    if not state.get("purchases"):
        return "final"            # everything rejected
    return "revalidate"


def route_after_revalidate(state: TravelState) -> str:
    status = state.get("booking_status")
    if status == BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value:
        return "prepare"
    if status == BOOK_STATUS.BOOKING_FAILED.value:
        return "final"
    if not state.get("purchases"):
        return "final"
    return "execute"


def route_after_execute(state: TravelState) -> str:
    if state.get("booking_status") == BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value:
        return "prepare"
    return "verify"


def route_after_verify(state: TravelState) -> str:
    if state.get("booking_status") == BOOK_STATUS.BOOKED.value:
        return "receipt"
    return "final"


def route_after_cancel_decision(state: TravelState) -> str:
    status = state.get("booking_status")
    if status == BOOK_STATUS.APPROVAL_REQUIRED_AGAIN.value:
        return "prepare_cancel"
    if status == BOOK_STATUS.CANCEL_PENDING.value:
        return "execute_cancel"
    return "final"




# =========================
# Booking & Payment links
# =========================

DEFAULT_ORIGIN_IATA = (os.getenv("DEFAULT_ORIGIN_IATA") or "").strip().upper()

_city_to_iata = None


def _airport_index():
    global _city_to_iata
    if _city_to_iata is None:
        index = {}
        try:
            import airportsdata

            for code, info in airportsdata.load("IATA").items():
                city = str(info.get("city") or "").strip().lower()
                if city and city not in index:
                    index[city] = str(code).upper()
        except Exception as error:
            print(f"[booking] airport index unavailable: {error}")
        _city_to_iata = index
    return _city_to_iata


def _iata_for(place):
    """Resolve a city/country name to an IATA code (best effort)."""
    key = str(place or "").strip().lower()
    if not key:
        return None

    index = _airport_index()
    if key in index:
        return index[key]

    if len(key) >= 3:
        for city, code in index.items():
            if key in city or (len(city) >= 3 and city in key):
                return code
    return None


def _extract_origin(query):
    """Pull the 'from <place>' phrase out of the user query."""
    import re

    match = re.search(
        r"\bfrom\s+([A-Za-z][A-Za-z' -]{0,40}?)"
        r"(?=\s+(?:with|and|including|under|for|by|via|,)|[,.\n]|$)",
        str(query or ""),
        re.IGNORECASE,
    )
    if not match:
        return None

    phrase = match.group(1).strip()
    words = phrase.split()

    # Longest phrase first: "Dhaka, Bangladesh" > "Dhaka" > "Bangladesh"
    for size in range(min(len(words), 3), 0, -1):
        candidate = " ".join(words[:size])
        if _iata_for(candidate):
            return candidate
    return phrase


def build_booking_links(query):
    """
    Build direct booking/payment links for flights and hotels.
    Payment happens on each provider's own secure checkout page.
    """
    destination = (extract_destination(query) or "").strip() or str(query).strip()
    origin = _extract_origin(query)

    dest_code = _iata_for(destination)
    origin_code = _iata_for(origin) or DEFAULT_ORIGIN_IATA or None

    dest_q = quote_plus(destination)
    route_q = quote_plus(
        f"Flights to {destination} from {origin}" if origin else f"Flights to {destination}"
    )

    flights = [
        {
            "name": "Google Flights",
            "url": f"https://www.google.com/travel/flights?q={route_q}",
        }
    ]
    if origin_code and dest_code and origin_code != dest_code:
        flights.append({
            "name": "Skyscanner",
            "url": (
                "https://www.skyscanner.net/transport/flights/"
                f"{origin_code.lower()}/{dest_code.lower()}/"
            ),
        })
        flights.append({
            "name": "Kayak",
            "url": f"https://www.kayak.com/flights/{origin_code}-{dest_code}?sort=bestflight_a",
        })
        flights.append({
            "name": "Trip.com",
            "url": (
                "https://www.trip.com/flights/showfarefirst"
                f"?acity={dest_code}&dcity={origin_code}"
            ),
        })

    hotels = [
        {
            "name": "Google Hotels",
            "url": f"https://www.google.com/travel/hotels/{dest_q}",
        },
        {
            "name": "Booking.com",
            "url": f"https://www.booking.com/searchresults.html?ss={dest_q}",
        },
        {
            "name": "Expedia",
            "url": f"https://www.expedia.com/Hotel-Search?destination={dest_q}",
        },
        {
            "name": "Airbnb",
            "url": f"https://www.airbnb.com/s/{dest_q}/homes",
        },
    ]

    return {
        "destination": destination,
        "origin": origin or "",
        "flights": flights,
        "hotels": hotels,
    }


def booking_markdown(booking):
    """Append a Booking & Payment section to the final answer."""
    if not booking:
        return ""

    route = (
        f"**Route:** {booking['origin']} ✈ {booking['destination']}"
        if booking.get("origin")
        else f"**Destination:** {booking.get('destination', '—')}"
    )

    flight_links = " · ".join(
        f"[{link['name']}]({link['url']})" for link in booking.get("flights", [])
    )
    hotel_links = " · ".join(
        f"[{link['name']}]({link['url']})" for link in booking.get("hotels", [])
    )

    return f"""

---

## 🔗 Booking & Payment

{route}

**Book flights:** {flight_links}

**Book hotels:** {hotel_links}

> 🔒 You are redirected to the provider's secure checkout page to pay.
> TripMate never collects or stores your card details.
"""


# =========================
# Build Graph
# =========================

def route_after_plan(state: TravelState) -> str:
    action = state.get("pending_action")
    if action == "cancel":
        return "cancel"
    if action == "ask":
        return "ask"
    if action == "status":
        return "status"
    return "research"


graph = StateGraph(TravelState)

graph.add_node("plan_agent", plan_agent)
graph.add_node("flight_agent", flight_agent)
graph.add_node("hotel_agent", hotel_agent)
graph.add_node("weather_agent", weather_agent)
graph.add_node("itinerary_agent", itinerary_agent)
graph.add_node("options_agent", options_agent)
graph.add_node("prepare_approval", prepare_approval)
graph.add_node("approval_gate", approval_gate)
graph.add_node("process_decision", process_decision)
graph.add_node("revalidate_agent", revalidate_agent)
graph.add_node("execute_booking_agent", execute_booking_agent)
graph.add_node("verify_booking_agent", verify_booking_agent)
graph.add_node("receipt_agent", receipt_agent)
graph.add_node("prepare_cancel", prepare_cancel)
graph.add_node("cancel_gate", cancel_gate)
graph.add_node("process_cancel_decision", process_cancel_decision)
graph.add_node("execute_cancel_agent", execute_cancel_agent)
graph.add_node("verify_cancel_agent", verify_cancel_agent)
graph.add_node("final_agent", final_agent)

graph.add_edge(START, "plan_agent")

graph.add_conditional_edges(
    "plan_agent",
    route_after_plan,
    {
        "cancel": "prepare_cancel",
        "ask": END,
        "status": END,
        "research": "flight_agent",
    },
)

graph.add_edge("flight_agent", "hotel_agent")
graph.add_edge("hotel_agent", "weather_agent")
graph.add_edge("weather_agent", "itinerary_agent")
graph.add_edge("itinerary_agent", "options_agent")

graph.add_conditional_edges(
    "options_agent",
    route_after_options,
    {
        "approval": "prepare_approval",
        "final": "final_agent",
    },
)

# Approval gate: interrupt → decision → revalidate / re-approve / final
graph.add_edge("prepare_approval", "approval_gate")
graph.add_edge("approval_gate", "process_decision")
graph.add_conditional_edges(
    "process_decision",
    route_after_decision,
    {
        "prepare": "prepare_approval",
        "revalidate": "revalidate_agent",
        "final": "final_agent",
    },
)

graph.add_conditional_edges(
    "revalidate_agent",
    route_after_revalidate,
    {
        "prepare": "prepare_approval",
        "execute": "execute_booking_agent",
        "final": "final_agent",
    },
)

graph.add_conditional_edges(
    "execute_booking_agent",
    route_after_execute,
    {
        "prepare": "prepare_approval",
        "verify": "verify_booking_agent",
    },
)

graph.add_conditional_edges(
    "verify_booking_agent",
    route_after_verify,
    {
        "receipt": "receipt_agent",
        "final": "final_agent",
    },
)

graph.add_edge("receipt_agent", "final_agent")

# Cancellation gate
graph.add_edge("prepare_cancel", "cancel_gate")
graph.add_edge("cancel_gate", "process_cancel_decision")
graph.add_conditional_edges(
    "process_cancel_decision",
    route_after_cancel_decision,
    {
        "prepare_cancel": "prepare_cancel",
        "execute_cancel": "execute_cancel_agent",
        "final": "final_agent",
    },
)
graph.add_edge("execute_cancel_agent", "verify_cancel_agent")
graph.add_edge("verify_cancel_agent", "final_agent")

graph.add_edge("final_agent", END)


# =========================
# PostgreSQL Checkpointer
# =========================
DATABASE_URL = get_database_url()

_conn = psycopg.connect(
    DATABASE_URL,
    autocommit=True,
    row_factory=dict_row
)

# Guard: never let the checkpointer write non-ASCII payloads (e.g. \u202f)
# into a database whose encoding can't represent them.
with _conn.cursor() as _cur:
    _cur.execute("SHOW server_encoding")
    _server_encoding = _cur.fetchone()["server_encoding"]
    if _server_encoding.upper() != "UTF8":
        raise RuntimeError(
            f"Database encoding is {_server_encoding!r}, but UTF8 is required "
            f"to store chat/itinerary text. Recreate the database with "
            f"ENCODING 'UTF8' and update DATABASE_URL."
        )
    _cur.execute("SET client_encoding TO 'UTF8'")

checkpointer = PostgresSaver(_conn)
checkpointer.setup()

# Durable booking log (references + receipts survive restarts).
with _conn.cursor() as _cur:
    _cur.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_records (
            record_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL,
            status TEXT NOT NULL,
            provider TEXT,
            title TEXT,
            approval_id TEXT,
            details JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _cur.execute(
        "CREATE INDEX IF NOT EXISTS booking_records_thread_idx "
        "ON booking_records (thread_id)"
    )

travel_graph = graph.compile(checkpointer=checkpointer)

# Booking records use their OWN connection: the checkpointer shares _conn
# across worker threads and psycopg forbids mixing pipeline mode with
# concurrent cursors on one connection.
_booking_conn = psycopg.connect(
    DATABASE_URL,
    autocommit=True,
    row_factory=dict_row
)
_booking_conn_lock = threading.Lock()


def _save_booking_record(thread_id: str, record: dict):
    try:
        with _booking_conn_lock, _booking_conn.cursor() as _cur:
            _cur.execute(
                """
                INSERT INTO booking_records
                    (record_id, thread_id, kind, reference, status,
                     provider, title, approval_id, details, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (record_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    details = EXCLUDED.details,
                    updated_at = now()
                """,
                (
                    record.get("record_id"),
                    thread_id,
                    record.get("kind") or "booking",
                    record.get("reference") or "",
                    record.get("status") or "confirmed",
                    record.get("provider") or "",
                    record.get("title") or "",
                    record.get("approval_id") or "",
                    json.dumps(record, default=str),
                ),
            )
    except Exception as error:
        # A storage hiccup must never roll back a live booking.
        print(f"[booking] could not persist record: {error}")


def load_booking_records(thread_id: str) -> list:
    try:
        with _booking_conn_lock, _booking_conn.cursor() as _cur:
            _cur.execute(
                "SELECT record_id, kind, reference, status, provider, title, "
                "approval_id, details FROM booking_records "
                "WHERE thread_id = %s ORDER BY created_at",
                (thread_id,),
            )
            rows = _cur.fetchall()
    except Exception as error:
        print(f"[booking] could not load records: {error}")
        return []

    records = []
    for row in rows:
        details = row.get("details") or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                details = {}
        record = dict(details)
        record["record_id"] = row.get("record_id")
        record["kind"] = row.get("kind")
        record["reference"] = row.get("reference")
        record["status"] = row.get("status")
        record["provider"] = row.get("provider")
        record["title"] = row.get("title")
        record["approval_id"] = row.get("approval_id")
        records.append(record)
    return records



# =========================
# Function for FastAPI
# =========================

# Serialize runs so rapid clicks (e.g. Dubai/Thailand/Global buttons)
# can't stack LLM calls into the same 8000 TPM window.
_travel_run_lock = threading.Lock()


def _current_interrupt(values: dict):
    raw = (values or {}).get("__interrupt__") or ()
    if not raw:
        return None
    last = raw[-1]
    return getattr(last, "value", last if isinstance(last, dict) else None)


def _pending_purchases(values: dict) -> list:
    purchases = list((values or {}).get("purchases") or [])
    if not purchases and (values or {}).get("cancel_context"):
        approval = (values["cancel_context"].get("approval") or {})
        if approval:
            purchases = [{"approval": approval}]
    return purchases


def _fmt_ts(value) -> str:
    if not value:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(float(value)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError, OSError):
        return "—"


def _approval_markdown(payload: dict) -> str:
    if not payload:
        return ""

    if payload.get("type") == "cancellation_approval":
        booking = payload.get("booking") or {}
        estimate = payload.get("estimate") or {}
        approval = payload.get("approval") or {}
        return "\n".join([
            "## ⚠️ Cancellation awaiting your approval",
            "",
            f"**Booking:** `{booking.get('reference', '—')}` — "
            f"{booking.get('title', booking.get('kind', ''))}",
            f"**Provider:** {booking.get('provider') or '—'}",
            f"**Originally paid:** "
            f"{float(estimate.get('amount_paid') or 0):.2f} "
            f"{estimate.get('currency', '')}",
            f"**Cancellation fee:** "
            f"{float(estimate.get('cancellation_fee') or 0):.2f} "
            f"{estimate.get('currency', '')}",
            f"**Refund due:** "
            f"{float(estimate.get('refund') or 0):.2f} "
            f"{estimate.get('currency', '')}",
            "",
            f"> Approval `{approval.get('approval_id', '')}` expires at "
            f"{_fmt_ts(approval.get('expires_at'))}.",
            "",
            "> Reply **\"approve cancellation\"** to cancel this booking or "
            "**\"keep my booking\"** to back out. Nothing happens without "
            "your explicit approval.",
        ])

    purchases = payload.get("purchases") or []
    first_approval = (purchases[0].get("approval") or {}) if purchases else {}

    lines = ["## 🔒 Approval required — nothing has been purchased yet", ""]

    if payload.get("reapproval_required"):
        lines += ["### ⚠️ Approval required again", ""]
        for reason in payload.get("reasons") or []:
            lines.append(f"- {reason}")
        lines += [
            "",
            "The details you approved changed, so that approval is now "
            "invalid and has expired. Confirm the updated selection to "
            "continue.",
            "",
        ]

    lines.append(payload.get("summary_markdown") or "")

    total = payload.get("total") or {}
    lines += [
        "",
        f"### Estimated trip total: **{float(total.get('total') or 0):.2f} "
        f"{total.get('currency', '')}**",
        "",
        f"> Approval `{first_approval.get('approval_id', '')}` expires at "
        f"{_fmt_ts(first_approval.get('expires_at'))}.",
        "",
        "Reply with:",
        "- **\"approve\"** — authorize every listed purchase",
        "- **\"approve only the flight\"** / **\"approve only the hotel\"**",
        "- **\"modify ...\"** — change the selection",
        "- **\"reject\"** — discard the selections",
        "",
        "> Every approval is bound to this exact traveler, itinerary, "
        "provider, price, dates and fare/room conditions — and expires if "
        "any of them change.",
    ]
    return "\n".join(lines)


def _booking_cta(result: dict) -> str:
    status = result.get("booking_status") or ""
    if status in (BookingStatus.BOOKED.value, BookingStatus.CANCELLED.value):
        return ""
    totals = trip_totals(result.get("purchases") or [])
    if not totals["total"]:
        return ""
    return (
        "\n\n---\n\n**Estimated total: "
        f"{totals['total']:.2f} {totals['currency']}**\n\n"
        "Say **\"book it\"** when you want these selections prepared for "
        "your explicit approval — nothing is ever purchased without it."
    )


def _build_response(thread_id: str, result: dict) -> dict:
    payload = None
    interrupts = result.get("__interrupt__") or ()
    if interrupts:
        last = interrupts[-1]
        payload = getattr(last, "value", last if isinstance(last, dict) else None)

    booking = build_booking_links(result.get("user_query") or "")
    status = result.get("booking_status") or BookingStatus.DRAFT.value

    if payload:
        answer = _approval_markdown(payload)
        links = {}
    else:
        answer = result["messages"][-1].content if result.get("messages") else ""
        if status in (BookingStatus.BOOKED.value, BookingStatus.CANCELLED.value):
            links = {}
        else:
            answer = f"{answer}{booking_markdown(booking)}"
            links = booking
        answer = f"{answer}{_booking_cta(result)}"

    records = result.get("booking_records") or []
    if not records and thread_id:
        records = load_booking_records(thread_id)

    return {
        "thread_id": thread_id,
        "answer": answer,
        "flight_results": result.get("flight_results", ""),
        "hotel_results": result.get("hotel_results", ""),
        "weather_results": result.get("weather_results", ""),
        "itinerary": result.get("itinerary", ""),
        "llm_calls": result.get("llm_calls", 0),
        "booking_links": links,
        "booking_status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "needs_decision": bool(payload),
        "approval": payload,
        "progress": result.get("progress") or [],
        "tasks": result.get("tasks") or [],
        "receipt": result.get("receipt") or "",
        "booking_records": records,
        "total": trip_totals(result.get("purchases") or []),
    }


def _stray_decision_reply(thread_id: str, user_input: str, snapshot) -> dict | None:
    """Answer approval-like replies when no approval is actually pending."""
    values = snapshot.values or {}
    purchases = values.get("purchases") or []
    has_granted = any(
        (p.get("approval") or {}).get("granted_at") for p in purchases
    )
    status = values.get("booking_status") or BookingStatus.DRAFT.value
    if not (has_granted or status in (
        BookingStatus.BOOKED.value, BookingStatus.CANCELLED.value
    )):
        return None

    # "do not cancel my booking" is a reassurance, not a decision — let the
    # graph's keep-branch answer it properly.
    if CANCEL_NEGATED_RE.search(str(user_input or "")):
        return None

    decision = parse_decision(
        user_input, values.get("pending_action") or "book", purchases
    )
    if decision["action"] not in ("approve_all", "reject_all", "custom"):
        return None

    answer = (
        f"Nothing is awaiting your approval right now — the workflow is "
        f"**{STATUS_LABELS.get(status, status)}**."
    )
    if values.get("receipt"):
        answer = f"{answer}\n\n{values.get('receipt')}"
    answer = (
        f"{answer}\n\nThere is nothing to approve, reject or modify. "
        "Ask me to **plan** or **book** a new trip, or to **cancel** a "
        "booking if you need to."
    )

    return {
        "thread_id": thread_id,
        "answer": answer,
        "flight_results": values.get("flight_results", ""),
        "hotel_results": values.get("hotel_results", ""),
        "weather_results": values.get("weather_results", ""),
        "itinerary": values.get("itinerary", ""),
        "llm_calls": 0,
        "booking_links": {},
        "booking_status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "needs_decision": False,
        "approval": None,
        "progress": values.get("progress") or [],
        "tasks": values.get("tasks") or [],
        "receipt": values.get("receipt") or "",
        "booking_records": values.get("booking_records")
        or load_booking_records(thread_id),
        "total": trip_totals(purchases),
    }


def _needs_decision_response(thread_id: str, values: dict, payload: dict) -> dict:
    status = values.get("booking_status") or BookingStatus.DRAFT.value
    pending_action = values.get("pending_action") or "book"
    return {
        "thread_id": thread_id,
        "answer": payload.get("help") or decision_help(pending_action),
        "flight_results": values.get("flight_results", ""),
        "hotel_results": values.get("hotel_results", ""),
        "weather_results": values.get("weather_results", ""),
        "itinerary": values.get("itinerary", ""),
        "llm_calls": 0,
        "booking_links": {},
        "booking_status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "needs_decision": True,
        "approval": payload,
        "progress": values.get("progress") or [],
        "tasks": values.get("tasks") or [],
        "receipt": values.get("receipt") or "",
        "booking_records": values.get("booking_records") or [],
        "total": trip_totals(values.get("purchases") or []),
    }


def run_travel_agent(user_input: str, thread_id: str | None = None):
    if not thread_id:
        thread_id = f"user_{uuid.uuid4().hex}"

    config = {
        "configurable": {
            "thread_id": thread_id
        }
    }

    with _travel_run_lock:
        snapshot = travel_graph.get_state(config)
        pending = bool(snapshot.next)

        if pending:
            # Resume the SAME workflow (same thread, same checkpoint)
            # instead of starting a disconnected process.
            values = snapshot.values or {}
            payload = _current_interrupt(values) or {}
            pending_action = values.get("pending_action") or "book"
            purchases = _pending_purchases(values)

            decision = parse_decision(user_input, pending_action, purchases)

            if decision["action"] == "unrecognized":
                return _needs_decision_response(thread_id, values, payload)

            result = travel_graph.invoke(
                Command(resume=decision),
                config=config,
            )
        else:
            stray = _stray_decision_reply(thread_id, user_input, snapshot)
            if stray:
                return stray

            result = travel_graph.invoke(
                {
                    "messages": [
                        HumanMessage(content=user_input)
                    ],
                    "user_query": user_input,
                    "thread_id": thread_id,
                    "flight_results": "",
                    "hotel_results": "",
                    "weather_results": "",
                    "itinerary": "",
                    "llm_calls": 0
                },
                config=config
            )

    return _build_response(thread_id, result)


def approve_booking(
    thread_id: str,
    action: str = "approve",
    approval_id: str | None = None,
    modifications: str | None = None,
):
    """Explicit user confirmation for a financially binding action."""
    if not thread_id:
        raise ValueError("thread_id is required.")

    config = {"configurable": {"thread_id": thread_id}}

    with _travel_run_lock:
        snapshot = travel_graph.get_state(config)
        if not snapshot.next:
            raise ValueError("No approval is pending for this thread.")

        values = snapshot.values or {}
        purchases = _pending_purchases(values)

        action = (action or "approve").lower().strip()
        decisions = {}

        if action in ("approve", "approved", "confirm", "confirmed"):
            if approval_id:
                decisions = {approval_id: True}
                action = "custom"
            else:
                action = "approve_all"
        elif action in ("reject", "rejected", "decline", "declined"):
            if approval_id:
                decisions = {approval_id: False}
                action = "custom"
            else:
                action = "reject_all"
        elif action != "modify":
            raise ValueError("action must be approve, reject or modify.")

        decision = {
            "action": action,
            "decisions": decisions,
            "modifications": modifications,
        }

        result = travel_graph.invoke(
            Command(resume=decision),
            config=config,
        )

    return _build_response(thread_id, result)


def request_cancellation(thread_id: str, reference: str | None = None):
    """Route a cancellation through the same approval workflow."""
    if not thread_id:
        raise ValueError("thread_id is required.")

    config = {"configurable": {"thread_id": thread_id}}

    with _travel_run_lock:
        snapshot = travel_graph.get_state(config)
        if snapshot.next:
            raise ValueError(
                "This thread has a pending approval. Resolve it first."
            )

        values = snapshot.values or {}
        records = values.get("booking_records") or load_booking_records(thread_id)
        confirmed = [
            r for r in records
            if r.get("status") in ("confirmed", "pending")
            and (not reference or r.get("reference") == reference)
        ]
        if not confirmed:
            raise ValueError(
                "There is no confirmed booking to cancel on this thread."
            )

        target = confirmed[-1]
        query = f"Cancel booking {target.get('reference')}"

        result = travel_graph.invoke(
            {
                "messages": [HumanMessage(content=query)],
                "user_query": query,
                "thread_id": thread_id,
                "llm_calls": 0,
            },
            config=config,
        )

    return _build_response(thread_id, result)


def get_booking_state(thread_id: str) -> dict:
    """Current workflow state for the status endpoint / UI polling."""
    if not thread_id:
        raise ValueError("thread_id is required.")

    config = {"configurable": {"thread_id": thread_id}}
    snapshot = travel_graph.get_state(config)
    values = snapshot.values or {}
    status = values.get("booking_status") or BookingStatus.DRAFT.value
    payload = _current_interrupt(values)

    records = values.get("booking_records") or load_booking_records(thread_id)
    plan = values.get("plan") or {}

    return {
        "thread_id": thread_id,
        "booking_status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "pending": bool(snapshot.next),
        "pending_action": values.get("pending_action") or "",
        "needs_decision": bool(payload),
        "approval": payload,
        "progress": values.get("progress") or [],
        "tasks": values.get("tasks") or [],
        "receipt": values.get("receipt") or "",
        "booking_records": records,
        "total": trip_totals(values.get("purchases") or []),
        "plan": {
            "origin": plan.get("origin", ""),
            "destination": plan.get("destination", ""),
            "start_date": plan.get("start_date", ""),
            "end_date": plan.get("end_date", ""),
            "travelers": plan.get("travelers", []),
        },
    }