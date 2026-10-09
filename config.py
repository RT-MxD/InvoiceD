"""
Central configuration, loaded from environment variables (.env supported).

Nothing here is secret by default — the pipeline runs fully with OpenRouter
and Neon PostgreSQL database with API keys.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))

INBOX_DIR = Path(os.getenv("INBOX_DIR", DATA_DIR / "inbox"))            # new invoices land here
PROCESSED_DIR = Path(os.getenv("PROCESSED_DIR", DATA_DIR / "processed"))  # successfully stored
QUARANTINE_DIR = Path(os.getenv("QUARANTINE_DIR", DATA_DIR / "quarantine"))  # failed guardrails

GUARDRAILS_FILE = Path(os.getenv("GUARDRAILS_FILE", BASE_DIR / "guardrails.yaml"))

# ---------------------------------------------------------------------------
# Database  (SQLite by default; point at Postgres/MySQL for production)
#   e.g. postgresql+psycopg2://user:pass@host:5432/invoices
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", f"postgresql:///{DATA_DIR / 'invoices.db'}")

# ---------------------------------------------------------------------------
# OpenRouter API Configuration
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-3.8-27b-instruct:free")
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "120"))    # seconds
OPENROUTER_TEMPERATURE = float(os.getenv("OPENROUTER_TEMPERATURE", "0"))  # 0 = deterministic

# ---------------------------------------------------------------------------
# OCR (for scanned image invoices and image-only PDFs). Free via Tesseract.
# ---------------------------------------------------------------------------
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() in ("1", "true", "yes")
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")   # e.g. "eng", "eng+deu"
OCR_DPI = int(os.getenv("OCR_DPI", "300"))         # rasterization DPI for scanned PDFs
# If a PDF yields fewer than this many characters of embedded text, we treat it
# as a scanned/image PDF and fall back to OCR.
MIN_PDF_TEXT_CHARS = int(os.getenv("MIN_PDF_TEXT_CHARS", "25"))

# ---------------------------------------------------------------------------
# Web API / browser UI
# ---------------------------------------------------------------------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# ---------------------------------------------------------------------------
# Email fetcher (optional). Leave IMAP_HOST empty to disable.
# ---------------------------------------------------------------------------
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
IMAP_SEARCH = os.getenv("IMAP_SEARCH", "UNSEEN")  # IMAP search criteria


def ensure_dirs() -> None:
    """Create the working directories if they don't exist yet."""
    for d in (DATA_DIR, INBOX_DIR, PROCESSED_DIR, QUARANTINE_DIR):
        d.mkdir(parents=True, exist_ok=True)
