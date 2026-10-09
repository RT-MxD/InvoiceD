# D-Invoice

An end-to-end, AI-powered invoice processing pipeline tailored for Indian GST formats. It automatically extracts data from PDFs and scanned images, validates it against customizable business rules (guardrails), and provides a full browser dashboard for triage and vendor account management.

## Features

- **Multi-format Ingestion**: Processes Text PDFs, scanned PDFs, and raw images (PNG/JPG) using local OCR (Tesseract).
- **Advanced AI Extraction**: Uses **OpenRouter (Nemotron)** to accurately extract complex invoice structures, specifically tailored for Indian textile and GST formats.
- **Indian GST Support**: Extracts Company Name, Vendor Name, E-way Bill Number, Challan No, Receiver Details, State, Broker, Sale Average Rate, HSN codes, and quantity units.
- **Vendor Accounts & Ledger**: Automatically aggregates invoices into Vendor Accounts, calculating total billed, total paid, and pending balances directly on the dashboard.
- **Validation Guardrails**: Configurable `guardrails.yaml` blocks or flags invoices for manual review based on required fields, value thresholds (e.g., auto-approve limits), arithmetic checks, and duplicates.
- **Serverless Ready**: Fully configured to run on Vercel with a remote **Neon Serverless Postgres** database.
- **Excel Backups**: One-click download of your entire database to Excel from the UI.

## Architecture

| Component | File | Purpose |
|-----------|------|---------|
| **API + UI** | `src/api.py` + `streamlit_app.py` | FastAPI backend and a Streamlit dashboard to upload, process, and manage vendor accounts. |
| **Extraction** | `src/extractor.py` | Connects to OpenRouter to parse raw text/OCR output into a strict Pydantic schema. |
| **Database** | `src/models.py`, `src/database.py` | SQLAlchemy ORM mapped to Postgres (Neon). Manages the `invoices`, `line_items`, `vendor_balances`, and `processing_log` tables. |
| **Guardrails** | `src/guardrails.py`, `guardrails.yaml` | The semantic validation engine. |

---

## Quick start (local)

### 1. Requirements
```bash
# OCR engine + PDF rasterizer (for scanned images / image PDFs)
# Windows: Install Tesseract-OCR and Poppler binaries and add them to PATH.
```

### 2. Python environment
```bash
cd invoice_pipeline
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configuration (.env)
Create a `.env` file in the root directory:
```ini
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free

# Neon Postgres URL
DATABASE_URL=postgresql://user:pass@ep-host.aws.neon.tech/Invoice_D?sslmode=require
```

### 4. Run the web UI (Streamlit & FastAPI)
```bash
# Start the FastAPI backend and Streamlit UI simultaneously
python main.py serve
```
Open `http://localhost:8501` to view the dashboard!

---

## The Database Schema

- **`invoices`** — one row per invoice. `UNIQUE(company_name, invoice_number)` blocks duplicates. Includes fields like `challan_no`, `e_way_bill_number`, `vendor_name`.
- **`line_items`** — child rows containing description, design no, color, pcs, rate, and amount.
- **`vendor_balances`** — Ledger table that tracks running totals for `paid` and `pending` balances per vendor.
- **`processing_log`** — audit trail: one row per file per stage.

---

## Backups

Because free-tier remote databases (like Neon) can expire or sleep, D-Invoice includes a built-in backup mechanism. 
From the **Tools -> Backup** page in the dashboard, you can instantly download a `.xlsx` file containing all your tables (Invoices, Line Items, Audit Logs) to your local machine.

---

## Guardrails reference (`guardrails.yaml`)

Each rule group has an enforcement level: `block` → quarantine, `warn` → store but flag `needs_review`. Checks include:
- Required fields (e.g. `company_name`, `invoice_number`, `challan_no`)
- Auto-approval thresholds (e.g., invoices > ₹400,000 require human review)
- Arithmetic validation (Subtotal + Tax == Total)
- Deduplication by content hash and company+number.
