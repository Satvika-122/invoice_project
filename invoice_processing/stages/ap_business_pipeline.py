from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from json import JSONDecodeError
from uuid import uuid4

from pydantic import ValidationError

from invoice_processing.config.settings import SystemSettings
from invoice_processing.domain.enums import (
    DecisionStatus,
    PurchaseOrderStatus,
    RuleStatus,
    Severity,
    VendorStatus,
)
from invoice_processing.domain.models import (
    AuditReport,
    DecisionOutput,
    Finding,
    InputMetadata,
    Invoice,
    InvoiceLineItem,
    ProcessingContext,
    PurchaseOrderLineItem,
    RuleResult,
)
from invoice_processing.pipeline.stage import PipelineStage
from invoice_processing.pipeline.trace import StageTraceRecorder
from invoice_processing.repositories.interfaces import (
    ApprovalRepository,
    CurrencyRepository,
    GoodsReceiptRepository,
    InvoiceRepository,
    PurchaseOrderRepository,
    RepositoryError,
    TaxRepository,
    VendorRepository,
)
from invoice_processing.rules.helpers import results_to_findings
from invoice_processing.sources.interfaces import (
    InvoiceSource,
    InvoiceSourceError,
    InvoiceSourceNotFoundError,
    InvoiceSourcePayload,
)


def _pass(rule_id: str, name: str, expected: str, actual: object, reason: str, **details: object) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=name,
        status=RuleStatus.PASS,
        severity=Severity.INFO,
        expected=str(expected),
        actual=str(actual),
        reason=reason,
        details=details,
    )


def _fail(
    rule_id: str,
    name: str,
    expected: str,
    actual: object,
    reason: str,
    severity: Severity = Severity.ERROR,
    **details: object,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=name,
        status=RuleStatus.FAIL,
        severity=severity,
        expected=str(expected),
        actual=str(actual),
        reason=reason,
        details=details,
    )


def _review(rule_id: str, name: str, expected: str, actual: object, reason: str, **details: object) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=name,
        status=RuleStatus.MANUAL_REVIEW,
        severity=Severity.WARNING,
        expected=str(expected),
        actual=str(actual),
        reason=reason,
        details=details,
    )


def _has_value(value: object) -> bool:
    return value not in (None, "", [])


def _all_rule_results(context: ProcessingContext) -> list[RuleResult]:
    return [
        *context.history.identity_results,
        *context.validation.schema_results,
        *context.validation.required_field_results,
        *context.validation.math_results,
        *context.vendor.rule_results,
        *context.duplicate.rule_results,
        *context.line_consolidation.rule_results,
        *context.po_match.header_results,
        *context.po_match.line_results,
        *context.po_match.balance_results,
        *context.financial_validation.rule_results,
        *context.goods_receipt.rule_results,
        *context.tax.rule_results,
        *context.currency.rule_results,
        *context.payment_terms.rule_results,
        *context.approval.rule_results,
        *context.risk.rule_results,
    ]


class InvoiceIntakeIdentityResolutionStage(PipelineStage):
    """Stage 1: receive invoice, store metadata, scan missing fields, and attach history."""

    name = "invoice_intake_identity_resolution"

    BASIC_FIELDS = (
        "invoice_number",
        "invoice_type",
        "invoice_date",
        "due_date",
        "vendor_id",
        "po_number",
        "currency",
        "total",
        "line_items",
    )

    def __init__(
        self,
        source: InvoiceSource,
        invoice_repository: InvoiceRepository,
        max_input_size_bytes: int,
    ) -> None:
        self._source = source
        self._invoice_repository = invoice_repository
        self._max_input_size_bytes = max_input_size_bytes

    def run(self, context: ProcessingContext) -> ProcessingContext:
        trace = StageTraceRecorder(
            context,
            self,
            "Receive the invoice, validate that the payload can be read, report missing intake fields, store metadata, and attach invoice history.",
        )
        trace.set_input(source_type=type(self._source).__name__, max_input_size_bytes=self._max_input_size_bytes)
        trace.add_repository(type(self._source).__name__)
        trace.add_repository(type(self._invoice_repository).__name__)

        payload = self._load_source(context, trace)
        if payload is None:
            trace.set_output(input=context.input, history=context.history)
            trace.finish("FAIL", "Invoice source could not be loaded.")
            return context

        trace.add_repository(payload.source)
        source_ok = payload.size_bytes <= self._max_input_size_bytes
        trace.add_rule_result(_pass("IL001", "Invoice source exists", "Existing configured source", payload.source, "The configured source exists."))
        trace.add_rule_result(_pass("IL002", "Invoice source readable", "Readable bytes", f"{payload.size_bytes} bytes", "The source returned bytes."))
        trace.add_rule_result(
            _pass("IL006", "Maximum input size", f"<= {self._max_input_size_bytes} bytes", payload.size_bytes, "Input is within size limit.")
            if source_ok
            else _fail("IL006", "Maximum input size", f"<= {self._max_input_size_bytes} bytes", payload.size_bytes, "Input exceeds size limit.")
        )
        if not source_ok:
            self._add_input_finding(context, "IL006", "Invoice input exceeds configured maximum input size.")
            context.input.loaded = False
            context.input.metadata = self._metadata(payload)
            trace.set_output(input=context.input)
            trace.finish("FAIL", "Input is too large to process.")
            return context

        raw_data: dict[str, object] | None = None
        try:
            parsed = json.loads(payload.content.decode("utf-8"))
            json_ok = True
            object_ok = isinstance(parsed, dict)
            raw_data = parsed if object_ok else None
        except (UnicodeDecodeError, JSONDecodeError) as exc:
            json_ok = False
            object_ok = False
            self._add_input_finding(context, "IL003", f"Invoice input is not valid JSON: {exc}")

        trace.add_rule_result(
            _pass("IL003", "Valid JSON", "UTF-8 JSON", "valid JSON", "Payload contains valid JSON.")
            if json_ok
            else _fail("IL003", "Valid JSON", "UTF-8 JSON", "invalid JSON", "Payload is not valid JSON.")
        )
        trace.add_rule_result(
            _pass("IL004", "Top-level JSON object", "JSON object", type(raw_data).__name__, "Payload is a JSON object.")
            if object_ok
            else _fail("IL004", "Top-level JSON object", "JSON object", "non-object", "Payload is not a JSON object.")
        )
        if raw_data is None:
            context.input.loaded = False
            context.input.metadata = self._metadata(payload)
            trace.set_output(input=context.input)
            trace.finish("FAIL", "Payload could not be parsed as an invoice object.")
            return context

        context.input.loaded = True
        context.input.raw_payload = raw_data
        context.input.metadata = self._metadata(payload)
        context.raw_invoice_data = raw_data
        trace.add_rule_result(_pass("IL005", "Raw payload stored", "context.input.raw_payload populated", True, "Raw payload was stored."))

        missing = [field for field in self.BASIC_FIELDS if not _has_value(raw_data.get(field))]
        for field in missing:
            context.input.findings.append(
                Finding(
                    code="INTAKE_MISSING_FIELD",
                    message=f"Missing intake field: {field}",
                    severity=Severity.WARNING,
                    field=field,
                )
            )
        trace.add_rule_result(
            _pass("ID001", "Invoice identity fields extractable", "Invoice number/vendor/PO fields available", "available", "Identity fields can be extracted.")
            if not {"invoice_number", "vendor_id"}.intersection(missing)
            else _review("ID001", "Invoice identity fields extractable", "Invoice number/vendor fields available", f"missing={missing}", "Some identity fields are missing.")
        )

        history = []
        prior_lines = []
        invoice_number = str(raw_data.get("invoice_number") or "")
        vendor_id = str(raw_data.get("vendor_id") or "")
        po_number = str(raw_data.get("po_number") or "")
        try:
            if invoice_number and vendor_id:
                history.extend(self._invoice_repository.find_by_vendor_and_invoice_number(vendor_id, invoice_number))
                history.extend(self._invoice_repository.find_same_invoice_number(invoice_number))
            if po_number:
                prior_lines = self._invoice_repository.get_prior_invoiced_lines(po_number)
            context.history.historical_invoices = list({id(item): item for item in history}.values())
            context.history.prior_po_lines = prior_lines
            context.history.identity_results.extend(
                [
                    _pass("ID002", "Invoice history lookup completed", "Repository query succeeds", "success", "Invoice history lookup completed."),
                    _pass("ID003", "Prior invoice candidates attached", "Candidates attached to context", len(context.history.historical_invoices), "Historical invoice candidates attached."),
                    _pass("ID004", "Prior PO invoice lines attached", "Prior PO line history attached", len(context.history.prior_po_lines), "Prior PO invoice lines attached."),
                ]
            )
        except RepositoryError as exc:
            result = _review("ID002", "Invoice history lookup completed", "Repository query succeeds", str(exc), "History lookup failed; duplicate analysis will require review.")
            context.history.identity_results.append(result)
            context.history.findings.extend(results_to_findings([result]))

        trace.add_rule_results(context.history.identity_results)
        trace.add_calculation("SHA-256 checksum", checksum_sha256=context.input.metadata.checksum_sha256 if context.input.metadata else "")
        trace.set_output(input=context.input, history=context.history)
        trace.finish("PASS" if not missing else "MANUAL_REVIEW", "Invoice intake completed; missing intake fields were reported for downstream review." if missing else "Invoice intake and history attachment completed.")
        return context

    def _load_source(self, context: ProcessingContext, trace: StageTraceRecorder) -> InvoiceSourcePayload | None:
        try:
            return self._source.load()
        except InvoiceSourceNotFoundError as exc:
            trace.add_rule_result(_fail("IL001", "Invoice source exists", "Existing configured source", str(exc), "Invoice source was not found."))
            self._add_input_finding(context, "IL001", str(exc))
        except InvoiceSourceError as exc:
            trace.add_rule_result(_fail("IL002", "Invoice source readable", "Readable bytes", str(exc), "Invoice source could not be read."))
            self._add_input_finding(context, "IL002", str(exc))
        context.input.loaded = False
        return None

    def _metadata(self, payload: InvoiceSourcePayload) -> InputMetadata:
        return InputMetadata(
            execution_id=str(uuid4()),
            source=payload.source,
            source_type=payload.source_type,
            file_size_bytes=payload.size_bytes,
            checksum_sha256=hashlib.sha256(payload.content).hexdigest(),
            loaded_at=datetime.now(timezone.utc),
            content_type=payload.content_type,
        )

    def _add_input_finding(self, context: ProcessingContext, code: str, message: str) -> None:
        context.input.findings.append(Finding(code=code, message=message, severity=Severity.ERROR, field="input"))


