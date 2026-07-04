from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from invoice_processing.domain.enums import (
    DecisionStatus,
    InvoiceType,
    PurchaseOrderStatus,
    RuleStatus,
    Severity,
    VendorStatus,
)


class MoneyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class InvoiceLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    quantity: Decimal = Field(gt=Decimal("0"))
    unit_price: Decimal = Field(ge=Decimal("0"))
    total: Decimal = Field(ge=Decimal("0"))
    po_line_id: str | None = None
    product_code: str | None = None
    sku: str | None = None
    item_category: str | None = None
    uom: str | None = None
    discount: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    freight: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = None
    invoice_type: InvoiceType = InvoiceType.STANDARD_INVOICE
    source_channel: str = "json"
    invoice_date: date | None = None
    due_date: date | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    po_number: str | None = None
    subtotal: Decimal | None = Field(default=None, ge=Decimal("0"))
    tax: Decimal | None = Field(default=None, ge=Decimal("0"))
    tax_rate: Decimal | None = Field(default=None, ge=Decimal("0"))
    discount: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    freight: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    total: Decimal | None = Field(default=None, ge=Decimal("0"))
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    vendor_tax_id: str | None = None
    department: str = "default"
    cost_center: str | None = None
    jurisdiction: str = "US"
    goods_receipt_required: bool = True
    line_items: list[InvoiceLineItem] = Field(default_factory=list)
    source_file: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: str
    name: str
    status: VendorStatus
    currency: str
    supported_currencies: list[str] = Field(default_factory=list)
    approval_status: str = "approved"
    payment_hold: bool = False
    soft_payment_hold: bool = False
    tax_id: str | None = None
    gst_number: str | None = None
    gst_compliance_status: str = "cleared"
    gst_last_cleared_month: str | None = None
    gst_pending_months: list[str] = Field(default_factory=list)
    tax_profile_complete: bool = True
    country: str = "US"
    supported_country: bool = True
    bank_last_changed_at: date | None = None
    bank_change_review_days: int = 7
    deleted: bool = False
    vendor_group_id: str | None = None
    allowed_po_tolerance_percent: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0"))
    allowed_po_tolerance_amount: Decimal = Field(default=Decimal("25.00"), ge=Decimal("0"))
    payment_terms_days: int = Field(default=30, ge=0)
    tax_required: bool = True

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("supported_currencies")
    @classmethod
    def normalize_supported_currencies(cls, value: list[str]) -> list[str]:
        return [currency.upper() for currency in value]


class PurchaseOrderLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_line_id: str
    description: str
    product_code: str | None = None
    sku: str | None = None
    item_category: str = "standard"
    uom: str | None = None
    quantity_ordered: Decimal = Field(gt=Decimal("0"))
    quantity_invoiced: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    unit_price: Decimal = Field(ge=Decimal("0"))
    line_total: Decimal = Field(ge=Decimal("0"))


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_number: str
    vendor_id: str
    status: PurchaseOrderStatus
    currency: str
    start_date: date | None = None
    end_date: date | None = None
    approved: bool = True
    department: str = "default"
    cost_center: str | None = None
    deleted: bool = False
    archived: bool = False
    allows_multi_po_invoice: bool = False
    total_amount: Decimal = Field(ge=Decimal("0"))
    invoiced_amount: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    tolerance_percent: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0"))
    tolerance_amount: Decimal = Field(default=Decimal("25.00"), ge=Decimal("0"))
    line_items: list[PurchaseOrderLineItem] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class ProcessedInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    vendor_id: str
    invoice_date: date
    total: Decimal = Field(ge=Decimal("0"))
    currency: str
    po_number: str | None = None
    vendor_group_id: str | None = None
    line_fingerprint: str | None = None
    cancelled_replacement_id: str | None = None
    decision: DecisionStatus
    processed_at: datetime


class ProcessedInvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_number: str
    po_line_id: str
    quantity: Decimal = Field(ge=Decimal("0"))
    amount: Decimal = Field(ge=Decimal("0"))


class GoodsReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    po_number: str
    po_line_id: str
    quantity_received: Decimal = Field(ge=Decimal("0"))
    quantity_invoiced: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    received_at: date


class TaxRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jurisdiction: str
    item_category: str
    tax_rate: Decimal = Field(ge=Decimal("0"))
    effective_from: date
    effective_to: date | None = None


class CurrencyRate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_currency: str
    to_currency: str
    rate: Decimal = Field(gt=Decimal("0"))
    rate_date: date


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department: str
    auto_approve_max_amount: Decimal = Field(ge=Decimal("0"))
    manual_review_risk_threshold: int = Field(ge=0, le=100)
    reject_risk_threshold: int = Field(ge=0, le=100)
    approver_roles: list[dict[str, str]] = Field(default_factory=list)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: Severity
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RuleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_name: str
    status: RuleStatus
    severity: Severity
    expected: str
    actual: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == RuleStatus.PASS


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    status: RuleStatus
    rule_results: list[RuleResult] = Field(default_factory=list)


class ValidationSection(BaseModel):
    schema_valid: bool | None = None
    required_fields_valid: bool | None = None
    math_valid: bool | None = None
    schema_results: list[RuleResult] = Field(default_factory=list)
    required_field_results: list[RuleResult] = Field(default_factory=list)
    math_results: list[RuleResult] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class VendorSection(BaseModel):
    matched: bool | None = None
    vendor: Vendor | None = None
    rule_results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class DuplicateSection(BaseModel):
    is_duplicate: bool | None = None
    matches: list[ProcessedInvoice] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    classification: str | None = None
    findings: list[Finding] = Field(default_factory=list)


class HistorySection(BaseModel):
    identity_results: list[RuleResult] = Field(default_factory=list)
    historical_invoices: list[ProcessedInvoice] = Field(default_factory=list)
    prior_po_lines: list[ProcessedInvoiceLine] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class LineConsolidationSection(BaseModel):
    normalized_lines: list[dict[str, Any]] = Field(default_factory=list)
    consolidated_lines: list[dict[str, Any]] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class FinancialValidationSection(BaseModel):
    rule_results: list[RuleResult] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class PurchaseOrderMatchSection(BaseModel):
    matched: bool | None = None
    purchase_order: PurchaseOrder | None = None
    amount_delta: Decimal | None = None
    within_tolerance: bool | None = None
    header_results: list[RuleResult] = Field(default_factory=list)
    line_results: list[RuleResult] = Field(default_factory=list)
    balance_results: list[RuleResult] = Field(default_factory=list)
    line_matches: list[dict[str, Any]] = Field(default_factory=list)
    balance_calculations: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class GoodsReceiptSection(BaseModel):
    required: bool | None = None
    receipts: list[GoodsReceipt] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class TaxSection(BaseModel):
    rule_results: list[RuleResult] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class CurrencySection(BaseModel):
    rule_results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class PaymentTermsSection(BaseModel):
    rule_results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class ApprovalSection(BaseModel):
    policy: ApprovalPolicy | None = None
    requires_approval: bool | None = None
    approver_role: str | None = None
    rule_results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class BusinessRulesSection(BaseModel):
    passed: bool | None = None
    findings: list[Finding] = Field(default_factory=list)


class RiskSection(BaseModel):
    score: int | None = Field(default=None, ge=0, le=100)
    level: str | None = None
    reasons: list[str] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)


class DecisionSection(BaseModel):
    status: DecisionStatus | None = None
    reason: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    requires_human_review: bool | None = None
    validation_errors: list[str] = Field(default_factory=list)


class AuditSection(BaseModel):
    run_id: str | None = None
    generated_at: datetime | None = None
    summary: str | None = None
    stage_results: list[dict[str, Any]] = Field(default_factory=list)


class ProcessingLog(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class RuleEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str | None = None
    rule: str
    expected: str
    actual: str
    passed: bool
    details: dict[str, Any] = Field(default_factory=dict)


class StageExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_number: int
    stage_name: str
    purpose: str
    input_received: dict[str, Any] = Field(default_factory=dict)
    repositories_accessed: list[str] = Field(default_factory=list)
    validation_rules_executed: list[RuleEvaluation] = Field(default_factory=list)
    comparisons_performed: list[dict[str, Any]] = Field(default_factory=list)
    intermediate_calculations: list[dict[str, Any]] = Field(default_factory=list)
    output_produced: dict[str, Any] = Field(default_factory=dict)
    result: str
    conclusion_reason: str
    processing_time_ms: float
    validations_executed: int
    validations_passed: int
    validations_failed: int


class InputMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    source: str
    source_type: str
    file_size_bytes: int
    checksum_sha256: str
    loaded_at: datetime
    content_type: str = "application/json"


class InputSection(BaseModel):
    loaded: bool | None = None
    raw_payload: dict[str, Any] | None = None
    metadata: InputMetadata | None = None
    findings: list[Finding] = Field(default_factory=list)


class ProcessingContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    input: InputSection = Field(default_factory=InputSection)
    raw_invoice_data: dict[str, Any] | None = None
    invoice: Invoice | None = None
    validation: ValidationSection = Field(default_factory=ValidationSection)
    history: HistorySection = Field(default_factory=HistorySection)
    vendor: VendorSection = Field(default_factory=VendorSection)
    duplicate: DuplicateSection = Field(default_factory=DuplicateSection)
    line_consolidation: LineConsolidationSection = Field(default_factory=LineConsolidationSection)
    financial_validation: FinancialValidationSection = Field(default_factory=FinancialValidationSection)
    po_match: PurchaseOrderMatchSection = Field(default_factory=PurchaseOrderMatchSection)
    goods_receipt: GoodsReceiptSection = Field(default_factory=GoodsReceiptSection)
    tax: TaxSection = Field(default_factory=TaxSection)
    currency: CurrencySection = Field(default_factory=CurrencySection)
    payment_terms: PaymentTermsSection = Field(default_factory=PaymentTermsSection)
    approval: ApprovalSection = Field(default_factory=ApprovalSection)
    business_rules: BusinessRulesSection = Field(default_factory=BusinessRulesSection)
    risk: RiskSection = Field(default_factory=RiskSection)
    decision: DecisionSection = Field(default_factory=DecisionSection)
    audit: AuditSection = Field(default_factory=AuditSection)
    logs: list[ProcessingLog] = Field(default_factory=list)
    execution_trace: list[StageExecutionTrace] = Field(default_factory=list)

    def add_log(self, stage: str, message: str, **data: Any) -> None:
        self.logs.append(ProcessingLog(stage=stage, message=message, data=data))


class DecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None
    vendor_id: str | None
    po_number: str | None
    status: DecisionStatus
    reason: str
    confidence: float
    risk_score: int
    risk_level: str
    requires_human_review: bool
    findings: list[Finding]


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    generated_at: datetime
    invoice: Invoice | None
    decision: DecisionSection
    validation: ValidationSection
    vendor: VendorSection
    duplicate: DuplicateSection
    po_match: PurchaseOrderMatchSection
    goods_receipt: GoodsReceiptSection | None = None
    tax: TaxSection | None = None
    currency: CurrencySection | None = None
    payment_terms: PaymentTermsSection | None = None
    approval: ApprovalSection | None = None
    business_rules: BusinessRulesSection
    risk: RiskSection
    logs: list[ProcessingLog]
    execution_trace: list[StageExecutionTrace] = Field(default_factory=list)


class InvoiceEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    invoice: Invoice | None = None

    @model_validator(mode="after")
    def allow_bare_invoice(self) -> InvoiceEnvelope:
        return self
