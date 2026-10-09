"""
Ingestion layer — pull invoices in from "outside" the system.

Two sources are supported:
  1. A local drop folder (data/inbox/)  — the default, works with zero setup.
     Think of it as the landing zone an SFTP job, S3 sync, or a "save
     attachment" rule would write into.
  2. An IMAP email inbox — optionally download PDF/text attachments from
     unread emails straight into the inbox folder.

Each fetched invoice is normalized to a `FetchedInvoice`: filename, extracted
text, and a content hash used for idempotency/deduplication downstream.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
from dataclasses import dataclass
from pathlib import Path
from typing import List

from . import config, ocr

TEXT_SUFFIXES = {".txt", ".text", ".md"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = ocr.IMAGE_SUFFIXES
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES | IMAGE_SUFFIXES


@dataclass
class FetchedInvoice:
    path: Path
    text: str
    source_hash: str


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_pdf_text(path: Path) -> str:
    """
    Extract text from a PDF. If it's a text-based PDF we read it directly; if
    almost no text comes back it's a scanned/image PDF, so we fall back to OCR.
    Imports are lazy so text-only users need no PDF/OCR dependencies.
    """
    import pdfplumber  # noqa: WPS433 (local import on purpose)

    parts: List[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()

    if len(text) >= config.MIN_PDF_TEXT_CHARS:
        return text

    # Looks like a scanned PDF — try OCR (falls back cleanly if unavailable).
    if config.OCR_ENABLED:
        ocr_text = ocr.pdf_to_text_ocr(path)  # raises OCRUnavailable with guidance
        if ocr_text:
            print(f"    (OCR used for scanned PDF {path.name})")
            return ocr_text
    return text  # may be empty; extractor will reject empty text with a clear error


def _read_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return _read_pdf_text(path)
    if suffix in IMAGE_SUFFIXES:
        print(f"    (OCR used for scanned image {path.name})")
        return ocr.image_to_text(path)  # raises OCRUnavailable with guidance
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    raise ValueError(f"Unsupported file type: {path.name}")


def fetch_from_folder(inbox: Path | None = None) -> List[FetchedInvoice]:
    """Read every supported invoice file currently sitting in the inbox folder."""
    inbox = inbox or config.INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)

    results: List[FetchedInvoice] = []
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        raw = path.read_bytes()
        try:
            text = _read_file(path)
        except Exception as exc:  # keep going on one bad file
            print(f"  ! could not read {path.name}: {exc}")
            continue
        results.append(
            FetchedInvoice(path=path, text=text, source_hash=_hash_bytes(raw))
        )
    return results


def fetch_from_email(inbox: Path | None = None) -> int:
    """
    Download invoice attachments from an IMAP mailbox into the inbox folder.
    Returns the number of attachments saved. No-op if IMAP is not configured.
    """
    if not config.IMAP_HOST:
        return 0

    inbox = inbox or config.INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)

    saved = 0
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT) as imap:
        imap.login(config.IMAP_USER, config.IMAP_PASSWORD)
        imap.select(config.IMAP_FOLDER)
        _, data = imap.search(None, config.IMAP_SEARCH)
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            for part in msg.walk():
                filename = part.get_filename()
                if not filename:
                    continue
                if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                # Prefix with message id to avoid collisions.
                dest = inbox / f"{num.decode()}_{filename}"
                dest.write_bytes(payload)
                saved += 1
            imap.store(num, "+FLAGS", "\\Seen")
    return saved
