"""
FastAPI backend + static browser UI + performance monitoring.

100% free to run: extraction uses your local OpenRouter model and OCR uses local
Tesseract — there are no paid API calls anywhere in this service.

Run it:
    uvicorn src.api:app --reload
    #  or:  python main.py serve
Then open http://localhost:8000
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from pydantic import BaseModel
import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from . import config, database, ocr
from .fetcher import SUPPORTED_SUFFIXES
from .pipeline import run

# ---- background processing state (single-process, thread-safe) -------------
_LOCK = threading.Lock()
_STATE = {"running": False, "last_result": None}

# ---- performance metrics store ----
_PERF_LOCK = threading.Lock()
_PERF_HISTORY: deque = deque(maxlen=1000)  # last 1000 requests
_PERF_COUNTERS = {
    "total_requests": 0,
    "start_time": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    database.init_db()
    _PERF_COUNTERS["start_time"] = time.time()
    yield


app = FastAPI(title="Invoice Pipeline", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------------------
# Performance middleware — track response times for every request
# ---------------------------------------------------------------------------
@app.middleware("http")
async def performance_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000

    # Add timing header
    response.headers["X-Response-Time"] = f"{elapsed_ms:.2f}ms"

    # Add cache-control for GET requests to reduce redundant refetches.
    # Streamlit's requests.Session and browsers honour these headers.
    if request.method == "GET" and request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, max-age=5, stale-while-revalidate=10"

    # Record metric
    with _PERF_LOCK:
        _PERF_COUNTERS["total_requests"] += 1
        _PERF_HISTORY.append({
            "path": request.url.path,
            "method": request.method,
            "status": response.status_code,
            "time_ms": round(elapsed_ms, 2),
            "timestamp": time.time(),
        })

    return response


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
from fastapi.responses import RedirectResponse

@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/docs")


# ---------------------------------------------------------------------------
# Health & stats
# ---------------------------------------------------------------------------
def _llm_up() -> bool:
    return bool(config.OPENROUTER_API_KEY)


_quarantine_cache: tuple[float, list[Path]] | None = None


def _quarantine_files() -> list[Path]:
    global _quarantine_cache
    # Cache filesystem scan for 30 s — quarantine only changes during pipeline runs
    if _quarantine_cache and time.time() - _quarantine_cache[0] < 30:
        return _quarantine_cache[1]
    if not config.QUARANTINE_DIR.exists():
        result: list[Path] = []
    else:
        result = [
            p for p in sorted(config.QUARANTINE_DIR.iterdir())
            if p.is_file()
            and not p.name.startswith(".")          # ignore .gitkeep and other dotfiles
            and not p.name.endswith(".report.json")  # ignore the sidecar reports
        ]
    _quarantine_cache = (time.time(), result)
    return result


@app.get("/api/health")
def health() -> dict:
    ocr_ok, ocr_msg = ocr.ocr_available()
    return {
        "openrouter": _llm_up(),
        "openrouter_api_key_set": bool(config.OPENROUTER_API_KEY),
        "model": config.OPENROUTER_MODEL,
        "ocr": ocr_ok,
        "ocr_message": ocr_msg,
        "processing": _STATE["running"],
    }


@app.get("/api/stats")
def stats() -> dict:
    data = database.counters()
    data["quarantined"] = len(_quarantine_files())
    data["processing"] = _STATE["running"]
    data["last_result"] = _STATE["last_result"]
    return data


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
@app.get("/api/invoices")
def list_invoices(status: str | None = None, limit: int = 200) -> list[dict]:
    return database.list_invoices(status=status, limit=limit)


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: int) -> dict:
    inv = database.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


@app.post("/api/invoices/{invoice_id}/approve")
def approve_invoice(invoice_id: int) -> dict:
    if not database.set_invoice_status(invoice_id, "stored"):
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {"ok": True, "id": invoice_id, "status": "stored"}


@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: int) -> dict:
    if not database.delete_invoice(invoice_id):
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {"ok": True, "id": invoice_id}


class InvoicePaymentUpdate(BaseModel):
    paid: float
    pending: float

@app.post("/api/invoices/{invoice_id}/payment")
def save_invoice_payment(invoice_id: int, payload: InvoicePaymentUpdate) -> dict:
    if not database.save_invoice_payment(invoice_id, payload.paid, payload.pending):
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {"ok": True, "id": invoice_id}


@app.get("/api/logs")
def logs(limit: int = 25) -> list[dict]:
    return database.recent_logs(limit=limit)


@app.get("/api/quarantine")
def quarantine() -> list[dict]:
    out = []
    for p in _quarantine_files():
        report_path = config.QUARANTINE_DIR / f"{p.name}.report.json"
        report = None
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                report = None
        out.append({
            "file": p.name,
            "violations": (report or {}).get("violations"),
            "extracted": (report or {}).get("extracted"),
        })
    return out


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------
@app.get("/api/performance")
def performance() -> dict:
    with _PERF_LOCK:
        history = list(_PERF_HISTORY)
        total = _PERF_COUNTERS["total_requests"]
        start_time = _PERF_COUNTERS["start_time"]

    if not history:
        return {
            "total_requests": total,
            "uptime_seconds": round(time.time() - (start_time or time.time()), 1),
            "avg_response_time_ms": 0,
            "min_response_time_ms": 0,
            "max_response_time_ms": 0,
            "p95_response_time_ms": 0,
            "endpoints": [],
            "recent": [],
        }

    times = [h["time_ms"] for h in history]
    sorted_times = sorted(times)
    p95_idx = min(int(len(sorted_times) * 0.95), len(sorted_times) - 1)

    # Per-endpoint breakdown
    endpoint_stats: dict[str, list[float]] = {}
    for h in history:
        key = f"{h['method']} {h['path']}"
        endpoint_stats.setdefault(key, []).append(h["time_ms"])

    endpoints = []
    for ep, ep_times in sorted(endpoint_stats.items(), key=lambda x: -len(x[1])):
        ep_sorted = sorted(ep_times)
        ep_p95_idx = min(int(len(ep_sorted) * 0.95), len(ep_sorted) - 1)
        endpoints.append({
            "endpoint": ep,
            "requests": len(ep_times),
            "avg_ms": round(statistics.mean(ep_times), 2),
            "min_ms": round(min(ep_times), 2),
            "max_ms": round(max(ep_times), 2),
            "p95_ms": round(ep_sorted[ep_p95_idx], 2),
        })

    # Recent requests (last 20)
    recent = history[-20:][::-1]

    return {
        "total_requests": total,
        "uptime_seconds": round(time.time() - (start_time or time.time()), 1),
        "avg_response_time_ms": round(statistics.mean(times), 2),
        "min_response_time_ms": round(min(times), 2),
        "max_response_time_ms": round(max(times), 2),
        "p95_response_time_ms": round(sorted_times[p95_idx], 2),
        "endpoints": endpoints,
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# Upload & process
# ---------------------------------------------------------------------------
from pydantic import BaseModel

class VendorBalanceUpdate(BaseModel):
    paid: int
    pending: int
    version: int

@app.get("/api/vendors/{vendor_name}/balance")
def get_balance(vendor_name: str) -> dict:
    from urllib.parse import unquote
    vendor = unquote(vendor_name)
    return database.get_vendor_balance(vendor)

@app.post("/api/vendors/{vendor_name}/balance")
def save_balance(vendor_name: str, payload: VendorBalanceUpdate) -> dict:
    from urllib.parse import unquote
    vendor = unquote(vendor_name)
    success = database.save_vendor_balance(vendor, payload.paid, payload.pending, payload.version)
    if not success:
        raise HTTPException(status_code=409, detail="These balances changed in another session. Reload saved balances before saving again.")
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), process: bool = Form(False),
                 background: BackgroundTasks = None) -> dict:
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type. Allowed: {sorted(SUPPORTED_SUFFIXES)}",
        )

    config.ensure_dirs()
    dest = config.INBOX_DIR / filename
    dest.write_bytes(await file.read())

    if process and background is not None:
        background.add_task(_process_pipeline)

    return {"ok": True, "saved": filename, "queued": bool(process)}


def _process_pipeline() -> None:
    """Run one pipeline pass, guarding against overlapping runs."""
    global _quarantine_cache
    if not _LOCK.acquire(blocking=False):
        return  # a run is already in progress
    _STATE["running"] = True
    try:
        result = run(fetch_email=False)
        _STATE["last_result"] = result.as_dict()
    except Exception as exc:  # never let a background crash kill the flag
        _STATE["last_result"] = {"error": str(exc)}
    finally:
        _STATE["running"] = False
        _quarantine_cache = None  # invalidate so new entries are visible immediately
        _LOCK.release()


@app.post("/api/process")
def process(background: BackgroundTasks) -> JSONResponse:
    if _STATE["running"]:
        return JSONResponse({"ok": False, "message": "Already processing"}, status_code=202)
    background.add_task(_process_pipeline)
    return JSONResponse({"ok": True, "message": "Processing started"}, status_code=202)


@app.get("/api/backup/download")
def download_backup():
    import io
    import pandas as pd
    from datetime import datetime
    from .database import _engine
    
    try:
        invoices_df = pd.read_sql("SELECT * FROM invoices", con=_engine)
        line_items_df = pd.read_sql("SELECT * FROM line_items", con=_engine)
        logs_df = pd.read_sql("SELECT * FROM processing_log", con=_engine)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    for df in [invoices_df, line_items_df, logs_df]:
        for col in df.select_dtypes(include=['datetimetz']).columns:
            try:
                df[col] = df[col].dt.tz_localize(None)
            except Exception:
                pass
                
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        invoices_df.to_excel(writer, sheet_name='Invoices', index=False)
        line_items_df.to_excel(writer, sheet_name='Line Items', index=False)
        logs_df.to_excel(writer, sheet_name='Logs', index=False)
        
    output.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    headers = {
        'Content-Disposition': f'attachment; filename="database_backup_{timestamp}.xlsx"'
    }
    return Response(
        content=output.getvalue(), 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
        headers=headers
    )
