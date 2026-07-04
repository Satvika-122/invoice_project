from __future__ import annotations

import json
import shutil
from pathlib import Path

from invoice_processing.app import exact_duplicate_check, normalize_invoice, run_invoice_pipeline
from invoice_processing.extraction import extract_invoice_from_pdf


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def copy_stage_data(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    invoice_path = tmp_path / "invoice.json"
    pos_path = tmp_path / "pos.json"
    ledger_path = tmp_path / "invoices.json"
    shutil.copyfile(ROOT / "data" / "invoice.json", invoice_path)
    shutil.copyfile(ROOT / "data" / "pos.json", pos_path)
    shutil.copyfile(ROOT / "data" / "invoices.json", ledger_path)
    invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    invoice.update(
        {
            "invoice_number": "INV-2026-0007",
            "vendor_bill_no": "INV-2026-0007",
            "vendor_name": "Bharat Tech Supplies Pvt Ltd",
            "subtotal": "45000.00",
            "tax": "0.00",
            "total": "45000.00",
            "line_items": [{"description": "Laptop", "quantity": "1", "unit_price": "45000.00", "line_amount": "45000.00"}],
        }
    )
    write_json(invoice_path, invoice)
    write_json(
        tmp_path / "purchase_orders.json",
        [
            {
                "po_id": "PO-9001",
                "vendor_id": "VEND-1001",
                "po_amount": "767000.00",
                "matched_amount": "0.00",
                "status": "open",
                "created_at": "2026-06-01T00:00:00Z",
                "line_items": [{"item_name": "Laptop", "ordered_quantity": "1", "unit_price": "45000.00"}],
            },
            {
                "po_id": "PO-SPLIT",
                "vendor_id": "VEND-1001",
                "po_amount": "300.00",
                "matched_amount": "0.00",
                "status": "open",
                "created_at": "2026-06-01T00:00:00Z",
                "line_items": [{"item_name": "Service", "ordered_quantity": "1", "unit_price": "100.00"}],
            },
        ],
    )
    write_json(
        tmp_path / "goods_receipts.json",
        [
            {"receipt_id": "GR-TEST-1", "po_id": "PO-9001", "item": "Laptop", "quantity_received": "1", "receipt_date": "2026-06-20"},
            {"receipt_id": "GR-SPLIT-1", "po_id": "PO-SPLIT", "item": "Service", "quantity_received": "1", "receipt_date": "2026-06-20"},
        ],
    )
    return invoice_path, pos_path, ledger_path


def run_with(invoice_path: Path, pos_path: Path, ledger_path: Path, persist: bool = False):
    return run_invoice_pipeline(
        invoice_path=invoice_path,
        vendors_path=ROOT / "data" / "vendors.json",
        purchase_orders_path=pos_path,
        processed_invoices_path=ledger_path,
        goods_receipts_path=ledger_path.parent / "goods_receipts.json",
        persist_processed=persist,
    )


def test_clean_split_invoice_auto_approves(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    result = run_with(invoice_path, pos_path, ledger_path)

    assert result.decision["decision"] == "auto_approved"
    assert "po_aggregate" in result.decision["checks_run"]
    assert result.decision["reasons"]


def test_duplicate_is_rejected_after_append(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)

    first = run_with(invoice_path, pos_path, ledger_path, persist=True)
    second = run_with(invoice_path, pos_path, ledger_path, persist=True)

    assert first.decision["decision"] == "auto_approved"
    assert first.persisted_to_history is True
    assert second.decision["decision"] == "rejected"
    assert any("Exact duplicate" in reason for reason in second.decision["reasons"])


def test_vendor_bill_no_is_used_as_duplicate_identity(tmp_path: Path) -> None:
    invoice = normalize_invoice(
        {
            "vendor_id": "VEND-1001",
            "vendor_bill_no": "BILL-ACME-001",
            "invoice_date": "2026-06-27",
            "total": 1200.30,
        }
    )
    ledger = [{"dedupe_key": "vend-1001|bill-acme-001|1200.30|2026-06-27"}]
    checks_run: list[str] = []
    reasons: list[str] = []
    trace: list[dict] = []

    duplicate = exact_duplicate_check(invoice, ledger, checks_run, reasons, trace)

    assert invoice["invoice_number"] == "BILL-ACME-001"
    assert invoice["dedupe_key"] == "vend-1001|bill-acme-001|1200.30|2026-06-27"
    assert duplicate is True


def test_reused_invoice_number_different_amount_needs_review(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger.append(
        {
            "invoice_id": "INV-REUSED",
            "vendor_id": "VEND-1001",
            "po_id": "PO-9001",
            "invoice_number": "INV-2026-0007",
            "invoice_date": "2026-06-01",
            "total": "100.00",
            "dedupe_key": "vend-1001|inv-2026-0007|100.00|2026-06-01",
        }
    )
    write_json(ledger_path, ledger)

    result = run_with(invoice_path, pos_path, ledger_path)

    assert result.decision["decision"] == "needs_review"
    assert any("Reused invoice number" in reason for reason in result.decision["reasons"])
    assert not any("Exact duplicate" in reason for reason in result.decision["reasons"])


def test_amount_beyond_tolerance_needs_review(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    invoice["invoice_number"] = "INV-OVER-TOL"
    invoice["subtotal"] = "800000.00"
    invoice["tax"] = "0.00"
    invoice["total"] = "800000.00"
    invoice["line_items"] = [{"description": "Laptop", "quantity": "1", "unit_price": "45000.00", "line_amount": "800000.00"}]
    write_json(invoice_path, invoice)

    result = run_with(invoice_path, pos_path, ledger_path)

    assert result.decision["decision"] == "needs_review"
    assert any("PO amount exceeded" in reason for reason in result.decision["reasons"])


def test_unapproved_vendor_rejected(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    invoice["vendor_id"] = "VEND-2002"
    invoice["invoice_number"] = "INV-BLOCKED"
    write_json(invoice_path, invoice)

    result = run_with(invoice_path, pos_path, ledger_path)

    assert result.decision["decision"] == "rejected"
    assert any("Vendor not approved" in reason for reason in result.decision["reasons"])


def test_scanned_pdf_extraction_returns_explicit_nulls(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf_path = tmp_path / "scanned.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n\x00\x01image-only")

    extracted = extract_invoice_from_pdf(pdf_path)

    assert extracted["extraction_method"] in {"ocr_unavailable", "ocr_stub_scanned_pdf"}
    assert extracted["invoice_number"] is None
    assert extracted["line_items"] == []
    assert "raw_extraction" in extracted


def test_missing_invoice_date_rejects(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    invoice.pop("invoice_date")
    write_json(invoice_path, invoice)

    result = run_with(invoice_path, pos_path, ledger_path)

    assert result.decision["decision"] == "rejected"
    assert any("invoice_date" in reason for reason in result.decision["reasons"])


def test_missing_total_rejects(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    invoice.pop("total")
    write_json(invoice_path, invoice)

    result = run_with(invoice_path, pos_path, ledger_path)

    assert result.decision["decision"] == "rejected"
    assert any("total" in reason for reason in result.decision["reasons"])


def test_implied_po_reference_is_extracted_from_free_text(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf_path = tmp_path / "implied-po.pdf"
    pdf_path.write_text(
        "Invoice Number: INV-PDF-1\nDate: 2026-06-27\nVendor: Bharat Tech Supplies Pvt Ltd\nRef: PO-9001\nTotal: 531000.00\nGST: 81000.00",
        encoding="utf-8",
    )

    extracted = extract_invoice_from_pdf(pdf_path)

    assert extracted["po_reference"] == "PO-9001"
    assert extracted["po_reference_confidence"] == "medium"


def test_split_po_two_sequential_invoices_updates_matched_amount_and_status(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    pos = [{"po_id": "PO-SPLIT", "vendor_id": "VEND-1001", "po_amount": "300.00", "matched_amount": "0.00", "status": "open", "created_at": "2026-06-01T00:00:00Z"}]
    write_json(pos_path, pos)
    write_json(ledger_path, [])

    first_invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    first_invoice.update(
        {
            "invoice_number": "INV-SPLIT-1",
                "po_number": "PO-SPLIT",
                "po_id": "PO-SPLIT",
                "vendor_bill_no": "INV-SPLIT-1",
            "subtotal": "100.00",
            "tax": "0.00",
            "total": "100.00",
            "line_items": [{"description": "Service", "quantity": "1", "unit_price": "100.00", "line_amount": "100.00"}],
        }
    )
    write_json(invoice_path, first_invoice)
    first = run_with(invoice_path, pos_path, ledger_path, persist=True)

    second_invoice = dict(first_invoice)
    second_invoice.update({"invoice_number": "INV-SPLIT-2", "vendor_bill_no": "INV-SPLIT-2", "subtotal": "200.00", "total": "200.00", "line_items": []})
    write_json(invoice_path, second_invoice)
    second = run_with(invoice_path, pos_path, ledger_path, persist=True)

    updated_po = json.loads(pos_path.read_text(encoding="utf-8"))[0]
    assert first.decision["decision"] == "auto_approved"
    assert second.decision["decision"] == "auto_approved"
    assert updated_po["matched_amount"] == "300.00"
    assert updated_po["status"] == "closed"


def test_closed_and_fully_matched_po_reject_before_aggregation(tmp_path: Path) -> None:
    invoice_path, pos_path, ledger_path = copy_stage_data(tmp_path)
    base_invoice = json.loads(invoice_path.read_text(encoding="utf-8"))
    base_invoice["vendor_name"] = "Bharat Tech Supplies Pvt Ltd"
    write_json(ledger_path, [])

    for status in ("closed", "fully_matched"):
        invoice = dict(base_invoice)
        invoice["invoice_number"] = f"INV-{status.upper()}"
        write_json(invoice_path, invoice)
        write_json(
            pos_path,
            [
                {
                    "po_id": "PO-9001",
                    "vendor_id": "VEND-1001",
                    "po_amount": "767000.00",
                    "matched_amount": "767000.00",
                    "status": status,
                    "created_at": "2026-06-01T00:00:00Z",
                }
            ],
        )

        result = run_with(invoice_path, pos_path, ledger_path)

        assert result.decision["decision"] == "rejected"
        assert any(f"PO PO-9001 is already {status}" in reason for reason in result.decision["reasons"])
        assert "po_status" in result.decision["checks_run"]
        assert "po_aggregate" not in result.decision["checks_run"]
