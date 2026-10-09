"""
Guardrails engine — the safety layer between the LLM and the database.

It loads rules from guardrails.yaml and evaluates a validated
`InvoiceExtraction` against them. It returns a `GuardrailReport` describing:
  * violations classified as "block" (quarantine) or "warn" (store + flag)
  * a possibly-modified copy of the invoice (e.g. PII-redacted raw text)

Nothing here calls the LLM or the DB — it is pure, testable business logic.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import yaml

from . import config
from .schemas import InvoiceExtraction


@dataclass
class Violation:
    rule: str
    severity: str  # "block" | "warn"
    message: str


@dataclass
class GuardrailReport:
    invoice: InvoiceExtraction
    redacted_raw_text: str
    violations: List[Violation] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(v.severity == "block" for v in self.violations)

    @property
    def needs_review(self) -> bool:
        return bool(self.violations)  # any warning still merits a human glance

    def summary(self) -> str:
        if not self.violations:
            return "OK"
        return "; ".join(f"[{v.severity}] {v.rule}: {v.message}" for v in self.violations)


def load_rules(path=None) -> dict:
    path = path or config.GUARDRAILS_FILE
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Guardrails:
    def __init__(self, rules: Optional[dict] = None):
        self.rules = rules if rules is not None else load_rules()
        self.enforcement = self.rules.get("enforcement", {})

    # -- helpers --------------------------------------------------------------
    def _severity(self, key: str, default: str = "block") -> str:
        return self.enforcement.get(key, default)

    def _redact(self, text: str) -> str:
        pii = self.rules.get("pii", {})
        if not pii.get("redact_in_raw_text"):
            return text
        for pat in pii.get("patterns", []):
            try:
                text = re.sub(pat["regex"], "[REDACTED]", text)
            except re.error:
                continue
        return text

    # -- individual checks ----------------------------------------------------
    def _check_required(self, inv: InvoiceExtraction, out: List[Violation]) -> None:
        for f in self.rules.get("required_fields", []):
            value = getattr(inv, f, None)
            if value in (None, "", 0):
                out.append(
                    Violation("required_fields", self._severity("required_fields"),
                              f"missing required field '{f}'")
                )

    def _check_field_bounds(self, inv: InvoiceExtraction, out: List[Violation]) -> None:
        for fname, spec in (self.rules.get("fields") or {}).items():
            value = getattr(inv, fname, None)
            if value is None:
                continue
            if "min" in spec and value < spec["min"]:
                out.append(Violation("field_bounds", self._severity("field_bounds"),
                                     f"{fname}={value} below min {spec['min']}"))
            if "max" in spec and value > spec["max"]:
                out.append(Violation("field_bounds", self._severity("field_bounds"),
                                     f"{fname}={value} above max {spec['max']}"))

    def _check_currency(self, inv: InvoiceExtraction, out: List[Violation]) -> None:
        pass

    def _check_arithmetic(self, inv: InvoiceExtraction, out: List[Violation]) -> None:
        cfg = self.rules.get("arithmetic", {})
        if not cfg.get("enabled"):
            return
        tol = float(cfg.get("tolerance", 0.02))
        sev = self._severity("arithmetic", "warn")

        if cfg.get("check_line_items_sum") and inv.line_items:
            line_sum = round(sum(li.amount for li in inv.line_items), 2)
            if getattr(inv, "bill_amount", None) and abs(line_sum - inv.bill_amount) > tol:
                out.append(Violation("arithmetic", sev,
                                     f"line items sum {line_sum} != bill_amount {inv.bill_amount}"))

    def _check_dates(self, inv: InvoiceExtraction, out: List[Violation]) -> None:
        cfg = self.rules.get("dates", {})
        if not cfg.get("enabled"):
            return
        sev = self._severity("dates", "warn")
        today = dt.date.today()

        if inv.invoice_date:
            age = (today - inv.invoice_date).days
            if age > int(cfg.get("max_age_days", 3650)):
                out.append(Violation("dates", sev, f"invoice_date {inv.invoice_date} too old"))
            if -age > int(cfg.get("max_future_days", 30)):
                out.append(Violation("dates", sev,
                                     f"invoice_date {inv.invoice_date} too far in future"))

        if inv.invoice_date and inv.due_date and inv.due_date < inv.invoice_date:
            due_sev = cfg.get("due_after_invoice", "warn")
            if due_sev in ("warn", "block"):
                out.append(Violation("dates", due_sev,
                                     f"due_date {inv.due_date} before invoice_date {inv.invoice_date}"))

    def _check_confidence(self, inv: InvoiceExtraction, out: List[Violation]) -> None:
        cfg = self.rules.get("confidence", {})
        min_store = float(cfg.get("min_to_store", 0.0))
        if inv.confidence < min_store:
            out.append(Violation("confidence", self._severity("confidence"),
                                 f"confidence {inv.confidence} below min_to_store {min_store}"))

    # -- public API -----------------------------------------------------------
    def evaluate(self, inv: InvoiceExtraction, raw_text: str = "") -> GuardrailReport:
        violations: List[Violation] = []
        checks: List[Callable[[InvoiceExtraction, List[Violation]], None]] = [
            self._check_required,
            self._check_field_bounds,
            self._check_currency,
            self._check_arithmetic,
            self._check_dates,
            self._check_confidence,
        ]
        for check in checks:
            check(inv, violations)

        return GuardrailReport(
            invoice=inv,
            redacted_raw_text=self._redact(raw_text),
            violations=violations,
        )

    def auto_approve_status(self, inv: InvoiceExtraction) -> str:
        """Decide the stored status based on confidence and amount thresholds."""
        # 1. Check confidence
        conf_threshold = float(self.rules.get("confidence", {}).get("min_auto_approve", 0.75))
        if inv.confidence < conf_threshold:
            return "needs_review"
            
        # 2. Check bill amount limit for auto-approval
        amount_limit = self.rules.get("fields", {}).get("bill_amount", {}).get("max_auto_approve")
        if amount_limit is not None and inv.bill_amount is not None:
            if inv.bill_amount > float(amount_limit):
                return "needs_review"
                
        return "stored"
