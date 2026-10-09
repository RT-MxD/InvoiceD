"""
SQLAlchemy ORM models — the database schema where validated invoices live.
Works on SQLite out of the box and on Postgres/MySQL by changing DATABASE_URL.

Updated with Indian GST invoice fields: buyer company, ack no/date,
buyer order no, e-way bill no, IGST/CGST/SGST, HSN codes, quantity units.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"
    # A company's invoice number is unique per company — this is our dedupe key.
    __table_args__ = (UniqueConstraint("company_name", "invoice_number", name="uq_company_invoice"),)

    id = Column(Integer, primary_key=True)

    invoice_number = Column(String(128), nullable=False, index=True)
    company_name = Column(String(256), nullable=False, index=True)
    vendor_name = Column(String(256))
    
    receiver_details = Column(Text)
    state = Column(String(64))
    broker = Column(String(128))
    e_way_bill_number = Column(String(128))
    challan_no = Column(String(128))
    
    bill_amount = Column(Float, default=0.0)
    sale_avg_rate = Column(Float, default=0.0)
    
    paid_amount = Column(Float, default=0.0)
    pending_amount = Column(Float, default=0.0)

    invoice_date = Column(Date)
    due_date = Column(Date)

    confidence = Column(Float, default=0.0)
    status = Column(String(32), default="stored")  # stored | needs_review
    source_file = Column(String(512))
    source_hash = Column(String(64), unique=True, index=True)  # idempotency
    raw_text = Column(Text)  # keep the original extracted text for audit

    created_at = Column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc))

    line_items = relationship(
        "LineItem", back_populates="invoice", cascade="all, delete-orphan"
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="CASCADE"))

    description = Column(Text)
    design_no = Column(String(128))
    color = Column(String(64))
    pcs = Column(Float, default=0.0)
    rate = Column(Float, default=0.0)
    amount = Column(Float, default=0.0)

    invoice = relationship("Invoice", back_populates="line_items")


class ProcessingLog(Base):
    """Audit trail — one row per file the pipeline touches, pass or fail."""

    __tablename__ = "processing_log"

    id = Column(Integer, primary_key=True)
    source_file = Column(String(512))
    source_hash = Column(String(64), index=True)
    stage = Column(String(32))          # fetch | extract | guardrails | store
    outcome = Column(String(32))        # success | quarantined | skipped | error
    detail = Column(Text)               # errors / guardrail violations, JSON-ish
    created_at = Column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc))


class VendorBalance(Base):
    """Vendor payment balances, moved from SQLite to Postgres for Vercel."""

    __tablename__ = "vendor_balances"

    vendor_name = Column(String(256), primary_key=True)
    paid_paise = Column(Integer, nullable=False, default=0)
    pending_paise = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc))
