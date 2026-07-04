from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from invoice_processing.extraction import IMAGE_SUFFIXES, extract_invoice_from_pdf


DEFAULT_RULES_CONFIG = {
    "tolerance_pct": Decimal("0.02"),
    "tolerance_abs": Decimal("5.00"),
    "fuzzy_match_amount_window_pct": Decimal("0.03"),
    "fuzzy_match_date_window_days": 7,
    "tax_tolerance_abs": Decimal("5.00"),
    "total_tolerance_abs": Decimal("5.00"),
}
CRITICAL_FIELDS = ("invoice_number", "invoice_date", "total")
BLOCKING_STATUSES = {"REJECTED", "MANUAL_REVIEW"}
PARTIAL_STATUS = "PARTIAL_APPROVAL"


@dataclass
class PipelineResult:
    """Result returned to the CLI."""

    decision: dict[str, Any]
    audit_report: dict[str, Any]
    execution_trace: dict[str, Any]
    persisted_to_history: bool


class DataStore(Protocol):
    """Persistence contract used by the pipeline business logic."""

    source_name: str

    def load_vendors(self) -> list[dict[str, Any]]:
        """Return vendor records used for approval checks."""

    def load_purchase_orders(self) -> list[dict[str, Any]]:
        """Return purchase order records used for matching and aggregation."""

    def load_invoice_ledger(self) -> list[dict[str, Any]]:
        """Return historical invoices used for duplicate and reused-number checks."""

    def load_rules_config(self) -> dict[str, Any]:
        """Return AP rule settings such as tolerance and fuzzy-match windows."""

    def load_goods_receipts(self, po_id: str) -> list[dict[str, Any]]:
        """Return goods receipts for a purchase order."""

    def load_approval_thresholds(self) -> dict[str, Any]:
        """Return approval routing thresholds."""

    def persist_approved_invoice(self, invoice: dict[str, Any], po: dict[str, Any] | None, decision: dict[str, Any]) -> bool:
        """Record an approved invoice and update PO running matched amount."""


@dataclass
class JsonDataStore:
    """JSON-file data store kept for local tests and fallback demos."""

    vendors_path: Path
    purchase_orders_path: Path
    processed_invoices_path: Path
    rules_config_path: Path = Path("config/rules_config.json")
    goods_receipts_path: Path = Path("data/goods_receipts.json")
    approval_thresholds_path: Path = Path("config/approval_thresholds.json")
    source_name: str = "json_files"

    def load_vendors(self) -> list[dict[str, Any]]:
        return read_json_array(self.vendors_path)

    def load_purchase_orders(self) -> list[dict[str, Any]]:
        purchase_orders = read_json_array(self.purchase_orders_path)
        if any(po.get("line_items") for po in purchase_orders):
            return purchase_orders
        detailed_path = self.purchase_orders_path.parent / "purchase_orders.json"
        if detailed_path.exists() and detailed_path != self.purchase_orders_path:
            detailed = read_json_array(detailed_path)
            details_by_id = {po_id(po): po for po in detailed}
            for po in purchase_orders:
                detail = details_by_id.get(po_id(po))
                if detail:
                    po["line_items"] = detail.get("line_items", [])
        return purchase_orders

    def load_invoice_ledger(self) -> list[dict[str, Any]]:
        return read_invoice_ledger(self.processed_invoices_path)

    def load_rules_config(self) -> dict[str, Any]:
        return load_rules_config(self.rules_config_path)

    def load_goods_receipts(self, po_id: str) -> list[dict[str, Any]]:
        if not self.goods_receipts_path.exists():
            return []
        return [
            normalize_goods_receipt(receipt)
            for receipt in read_json_array(self.goods_receipts_path)
            if clean_text(receipt.get("po_id") or receipt.get("po_number")) == po_id
        ]

    def load_approval_thresholds(self) -> dict[str, Any]:
        return load_approval_thresholds(self.approval_thresholds_path)

    def persist_approved_invoice(self, invoice: dict[str, Any], po: dict[str, Any] | None, decision: dict[str, Any]) -> bool:
        ledger = self.load_invoice_ledger()
        persisted = append_invoice_to_ledger(self.processed_invoices_path, ledger, invoice, po, decision)
        if persisted and po is not None:
            update_purchase_order_match(self.purchase_orders_path, po, invoice)
        return persisted


