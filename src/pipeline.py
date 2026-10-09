"""
Orchestration — wire the stages together:

    fetch -> extract (OpenRouter) -> guardrails -> store / quarantine

Every file gets an audit-log entry at each stage. Processing is idempotent:
a file whose content we've already stored is skipped, and duplicates
(same company + invoice number) are quarantined rather than double-counted.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List

from . import config, database
from .extractor import ExtractionError, extract_invoice
from .fetcher import FetchedInvoice, fetch_from_email, fetch_from_folder
from .guardrails import Guardrails


@dataclass
class PipelineResult:
    stored: int = 0
    quarantined: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "stored": self.stored,
            "quarantined": self.quarantined,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def _move(path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():  # avoid clobbering; suffix with a counter
        stem, suffix, i = dest.stem, dest.suffix, 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{i}{suffix}"
            i += 1
    shutil.move(str(path), str(dest))


def _process_one(item: FetchedInvoice, guard: Guardrails, result: PipelineResult) -> None:
    src = str(item.path)

    # 1) Idempotency — already stored this exact content?
    if database.already_processed(item.source_hash):
        database.log_event(source_file=src, source_hash=item.source_hash,
                           stage="fetch", outcome="skipped", detail="duplicate content hash")
        result.skipped += 1
        _move(item.path, config.PROCESSED_DIR)
        print(f"  = {item.path.name}: already processed, skipped")
        return

    # 2) Extract with OpenRouter
    try:
        invoice = extract_invoice(item.text)
    except ExtractionError as exc:
        database.log_event(source_file=src, source_hash=item.source_hash,
                           stage="extract", outcome="error", detail=str(exc))
        result.errors += 1
        print(f"  ! {item.path.name}: extraction failed — {exc}")
        return

    # 3) Guardrails
    report = guard.evaluate(invoice, raw_text=item.text)

    # 3a) Duplicate by business key
    dedup = guard.rules.get("deduplication", {})
    if dedup.get("by_company_and_number"):
        dup_id = database.find_duplicate(invoice.company_name, invoice.invoice_number)
        if dup_id:
            report.violations.append(_dup_violation(dup_id))

    if report.blocked:
        detail = report.summary()
        database.log_event(source_file=src, source_hash=item.source_hash,
                           stage="guardrails", outcome="quarantined", detail=detail)
        result.quarantined += 1
        _write_sidecar(item, invoice, detail)
        _move(item.path, config.QUARANTINE_DIR)
        print(f"  [ERR] {item.path.name}: quarantined — {detail}")
        return

    # 4) Store
    status = guard.auto_approve_status(invoice)
    if report.needs_review:
        status = "needs_review"
    try:
        inv_id = database.store_invoice(
            invoice,
            source_file=src,
            source_hash=item.source_hash,
            raw_text=report.redacted_raw_text,
            status=status,
        )
    except Exception as exc:
        database.log_event(source_file=src, source_hash=item.source_hash,
                           stage="store", outcome="error", detail=str(exc))
        result.errors += 1
        print(f"  [ERR] {item.path.name}: store failed — {exc}")
        return

    database.log_event(source_file=src, source_hash=item.source_hash, stage="store",
                       outcome="success", detail=f"invoice id={inv_id} status={status}")
    result.stored += 1
    _move(item.path, config.PROCESSED_DIR)
    flag = "  (needs review)" if status == "needs_review" else ""
    print(f"  [OK] {item.path.name}: stored #{inv_id} "
          f"{invoice.company_name} / {invoice.invoice_number} "
          f"{invoice.bill_amount}{flag}")


def _dup_violation(dup_id: int):
    from .guardrails import Violation
    return Violation("duplicate", "block", f"already stored as invoice id {dup_id}")


def _write_sidecar(item: FetchedInvoice, invoice, detail: str) -> None:
    """Drop a .json next to a quarantined file explaining why, for humans."""
    sidecar = config.QUARANTINE_DIR / f"{item.path.name}.report.json"
    config.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {"file": item.path.name, "violations": detail,
             "extracted": invoice.model_dump(mode="json")},
            indent=2, default=str,
        ),
        encoding="utf-8",
    )


def run(fetch_email: bool = True) -> PipelineResult:
    """Run one full pass over everything currently available to ingest."""
    config.ensure_dirs()
    database.init_db()
    guard = Guardrails()

    if fetch_email and config.IMAP_HOST:
        n = fetch_from_email()
        if n:
            print(f"Fetched {n} attachment(s) from email into inbox.")

    items: List[FetchedInvoice] = fetch_from_folder()
    if not items:
        print("Inbox is empty — drop invoice .txt/.pdf files into data/inbox/.")
        return PipelineResult()

    print(f"Processing {len(items)} invoice(s)...")
    result = PipelineResult()
    for item in items:
        _process_one(item, guard, result)

    print(f"\nDone: {result.as_dict()}")
    return result
