from pathlib import Path
import traceback
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

# This is to allow nested event loops for async calls in FastAPI
import nest_asyncio
nest_asyncio.apply()


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="TripMate AI",
    description="LangGraph Multi-Agent Travel Planner with FastAPI Frontend",
    version="1.1.0"
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