@dataclass
class SQLiteDataStore:
    """SQLite data store for the production-style AP pipeline."""

    database_path: Path
    schema_path: Path = Path("db/schema_sqlite.sql")
    source_name: str = "sqlite"

    def load_vendors(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT vendor_id, name, approved, tax_id, created_at
                    FROM vendors
                    ORDER BY vendor_id
                    """
                ).fetchall()
            ]

    def load_purchase_orders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            purchase_orders = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT po_id, vendor_id,
                           CAST(po_amount AS TEXT) AS po_amount,
                           CAST(matched_amount AS TEXT) AS matched_amount,
                           status, created_at
                    FROM purchase_orders
                    ORDER BY po_id
                    """
                ).fetchall()
            ]
            if self._table_exists(conn, "purchase_order_line_items"):
                line_rows = conn.execute(
                    """
                    SELECT po_line_id, po_id, item_name,
                           CAST(ordered_quantity AS TEXT) AS ordered_quantity,
                           CAST(unit_price AS TEXT) AS unit_price
                    FROM purchase_order_line_items
                    ORDER BY po_line_id
                    """
                ).fetchall()
                lines_by_po: dict[str, list[dict[str, Any]]] = {}
                for row in line_rows:
                    line = dict(row)
                    lines_by_po.setdefault(clean_text(line.get("po_id")), []).append(line)
                for po in purchase_orders:
                    po["line_items"] = lines_by_po.get(clean_text(po.get("po_id")), [])
            return purchase_orders

    def load_invoice_ledger(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT invoice_id, vendor_id, po_id, invoice_number, invoice_date,
                       CAST(total_amount AS TEXT) AS total,
                       dedupe_key, raw_extraction, source_file, created_at
                FROM invoices
                ORDER BY created_at
                """
            ).fetchall()
        return [normalize_ledger_entry(dict(row)) for row in rows]

    def load_rules_config(self) -> dict[str, Any]:
        config = dict(DEFAULT_RULES_CONFIG)
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM rules_config").fetchall()
        raw = {row["key"]: row["value"] for row in rows}
        for key in ("tolerance_pct", "tolerance_abs", "fuzzy_match_amount_window_pct"):
            if key in raw:
                config[key] = money(raw[key])
        if "fuzzy_match_date_window_days" in raw:
            config["fuzzy_match_date_window_days"] = int(raw["fuzzy_match_date_window_days"])
        for key in ("tax_tolerance_abs", "total_tolerance_abs"):
            if key in raw:
                config[key] = money(raw[key])
        return config

    def load_goods_receipts(self, po_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if not self._table_exists(conn, "goods_receipts"):
                return []
            rows = conn.execute(
                """
                SELECT receipt_id, po_id, item,
                       CAST(quantity_received AS TEXT) AS quantity_received,
                       receipt_date
                FROM goods_receipts
                WHERE po_id = ?
                ORDER BY receipt_date, receipt_id
                """,
                (po_id,),
            ).fetchall()
        return [normalize_goods_receipt(dict(row)) for row in rows]

    def load_approval_thresholds(self) -> dict[str, Any]:
        with self._connect() as conn:
            if self._table_exists(conn, "approval_thresholds"):
                rows = conn.execute(
                    """
                    SELECT CAST(min_amount AS TEXT) AS min_amount,
                           CAST(max_amount AS TEXT) AS max_amount,
                           role
                    FROM approval_thresholds
                    ORDER BY min_amount
                    """
                ).fetchall()
                if rows:
                    return {"thresholds": [dict(row) for row in rows]}
        return load_approval_thresholds()

    def persist_approved_invoice(self, invoice: dict[str, Any], po: dict[str, Any] | None, decision: dict[str, Any]) -> bool:
        po_value = invoice.get("po_id") or (po_id(po) if po else None)
        with self._connect() as conn:
            with conn:
                existing = conn.execute(
                    "SELECT invoice_id FROM invoices WHERE dedupe_key = ?",
                    (invoice.get("dedupe_key"),),
                ).fetchone()
                if existing:
                    return False

                invoice_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO invoices (
                        invoice_id, vendor_id, po_id, invoice_number, invoice_date, total_amount,
                        dedupe_key, raw_extraction, source_file
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_id,
                        invoice.get("vendor_id"),
                        po_value,
                        invoice.get("invoice_number"),
                        parse_date(invoice.get("invoice_date")),
                        money_to_string(invoice.get("total")),
                        invoice.get("dedupe_key"),
                        json.dumps(make_json_safe(invoice.get("raw_extraction"))),
                        invoice.get("source_file") or "",
                    ),
                )

                for line in invoice.get("line_items") or []:
                    conn.execute(
                        """
                        INSERT INTO invoice_line_items (
                            line_item_id, invoice_id, description, quantity, unit_price, line_amount, tax_amount
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            invoice_id,
                            line.get("description"),
                            money_to_string(line.get("quantity")),
                            money_to_string(line.get("unit_price")),
                            money_to_string(line.get("line_amount")),
                            money_to_string(line.get("tax_amount")),
                        ),
                    )

                if po_value:
                    invoice_total = money_to_string(invoice.get("total"))
                    conn.execute(
                        """
                        UPDATE purchase_orders
                        SET matched_amount = printf('%.2f', CAST(matched_amount AS NUMERIC) + CAST(? AS NUMERIC)),
                            status = CASE
                                WHEN CAST(matched_amount AS NUMERIC) + CAST(? AS NUMERIC) >= CAST(po_amount AS NUMERIC) THEN 'closed'
                                WHEN CAST(matched_amount AS NUMERIC) + CAST(? AS NUMERIC) > 0 THEN 'partially_matched'
                                ELSE 'open'
                            END
                        WHERE po_id = ?
                        """,
                        (invoice_total, invoice_total, invoice_total, po_value),
                    )

                run_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO processing_runs (run_id, invoice_id, finished_at, status, checks_run)
                    VALUES (?, ?, ?, 'completed', ?)
                    """,
                    (
                        run_id,
                        invoice_id,
                        datetime.now(timezone.utc),
                        json.dumps(decision.get("checks_run", [])),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO decisions (decision_id, run_id, invoice_id, verdict, reasons)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        invoice_id,
                        decision["decision"],
                        json.dumps(decision.get("reasons", [])),
                    ),
                )
        return True

    def first_purchase_order(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT po_id, vendor_id,
                       CAST(po_amount AS TEXT) AS po_amount,
                       CAST(matched_amount AS TEXT) AS matched_amount,
                       status, created_at
                FROM purchase_orders
                ORDER BY po_id
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def _connect(self) -> Any:
        self._initialize_database()
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize_database(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        should_initialize = not self.database_path.exists()
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            has_vendors = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'vendors'"
            ).fetchone()
            if should_initialize or not has_vendors:
                conn.executescript(self.schema_path.read_text(encoding="utf-8"))

    @staticmethod
    def _table_exists(conn: Any, table_name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return bool(row)


def run_invoice_pipeline(
    invoice_path: Path,
    vendors_path: Path,
    purchase_orders_path: Path,
    processed_invoices_path: Path,
    goods_receipts_path: Path = Path("data/goods_receipts.json"),
    tax_rules_path: Path = Path("data/tax_rules.json"),
    currency_rates_path: Path = Path("data/currency_rates.json"),
    config_dir: Path = Path("config"),
    persist_processed: bool = False,
    database_url: str | None = None,
) -> PipelineResult:
    """Run Stage 1 from the SDD: JSON-file core invoice decision logic."""
    del tax_rules_path, currency_rates_path

    trace: list[dict[str, Any]] = []
    reasons: list[str] = []
    checks_run: list[str] = []

    data_store: DataStore
    if database_url:
        data_store = SQLiteDataStore(Path(database_url))
    else:
        data_store = JsonDataStore(
            vendors_path=vendors_path,
            purchase_orders_path=purchase_orders_path,
            processed_invoices_path=processed_invoices_path,
            rules_config_path=config_dir / "rules_config.json",
            goods_receipts_path=goods_receipts_path,
            approval_thresholds_path=config_dir / "approval_thresholds.json",
        )

    invoice_raw = read_invoice_input(invoice_path)
    vendors = data_store.load_vendors()
    pos = data_store.load_purchase_orders()
    ledger = data_store.load_invoice_ledger()
    rules_config = data_store.load_rules_config()
    approval_thresholds = data_store.load_approval_thresholds()
    trace_step(
        trace,
        "data_source",
        "Load AP reference data and invoice history from configured data store.",
        {
            "source": data_store.source_name,
            "vendors_loaded": len(vendors),
            "purchase_orders_loaded": len(pos),
            "historical_invoices_loaded": len(ledger),
            "rules_config": rules_config,
            "approval_thresholds": approval_thresholds,
        },
    )

    invoice = normalize_invoice(invoice_raw)
    trace_step(
        trace,
        "normalize_invoice",
        "Normalize incoming invoice and build dedupe_key.",
        {
            "invoice_number": invoice.get("invoice_number"),
            "vendor_id": invoice.get("vendor_id"),
            "po_id": invoice.get("po_id"),
            "total": money_to_string(invoice.get("total")),
            "dedupe_key": invoice.get("dedupe_key"),
        },
    )

    missing_fields = missing_critical_field_check(invoice, checks_run, reasons, trace)
    if missing_fields:
        decision = build_decision(
            invoice=invoice,
            duplicate=False,
            reused_number=False,
            vendor=None,
            po=None,
            tolerance_ok=False,
            missing_fields=missing_fields,
            reasons=reasons,
            checks_run=checks_run,
            validation_results=[],
        )
        trace_step(trace, "short_circuit", "Stop after terminal completeness failure.", {"decision": decision["decision"], "reason": "missing critical fields"})
        return build_result(invoice_path, decision, trace, False)

    vendor = vendor_approval_check(invoice, vendors, checks_run, reasons, trace)
    if vendor is None:
        decision = build_decision(
            invoice=invoice,
            duplicate=False,
            reused_number=False,
            vendor=None,
            po=None,
            tolerance_ok=False,
            missing_fields=[],
            reasons=reasons,
            checks_run=checks_run,
            validation_results=[],
        )
        trace_step(trace, "short_circuit", "Stop after terminal vendor rejection.", {"decision": decision["decision"], "reason": "vendor not approved or not found"})
        return build_result(invoice_path, decision, trace, False)

    duplicate = exact_duplicate_check(invoice, ledger, checks_run, reasons, trace)
    if duplicate:
        decision = build_decision(
            invoice=invoice,
            duplicate=True,
            reused_number=False,
            vendor=vendor,
            po=None,
            tolerance_ok=False,
            missing_fields=[],
            reasons=reasons,
            checks_run=checks_run,
            validation_results=[],
        )
        trace_step(trace, "short_circuit", "Stop after terminal duplicate rejection.", {"decision": decision["decision"], "reason": "exact duplicate"})
        return build_result(invoice_path, decision, trace, False)

    reused_number = reused_invoice_number_check(invoice, ledger, checks_run, reasons, trace)

    po = po_lookup(invoice, vendor, pos, checks_run, reasons, trace, rules_config)
    if po is None:
        decision = build_decision(
            invoice=invoice,
            duplicate=False,
            reused_number=reused_number,
            vendor=vendor,
            po=None,
            tolerance_ok=False,
            missing_fields=[],
            reasons=reasons,
            checks_run=checks_run,
            validation_results=[],
        )
        trace_step(trace, "short_circuit", "Stop after terminal PO matching failure.", {"decision": decision["decision"], "reason": "no matching PO"})
        return build_result(invoice_path, decision, trace, False)

    if closed_po_check(po, checks_run, reasons, trace):
        decision = build_decision(
            invoice=invoice,
            duplicate=False,
            reused_number=reused_number,
            vendor=vendor,
            po=po,
            tolerance_ok=False,
            missing_fields=[],
            reasons=reasons,
            checks_run=checks_run,
            validation_results=[],
        )
        trace_step(trace, "short_circuit", "Stop after terminal PO status rejection.", {"decision": decision["decision"], "reason": "closed or fully matched PO"})
        return build_result(invoice_path, decision, trace, False)

    aggregate = aggregate_against_po(invoice, po, ledger, checks_run, reasons, trace)
    tolerance_ok = tolerance_check(invoice, po, aggregate, checks_run, reasons, trace, rules_config)
    goods_receipts = data_store.load_goods_receipts(po_id(po))
    validation_results = run_ap_validations(
        invoice=invoice,
        po=po,
        goods_receipts=goods_receipts,
        rules_config=rules_config,
        approval_thresholds=approval_thresholds,
        checks_run=checks_run,
        trace=trace,
    )

    decision = build_decision(
        invoice=invoice,
        duplicate=duplicate,
        reused_number=reused_number,
        vendor=vendor,
        po=po,
        tolerance_ok=tolerance_ok,
        missing_fields=missing_fields,
        reasons=reasons,
        checks_run=checks_run,
        validation_results=validation_results,
    )

    persisted = False
    if persist_processed and decision["decision"] == "auto_approved":
        persisted = data_store.persist_approved_invoice(invoice, po, decision)

    return build_result(invoice_path, decision, trace, persisted)


def first_purchase_order_from_db(database_url: str) -> dict[str, Any] | None:
    """Return the first PO from SQLite for the CLI preview."""
    return SQLiteDataStore(Path(database_url)).first_purchase_order()


def build_result(invoice_path: Path, decision: dict[str, Any], trace: list[dict[str, Any]], persisted: bool) -> PipelineResult:
    audit_report = {
        "generated_at": now_iso(),
        "source_invoice": str(invoice_path),
        "decision": decision,
        "trace": trace,
        "ledger_updated": persisted,
    }
    return PipelineResult(
        decision=decision,
        audit_report=audit_report,
        execution_trace={"execution_trace": trace},
        persisted_to_history=persisted,
    )


def normalize_invoice(invoice: dict[str, Any]) -> dict[str, Any]:
    """Normalize field names used by the SDD and older sample JSON files."""
    normalized = dict(invoice)
    normalized["invoice_number"] = clean_text(invoice.get("vendor_bill_no") or invoice.get("bill_id") or invoice.get("invoice_number"))
    normalized["vendor_bill_no"] = normalized["invoice_number"]
    normalized["vendor_id"] = clean_text(invoice.get("vendor_id"))
    normalized["po_id"] = clean_text(invoice.get("po_id") or invoice.get("po_number"))
    normalized["invoice_date"] = clean_text(invoice.get("invoice_date"))
    normalized["currency"] = clean_text(invoice.get("currency") or "INR").upper()
    normalized["subtotal"] = money(invoice.get("subtotal"))
    normalized["tax"] = money(invoice.get("tax"))
    normalized["total"] = money(invoice.get("total"))
    normalized["dedupe_key"] = build_dedupe_key(normalized)
    normalized["raw_extraction"] = invoice.get("raw_extraction", invoice)
    return normalized


def build_dedupe_key(invoice: dict[str, Any]) -> str:
    vendor_id = clean_text(invoice.get("vendor_id")).lower()
    invoice_number = clean_text(invoice.get("vendor_bill_no") or invoice.get("bill_id") or invoice.get("invoice_number")).lower()
    amount = money_to_string(invoice.get("total"))
    invoice_date = clean_text(invoice.get("invoice_date"))
    return f"{vendor_id}|{invoice_number}|{amount}|{invoice_date}" if vendor_id and invoice_number and invoice_date else ""


def exact_duplicate_check(
    invoice: dict[str, Any],
    ledger: list[dict[str, Any]],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
) -> bool:
    checks_run.append("duplicate")
    matches = [entry for entry in ledger if entry.get("dedupe_key") == invoice.get("dedupe_key") and invoice.get("dedupe_key")]
    duplicate = bool(matches)
    if duplicate:
        reasons.append(f"Exact duplicate: dedupe_key {invoice['dedupe_key']} already exists in invoice history")
    trace_step(trace, "duplicate", "Exact duplicate check against invoice history.", {"duplicate": duplicate, "matches": matches})
    return duplicate


def reused_invoice_number_check(
    invoice: dict[str, Any],
    ledger: list[dict[str, Any]],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
) -> bool:
    checks_run.append("reused_invoice_number")
    invoice_number = invoice.get("invoice_number")
    vendor_id = invoice.get("vendor_id")
    reused_matches = [
        entry
        for entry in ledger
        if entry.get("vendor_id") == vendor_id
        and entry.get("invoice_number") == invoice_number
        and (money(entry.get("total")) != money(invoice.get("total")) or clean_text(entry.get("invoice_date")) != invoice.get("invoice_date"))
    ]
    reused = bool(reused_matches)
    if reused:
        reasons.append("Reused invoice number: same vendor and invoice number but different amount or date")
    trace_step(trace, "reused_invoice_number", "Same invoice number with different amount/date check.", {"reused": reused, "matches": reused_matches})
    return reused


def vendor_approval_check(
    invoice: dict[str, Any],
    vendors: list[dict[str, Any]],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    checks_run.append("vendor_approval")
    vendor, resolution = resolve_vendor(invoice, vendors)
    if vendor is not None and clean_text(invoice.get("vendor_id")) != clean_text(vendor.get("vendor_id")):
        invoice["vendor_id"] = clean_text(vendor.get("vendor_id"))
        invoice["dedupe_key"] = build_dedupe_key(invoice)
    approved = vendor_is_approved(vendor)
    invoice_vendor_name = clean_text(invoice.get("vendor_name"))
    master_vendor_name = clean_text((vendor or {}).get("name"))
    name_matches = vendor_names_match(invoice, invoice_vendor_name, master_vendor_name)
    if vendor is None:
        reasons.append(f"Vendor not found: {invoice.get('vendor_id')}")
    elif not approved:
        reasons.append(f"Vendor not approved: {invoice.get('vendor_id')}")
    elif not invoice_vendor_name:
        reasons.append("Vendor name missing: invoice vendor_name is required for vendor master validation")
    elif not name_matches:
        reasons.append(f"Vendor name mismatch: invoice has '{invoice_vendor_name}', vendor master has '{master_vendor_name}'")
    trace_step(
        trace,
        "vendor_approval",
        "Vendor approval and exact vendor-name check against vendor master.",
        {
            "vendor": vendor,
            "approved": approved,
            "invoice_vendor_name": invoice_vendor_name,
            "master_vendor_name": master_vendor_name,
            "name_matches": name_matches,
            "resolution": resolution,
            "comparison": "exact_or_ocr_normalized",
        },
    )
    return vendor if approved and name_matches else None


def resolve_vendor(invoice: dict[str, Any], vendors: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Resolve vendor by exact ID, then by OCR-normalized name when input came from extraction."""
    invoice_vendor_id = clean_text(invoice.get("vendor_id"))
    if invoice_vendor_id:
        vendor = next((item for item in vendors if clean_text(item.get("vendor_id")) == invoice_vendor_id), None)
        if vendor is not None:
            return vendor, "vendor_id_exact"

    extraction_method = clean_text(invoice.get("extraction_method") or (invoice.get("raw_extraction") or {}).get("method"))
    if extraction_method:
        invoice_vendor_name = compact_name(clean_text(invoice.get("vendor_name")))
        name_matches = [item for item in vendors if compact_name(clean_text(item.get("name"))) == invoice_vendor_name]
        if len(name_matches) == 1:
            return name_matches[0], "ocr_vendor_name_normalized"
        if len(name_matches) > 1:
            return None, "ocr_vendor_name_ambiguous"

    return None, "not_found"


