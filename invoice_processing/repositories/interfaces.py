from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal

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


class RepositoryError(RuntimeError):
    """Base repository exception."""


class RepositoryUnavailableError(RepositoryError):
    """Raised when reference data cannot be loaded."""


class ReferenceDataCorruptError(RepositoryError):
    """Raised when reference data exists but fails validation."""


class DuplicateReferenceRecordError(RepositoryError):
    """Raised when reference data contains duplicate records that should be unique."""


class PurchaseOrderDataInconsistentError(RepositoryError):
    """Raised when PO header and line state is internally inconsistent."""


class TaxRuleNotFoundError(RepositoryError):
    """Raised when a required tax rule cannot be found."""


class CurrencyNotSupportedError(RepositoryError):
    """Raised when a currency is unsupported."""


class FxRateUnavailableError(RepositoryError):
    """Raised when an FX rate is unavailable."""


class ApprovalPolicyNotFoundError(RepositoryError):
    """Raised when no approval policy exists."""


class ApproverNotFoundError(RepositoryError):
    """Raised when no approver can be resolved."""


class VendorRepository(ABC):
    @abstractmethod
    def get_by_id(self, vendor_id: str) -> Vendor | None:
        raise NotImplementedError

    @abstractmethod
    def find_by_normalized_name(self, name: str) -> list[Vendor]:
        raise NotImplementedError

    @abstractmethod
    def find_by_tax_id(self, tax_id: str) -> list[Vendor]:
        raise NotImplementedError

    @abstractmethod
    def find_duplicate_ids(self, vendor_id: str) -> list[Vendor]:
        raise NotImplementedError


class PurchaseOrderRepository(ABC):
    @abstractmethod
    def get_by_number(self, po_number: str) -> PurchaseOrder | None:
        raise NotImplementedError

    @abstractmethod
    def get_lines(self, po_number: str) -> list[PurchaseOrderLineItem]:
        raise NotImplementedError


class InvoiceRepository(ABC):
    @abstractmethod
    def find_by_vendor_and_invoice_number(
        self, vendor_id: str, invoice_number: str
    ) -> list[ProcessedInvoice]:
        raise NotImplementedError

    @abstractmethod
    def find_by_vendor_group_and_invoice_number(
        self, vendor_group_id: str, invoice_number: str
    ) -> list[ProcessedInvoice]:
        raise NotImplementedError

    @abstractmethod
    def find_possible_duplicates(
        self,
        vendor_id: str,
        amount: Decimal,
        currency: str,
        invoice_date: date,
        date_window_days: int,
    ) -> list[ProcessedInvoice]:
        raise NotImplementedError

    @abstractmethod
    def find_same_invoice_number(self, invoice_number: str) -> list[ProcessedInvoice]:
        raise NotImplementedError

    @abstractmethod
    def find_by_line_fingerprint(self, fingerprint: str) -> list[ProcessedInvoice]:
        raise NotImplementedError

    @abstractmethod
    def get_prior_invoiced_lines(self, po_number: str) -> list[ProcessedInvoiceLine]:
        raise NotImplementedError

    @abstractmethod
    def find_similar(self, vendor_id: str, total: str, currency: str) -> list[ProcessedInvoice]:
        raise NotImplementedError

    @abstractmethod
    def record_processed_invoice(
        self,
        invoice: ProcessedInvoice,
        lines: list[ProcessedInvoiceLine],
    ) -> bool:
        """Persist an approved/processed invoice.

        Returns True when a new history record was written. Returns False when
        the same vendor/invoice-number record already exists, making the write
        idempotent for repeated application-service calls.
        """
        raise NotImplementedError


class GoodsReceiptRepository(ABC):
    @abstractmethod
    def get_receipts_by_po(self, po_number: str) -> list[GoodsReceipt]:
        raise NotImplementedError

    @abstractmethod
    def get_received_quantity(self, po_number: str, po_line_id: str) -> Decimal:
        raise NotImplementedError


class TaxRepository(ABC):
    @abstractmethod
    def get_tax_rule(
        self, jurisdiction: str, item_category: str, effective_date: date
    ) -> TaxRule | None:
        raise NotImplementedError


class CurrencyRepository(ABC):
    @abstractmethod
    def is_supported(self, currency: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_rate(
        self, from_currency: str, to_currency: str, rate_date: date
    ) -> CurrencyRate | None:
        raise NotImplementedError


class ApprovalRepository(ABC):
    @abstractmethod
    def get_approval_policy(self, department: str, currency: str) -> ApprovalPolicy:
        raise NotImplementedError

    @abstractmethod
    def resolve_approver(
        self, amount: Decimal, risk_level: str, department: str
    ) -> str | None:
        raise NotImplementedError
