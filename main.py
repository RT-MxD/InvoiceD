#!/usr/bin/env python3
"""
CLI entry point for the invoice pipeline.

Usage:
    python main.py run              # process everything in the inbox once
    python main.py run --no-email   # skip the IMAP email fetch step
    python main.py watch --interval 30   # keep polling the inbox every 30s
    python main.py list             # show stored invoices
    python main.py stats            # show pipeline counters
    python main.py seed             # copy bundled sample invoices into the inbox
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from sqlalchemy import func, select

from src import config, database
from src.models import Invoice, ProcessingLog
from src.pipeline import run


def cmd_run(args: argparse.Namespace) -> None:
    run(fetch_email=not args.no_email)


def cmd_watch(args: argparse.Namespace) -> None:
    print(f"Watching {config.INBOX_DIR} every {args.interval}s. Ctrl-C to stop.")
    try:
        while True:
            run(fetch_email=not args.no_email)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_list(args: argparse.Namespace) -> None:
    database.init_db()
    with database.session_scope() as s:
        rows = s.scalars(select(Invoice).order_by(Invoice.created_at.desc()).limit(args.limit)).all()
        if not rows:
            print("No invoices stored yet.")
            return
        print(f"{'ID':<4} {'VENDOR':<24} {'INVOICE#':<16} {'DATE':<12} {'BILL':>12} STATUS")
        print("-" * 87)
        for r in rows:
            print(f"{r.id:<4} {(r.company_name or '')[:23]:<24} "
                  f"{(r.invoice_number or '')[:15]:<16} "
                  f"{str(r.invoice_date or ''):<12} {r.bill_amount:>12,.2f} {r.status}")


def cmd_stats(args: argparse.Namespace) -> None:
    database.init_db()
    with database.session_scope() as s:
        total = s.scalar(select(func.count(Invoice.id)))
        needs = s.scalar(select(func.count(Invoice.id)).where(Invoice.status == "needs_review"))
        summed = s.scalar(select(func.sum(Invoice.bill_amount))) or 0.0
        print(f"Invoices stored : {total}")
        print(f"Needs review    : {needs}")
        print(f"Total value     : {summed:,.2f}")
        print("\nLast processing-log entries:")
        logs = s.scalars(select(ProcessingLog).order_by(ProcessingLog.created_at.desc()).limit(10)).all()
        for lg in logs:
            print(f"  [{lg.stage:<10}] {lg.outcome:<11} {Path(lg.source_file or '').name}  {lg.detail[:60]}")


def cmd_serve(args: argparse.Namespace) -> None:
    """Launch the FastAPI web app and Streamlit UI."""
    import uvicorn
    import subprocess
    import sys
    import os

    print(f"Invoice Pipeline API -> http://localhost:{args.port}")
    print("Starting Streamlit UI...")
    
    env = os.environ.copy()
    env["INVOICE_API_URL"] = f"http://localhost:{args.port}"
    
    ui_process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "streamlit_app.py"],
        env=env
    )
    
    try:
        uvicorn.run("src.api:app", host=args.host, port=args.port, reload=args.reload)
    finally:
        ui_process.terminate()


def cmd_seed(args: argparse.Namespace) -> None:
    """Copy the bundled sample invoices into the inbox so you can try it fast."""
    config.ensure_dirs()
    samples = Path(__file__).parent / "data" / "samples"
    if not samples.exists():
        print("No samples directory found.")
        return
    n = 0
    for f in samples.iterdir():
        if f.is_file():
            shutil.copy(f, config.INBOX_DIR / f.name)
            n += 1
    print(f"Copied {n} sample invoice(s) into {config.INBOX_DIR}")


def cmd_backup(args: argparse.Namespace) -> None:
    """Run an immediate backup of the database to an Excel file."""
    from src.backup import run_backup
    run_backup()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automated invoice fetch -> extract -> store pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="process the inbox once")
    pr.add_argument("--no-email", action="store_true", help="skip IMAP email fetch")
    pr.set_defaults(func=cmd_run)

    pw = sub.add_parser("watch", help="poll the inbox on an interval")
    pw.add_argument("--interval", type=int, default=30, help="seconds between passes")
    pw.add_argument("--no-email", action="store_true", help="skip IMAP email fetch")
    pw.set_defaults(func=cmd_watch)

    pl = sub.add_parser("list", help="list stored invoices")
    pl.add_argument("--limit", type=int, default=50)
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("stats", help="show counters and recent log")
    ps.set_defaults(func=cmd_stats)

    psd = sub.add_parser("seed", help="copy sample invoices into the inbox")
    psd.set_defaults(func=cmd_seed)
    
    pb = sub.add_parser("backup", help="run a database backup to excel now")
    pb.set_defaults(func=cmd_backup)

    pv = sub.add_parser("serve", help="run the FastAPI backend")
    pv.add_argument("--host", default=config.API_HOST)
    pv.add_argument("--port", type=int, default=config.API_PORT)
    pv.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    pv.set_defaults(func=cmd_serve)

    return p


if __name__ == "__main__":
    parser = build_parser()
    ns = parser.parse_args()
    ns.func(ns)