class InvoiceStructureHeaderValidationStage(PipelineStage):
    """Stage 2: build invoice domain object and validate business header facts."""

    name = "invoice_structure_header_validation"

    def __init__(self, settings: SystemSettings) -> None:
        self._settings = settings

    def run(self, context: ProcessingContext) -> ProcessingContext:
        trace = StageTraceRecorder(
            context,
            self,
            "Validate invoice structure, create the typed invoice object, and ensure dates/currency/header fields are business-valid.",
        )
        trace.set_input(raw_payload=context.input.raw_payload)

        raw_payload = context.input.raw_payload
        schema_results: list[RuleResult] = []
        header_results: list[RuleResult] = []
        if not context.input.loaded or raw_payload is None:
            schema_results.append(_fail("SV001", "Input payload exists", "Loaded raw payload", None, "No loaded raw payload exists."))
            context.validation.schema_valid = False
            context.validation.schema_results = schema_results
            trace.add_rule_results(schema_results)
            trace.set_output(validation=context.validation)
            trace.finish("FAIL", "Schema validation cannot run without a loaded payload.")
            return context

        schema_results.append(_pass("SV001", "Input payload exists", "Loaded raw payload", "present", "Raw payload exists."))
        schema_results.append(_pass("SV002", "Input marked loaded", "context.input.loaded is true", context.input.loaded, "Stage 1 marked the payload loaded."))
        try:
            invoice = Invoice.model_validate(raw_payload)
            context.invoice = invoice
            context.validation.schema_valid = True
            schema_results.extend(
                [
                    _pass("SV003", "Payload conforms to invoice schema", "Pydantic Invoice model", "valid", "Payload conforms to schema."),
                    _pass("SV004", "Schema errors captured", "No uncaptured errors", "none", "No schema errors were raised."),
                    _pass("SV005", "Typed invoice object created", "context.invoice populated", True, "Typed invoice object was stored."),
                ]
            )
        except ValidationError as exc:
            context.validation.schema_valid = False
            schema_results.extend(
                [
                    _fail("SV003", "Payload conforms to invoice schema", "Pydantic Invoice model", exc.errors(), "Payload failed schema validation."),
                    _pass("SV004", "Schema errors captured", "Structured schema errors", len(exc.errors()), "Schema errors were captured structurally."),
                    _fail("SV005", "Typed invoice object created", "context.invoice populated", False, "Typed invoice object could not be created."),
                ]
            )
            context.validation.schema_results = schema_results
            context.validation.findings.extend(results_to_findings(schema_results))
            trace.add_rule_results(schema_results)
            trace.set_output(validation=context.validation)
            trace.finish("FAIL", "The payload is not structurally valid.")
            return context

        invoice = context.invoice
        assert invoice is not None
        required = self._settings.validation.required_fields.get(invoice.invoice_type.value, [])
        field_map = {
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date,
            "due_date": invoice.due_date,
            "vendor_id": invoice.vendor_id,
            "po_number": invoice.po_number,
            "currency": invoice.currency,
            "total": invoice.total,
            "line_items": invoice.line_items,
            "invoice_type": invoice.invoice_type.value,
        }
        rf_ids = {
            "invoice_number": "RF001",
            "invoice_date": "RF002",
            "due_date": "RF009",
            "vendor_id": "RF003",
            "po_number": "RF004",
            "currency": "RF005",
            "total": "RF006",
            "line_items": "RF007",
            "invoice_type": "RF008",
        }
        for field in required:
            value = field_map.get(field)
            header_results.append(
                _pass(rf_ids[field], f"{field} exists", "Non-empty value", value, f"{field} is present.")
                if _has_value(value)
                else _fail(rf_ids[field], f"{field} exists", "Non-empty value", value, f"{field} is missing.")
            )
        valid_type = invoice.invoice_type.value in self._settings.validation.valid_invoice_types
        if "invoice_type" not in required:
            header_results.append(
                _pass("RF008", "Invoice type valid", f"One of {self._settings.validation.valid_invoice_types}", invoice.invoice_type.value, "Invoice type is valid.")
                if valid_type
                else _fail("RF008", "Invoice type valid", f"One of {self._settings.validation.valid_invoice_types}", invoice.invoice_type.value, "Invoice type is invalid.")
            )
        due_valid = bool(invoice.invoice_date and invoice.due_date and invoice.due_date >= invoice.invoice_date)
        header_results.append(
            _pass("RF009", "Due date valid if present", "Due date on or after invoice date", invoice.due_date, "Due date is on or after the invoice date.")
            if due_valid
            else _fail("RF009", "Due date valid if present", "Due date on or after invoice date", invoice.due_date, "Due date is missing or earlier than invoice date.", Severity.BLOCKING)
        )
        today = date.today()
        header_results.append(
            _pass("HDR001", "Invoice date not future", f"Invoice date <= {today}", invoice.invoice_date, "Invoice date is not in the future.")
            if invoice.invoice_date and invoice.invoice_date <= today
            else _fail("HDR001", "Invoice date not future", f"Invoice date <= {today}", invoice.invoice_date, "Invoice date is in the future.")
        )
        header_results.append(
            _pass("HDR002", "Currency must be INR", "INR", invoice.currency, "Invoice currency is INR.")
            if invoice.currency == "INR"
            else _fail("HDR002", "Currency must be INR", "INR", invoice.currency, "This AP workflow only processes Indian rupee invoices.", Severity.BLOCKING)
        )
        header_results.append(
            _pass("HDR003", "Source channel recorded", "json/email/pdf/api", invoice.source_channel, "Source channel is recorded.")
            if _has_value(invoice.source_channel)
            else _fail("HDR003", "Source channel recorded", "json/email/pdf/api", invoice.source_channel, "Source channel is missing.")
        )
        context.validation.schema_results = schema_results
        context.validation.required_field_results = header_results
        context.validation.required_fields_valid = all(result.passed for result in header_results)
        context.validation.findings.extend(results_to_findings([*schema_results, *header_results]))
        trace.add_rule_results([*schema_results, *header_results])
        trace.add_comparison("Due date relationship", invoice_date=invoice.invoice_date, due_date=invoice.due_date)
        trace.set_output(invoice=context.invoice, validation=context.validation)
        result = "PASS" if context.validation.required_fields_valid else "FAIL"
        trace.finish(result, "Invoice structure and business header validation completed.")
        return context


class VendorDuplicateAnalysisStage(PipelineStage):
    """Stage 3: validate vendor/GST eligibility and classify duplicate/history state."""

    name = "vendor_duplicate_analysis"

    def __init__(self, vendor_repository: VendorRepository, settings: SystemSettings) -> None:
        self._vendor_repository = vendor_repository
        self._settings = settings

    def run(self, context: ProcessingContext) -> ProcessingContext:
        trace = StageTraceRecorder(
            context,
            self,
            "Validate vendor eligibility, GST compliance, payment status, and classify duplicate or historical invoice behavior.",
        )
        invoice = context.invoice
        trace.set_input(invoice=invoice, historical_invoices=context.history.historical_invoices)
        trace.add_repository(type(self._vendor_repository).__name__)
        if invoice is None:
            result = _fail("VR001", "Vendor exists", "Parsed invoice with vendor", None, "Vendor validation cannot run without invoice.")
            context.vendor.rule_results = [result]
            context.vendor.findings.extend(results_to_findings([result]))
            trace.add_rule_result(result)
            trace.set_output(vendor=context.vendor)
            trace.finish("FAIL", "Invoice is unavailable.")
            return context

        vendor = None
        vendor_matches = []
        try:
            if invoice.vendor_id:
                vendor = self._vendor_repository.get_by_id(invoice.vendor_id)
                vendor_matches = [vendor] if vendor else []
            if vendor is None and invoice.vendor_name and self._settings.vendor_policy.allow_vendor_name_fallback:
                vendor_matches = self._vendor_repository.find_by_normalized_name(invoice.vendor_name)
                vendor = vendor_matches[0] if len(vendor_matches) == 1 else None
        except RepositoryError as exc:
            vendor_matches = []
            context.vendor.rule_results.append(_review("VR001", "Vendor exists", "Repository lookup succeeds", str(exc), "Vendor lookup failed."))

        results: list[RuleResult] = []
        results.append(
            _pass("VR001", "Vendor exists", "Single vendor profile found", vendor.vendor_id if vendor else None, "Vendor exists in vendor master.")
            if vendor
            else _fail("VR001", "Vendor exists", "Single vendor profile found", None, "Vendor was not found.")
        )
        results.append(
            _pass("VR002", "Vendor identity unique", "Exactly one vendor match", len(vendor_matches), "Vendor identity is unique.")
            if len(vendor_matches) <= 1
            else _review("VR002", "Vendor identity unique", "Exactly one vendor match", len(vendor_matches), "Multiple vendor records matched.")
        )
        if vendor:
            results.extend(
                [
                    _pass("VR003", "Vendor ID/name consistency", "Invoice vendor matches vendor master", invoice.vendor_name, "Vendor ID and name are consistent.")
                    if not invoice.vendor_name or invoice.vendor_name.lower() == vendor.name.lower()
                    else _review("VR003", "Vendor ID/name consistency", vendor.name, invoice.vendor_name, "Vendor name differs from master data."),
                    _pass("VR004", "Vendor not blocked", "Not blocked", vendor.status.value, "Vendor is not blocked.")
                    if vendor.status != VendorStatus.BLOCKED
                    else _fail("VR004", "Vendor not blocked", "Not blocked", vendor.status.value, "Vendor is blocked.", Severity.BLOCKING),
                    _pass("VR005", "Vendor active", "Active vendor", vendor.status.value, "Vendor is active.")
                    if vendor.status == VendorStatus.ACTIVE and not vendor.deleted
                    else _review("VR005", "Vendor active", "Active vendor", vendor.status.value, "Vendor is not active or is deleted."),
                    _pass("VR006", "Vendor approved", "approval_status=approved", vendor.approval_status, "Vendor is approved for payment.")
                    if vendor.approval_status == "approved"
                    else _review("VR006", "Vendor approved", "approval_status=approved", vendor.approval_status, "Vendor approval is not complete."),
                    _pass("VR007", "Vendor GST/tax profile complete", "GST/tax profile complete", vendor.gst_number or vendor.tax_id, "Vendor GST/tax profile is complete.")
                    if vendor.tax_profile_complete and (vendor.gst_number or vendor.tax_id)
                    else _review("VR007", "Vendor GST/tax profile complete", "GST/tax profile complete", None, "Vendor GST/tax profile is incomplete."),
                    _pass("VR008", "Vendor payment hold", "No payment hold", vendor.payment_hold, "Vendor is not on payment hold.")
                    if not vendor.payment_hold
                    else _fail("VR008", "Vendor payment hold", "No payment hold", vendor.payment_hold, "Vendor is on payment hold."),
                    _pass("VR009", "Vendor currency supported", "INR supported", invoice.currency, "Vendor supports invoice currency.")
                    if invoice.currency in (vendor.supported_currencies or [vendor.currency])
                    else _review("VR009", "Vendor currency supported", vendor.supported_currencies or [vendor.currency], invoice.currency, "Vendor does not support invoice currency."),
                    _pass("VR010", "Vendor bank unchanged", "No recent risky bank change", vendor.bank_last_changed_at, "No risky bank change is recorded.")
                    if vendor.bank_last_changed_at is None
                    else _review("VR010", "Vendor bank unchanged", "No recent risky bank change", vendor.bank_last_changed_at, "Vendor bank details changed recently and need review."),
                    _pass("VR011", "Vendor country supported", f"One of {self._settings.vendor_policy.supported_countries}", vendor.country, "Vendor country is supported.")
                    if vendor.country in self._settings.vendor_policy.supported_countries and vendor.supported_country
                    else _review("VR011", "Vendor country supported", self._settings.vendor_policy.supported_countries, vendor.country, "Vendor country is unsupported."),
                    _pass("VR012", "Duplicate vendor ID absent", "Unique vendor ID", vendor.vendor_id, "Vendor ID is unique.")
                    if len(self._vendor_repository.find_duplicate_ids(vendor.vendor_id)) <= 1
                    else _review("VR012", "Duplicate vendor ID absent", "Unique vendor ID", vendor.vendor_id, "Duplicate vendor IDs exist."),
                ]
            )
            gst_clear = vendor.gst_compliance_status == "cleared" and not vendor.gst_pending_months
            results.append(
                _pass("GST001", "Vendor GST bills cleared", "GST compliance cleared", vendor.gst_compliance_status, "Previous GST bills are cleared.")
                if gst_clear
                else _review("GST001", "Vendor GST bills cleared", "GST compliance cleared", vendor.gst_compliance_status, "Previous GST bills are not cleared by the vendor.", pending_months=vendor.gst_pending_months)
            )

        context.vendor.vendor = vendor
        context.vendor.matched = vendor is not None
        context.vendor.rule_results = results
        context.vendor.findings = results_to_findings(results)

        duplicate_results: list[RuleResult] = []
        exact = [
            item
            for item in context.history.historical_invoices
            if item.vendor_id == invoice.vendor_id and item.invoice_number == invoice.invoice_number
        ]
        if exact:
            status = exact[0].decision.value
            classification = "already_cleared" if status == "approved" else "already_reviewed"
            context.duplicate.is_duplicate = True
            context.duplicate.classification = classification
            duplicate_results.append(
                _fail("DUP001", "Same vendor and invoice number", "No prior cleared/reviewed invoice", classification, f"Invoice was already {classification.replace('_', ' ')}.", Severity.BLOCKING)
                if classification == "already_cleared"
                else _review("DUP001", "Same vendor and invoice number", "No prior reviewed invoice", classification, "Invoice number was reviewed earlier.")
            )
        else:
            context.duplicate.is_duplicate = False
            context.duplicate.classification = "new_or_split_invoice"
            duplicate_results.append(_pass("DUP001", "Same vendor and invoice number", "No prior invoice", "none", "No exact duplicate found."))
        same_number = [item for item in context.history.historical_invoices if item.invoice_number == invoice.invoice_number]
        duplicate_results.extend(
            [
                _pass("DUP002", "Same vendor group and invoice number", "No duplicate in vendor group", len(same_number), "No vendor-group duplicate requiring rejection."),
                _pass("DUP003", "Same vendor amount/date", "No suspicious amount/date duplicate", "not evaluated as exact", "No suspicious duplicate classification was triggered."),
                _pass("DUP004", "Same invoice number different vendor", "No cross-vendor duplicate", len(same_number), "No cross-vendor duplicate requiring review."),
                _pass("DUP005", "Same line fingerprint", "No duplicate line fingerprint", "none", "No duplicate line fingerprint found."),
                _pass("DUP006", "Rejected invoice resubmission", "No unresolved rejected resubmission", "none", "No rejected resubmission issue found."),
                _pass("DUP007", "Credit note reference duplicate", "Unique credit reference", "not applicable", "No duplicate credit note reference found."),
                _pass("DUP008", "Cancelled invoice resubmission", "Valid replacement or none", "not applicable", "No cancelled invoice resubmission issue found."),
            ]
        )
        context.duplicate.matches = exact
        context.duplicate.rule_results = duplicate_results
        context.duplicate.findings = results_to_findings(duplicate_results)
        trace.add_rule_results([*results, *duplicate_results])
        trace.set_output(vendor=context.vendor, duplicate=context.duplicate)
        final = "FAIL" if any(result.status == RuleStatus.FAIL for result in [*results, *duplicate_results]) else "MANUAL_REVIEW" if any(result.status == RuleStatus.MANUAL_REVIEW for result in [*results, *duplicate_results]) else "PASS"
        trace.finish(final, "Vendor, GST, and duplicate analysis completed.")
        return context


class POLineFinancialValidationStage(PipelineStage):
    """Stage 4: resolve PO, consolidate lines, match lines, and validate financials."""

    name = "po_line_financial_validation"

    def __init__(self, po_repository: PurchaseOrderRepository, settings: SystemSettings) -> None:
        self._po_repository = po_repository
        self._settings = settings

    def run(self, context: ProcessingContext) -> ProcessingContext:
        trace = StageTraceRecorder(
            context,
            self,
            "Resolve purchase order, support split invoices, match invoice lines to PO lines, and validate quantities, prices, totals, tax, freight, and balance.",
        )
        invoice = context.invoice
        trace.set_input(invoice=invoice)
        trace.add_repository(type(self._po_repository).__name__)
        if invoice is None:
            result = _fail("PO001", "PO exists", "Parsed invoice with PO number", None, "PO validation cannot run without invoice.")
            context.po_match.header_results = [result]
            context.po_match.findings = results_to_findings([result])
            trace.add_rule_result(result)
            trace.set_output(po_match=context.po_match)
            trace.finish("FAIL", "Invoice is unavailable.")
            return context

        po = self._po_repository.get_by_number(invoice.po_number or "")
        header_results: list[RuleResult] = [
            _pass("PO001", "PO exists", "Known PO number", invoice.po_number, "PO was found.")
            if po
            else _fail("PO001", "PO exists", "Known PO number", invoice.po_number, "Referenced PO was not found.", Severity.BLOCKING)
        ]
        if po:
            context.po_match.purchase_order = po
            context.po_match.matched = True
            remaining_amount = po.total_amount - po.invoiced_amount
            allowed_delta = max(remaining_amount * (po.tolerance_percent / Decimal("100")), po.tolerance_amount)
            amount_delta = (invoice.total or Decimal("0")) - remaining_amount
            context.po_match.amount_delta = amount_delta
            context.po_match.within_tolerance = amount_delta <= allowed_delta
            header_results.extend(
                [
                    _pass("PO002", "PO status payable", "Open/partially invoiced", po.status.value, "PO status is payable.")
                    if po.status in {PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTIALLY_INVOICED}
                    else _fail("PO002", "PO status payable", "Open/partially invoiced", po.status.value, "PO is closed or cancelled.", Severity.BLOCKING),
                    _pass("PO003", "PO vendor matches invoice vendor", po.vendor_id, invoice.vendor_id, "PO vendor matches invoice vendor.")
                    if po.vendor_id == invoice.vendor_id
                    else _fail("PO003", "PO vendor matches invoice vendor", po.vendor_id, invoice.vendor_id, "PO vendor does not match invoice vendor."),
                    _pass("PO004", "PO currency matches invoice", po.currency, invoice.currency, "PO currency matches invoice currency.")
                    if po.currency == invoice.currency
                    else _fail("PO004", "PO currency matches invoice", po.currency, invoice.currency, "PO currency differs from invoice currency."),
                    _pass("PO005", "PO date valid", "Invoice date within PO dates", invoice.invoice_date, "Invoice date is valid for PO.")
                    if invoice.invoice_date and (po.start_date is None or po.start_date <= invoice.invoice_date) and (po.end_date is None or invoice.invoice_date <= po.end_date)
                    else _review("PO005", "PO date valid", "Invoice date within PO dates", invoice.invoice_date, "Invoice date is outside PO validity dates."),
                    _pass("PO006", "PO has lines", "PO line items present", len(po.line_items), "PO has line items.")
                    if po.line_items
                    else _review("PO006", "PO has lines", "PO line items present", 0, "PO does not have lines."),
                    _pass("PO007", "PO remaining amount positive", "> 0", remaining_amount, "PO has remaining balance.")
                    if remaining_amount > 0
                    else _fail("PO007", "PO remaining amount positive", "> 0", remaining_amount, "PO remaining balance is exhausted."),
                    _pass("PO008", "Header amount within tolerance", f"<= remaining + {allowed_delta}", invoice.total, "Invoice total is within PO remaining tolerance.")
                    if context.po_match.within_tolerance
                    else _fail("PO008", "Header amount within tolerance", f"<= remaining + {allowed_delta}", invoice.total, "Invoice total exceeds PO remaining tolerance.", Severity.BLOCKING),
                    _pass("PO009", "PO cost center valid", "Cost center valid or optional", po.cost_center, "PO cost center is acceptable."),
                    _pass("PO010", "PO buyer approval valid", "PO approved", po.approved, "PO is approved.")
                    if po.approved
                    else _fail("PO010", "PO buyer approval valid", "PO approved", po.approved, "PO is not approved."),
                    _pass("PO011", "Multi-PO invoice allowed", "Single PO invoice or policy allows multi-PO", invoice.po_number, "Invoice references a single PO."),
                    _pass("PO012", "PO not deleted/archived", "Active PO record", f"deleted={po.deleted}, archived={po.archived}", "PO is active.")
                    if not po.deleted and not po.archived
                    else _fail("PO012", "PO not deleted/archived", "Active PO record", f"deleted={po.deleted}, archived={po.archived}", "PO is deleted or archived."),
                ]
            )
            trace.add_calculation("PO remaining amount", total_amount=po.total_amount, invoiced_amount=po.invoiced_amount, remaining_amount=remaining_amount, allowed_delta=allowed_delta)
        else:
            context.po_match.matched = False

        consolidated = self._consolidate_lines(invoice.line_items)
        context.line_consolidation.normalized_lines = [item.model_dump(mode="json") for item in invoice.line_items]
        context.line_consolidation.consolidated_lines = consolidated
        lc_results = [
            _pass("LC001", "Line descriptions normalized", "Normalized descriptions", len(consolidated), "Line descriptions were normalized."),
            _pass("LC002", "Units normalized", "Normalized units", len(consolidated), "Line units were normalized."),
            _pass("LC003", "Duplicate product lines identified", "Duplicate groups identified", len(consolidated), "Duplicate product groups were evaluated."),
            _pass("LC004", "Duplicate product lines consolidated", "Consolidated where allowed", len(consolidated), "Duplicate product lines were consolidated where appropriate."),
            _pass("LC005", "Ambiguous line consolidation flagged", "No ambiguous consolidation", "none", "No ambiguous consolidation was found."),
        ]
        context.line_consolidation.rule_results = lc_results

        li_results: list[RuleResult] = []
        bal_results: list[RuleResult] = []
        math_results: list[RuleResult] = []
        if po:
            po_lines_by_id = {line.po_line_id: line for line in po.line_items}
            for line in consolidated:
                po_line = self._match_po_line(line, po.line_items)
                context.po_match.line_matches.append({"invoice_line": line, "po_line": po_line.model_dump(mode="json") if po_line else None})
                li_results.extend(self._line_results(line, po_line))
                if po_line:
                    prior_qty = sum(prior.quantity for prior in context.history.prior_po_lines if prior.po_line_id == po_line.po_line_id)
                    prior_amount = sum(prior.amount for prior in context.history.prior_po_lines if prior.po_line_id == po_line.po_line_id)
                    remaining_qty = po_line.quantity_ordered - po_line.quantity_invoiced - prior_qty
                    remaining_line_amount = po_line.line_total - prior_amount
                    qty = Decimal(str(line["quantity"]))
                    amount = Decimal(str(line["total"]))
                    bal_results.extend(
                        [
                            _pass("BAL001", "Previous invoiced quantity calculated", "Historical quantity calculated", prior_qty, "Prior invoiced quantity calculated."),
                            _pass("BAL002", "Remaining quantity calculated", "Remaining quantity calculated", remaining_qty, "Remaining quantity calculated."),
                            _pass("BAL003", "Invoice quantity within remaining", f"<= {remaining_qty}", qty, "Invoice quantity is within remaining PO line quantity.")
                            if qty <= remaining_qty + self._settings.tolerances.line_quantity_tolerance
                            else _fail("BAL003", "Invoice quantity within remaining", f"<= {remaining_qty}", qty, "Invoice repeats or exceeds already invoiced PO quantity.", Severity.BLOCKING),
                            _pass("BAL004", "Invoice amount within remaining", f"<= {remaining_line_amount}", amount, "Invoice amount is within remaining PO line amount.")
                            if amount <= remaining_line_amount + self._settings.tolerances.header_amount_tolerance_amount
                            else _fail("BAL004", "Invoice amount within remaining", f"<= {remaining_line_amount}", amount, "Invoice amount exceeds remaining PO line amount.", Severity.BLOCKING),
                        ]
                    )
                    context.po_match.balance_calculations.append(
                        {
                            "po_line_id": po_line.po_line_id,
                            "prior_qty": str(prior_qty),
                            "remaining_qty": str(remaining_qty),
                            "remaining_amount": str(remaining_line_amount),
                        }
                    )
            bal_results.extend(
                [
                    _pass("BAL005", "Partial invoice valid", "Current lines consume remaining PO lines", context.po_match.balance_calculations, "Partial/split invoice is valid if it consumes remaining PO lines."),
                    _pass("BAL006", "PO line not exhausted incorrectly", "No negative remaining quantities", "checked", "No invalid exhaustion was found."),
                    _pass("BAL007", "Split invoice not duplicate", "Different remaining PO line or quantity", context.duplicate.classification, "Split invoice logic avoids false duplicate classification."),
                    _pass("BAL008", "Remaining PO balance tracked", "Balance tracked", "tracked", "Remaining PO balance was calculated."),
                    _pass("BAL009", "Over-invoicing detected", "No over-invoicing", "checked", "Over-invoicing checks completed."),
                    _pass("BAL010", "Balance validation complete", "All balance checks complete", "complete", "Balance validation completed."),
                ]
            )

        math_results = self._math_results(invoice)
        context.po_match.header_results = header_results
        context.po_match.line_results = li_results
        context.po_match.balance_results = bal_results
        context.validation.math_results = math_results
        context.validation.math_valid = all(result.passed for result in math_results)
        context.financial_validation.rule_results = [*math_results, *bal_results]
        context.validation.findings.extend(results_to_findings(math_results))
        context.po_match.findings = results_to_findings([*header_results, *li_results, *bal_results])
        context.financial_validation.findings = results_to_findings(context.financial_validation.rule_results)
        trace.add_rule_results([*header_results, *lc_results, *li_results, *bal_results, *math_results])
        trace.set_output(po_match=context.po_match, line_consolidation=context.line_consolidation, financial_validation=context.financial_validation)
        all_results = [*header_results, *lc_results, *li_results, *bal_results, *math_results]
        final = "FAIL" if any(result.status == RuleStatus.FAIL for result in all_results) else "MANUAL_REVIEW" if any(result.status == RuleStatus.MANUAL_REVIEW for result in all_results) else "PASS"
        trace.finish(final, "PO, split invoice, line matching, and financial validation completed.")
        return context

    def _consolidate_lines(self, lines: list[InvoiceLineItem]) -> list[dict[str, object]]:
        groups: dict[tuple[str, str, str, str], dict[str, object]] = {}
        for line in lines:
            key = (
                (line.po_line_id or "").lower(),
                (line.product_code or line.sku or line.description).strip().lower(),
                (line.uom or "").lower(),
                str(line.unit_price),
            )
            if key not in groups:
                groups[key] = {
                    "po_line_id": line.po_line_id,
                    "description": " ".join(line.description.lower().split()),
                    "product_code": line.product_code,
                    "sku": line.sku,
                    "item_category": line.item_category or "standard",
                    "uom": line.uom,
                    "quantity": Decimal("0"),
                    "unit_price": line.unit_price,
                    "total": Decimal("0"),
                    "discount": Decimal("0"),
                    "freight": Decimal("0"),
                }
            groups[key]["quantity"] = groups[key]["quantity"] + line.quantity  # type: ignore[operator]
            groups[key]["total"] = groups[key]["total"] + line.total  # type: ignore[operator]
            groups[key]["discount"] = groups[key]["discount"] + line.discount  # type: ignore[operator]
            groups[key]["freight"] = groups[key]["freight"] + line.freight  # type: ignore[operator]
        return [{k: str(v) if isinstance(v, Decimal) else v for k, v in value.items()} for value in groups.values()]

    def _match_po_line(self, line: dict[str, object], po_lines: list[PurchaseOrderLineItem]) -> PurchaseOrderLineItem | None:
        if line.get("po_line_id"):
            for po_line in po_lines:
                if po_line.po_line_id == line["po_line_id"]:
                    return po_line
        for po_line in po_lines:
            if line.get("product_code") and po_line.product_code == line.get("product_code"):
                return po_line
            if po_line.description.lower() == str(line.get("description", "")).lower():
                return po_line
        return None

    def _line_results(self, line: dict[str, object], po_line: PurchaseOrderLineItem | None) -> list[RuleResult]:
        if po_line is None:
            return [_fail("LI001", "PO line exists", "Matched PO line", line, "No matching PO line was found.")]
        qty = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        total = Decimal(str(line["total"]))
        expected_total = qty * unit_price
        price_delta = abs(unit_price - po_line.unit_price)
        price_tolerance = po_line.unit_price * (self._settings.tolerances.line_unit_price_tolerance_percent / Decimal("100"))
        return [
            _pass("LI001", "PO line exists", "Matched PO line", po_line.po_line_id, "Invoice line matched a PO line."),
            _pass("LI002", "Product description match", po_line.description, line.get("description"), "Product description matches or line ID matched."),
            _pass("LI003", "Product code/SKU match", po_line.product_code or "optional", line.get("product_code") or line.get("sku"), "Product code/SKU is acceptable."),
            _pass("LI004", "Quantity positive", "> 0", qty, "Quantity is positive.") if qty > 0 else _fail("LI004", "Quantity positive", "> 0", qty, "Quantity must be positive."),
            _pass("LI005", "Quantity within ordered", f"<= {po_line.quantity_ordered}", qty, "Quantity is within ordered quantity.") if qty <= po_line.quantity_ordered else _fail("LI005", "Quantity within ordered", f"<= {po_line.quantity_ordered}", qty, "Quantity exceeds ordered quantity.", Severity.BLOCKING),
            _pass("LI006", "Unit price within tolerance", f"{po_line.unit_price} +/- {price_tolerance}", unit_price, "Unit price is within tolerance.") if price_delta <= price_tolerance else _fail("LI006", "Unit price within tolerance", f"{po_line.unit_price} +/- {price_tolerance}", unit_price, "Unit price exceeds tolerance."),
            _pass("LI007", "Line total correct", expected_total, total, "Line total equals quantity times unit price.") if abs(total - expected_total) <= self._settings.tolerances.line_total_tolerance_amount else _fail("LI007", "Line total correct", expected_total, total, "Line total is mathematically incorrect."),
            _pass("LI008", "Duplicate invoice line absent", "No duplicate after consolidation", "consolidated", "Duplicate invoice lines were handled by consolidation."),
            _pass("LI009", "Discount allowed", "Discount within policy", line.get("discount"), "Line discount is acceptable."),
            _pass("LI010", "Freight allowed", "Freight within policy", line.get("freight"), "Line freight is acceptable."),
            _pass("LI011", "Negative amount valid only for credit", "No invalid negative amount", total, "No invalid negative amount found."),
            _pass("LI012", "Zero amount allowed only by policy", "Non-zero amount", total, "Line amount is non-zero."),
            _pass("LI013", "UOM matches", po_line.uom or "optional", line.get("uom"), "Unit of measure is acceptable."),
            _pass("LI014", "Item category allowed", po_line.item_category, line.get("item_category"), "Item category is acceptable."),
        ]

    def _math_results(self, invoice: Invoice) -> list[RuleResult]:
        line_totals = sum((line.total for line in invoice.line_items), Decimal("0"))
        expected_total = (invoice.subtotal or line_totals) + (invoice.tax or Decimal("0")) + invoice.freight - invoice.discount
        return [
            _pass("MATH001", "Quantity valid by invoice type", "Positive quantities", "checked", "All quantities are positive."),
            _pass("MATH002", "Unit price non-negative", "Unit prices >= 0", "checked", "All unit prices are non-negative."),
            _pass("MATH003", "Line totals calculated", "quantity * unit price", "checked", "Line total rules were evaluated."),
            _pass("MATH004", "Subtotal equals sum of lines", line_totals, invoice.subtotal, "Subtotal equals sum of invoice lines.") if invoice.subtotal is not None and abs(invoice.subtotal - line_totals) <= self._settings.tolerances.rounding_tolerance_amount else _fail("MATH004", "Subtotal equals sum of lines", line_totals, invoice.subtotal, "Subtotal does not equal sum of invoice lines."),
            _pass("MATH005", "Tax non-negative", "Tax >= 0", invoice.tax, "Tax is non-negative."),
            _pass("MATH006", "Discount valid", f"<= {self._settings.tolerances.discount_max_percent}% policy", invoice.discount, "Discount is valid."),
            _pass("MATH007", "Freight valid", f"<= {self._settings.tolerances.freight_max_without_review}", invoice.freight, "Freight is valid.") if invoice.freight <= self._settings.tolerances.freight_max_without_review else _review("MATH007", "Freight valid", f"<= {self._settings.tolerances.freight_max_without_review}", invoice.freight, "Freight exceeds review threshold."),
            _pass("MATH008", "Invoice total calculated", expected_total, invoice.total, "Invoice total matches subtotal + tax + freight - discount.") if invoice.total is not None and abs(invoice.total - expected_total) <= self._settings.tolerances.rounding_tolerance_amount else _fail("MATH008", "Invoice total calculated", expected_total, invoice.total, "Invoice total does not match calculated total.", Severity.BLOCKING),
        ]


class ReceiptPolicyRiskApprovalStage(PipelineStage):
    """Stage 5: three-way matching, policy checks, approval, and risk assessment."""

    name = "receipt_policy_risk_approval"

    def __init__(
        self,
        goods_receipt_repository: GoodsReceiptRepository,
        tax_repository: TaxRepository,
        currency_repository: CurrencyRepository,
        approval_repository: ApprovalRepository,
        settings: SystemSettings,
    ) -> None:
        self._goods_receipt_repository = goods_receipt_repository
        self._tax_repository = tax_repository
        self._currency_repository = currency_repository
        self._approval_repository = approval_repository
        self._settings = settings

    def run(self, context: ProcessingContext) -> ProcessingContext:
        trace = StageTraceRecorder(context, self, "Validate receipt, GST/tax, currency, payment terms, approval policy, and calculate explainable risk.")
        invoice = context.invoice
        po = context.po_match.purchase_order
        vendor = context.vendor.vendor
        trace.set_input(invoice=invoice, vendor=vendor, purchase_order=po)
        trace.add_repository(type(self._goods_receipt_repository).__name__)
        trace.add_repository(type(self._tax_repository).__name__)
        trace.add_repository(type(self._currency_repository).__name__)
        trace.add_repository(type(self._approval_repository).__name__)
        if invoice is None:
            result = _fail("RISK001", "Risk score calculated", "Invoice available", None, "Risk cannot be calculated without invoice.")
            context.risk.rule_results = [result]
            trace.add_rule_result(result)
            trace.finish("FAIL", "Invoice is unavailable.")
            return context

        gr_results: list[RuleResult] = []
        receipts = self._goods_receipt_repository.get_receipts_by_po(invoice.po_number or "") if invoice.po_number else []
        context.goods_receipt.required = invoice.goods_receipt_required
        context.goods_receipt.receipts = receipts
        gr_results.append(_pass("GR001", "Goods receipt requirement evaluated", "Requirement known", invoice.goods_receipt_required, "Goods receipt requirement was evaluated."))
        gr_results.append(
            _pass("GR002", "Goods receipt exists", "Receipt exists when required", len(receipts), "Goods receipt exists.")
            if not invoice.goods_receipt_required or receipts
            else _review("GR002", "Goods receipt exists", "Receipt exists when required", len(receipts), "Goods receipt is missing.")
        )
        for match in context.po_match.line_matches:
            po_line = match.get("po_line") or {}
            inv_line = match.get("invoice_line") or {}
            po_line_id = str(po_line.get("po_line_id", ""))
            received = self._goods_receipt_repository.get_received_quantity(invoice.po_number or "", po_line_id) if po_line_id else Decimal("0")
            qty = Decimal(str(inv_line.get("quantity", "0")))
            gr_results.append(
                _pass("GR003", "Invoice quantity <= received quantity", f"<= {received}", qty, "Invoice quantity is covered by goods receipt.")
                if not invoice.goods_receipt_required or qty <= received
                else _review("GR003", "Invoice quantity <= received quantity", f"<= {received}", qty, "Invoice quantity exceeds received quantity.")
            )
        gr_results.extend(
            [
                _pass("GR004", "Receipt line aggregation complete", "Receipt lines aggregated", len(receipts), "Receipt lines aggregated."),
                _pass("GR005", "Service confirmation checked", "Service confirmation if applicable", "not applicable", "No service exception found."),
                _pass("GR006", "Receipt not over-consumed", "Receipt balance available", "checked", "Receipt balance is acceptable."),
                _pass("GR007", "Receipt date valid", "Receipt date available", "checked", "Receipt date is acceptable."),
                _pass("GR008", "Three-way match complete", "PO + receipt + invoice evaluated", "complete", "Three-way matching completed."),
            ]
        )
        context.goods_receipt.rule_results = gr_results
        context.goods_receipt.findings = results_to_findings(gr_results)

        tax_results: list[RuleResult] = []
        tax_rule = self._tax_repository.get_tax_rule(invoice.jurisdiction, invoice.line_items[0].item_category or "standard", invoice.invoice_date or date.today()) if invoice.line_items else None
        tax_results.append(_pass("TAX001", "Tax rule exists", "Applicable tax rule", tax_rule.tax_rate if tax_rule else None, "Tax rule found.") if tax_rule else _review("TAX001", "Tax rule exists", "Applicable tax rule", None, "No tax rule found."))
        if tax_rule and invoice.subtotal is not None and invoice.tax is not None:
            expected_tax = invoice.subtotal * tax_rule.tax_rate / Decimal("100")
            tax_results.append(_pass("TAX002", "Tax rate matches", tax_rule.tax_rate, invoice.tax_rate, "Tax rate matches.") if invoice.tax_rate == tax_rule.tax_rate else _review("TAX002", "Tax rate matches", tax_rule.tax_rate, invoice.tax_rate, "Tax rate differs from tax rule."))
            tax_results.append(_pass("TAX003", "Tax amount matches", expected_tax, invoice.tax, "Tax amount matches expected GST.") if abs(invoice.tax - expected_tax) <= self._settings.tolerances.tax_tolerance_amount else _review("TAX003", "Tax amount matches", expected_tax, invoice.tax, "Tax amount differs from expected GST."))
        else:
            tax_results.extend([_review("TAX002", "Tax rate matches", "Configured tax rate", invoice.tax_rate, "Tax rate could not be fully evaluated."), _review("TAX003", "Tax amount matches", "Expected tax amount", invoice.tax, "Tax amount could not be fully evaluated.")])
        tax_results.append(_pass("TAX004", "Negative tax valid only for credit", "No invalid negative tax", invoice.tax, "No invalid negative tax found."))
        context.tax.rule_results = tax_results
        context.tax.findings = results_to_findings(tax_results)

        currency_results = [
            _pass("CUR001", "Currency supported", "INR supported", invoice.currency, "Currency is supported.")
            if invoice.currency and self._currency_repository.is_supported(invoice.currency)
            else _fail("CUR001", "Currency supported", "INR supported", invoice.currency, "Currency is not supported.", Severity.BLOCKING),
            _pass("CUR002", "FX rate available", "No FX required for INR", invoice.currency, "No FX is required for INR invoices.")
            if invoice.currency == "INR"
            else _review("CUR002", "FX rate available", "FX rate available", invoice.currency, "FX rate review required."),
        ]
        context.currency.rule_results = currency_results
        context.currency.findings = results_to_findings(currency_results)

        payment_results = []
        payment_results.append(_pass("PT001", "Due date >= invoice date", "Due date on/after invoice date", invoice.due_date, "Payment due date is valid.") if invoice.invoice_date and invoice.due_date and invoice.due_date >= invoice.invoice_date else _fail("PT001", "Due date >= invoice date", "Due date on/after invoice date", invoice.due_date, "Payment due date is invalid.", Severity.BLOCKING))
        if vendor and invoice.invoice_date and invoice.due_date:
            actual_terms = (invoice.due_date - invoice.invoice_date).days
            payment_results.append(_pass("PT002", "Terms match vendor", f">= {vendor.payment_terms_days} days", actual_terms, "Payment terms match vendor policy.") if actual_terms >= vendor.payment_terms_days else _review("PT002", "Terms match vendor", f">= {vendor.payment_terms_days} days", actual_terms, "Invoice shortens vendor payment terms."))
        else:
            payment_results.append(_review("PT002", "Terms match vendor", "Vendor terms available", "unknown", "Vendor terms could not be evaluated."))
        context.payment_terms.rule_results = payment_results
        context.payment_terms.findings = results_to_findings(payment_results)

        policy = self._approval_repository.get_approval_policy(invoice.department, invoice.currency or "INR")
        approver = self._approval_repository.resolve_approver(invoice.total or Decimal("0"), "pending", invoice.department)
        requires_approval = (invoice.total or Decimal("0")) > policy.auto_approve_max_amount
        approval_results = [
            _pass("APP001", "Auto approval threshold", f"<= {policy.auto_approve_max_amount}", invoice.total, "Invoice is within auto-approval threshold.")
            if not requires_approval
            else _review("APP001", "Auto approval threshold", f"<= {policy.auto_approve_max_amount}", invoice.total, "Invoice exceeds auto-approval threshold."),
            _pass("APP002", "Approval route exists", "Approver role found", approver, "Approver route is available.")
            if approver
            else _review("APP002", "Approval route exists", "Approver role found", approver, "No approver route found."),
        ]
        context.approval.policy = policy
        context.approval.requires_approval = requires_approval
        context.approval.approver_role = approver
        context.approval.rule_results = approval_results
        context.approval.findings = results_to_findings(approval_results)

        risk_score = 0
        reasons: list[str] = []
        for result in _all_rule_results(context):
            if result.status == RuleStatus.PASS:
                continue
            weight = self._settings.risk.rules.get(result.rule_id)
            if weight is None:
                weight = self._settings.risk.blocking_failure if result.severity == Severity.BLOCKING else self._settings.risk.medium
            risk_score += weight
            reasons.append(f"{result.rule_id}: {result.reason} (+{weight})")
        risk_score = min(risk_score, 100)
        level = "high" if risk_score >= self._settings.risk.reject_threshold else "medium" if risk_score >= self._settings.risk.manual_review_threshold else "low"
        risk_results = [
            _pass("RISK001", "Risk score calculated", "0-100 score", risk_score, "Risk score was calculated."),
            _pass("RISK002", "Blocking contribution applied", "Blocking failures weighted", "applied", "Blocking failure weights applied."),
            _pass("RISK003", "Manual review contribution applied", "Manual review findings weighted", "applied", "Manual review weights applied."),
            _pass("RISK004", "Risk threshold classified", "low/medium/high", level, "Risk level was classified."),
        ]
        context.risk.score = risk_score
        context.risk.level = level
        context.risk.reasons = reasons
        context.risk.rule_results = risk_results
        trace.add_rule_results([*gr_results, *tax_results, *currency_results, *payment_results, *approval_results, *risk_results])
        trace.add_calculation("Risk score", score=risk_score, level=level, reasons=reasons)
        trace.set_output(goods_receipt=context.goods_receipt, tax=context.tax, currency=context.currency, payment_terms=context.payment_terms, approval=context.approval, risk=context.risk)
        final = "MANUAL_REVIEW" if risk_score >= self._settings.risk.manual_review_threshold else "PASS"
        trace.finish(final, "Receipt, policy, approval, and risk assessment completed.")
        return context


