import asyncio
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from backend import (
    approve_booking,
    get_booking_state,
    request_cancellation,
    run_travel_agent,
)
from booking import (
    BrowserSession,
    ProviderError,
    available_kinds,
    close_provider,
    provider_mode,
    resolve_adapter,
)

# This is to allow nested event loops for async calls in FastAPI
import nest_asyncio
nest_asyncio.apply()


BASE_DIR = Path(__file__).resolve().parent

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Shut the browser down when the server stops."""
    try:
        yield
    finally:
        # Playwright teardown blocks while Chromium exits, so keep it off
        # the event loop.
        await asyncio.to_thread(close_provider)


app = FastAPI(
    title="TripMate AI",
    description="LangGraph Multi-Agent Travel Planner with FastAPI Frontend",
    version="1.1.0",
    lifespan=lifespan,
)


app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static"
)


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates")
)



class TravelRequest(BaseModel):
    message: str
    thread_id: str | None = None


class ApprovalRequest(BaseModel):
    thread_id: str
    action: str = "approve"          # approve | reject | modify
    approval_id: str | None = None   # bind the decision to one purchase
    modifications: str | None = None


class CancelRequest(BaseModel):
    thread_id: str
    reference: str | None = None


class BrowserSearchRequest(BaseModel):
    kind: str = "flight"                      # flight | hotel
    origin: str = ""
    destination: str = ""
    start_date: str = ""
    end_date: str = ""
    travelers: list[dict] | None = None
    currency: str = "USD"
    limit: int = 5


def _playwright_installed() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def _booking_payload(result: dict) -> dict:
    return {
        "thread_id": result.get("thread_id"),
        "booking_status": result.get("booking_status"),
        "status_label": result.get("status_label"),
        "needs_decision": result.get("needs_decision"),
        "approval": result.get("approval"),
        "progress": result.get("progress"),
        "tasks": result.get("tasks"),
        "receipt": result.get("receipt"),
        "booking_records": result.get("booking_records"),
        "total": result.get("total"),
        "booking_links": result.get("booking_links", {}),
    }



@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={}
    )


@app.post("/api/travel")
async def travel_planner(request_data: TravelRequest):
    try:
        user_message = request_data.message.strip()

        if not user_message:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Message cannot be empty."
                }
            )

        result = run_travel_agent(
            user_input=user_message,
            thread_id=request_data.thread_id
        )

        return JSONResponse(
            content={
                "success": True,
                "thread_id": result["thread_id"],
                "answer": result["answer"],
                "flight_results": result["flight_results"],
                "hotel_results": result["hotel_results"],
                "itinerary": result["itinerary"],
                "llm_calls": result["llm_calls"],
                "booking_links": result.get("booking_links", {}),
                **_booking_payload(result),
            }
        )

    except Exception as e:
        print("ERROR:", e)
        traceback.print_exc()

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e)
            }
        )



@app.post("/api/booking/approve")
async def booking_approval(request_data: ApprovalRequest):
    """Explicit confirmation for a purchase or cancellation."""
    try:
        result = approve_booking(
            thread_id=request_data.thread_id,
            action=request_data.action,
            approval_id=request_data.approval_id,
            modifications=request_data.modifications,
        )
        return JSONResponse(
            content={"success": True, "answer": result["answer"],
                     **_booking_payload(result)}
        )
    except ValueError as error:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": str(error)}
        )
    except Exception as error:
        print("APPROVAL ERROR:", error)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(error)}
        )


@app.post("/api/booking/cancel")
async def booking_cancel(request_data: CancelRequest):
    """Ask for approval to cancel — the cancel itself is never automatic."""
    try:
        result = request_cancellation(
            thread_id=request_data.thread_id,
            reference=request_data.reference,
        )
        return JSONResponse(
            content={"success": True, "answer": result["answer"],
                     **_booking_payload(result)}
        )
    except ValueError as error:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": str(error)}
        )
    except Exception as error:
        print("CANCEL ERROR:", error)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(error)}
        )


@app.get("/api/booking/status/{thread_id}")
async def booking_status(thread_id: str):
    try:
        state = get_booking_state(thread_id)
        return JSONResponse(content={"success": True, **state})
    except ValueError as error:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(error)}
        )
    except Exception as error:
        print("STATUS ERROR:", error)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(error)}
        )



@app.get("/api/browser/status")
async def browser_status():
    """Report whether browser automation is available and how it is set up."""
    return {
        "success": True,
        "provider_mode": provider_mode(),
        "browser_enabled": provider_mode() == "playwright",
        "adapters": available_kinds(),
        "headless": str(os.getenv("BROWSER_HEADLESS", "true")).lower() not in {
            "0", "false", "no", "off",
        },
        "playwright_installed": _playwright_installed(),
    }


@app.post("/api/browser/search")
async def browser_search(request_data: BrowserSearchRequest):
    """Run a live browser search against a booking site.

    This is the read-only half of the automation: it never books anything.
    """
    try:
        kind = (request_data.kind or "flight").strip().lower()
        if kind not in available_kinds():
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Unknown kind '{kind}'. Use one of: "
                    f"{', '.join(available_kinds())}",
                },
            )

        travelers = request_data.travelers or [{"name": "Guest", "type": "adult"}]
        plan = {
            "origin": request_data.origin,
            "destination": request_data.destination,
            "start_date": request_data.start_date,
            "end_date": request_data.end_date,
            "currency": request_data.currency,
            "travelers": travelers,
        }

        adapter = resolve_adapter(kind)
        limit = max(1, int(request_data.limit or 5))

        def _search():
            # A fresh session per request keeps one bad site from poisoning
            # the next search. It also means the (blocking) Playwright call
            # stays off the event loop.
            session = BrowserSession()
            try:
                return session.run(
                    lambda page: adapter.search(page, plan, travelers, limit=limit)
                )
            finally:
                session.close()

        offers = await asyncio.to_thread(_search)

        return JSONResponse(
            content={
                "success": True,
                "kind": kind,
                "provider": adapter.name,
                "count": len(offers),
                "offers": [offer.to_dict() for offer in offers],
            }
        )
    except ProviderError as error:
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(error), "payload": error.payload},
        )
    except Exception as error:
        print("BROWSER SEARCH ERROR:", error)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(error)},
        )


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "message": "AI Travel Planner API is running"
    }


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(content={})



if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )