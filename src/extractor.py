"""
Extraction layer — turn raw invoice text into a structured `InvoiceExtraction`
using a free, local OpenRouter model.

We use OpenRouter's `format: json` mode so the model is constrained to emit valid
JSON, plus a strict system prompt and a JSON skeleton. The result is then run
through the Pydantic schema, which coerces messy values and rejects anything
structurally wrong.

Updated to extract Indian GST invoice fields: buyer company name, ack no/date,
buyer order no, e-way bill no, HSN codes, IGST/CGST/SGST, quantity units (kgs).
"""
from __future__ import annotations

import json
from typing import Any, Dict

import requests

from . import config
from .schemas import InvoiceExtraction

SYSTEM_PROMPT = """You are a meticulous accounts-payable data-extraction engine.
You are given the raw text of a single invoice. Extract the fields exactly as
they appear in the document. Do NOT invent, guess, or calculate values that are
not present — use null (or 0 for numeric totals) when a field is genuinely
missing. Return ONLY a JSON object, no prose, no markdown fences.

Pay special attention to Indian GST invoices which may contain:
- Buyer/Bill-to company name (distinct from the company/seller)
- Invoice Number and Date
- e-Way Bill Number
- HSN/SAC codes for each line item
- Quantity in specific units (kgs, ltrs, pcs, etc.)
- IGST, CGST, SGST tax breakdowns
- Sale average rate (if available)

Specifically for D.K TEXTILE invoices or similar formats, extract:
- Company name ("D.K TEXTILE")
- Vendor name (e.g. "SHREE RADHA RANI CREATION")
- Details of receiver/Billed to: (as receiver_details)
- Invoice No
- Date (Invoice date)
- State
- Due Date: Extract if present, otherwise null.
- Description, Design No, Color, Pcs, Rate, Amount (for Line Items)
- Bill Amount
- Broker
- Sale avg rate
- e-way_bill_number
- Challan No

"""

# Skeleton shown to the model so it knows the exact keys/types we expect.
JSON_SKELETON: Dict[str, Any] = {
    "invoice_number": "string",
    "company_name": "string — the seller/supplier company",
    "vendor_name": "string or null — e.g. SHREE RADHA RANI CREATION",
    "receiver_details": "string or null — Details of receiver/Billed to",
    "state": "string or null — State of the buyer",
    "broker": "string or null — Broker name",
    "e-way_bill_number": "string or null — e-Way Bill Number",
    "challan_no": "string or null — Challan No",
    "bill_amount": 0.0,
    "sale_avg_rate": 0.0,
    "invoice_date": "YYYY-MM-DD or null",
    "due_date": "YYYY-MM-DD or null — extract from document if present",
    "line_items": [
        {
            "description": "string",
            "design_no": "string or null",
            "color": "string or null",
            "pcs": 0.0,
            "rate": 0.0,
            "amount": 0.0,
        }
    ],
    "confidence": "float 0..1 — how sure you are the extraction is correct",
}


def _build_prompt(invoice_text: str) -> str:
    return (
        f"Return a JSON object with EXACTLY these keys and types:\n"
        f"{json.dumps(JSON_SKELETON, indent=2)}\n\n"
        f"--- INVOICE TEXT START ---\n{invoice_text}\n--- INVOICE TEXT END ---"
    )


class ExtractionError(RuntimeError):
    """Raised when OpenRouter is unreachable or returns unusable output."""


def extract_invoice(invoice_text: str) -> InvoiceExtraction:
    """Call OpenRouter and validate the response into an InvoiceExtraction."""
    if not invoice_text.strip():
        raise ExtractionError("Empty invoice text — nothing to extract.")

    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(invoice_text)}
        ],
        "temperature": config.OPENROUTER_TEMPERATURE,
        "response_format": {"type": "json_object"}
    }

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=config.OPENROUTER_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ExtractionError(
            f"Could not reach OpenRouter API "
            f"(is the API key valid and model '{config.OPENROUTER_MODEL}' available?): {exc}"
        ) from exc

    body = resp.json()
    if "choices" not in body or not body["choices"]:
        raise ExtractionError(f"OpenRouter returned an empty response: {body}")
        
    raw_json = body["choices"][0]["message"]["content"].strip()
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Model did not return valid JSON: {exc}\n{raw_json[:500]}") from exc

    try:
        return InvoiceExtraction.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ExtractionError(f"Extracted data failed schema validation: {exc}") from exc