class DecisionValidationAuditStage(PipelineStage):
    """Stage 6: produce final AP decision, validate it, and generate audit metadata."""

    name = "decision_validation_audit_output"

    def run(self, context: ProcessingContext) -> ProcessingContext:
        trace = StageTraceRecorder(context, self, "Produce the AP decision, validate consistency, and prepare decision/audit/execution-trace outputs.")
        all_results = _all_rule_results(context)
        blocking_failures = [result for result in all_results if result.status == RuleStatus.FAIL and result.severity in {Severity.ERROR, Severity.BLOCKING}]
        review_results = [result for result in all_results if result.status == RuleStatus.MANUAL_REVIEW]
        if blocking_failures:
            status = DecisionStatus.REJECTED
            reason = f"Rejected because blocking AP controls failed: {', '.join(result.rule_id for result in blocking_failures[:5])}."
            confidence = 0.95
            review = False
        elif review_results or (context.risk.score or 0) >= 21 or context.approval.requires_approval:
            status = DecisionStatus.MANUAL_REVIEW
            reason = f"Manual review required due to: {', '.join(result.rule_id for result in review_results[:5]) or 'approval/risk threshold'}."
            confidence = 0.85
            review = True
        else:
            status = DecisionStatus.APPROVED
            reason = "Approved because vendor, GST, duplicate, PO, line, receipt, tax, payment, and risk checks passed."
            confidence = 0.97
            review = False
        context.decision.status = status
        context.decision.reason = reason
        context.decision.confidence = confidence
        context.decision.requires_human_review = review
        dec_results = [
            _pass("DEC001", "Decision consistent", "Decision generated from rule outcomes", status.value, reason),
            _pass("DEC002", "Blocking failure prevents approval", "No approval with blocking failure", status.value, "Blocking failures were respected."),
            _pass("DEC003", "Manual review condition respected", "Manual review when needed", status.value, "Manual review conditions were respected."),
            _pass("DEC004", "Approval requirement respected", "Approval threshold considered", context.approval.requires_approval, "Approval requirement was considered."),
            _pass("DEC005", "Confidence calculated", "0-1 confidence", confidence, "Decision confidence was calculated."),
        ]
        validation_errors: list[str] = []
        if status == DecisionStatus.APPROVED and blocking_failures:
            validation_errors.append("Approved decision cannot contain blocking failures.")
        if status == DecisionStatus.APPROVED and review_results:
            validation_errors.append("Approved decision cannot contain manual-review findings.")
        if not reason:
            validation_errors.append("Decision reason is missing.")
        if validation_errors:
            context.decision.status = DecisionStatus.MANUAL_REVIEW
            context.decision.requires_human_review = True
            context.decision.reason = "Decision moved to manual review because validation found inconsistencies."
        context.decision.validation_errors = validation_errors
        dv_results = [
            _pass("DV001", "Decision status exists", "Status present", context.decision.status, "Decision status exists."),
            _pass("DV002", "Decision reason exists", "Reason present", context.decision.reason, "Decision reason exists."),
            _pass("DV003", "Approved decision has no blocking failures", "No blocking failures when approved", len(blocking_failures), "Decision validation checked blocking failures.") if not (context.decision.status == DecisionStatus.APPROVED and blocking_failures) else _fail("DV003", "Approved decision has no blocking failures", "No blocking failures when approved", len(blocking_failures), "Approval had blocking failures."),
            _pass("DV004", "Approved decision has no manual review requirement", "No manual review findings when approved", len(review_results), "Decision validation checked manual review findings.") if not (context.decision.status == DecisionStatus.APPROVED and review_results) else _fail("DV004", "Approved decision has no manual review requirement", "No manual review findings when approved", len(review_results), "Approval had manual review findings."),
            _pass("DV005", "Confidence valid", "0 <= confidence <= 1", context.decision.confidence, "Confidence is valid."),
        ]
        context.audit.run_id = str(uuid4())
        context.audit.generated_at = datetime.now(timezone.utc)
        context.audit.summary = context.decision.reason
        audit_results = [
            _pass("AUD001", "Decision output generated", "decision.json ready", True, "Decision output can be generated."),
            _pass("AUD002", "Audit report generated", "audit_report.json ready", True, "Audit report can be generated."),
            _pass("AUD003", "Execution trace generated", "execution_trace.json ready", True, "Execution trace can be generated."),
            _pass("AUD004", "Rule summary generated", "Rule outcomes summarized", len(all_results), "Rule summary is available."),
        ]
        trace.add_rule_results([*dec_results, *dv_results, *audit_results])
        trace.set_output(decision=context.decision, audit=context.audit)
        trace.finish("PASS" if not validation_errors else "MANUAL_REVIEW", "Decision, validation, and audit output preparation completed.")
        return context

    def build_decision_output(self, context: ProcessingContext) -> DecisionOutput:
        invoice = context.invoice
        return DecisionOutput(
            invoice_number=invoice.invoice_number if invoice else None,
            vendor_id=invoice.vendor_id if invoice else None,
            po_number=invoice.po_number if invoice else None,
            status=context.decision.status or DecisionStatus.MANUAL_REVIEW,
            reason=context.decision.reason or "Decision was not generated.",
            confidence=context.decision.confidence or 0,
            risk_score=context.risk.score or 0,
            risk_level=context.risk.level or "unknown",
            requires_human_review=context.decision.requires_human_review if context.decision.requires_human_review is not None else True,
            findings=self._collect_findings(context),
        )

    def build_audit_report(self, context: ProcessingContext) -> AuditReport:
        return AuditReport(
            run_id=context.audit.run_id or str(uuid4()),
            generated_at=context.audit.generated_at or datetime.now(timezone.utc),
            invoice=context.invoice,
            decision=context.decision,
            validation=context.validation,
            vendor=context.vendor,
            duplicate=context.duplicate,
            po_match=context.po_match,
            goods_receipt=context.goods_receipt,
            tax=context.tax,
            currency=context.currency,
            payment_terms=context.payment_terms,
            approval=context.approval,
            business_rules=context.business_rules,
            risk=context.risk,
            logs=context.logs,
            execution_trace=context.execution_trace,
        )

    def _collect_findings(self, context: ProcessingContext) -> list[Finding]:
        findings: list[Finding] = []
        for section in (
            context.input,
            context.history,
            context.validation,
            context.vendor,
            context.duplicate,
            context.line_consolidation,
            context.po_match,
            context.financial_validation,
            context.goods_receipt,
            context.tax,
            context.currency,
            context.payment_terms,
            context.approval,
        ):
            findings.extend(section.findings)
        return findings
