from enum import StrEnum


class DecisionStatus(StrEnum):
    APPROVED = "approved"
    MANUAL_REVIEW = "manual_review"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


class RuleStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    MANUAL_REVIEW = "manual_review"


class InvoiceType(StrEnum):
    STANDARD_INVOICE = "standard_invoice"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"


class PurchaseOrderStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_INVOICED = "partially_invoiced"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class VendorStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"
    DELETED = "deleted"
