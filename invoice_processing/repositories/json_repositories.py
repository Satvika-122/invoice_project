from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from invoice_processing.config.settings import ApprovalSettings
from invoice_processing.domain.models import (
    ApprovalPolicy,
    CurrencyRate,
    GoodsReceipt,
    ProcessedInvoice,
    ProcessedInvoiceLine,
    PurchaseOrder,
    PurchaseOrderLineItem,
    TaxRule,
    Vendor,
)
from invoice_processing.repositories.interfaces import (
    ApprovalPolicyNotFoundError,
    ApprovalRepository,
    CurrencyRepository,
    DuplicateReferenceRecordError,
    GoodsReceiptRepository,
    InvoiceRepository,
    PurchaseOrderRepository,
    ReferenceDataCorruptError,
    RepositoryUnavailableError,
    TaxRepository,
    VendorRepository,
)

T = TypeVar("T", bound=BaseModel)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepositoryUnavailableError(f"Reference data file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReferenceDataCorruptError(f"Invalid JSON in reference data file: {path}") from exc


def _load_list(path: Path, model: type[T]) -> list[T]:
    try:
        return TypeAdapter(list[model]).validate_python(_load_json(path))  # type: ignore[valid-type]
    except ValidationError as exc:
        raise ReferenceDataCorruptError(f"Invalid records in reference data file: {path}") from exc


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


class JsonVendorRepository(VendorRepository):
    """JSON-backed vendor repository."""

    def __init__(self, path: Path) -> None:
        self._vendors = _load_list(path, Vendor)

    def get_by_id(self, vendor_id: str) -> Vendor | None:
        matches = [vendor for vendor in self._vendors if vendor.vendor_id == vendor_id]
        if len(matches) > 1:
            raise DuplicateReferenceRecordError(f"Duplicate vendor ID found: {vendor_id}")
        return matches[0] if matches else None

    def find_by_normalized_name(self, name: str) -> list[Vendor]:
        normalized = _norm(name)
        return [vendor for vendor in self._vendors if _norm(vendor.name) == normalized]

    def find_by_tax_id(self, tax_id: str) -> list[Vendor]:
        return [vendor for vendor in self._vendors if vendor.tax_id == tax_id]

    def find_duplicate_ids(self, vendor_id: str) -> list[Vendor]:
        return [vendor for vendor in self._vendors if vendor.vendor_id == vendor_id]

    def get_by_name(self, name: str) -> Vendor | None:
        matches = self.find_by_normalized_name(name)
        return matches[0] if len(matches) == 1 else None


class JsonPurchaseOrderRepository(PurchaseOrderRepository):
    """JSON-backed purchase order repository."""

    def __init__(self, path: Path) -> None:
        self._purchase_orders = _load_list(path, PurchaseOrder)

    def get_by_number(self, po_number: str) -> PurchaseOrder | None:
        matches = [po for po in self._purchase_orders if po.po_number == po_number]
        if len(matches) > 1:
            raise DuplicateReferenceRecordError(f"Duplicate PO number found: {po_number}")
        return matches[0] if matches else None

    def get_lines(self, po_number: str) -> list[PurchaseOrderLineItem]:
        po = self.get_by_number(po_number)
        return po.line_items if po else []


class JsonInvoiceRepository(InvoiceRepository):
    """JSON-backed processed invoice repository."""

    def __init__(self, path: Path) -> None:
        self._path = path
        raw = _load_json(path)
        if isinstance(raw, dict):
            invoices_raw = raw.get("invoices", [])
            lines_raw = raw.get("lines", [])
        else:
            invoices_raw = raw
            lines_raw = []
        try:
            self._processed_invoices = TypeAdapter(list[ProcessedInvoice]).validate_python(invoices_raw)
            self._processed_lines = TypeAdapter(list[ProcessedInvoiceLine]).validate_python(lines_raw)
        except ValidationError as exc:
            raise ReferenceDataCorruptError(f"Invalid processed invoice reference data: {path}") from exc

    def find_by_vendor_and_invoice_number(
        self, vendor_id: str, invoice_number: str
    ) -> list[ProcessedInvoice]:
        return [
            invoice
            for invoice in self._processed_invoices
            if invoice.vendor_id == vendor_id and invoice.invoice_number == invoice_number
        ]

    def find_by_vendor_group_and_invoice_number(
        self, vendor_group_id: str, invoice_number: str
    ) -> list[ProcessedInvoice]:
        return [
            invoice
            for invoice in self._processed_invoices
            if invoice.vendor_group_id == vendor_group_id and invoice.invoice_number == invoice_number
        ]

    def find_possible_duplicates(
        self,
        vendor_id: str,
        amount: Decimal,
        currency: str,
        invoice_date: date,
        date_window_days: int,
    ) -> list[ProcessedInvoice]:
        return [
            invoice
            for invoice in self._processed_invoices
            if invoice.vendor_id == vendor_id
            and invoice.currency == currency.upper()
            and invoice.total == amount
            and abs((invoice.invoice_date - invoice_date).days) <= date_window_days
        ]

    def find_same_invoice_number(self, invoice_number: str) -> list[ProcessedInvoice]:
        return [
            invoice for invoice in self._processed_invoices if invoice.invoice_number == invoice_number
        ]

    def find_by_line_fingerprint(self, fingerprint: str) -> list[ProcessedInvoice]:
        return [
            invoice for invoice in self._processed_invoices if invoice.line_fingerprint == fingerprint
        ]

    def get_prior_invoiced_lines(self, po_number: str) -> list[ProcessedInvoiceLine]:
        return [line for line in self._processed_lines if line.po_number == po_number]

    def find_similar(self, vendor_id: str, total: str, currency: str) -> list[ProcessedInvoice]:
        return [
            invoice
            for invoice in self._processed_invoices
            if invoice.vendor_id == vendor_id
            and str(invoice.total) == total
            and invoice.currency == currency.upper()
        ]

    def record_processed_invoice(
        self,
        invoice: ProcessedInvoice,
        lines: list[ProcessedInvoiceLine],
    ) -> bool:
        """Append a processed invoice record to the JSON history store."""
        existing = self.find_by_vendor_and_invoice_number(invoice.vendor_id, invoice.invoice_number)
        if existing:
            return False

        self._processed_invoices.append(invoice)
        self._processed_lines.extend(lines)
        self._persist()
        return True

    def _persist(self) -> None:
        payload = {
            "invoices": [
                invoice.model_dump(mode="json")
                for invoice in self._processed_invoices
            ],
            "lines": [
                line.model_dump(mode="json")
                for line in self._processed_lines
            ],
        }
        try:
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            raise RepositoryUnavailableError(f"Could not write processed invoice history: {self._path}") from exc


class JsonGoodsReceiptRepository(GoodsReceiptRepository):
    """JSON-backed goods receipt repository."""

    def __init__(self, path: Path) -> None:
        self._receipts = _load_list(path, GoodsReceipt)

    def get_receipts_by_po(self, po_number: str) -> list[GoodsReceipt]:
        return [receipt for receipt in self._receipts if receipt.po_number == po_number]

    def get_received_quantity(self, po_number: str, po_line_id: str) -> Decimal:
        return sum(
            (
                receipt.quantity_received
                for receipt in self._receipts
                if receipt.po_number == po_number and receipt.po_line_id == po_line_id
            ),
            Decimal("0"),
        )


class JsonTaxRepository(TaxRepository):
    """JSON-backed tax rule repository."""

    def __init__(self, path: Path) -> None:
        self._rules = _load_list(path, TaxRule)

    def get_tax_rule(
        self, jurisdiction: str, item_category: str, effective_date: date
    ) -> TaxRule | None:
        for rule in self._rules:
            if (
                rule.jurisdiction == jurisdiction
                and rule.item_category == item_category
                and rule.effective_from <= effective_date
                and (rule.effective_to is None or effective_date <= rule.effective_to)
            ):
                return rule
        return None


class JsonCurrencyRepository(CurrencyRepository):
    """JSON-backed currency repository."""

    def __init__(self, path: Path) -> None:
        raw = _load_json(path)
        if not isinstance(raw, dict):
            raise ReferenceDataCorruptError("Currency reference data must be an object.")
        self._supported = [str(item).upper() for item in raw.get("supported_currencies", [])]
        self._rates = TypeAdapter(list[CurrencyRate]).validate_python(raw.get("rates", []))

    def is_supported(self, currency: str) -> bool:
        return currency.upper() in self._supported

    def get_rate(
        self, from_currency: str, to_currency: str, rate_date: date
    ) -> CurrencyRate | None:
        candidates = [
            rate
            for rate in self._rates
            if rate.from_currency == from_currency.upper()
            and rate.to_currency == to_currency.upper()
            and rate.rate_date <= rate_date
        ]
        return max(candidates, key=lambda rate: rate.rate_date, default=None)


class ConfigApprovalRepository(ApprovalRepository):
    """Approval repository backed by loaded approval settings."""

    def __init__(self, settings: ApprovalSettings) -> None:
        self._settings = settings

    def get_approval_policy(self, department: str, currency: str) -> ApprovalPolicy:
        policies = self._settings.departments.get(department) or self._settings.departments.get("default")
        if not policies:
            raise ApprovalPolicyNotFoundError(f"No approval policy configured for {department}.")
        return ApprovalPolicy(
            department=department,
            auto_approve_max_amount=self._settings.auto_approve_max_amount,
            manual_review_risk_threshold=self._settings.manual_review_risk_threshold,
            reject_risk_threshold=self._settings.reject_risk_threshold,
            approver_roles=policies,
        )

    def resolve_approver(
        self, amount: Decimal, risk_level: str, department: str
    ) -> str | None:
        policy = self.get_approval_policy(department, "")
        for row in policy.approver_roles:
            if amount <= Decimal(row["max_amount"]):
                return row["role"]
        return None