def vendor_names_match(invoice: dict[str, Any], invoice_vendor_name: str, master_vendor_name: str) -> bool:
    """Compare vendor names strictly for JSON and OCR-normalized for extracted documents."""
    if not invoice_vendor_name or not master_vendor_name:
        return False
    if invoice_vendor_name == master_vendor_name:
        return True

    extraction_method = clean_text(invoice.get("extraction_method") or (invoice.get("raw_extraction") or {}).get("method"))
    if extraction_method:
        return compact_name(invoice_vendor_name) == compact_name(master_vendor_name)
    return False


def compact_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def po_lookup(
    invoice: dict[str, Any],
    vendor: dict[str, Any] | None,
    pos: list[dict[str, Any]],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
    rules_config: dict[str, Any],
) -> dict[str, Any] | None:
    checks_run.append("po_match")
    exact_po_id = invoice.get("po_id")
    exact = next((po for po in pos if po_id(po) == exact_po_id), None)
    if exact:
        trace_step(trace, "po_match", "PO lookup by exact po_id.", {"match_type": "exact", "po": exact})
        return exact

    vendor_id = invoice.get("vendor_id") or (vendor or {}).get("vendor_id")
    fuzzy = None
    for po in pos:
        if clean_text(po.get("vendor_id")) != vendor_id:
            continue
        po_amount_value = po_amount(po)
        matched = po_matched_amount(po)
        remaining = po_amount_value - matched
        amount_window = po_amount_value * rules_config["fuzzy_match_amount_window_pct"]
        invoice_date = parse_date(invoice.get("invoice_date"))
        po_created_at = parse_datetime_date(po.get("created_at"))
        date_window_days = int(rules_config["fuzzy_match_date_window_days"])
        date_ok = invoice_date is not None and po_created_at is not None and abs((invoice_date - po_created_at).days) <= date_window_days
        if invoice["total"] <= remaining + amount_window and date_ok:
            fuzzy = po
            break

    if fuzzy is None:
        reasons.append(f"No matching PO found by exact po_id or vendor/amount/date fuzzy match within {rules_config['fuzzy_match_date_window_days']} days")
    else:
        reasons.append(f"PO fuzzy match used vendor, amount window, and invoice date within {rules_config['fuzzy_match_date_window_days']} days")
    trace_step(trace, "po_match", "PO lookup fallback by vendor plus amount and invoice-date window.", {"match_type": "fuzzy" if fuzzy else "none", "po": fuzzy, "date_window_days": rules_config["fuzzy_match_date_window_days"]})
    return fuzzy


