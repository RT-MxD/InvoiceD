"""
Pydantic schemas describing the *shape* of an invoice once the LLM has
extracted it. These give us a first, structural line of defense: if OpenRouter
returns malformed JSON or the wrong types, validation fails loudly here
before anything touches the database.

Updated to support Indian GST invoice fields (Invoice number,Eway bill)
HSN codes, IGST/CGST/SGST, quantity units in kgs, etc.).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class LineItem(BaseModel):
    description: str = Field(default="", description="What was billed")
    design_no: Optional[str] = Field(default=None, description="Design No")
    color: Optional[str] = Field(default=None, description="Color")
    pcs: float = Field(default=0.0, description="Pcs")
    rate: float = Field(default=0.0, description="Rate")
    amount: float = Field(default=0.0, description="rate * pcs")

    @field_validator("amount", "pcs", "rate", mode="before")
    @classmethod
    def _coerce_number(cls, v):
        """LLMs love returning '1,234.50' or '$10.00' — clean it up."""
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("₹", "").strip()
            if v in ("", "-", "n/a", "N/A", "null", "None"):
                return 0.0
        return v


class InvoiceExtraction(BaseModel):
    """The structured result we ask the model to produce for one invoice."""

    invoice_number: str = Field(description="Unique invoice identifier from the document")
    company_name: str = Field(default="", description="Company that issued the invoice")
    vendor_name: Optional[str] = Field(default=None, description="Vendor name")

    receiver_details: Optional[str] = Field(default=None, description="Details of receiver/Billed to")
    state: Optional[str] = Field(default=None, description="State of the buyer")
    broker: Optional[str] = Field(default=None, description="Broker name")
    e_way_bill_number: Optional[str] = Field(default=None, alias="e-way_bill_number", description="e-Way Bill Number")
    challan_no: Optional[str] = Field(default=None, description="Challan No")
    
    sale_avg_rate: float = Field(default=0.0, description="Sale avg rate")
    bill_amount: float = Field(default=0.0, description="Total Bill Amount")

    invoice_date: Optional[date] = Field(default=None)
    due_date: Optional[date] = Field(default=None)

    line_items: List[LineItem] = Field(default_factory=list)

    # The model's own confidence that the extraction is correct (0..1).
    confidence: float = Field(default=0.5, ge=0, le=1)

    @field_validator("bill_amount", "sale_avg_rate", mode="before")
    @classmethod
    def _coerce_number(cls, v):
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("₹", "").strip()
            if v in ("", "-", "n/a", "N/A", "null", "None"):
                return 0.0
        return v

    @field_validator("invoice_date", "due_date", mode="before")
    @classmethod
    def _empty_date_to_none(cls, v):
        if v in ("", "n/a", "N/A", "null", "None", "unknown"):
            return None
        return v

    @model_validator(mode="after")
    def _compute_due_date(self) -> "InvoiceExtraction":
        if self.invoice_date and not self.due_date:
            # Default to Net 30 terms if due date is missing
            self.due_date = self.invoice_date + timedelta(days=30)
        return self