def closed_po_check(
    po: dict[str, Any],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
) -> bool:
    checks_run.append("po_status")
    status = clean_text(po.get("status")).lower()
    rejected = status in {"closed", "fully_matched"}
    if rejected:
        reasons.append(f"PO {po_id(po)} is already {status} \u2014 cannot accept additional invoices")
    trace_step(
        trace,
        "po_status",
        "Reject invoices against closed or fully matched purchase orders before aggregation.",
        {"po_id": po_id(po), "status": status, "rejected": rejected},
    )
    return rejected


def aggregate_against_po(
    invoice: dict[str, Any],
    po: dict[str, Any] | None,
    ledger: list[dict[str, Any]],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    checks_run.append("po_aggregate")
    if po is None:
        aggregate = {"po_amount": "0.00", "previous_matched_amount": "0.00", "projected_matched_amount": money_to_string(invoice.get("total"))}
        trace_step(trace, "po_aggregate", "Aggregate skipped because no PO matched.", aggregate)
        return aggregate

    current_po_id = po_id(po)
    ledger_total = sum(money(entry.get("total")) for entry in ledger if clean_text(entry.get("po_id") or entry.get("po_number")) == current_po_id)
    previous = max(po_matched_amount(po), ledger_total)
    projected = previous + money(invoice.get("total"))
    aggregate = {
        "po_id": current_po_id,
        "po_amount": money_to_string(po_amount(po)),
        "previous_matched_amount": money_to_string(previous),
        "invoice_total": money_to_string(invoice["total"]),
        "projected_matched_amount": money_to_string(projected),
    }
    trace_step(trace, "po_aggregate", "Aggregate running matched amount against PO for split invoices.", aggregate)
    return aggregate


def tolerance_check(
    invoice: dict[str, Any],
    po: dict[str, Any] | None,
    aggregate: dict[str, Any],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
    rules_config: dict[str, Any],
) -> bool:
    del invoice
    checks_run.append("tolerance")
    if po is None:
        trace_step(trace, "tolerance", "Tolerance check skipped because no PO matched.", {"passed": False})
        return False

    amount = po_amount(po)
    projected = money(aggregate["projected_matched_amount"])
    overage = max(projected - amount, Decimal("0"))
    allowed = max(amount * rules_config["tolerance_pct"], rules_config["tolerance_abs"])
    passed = overage <= allowed
    if not passed:
        pct = (overage / amount * Decimal("100")) if amount else Decimal("0")
        reasons.append(f"PO amount exceeded: {money_to_string(projected)} vs {money_to_string(amount)} ({pct:.2f}% over, allowed {money_to_string(allowed)})")
    trace_step(trace, "tolerance", "Check projected matched amount against PO tolerance.", {"projected": money_to_string(projected), "po_amount": money_to_string(amount), "overage": money_to_string(overage), "allowed": money_to_string(allowed), "passed": passed})
    return passed


def missing_critical_field_check(
    invoice: dict[str, Any],
    checks_run: list[str],
    reasons: list[str],
    trace: list[dict[str, Any]],
) -> list[str]:
    checks_run.append("completeness")
    missing = [field for field in CRITICAL_FIELDS if not invoice.get(field)]
    if missing:
        reasons.append("Missing critical fields: " + ", ".join(missing))
    trace_step(trace, "completeness", "Missing critical field check.", {"missing_fields": missing})
    return missing


def run_ap_validations(
    invoice: dict[str, Any],
    po: dict[str, Any],
    goods_receipts: list[dict[str, Any]],
    rules_config: dict[str, Any],
    approval_thresholds: dict[str, Any],
    checks_run: list[str],
    trace: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validations = [
        ocr_confidence_validation(invoice),
        total_calculation_validation(invoice, rules_config),
        tax_validation(invoice, rules_config),
        quantity_validation(invoice, po),
        unit_price_validation(invoice, po),
        three_way_matching_validation(invoice, po, goods_receipts),
        approval_workflow_validation(invoice, approval_thresholds),
    ]
    for validation in validations:
        checks_run.append(validation["rule"])
        trace_step(trace, validation["rule"], validation["reason"], validation)
    return validations


def ocr_confidence_validation(invoice: dict[str, Any]) -> dict[str, Any]:
    raw = invoice.get("raw_extraction") or {}
    confidence = raw.get("confidence", invoice.get("ocr_confidence"))
    method = raw.get("method") or invoice.get("extraction_method") or "structured_json"
    if confidence is None:
        return validation_result("OCR Confidence", "PASS", "Structured JSON input; OCR confidence is not applicable.", {"method": method})
    numeric = Decimal(str(confidence))
    if numeric >= Decimal("0.90"):
        status = "PASS"
        reason = "Extraction confidence is high."
    elif numeric >= Decimal("0.75"):
        status = "LOW_CONFIDENCE"
        reason = "Extraction confidence is acceptable but should be watched."
    else:
        status = "MANUAL_REVIEW"
        reason = "Extraction confidence is below the safe processing threshold."
    return validation_result("OCR Confidence", status, reason, {"method": method, "confidence": str(numeric)})


def total_calculation_validation(invoice: dict[str, Any], rules_config: dict[str, Any]) -> dict[str, Any]:
    line_total = sum(money(line.get("line_amount")) for line in invoice.get("line_items") or [])
    subtotal = money(invoice.get("subtotal"))
    tax = money(invoice.get("tax"))
    actual_total = money(invoice.get("total"))
    tolerance = money(rules_config.get("total_tolerance_abs", "5.00"))
    subtotal_difference = abs(line_total - subtotal) if line_total else Decimal("0")
    expected_total = subtotal + tax
    total_difference = abs(expected_total - actual_total)
    passed = subtotal_difference <= tolerance and total_difference <= tolerance
    status = "PASS" if passed else "MANUAL_REVIEW"
    reason = "Invoice arithmetic is correct." if passed else "Invoice arithmetic does not reconcile."
    return validation_result(
        "Total Calculation Validation",
        status,
        reason,
        {
            "line_subtotal": money_to_string(line_total),
            "invoice_subtotal": money_to_string(subtotal),
            "subtotal_difference": money_to_string(subtotal_difference),
            "expected_total": money_to_string(expected_total),
            "actual_total": money_to_string(actual_total),
            "total_difference": money_to_string(total_difference),
            "allowed_tolerance": money_to_string(tolerance),
        },
    )


def tax_validation(invoice: dict[str, Any], rules_config: dict[str, Any]) -> dict[str, Any]:
    subtotal = money(invoice.get("subtotal"))
    tax = money(invoice.get("tax"))
    total = money(invoice.get("total"))
    expected_total = subtotal + tax
    difference = abs(expected_total - total)
    tolerance = money(rules_config.get("tax_tolerance_abs", "5.00"))
    passed = difference <= tolerance
    status = "PASS" if passed else "MANUAL_REVIEW"
    reason = "Subtotal plus tax matches invoice total." if passed else "Subtotal plus tax does not match invoice total."
    return validation_result(
        "Tax Validation",
        status,
        reason,
        {
            "subtotal": money_to_string(subtotal),
            "tax": money_to_string(tax),
            "expected_total": money_to_string(expected_total),
            "actual_total": money_to_string(total),
            "difference": money_to_string(difference),
            "allowed_tolerance": money_to_string(tolerance),
        },
    )


def quantity_validation(invoice: dict[str, Any], po: dict[str, Any]) -> dict[str, Any]:
    po_lines = po.get("line_items") or []
    if not po_lines:
        return validation_result("Quantity Validation", "MANUAL_REVIEW", "PO line items are unavailable for quantity validation.", {})

    details = []
    statuses = []
    for invoice_line in invoice.get("line_items") or []:
        matched = match_po_line(invoice_line, po_lines)
        invoice_quantity = money(invoice_line.get("quantity"))
        if matched is None:
            details.append({"invoice_item": invoice_line.get("description"), "status": "MANUAL_REVIEW", "reason": "No matching PO line."})
            statuses.append("MANUAL_REVIEW")
            continue
        ordered = money(matched.get("ordered_quantity") or matched.get("quantity_ordered"))
        if invoice_quantity == ordered:
            status = "PASS"
            reason = "Invoice quantity equals PO ordered quantity."
        elif invoice_quantity > ordered:
            status = "MANUAL_REVIEW"
            reason = "Invoice quantity is greater than PO ordered quantity."
        else:
            status = PARTIAL_STATUS
            reason = "Invoice quantity is lower than PO ordered quantity; partial approval depends on goods receipt."
        details.append(
            {
                "invoice_item": invoice_line.get("description"),
                "po_item": matched.get("item_name") or matched.get("description"),
                "invoice_quantity": money_to_string(invoice_quantity),
                "ordered_quantity": money_to_string(ordered),
                "status": status,
                "reason": reason,
            }
        )
        statuses.append(status)

    return validation_result("Quantity Validation", combine_statuses(statuses), "Line-item quantities compared against PO lines.", {"lines": details})


def unit_price_validation(invoice: dict[str, Any], po: dict[str, Any]) -> dict[str, Any]:
    po_lines = po.get("line_items") or []
    if not po_lines:
        return validation_result("Unit Price Validation", "MANUAL_REVIEW", "PO line items are unavailable for unit price validation.", {})

    details = []
    statuses = []
    for invoice_line in invoice.get("line_items") or []:
        matched = match_po_line(invoice_line, po_lines)
        invoice_price = money(invoice_line.get("unit_price"))
        if matched is None:
            details.append({"invoice_item": invoice_line.get("description"), "status": "MANUAL_REVIEW", "reason": "No matching PO line."})
            statuses.append("MANUAL_REVIEW")
            continue
        expected_price = money(matched.get("unit_price"))
        difference = invoice_price - expected_price
        status = "PASS" if difference == 0 else "MANUAL_REVIEW"
        reason = "Invoice unit price matches PO unit price." if status == "PASS" else "Invoice unit price differs from PO unit price."
        details.append(
            {
                "invoice_item": invoice_line.get("description"),
                "po_item": matched.get("item_name") or matched.get("description"),
                "expected_price": money_to_string(expected_price),
                "invoice_price": money_to_string(invoice_price),
                "difference": money_to_string(difference),
                "status": status,
                "reason": reason,
            }
        )
        statuses.append(status)

    return validation_result("Unit Price Validation", combine_statuses(statuses), "Line-item unit prices compared against PO lines.", {"lines": details})


def three_way_matching_validation(invoice: dict[str, Any], po: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    po_lines = po.get("line_items") or []
    if not receipts:
        return validation_result("Goods Receipt Validation", "MANUAL_REVIEW", "No goods receipt found for this PO.", {"po_id": po_id(po)})
    if not po_lines:
        return validation_result("Goods Receipt Validation", "MANUAL_REVIEW", "PO line items are unavailable for three-way matching.", {"po_id": po_id(po)})

    details = []
    statuses = []
    for invoice_line in invoice.get("line_items") or []:
        matched = match_po_line(invoice_line, po_lines)
        invoice_quantity = money(invoice_line.get("quantity"))
        if matched is None:
            statuses.append("MANUAL_REVIEW")
            details.append({"invoice_item": invoice_line.get("description"), "status": "MANUAL_REVIEW", "reason": "No matching PO line."})
            continue
        item_keys = {
            normalize_item_name(matched.get("item_name")),
            normalize_item_name(matched.get("description")),
            normalize_item_name(matched.get("po_line_id")),
        }
        received = sum(money(receipt.get("quantity_received")) for receipt in receipts if normalize_item_name(receipt.get("item")) in item_keys)
        if invoice_quantity == received:
            status = "PASS"
            reason = "Invoice quantity equals received quantity."
        elif invoice_quantity > received:
            status = PARTIAL_STATUS
            reason = "Invoice quantity is greater than received quantity; partial approval or hold required for excess quantity."
        else:
            status = "MANUAL_REVIEW"
            reason = "Invoice quantity is lower than received quantity."
        statuses.append(status)
        details.append(
            {
                "invoice_item": invoice_line.get("description"),
                "invoice_quantity": money_to_string(invoice_quantity),
                "received_quantity": money_to_string(received),
                "status": status,
                "reason": reason,
            }
        )

    return validation_result("Goods Receipt Validation", combine_statuses(statuses), "PO, goods receipt, and invoice quantities compared.", {"lines": details})


def approval_workflow_validation(invoice: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    amount = money(invoice.get("total"))
    route = approval_route(amount, thresholds)
    return validation_result(
        "Approval Workflow",
        route["status"],
        route["reason"],
        {
            "invoice_amount": money_to_string(amount),
            "approval_level": route["approval_level"],
            "assigned_approver_role": route["role"],
        },
    )


def approval_route(amount: Decimal, thresholds: dict[str, Any]) -> dict[str, str]:
    rows = thresholds.get("thresholds")
    if not rows:
        rows = thresholds.get("departments", {}).get("default", [])
    if not rows:
        return {"status": "MANUAL_REVIEW", "approval_level": "Unconfigured", "role": "Finance Operations", "reason": "No approval thresholds configured."}

    normalized_rows = sorted(rows, key=lambda row: money(row.get("min_amount", "0")))
    for index, row in enumerate(normalized_rows):
        min_amount = money(row.get("min_amount", "0"))
        max_amount = money(row.get("max_amount", "999999999"))
        if min_amount <= amount <= max_amount:
            role = clean_text(row.get("role")) or "Finance Reviewer"
            if index == 0:
                return {"status": "PASS", "approval_level": "Auto Approved", "role": role, "reason": "Invoice amount is within auto-approval threshold."}
            return {"status": "MANUAL_REVIEW", "approval_level": f"Level {index + 1}", "role": role, "reason": f"Invoice amount requires {role} approval."}
    last_role = clean_text(normalized_rows[-1].get("role")) or "Senior Finance Approval"
    return {"status": "MANUAL_REVIEW", "approval_level": "Highest Approval", "role": last_role, "reason": f"Invoice amount exceeds configured thresholds and requires {last_role}."}


def validation_result(rule: str, status: str, reason: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"rule": rule, "status": status, "reason": reason, "details": make_json_safe(details)}


def combine_statuses(statuses: list[str]) -> str:
    if not statuses:
        return "PASS"
    if any(status in BLOCKING_STATUSES for status in statuses):
        return "MANUAL_REVIEW"
    if any(status == PARTIAL_STATUS for status in statuses):
        return PARTIAL_STATUS
    if any(status == "LOW_CONFIDENCE" for status in statuses):
        return "LOW_CONFIDENCE"
    return "PASS"


def match_po_line(invoice_line: dict[str, Any], po_lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    invoice_item = normalize_item_name(invoice_line.get("description") or invoice_line.get("item_name"))
    for po_line in po_lines:
        candidates = [
            po_line.get("item_name"),
            po_line.get("description"),
            po_line.get("product_code"),
            po_line.get("sku"),
        ]
        if invoice_item and invoice_item in {normalize_item_name(value) for value in candidates}:
            return po_line
    return None


def normalize_item_name(value: Any) -> str:
    return clean_text(value).lower()


def build_decision(
    invoice: dict[str, Any],
    duplicate: bool,
    reused_number: bool,
    vendor: dict[str, Any] | None,
    po: dict[str, Any] | None,
    tolerance_ok: bool,
    missing_fields: list[str],
    reasons: list[str],
    checks_run: list[str],
    validation_results: list[dict[str, Any]],
) -> dict[str, Any]:
    if has_closed_po_rejection(reasons):
        verdict = "rejected"
    elif duplicate:
        verdict = "rejected"
    elif missing_fields or vendor is None or po is None:
        verdict = "rejected"
    elif any(result["status"] == PARTIAL_STATUS for result in validation_results):
        verdict = "partial_approved"
    elif any(result["status"] in BLOCKING_STATUSES or result["status"] == "LOW_CONFIDENCE" for result in validation_results):
        verdict = "needs_review"
    elif reused_number or not tolerance_ok:
        verdict = "needs_review"
    else:
        verdict = "auto_approved"

    has_validation_findings = any(result["status"] != "PASS" for result in validation_results)
    if not reasons and not has_validation_findings:
        reasons.append("Invoice passed duplicate, vendor approval, PO match, tolerance, completeness, and AP validation checks")
    for result in validation_results:
        if result["status"] != "PASS":
            reasons.append(f"{result['rule']}: {result['reason']}")

    return {
        "decision": verdict,
        "invoice_number": invoice.get("invoice_number"),
        "vendor_id": invoice.get("vendor_id"),
        "po_id": invoice.get("po_id") or (po_id(po) if po else None),
        "dedupe_key": invoice.get("dedupe_key"),
        "reasons": reasons,
        "checks_run": checks_run,
        "validations": validation_results,
        "decided_at": now_iso(),
    }


def has_closed_po_rejection(reasons: list[str]) -> bool:
    return any("cannot accept additional invoices" in reason for reason in reasons)


def append_invoice_to_ledger(
    path: Path,
    ledger: list[dict[str, Any]],
    invoice: dict[str, Any],
    po: dict[str, Any] | None,
    decision: dict[str, Any],
) -> bool:
    if any(entry.get("dedupe_key") == invoice.get("dedupe_key") for entry in ledger):
        return False

    ledger.append(
        {
            "invoice_id": f"INV-{len(ledger) + 1:05d}",
            "vendor_id": invoice.get("vendor_id"),
            "po_id": invoice.get("po_id") or (po_id(po) if po else None),
            "invoice_number": invoice.get("invoice_number"),
            "invoice_date": invoice.get("invoice_date"),
            "subtotal": money_to_string(invoice.get("subtotal")),
            "tax": money_to_string(invoice.get("tax")),
            "total": money_to_string(invoice.get("total")),
            "currency": invoice.get("currency"),
            "dedupe_key": invoice.get("dedupe_key"),
            "raw_extraction": invoice.get("raw_extraction"),
            "source_file_url": invoice.get("source_file") or "",
            "decision": decision["decision"],
            "created_at": now_iso(),
        }
    )
    path.write_text(json.dumps(make_json_safe(ledger), indent=2), encoding="utf-8")
    return True


def update_purchase_order_match(path: Path, po: dict[str, Any], invoice: dict[str, Any]) -> None:
    pos = read_json_array(path)
    current_id = po_id(po)
    for item in pos:
        if po_id(item) != current_id:
            continue
        new_matched = po_matched_amount(item) + money(invoice.get("total"))
        amount = po_amount(item)
        item["matched_amount"] = money_to_string(new_matched)
        if new_matched >= amount:
            item["status"] = "closed"
        elif new_matched > 0:
            item["status"] = "partially_matched"
        else:
            item["status"] = "open"
        break
    path.write_text(json.dumps(make_json_safe(pos), indent=2), encoding="utf-8")


def trace_step(trace: list[dict[str, Any]], check: str, purpose: str, output: dict[str, Any]) -> None:
    trace.append({"check": check, "purpose": purpose, "output": make_json_safe(output), "timestamp": now_iso()})


def read_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def read_invoice_input(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES:
        extracted = extract_invoice_from_pdf(path)
        return {
            "vendor_name": extracted["vendor_name"],
            "vendor_id": extracted["vendor_id"],
            "invoice_number": extracted["invoice_number"],
            "invoice_date": extracted["invoice_date"],
            "line_items": extracted["line_items"],
            "subtotal": extracted["subtotal"],
            "tax": extracted["tax_amount"],
            "total": extracted["total_amount"],
            "po_id": extracted["po_reference"],
            "ocr_confidence": extracted.get("ocr_confidence"),
            "extraction_method": extracted.get("extraction_method"),
            "raw_extraction": extracted,
            "source_file": str(path),
        }
    return read_json_object(path)


def read_json_array(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return data


def read_invoice_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [normalize_ledger_entry(entry) for entry in data]
    if isinstance(data, dict):
        return [normalize_ledger_entry(entry) for entry in data.get("invoices", [])]
    raise ValueError(f"{path} must contain a JSON array or object with invoices[]")


def load_rules_config(path: Path = Path("config/rules_config.json")) -> dict[str, Any]:
    """Load configurable rule values from file, mirroring the future rules_config table."""
    config = dict(DEFAULT_RULES_CONFIG)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in ("tolerance_pct", "tolerance_abs", "fuzzy_match_amount_window_pct", "tax_tolerance_abs", "total_tolerance_abs"):
                if key in raw:
                    config[key] = money(raw[key])
            if "fuzzy_match_date_window_days" in raw:
                config["fuzzy_match_date_window_days"] = int(raw["fuzzy_match_date_window_days"])
    return config


def load_approval_thresholds(path: Path = Path("config/approval_thresholds.json")) -> dict[str, Any]:
    if not path.exists():
        return {
            "thresholds": [
                {"min_amount": "0.00", "max_amount": "50000.00", "role": "AP Automation"},
                {"min_amount": "50000.01", "max_amount": "500000.00", "role": "Finance Manager"},
                {"min_amount": "500000.01", "max_amount": "999999999.00", "role": "Senior Finance Approval"},
            ]
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    raise ValueError(f"{path} must contain a JSON object")


def normalize_ledger_entry(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    normalized["po_id"] = clean_text(entry.get("po_id") or entry.get("po_number"))
    normalized["invoice_number"] = clean_text(entry.get("invoice_number"))
    normalized["vendor_id"] = clean_text(entry.get("vendor_id"))
    normalized["invoice_date"] = clean_text(entry.get("invoice_date"))
    normalized["total"] = money(entry.get("total"))
    normalized["dedupe_key"] = clean_text(entry.get("dedupe_key")) or build_dedupe_key(normalized)
    return normalized


def normalize_goods_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(receipt)
    normalized["po_id"] = clean_text(receipt.get("po_id") or receipt.get("po_number"))
    normalized["item"] = clean_text(receipt.get("item") or receipt.get("item_name") or receipt.get("description") or receipt.get("po_line_id"))
    normalized["quantity_received"] = money(receipt.get("quantity_received"))
    normalized["receipt_date"] = clean_text(receipt.get("receipt_date") or receipt.get("received_at"))
    return normalized


def vendor_is_approved(vendor: dict[str, Any] | None) -> bool:
    if vendor is None:
        return False
    if "approved" in vendor:
        return bool(vendor["approved"])
    return clean_text(vendor.get("approval_status")).lower() == "approved" and clean_text(vendor.get("status")).lower() == "active"


def po_id(po: dict[str, Any] | None) -> str:
    return clean_text((po or {}).get("po_id") or (po or {}).get("po_number"))


def po_amount(po: dict[str, Any]) -> Decimal:
    return money(po.get("po_amount") or po.get("total_amount"))


def po_matched_amount(po: dict[str, Any]) -> Decimal:
    return money(po.get("matched_amount") or po.get("invoiced_amount"))


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money_to_string(value: Any) -> str:
    return f"{money(value):.2f}"


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(clean_text(value)[:10])
    except ValueError:
        return None


def parse_datetime_date(value: Any) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    return parse_date(text)


def make_json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return money_to_string(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: make_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [make_json_safe(item) for item in value]
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
