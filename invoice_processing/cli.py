from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from invoice_processing.app import first_purchase_order_from_db, run_invoice_pipeline
from invoice_processing.extraction import IMAGE_SUFFIXES


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AP invoice processing pipeline.")
    parser.add_argument("--invoice", type=Path, default=Path("data/invoice.json"))
    parser.add_argument("--batch-dir", type=Path, default=None, help="Process all JSON/PDF invoices in a directory.")
    parser.add_argument("--vendors", type=Path, default=Path("data/vendors.json"))
    parser.add_argument("--pos", "--purchase-orders", dest="pos", type=Path, default=Path("data/pos.json"))
    parser.add_argument("--invoices", "--processed-invoices", dest="invoices", type=Path, default=Path("data/invoices.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--use-db", action="store_true", help="Read/write AP data from local SQLite instead of JSON files.")
    parser.add_argument("--db-path", type=Path, default=Path("data/invoice_processing.db"))
    parser.add_argument("--db-host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--db-port", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--db-name", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--db-user", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--db-password", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-persist-history",
        action="store_true",
        help="Do not record approved invoices into processed invoice history.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    database_url = build_database_url(args) if args.use_db else None
    if database_url:
        print_first_purchase_order_from_database(database_url)
    else:
        print_first_purchase_order(args.pos)

    if args.batch_dir:
        process_batch(args, database_url)
        return

    result = run_invoice_pipeline(
        invoice_path=args.invoice,
        vendors_path=args.vendors,
        purchase_orders_path=args.pos,
        processed_invoices_path=args.invoices,
        persist_processed=not args.no_persist_history,
        database_url=database_url,
    )

    decision_path = args.output_dir / "decision.json"
    audit_path = args.output_dir / "audit_report.json"
    execution_trace_path = args.output_dir / "execution_trace.json"
    decision_path.write_text(json.dumps(result.decision, indent=2), encoding="utf-8")
    audit_path.write_text(json.dumps(result.audit_report, indent=2), encoding="utf-8")
    execution_trace_path.write_text(json.dumps(result.execution_trace, indent=2), encoding="utf-8")

    print(f"Decision written to {decision_path}")
    print(f"Audit report written to {audit_path}")
    print(f"Execution trace written to {execution_trace_path}")
    print(f"Bill number: {result.decision.get('invoice_number') or 'missing'}")
    print(f"Final decision: {result.decision['decision']}")
    print("Reasons:")
    for reason in result.decision["reasons"]:
        print(f"- {reason}")
    print(f"Recorded in processed invoice history: {result.persisted_to_history}")


def process_batch(args: argparse.Namespace, database_url: str | None) -> None:
    invoice_paths = sorted(
        path
        for path in args.batch_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".json", ".pdf", *IMAGE_SUFFIXES}
    )
    summary = {
        "total_invoices": len(invoice_paths),
        "approved": 0,
        "partial_approved": 0,
        "manual_review": 0,
        "rejected": 0,
        "items": [],
    }
    for invoice_path in invoice_paths:
        result = run_invoice_pipeline(
            invoice_path=invoice_path,
            vendors_path=args.vendors,
            purchase_orders_path=args.pos,
            processed_invoices_path=args.invoices,
            persist_processed=not args.no_persist_history,
            database_url=database_url,
        )
        safe_name = invoice_path.stem.replace(" ", "_")
        decision_path = args.output_dir / f"{safe_name}_decision.json"
        audit_path = args.output_dir / f"{safe_name}_audit_report.json"
        execution_trace_path = args.output_dir / f"{safe_name}_execution_trace.json"
        decision_path.write_text(json.dumps(result.decision, indent=2), encoding="utf-8")
        audit_path.write_text(json.dumps(result.audit_report, indent=2), encoding="utf-8")
        execution_trace_path.write_text(json.dumps(result.execution_trace, indent=2), encoding="utf-8")

        decision = result.decision["decision"]
        if decision == "auto_approved":
            summary["approved"] += 1
        elif decision == "partial_approved":
            summary["partial_approved"] += 1
        elif decision == "rejected":
            summary["rejected"] += 1
        else:
            summary["manual_review"] += 1
        summary["items"].append(
            {
                "invoice": str(invoice_path),
                "bill_number": result.decision.get("invoice_number"),
                "decision": decision,
                "reasons": result.decision["reasons"],
                "persisted_to_history": result.persisted_to_history,
            }
        )

    batch_path = args.output_dir / "batch_summary.json"
    batch_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Batch summary written to {batch_path}")
    print(json.dumps(summary, indent=2))


def print_first_purchase_order(pos_path: Path) -> None:
    purchase_orders = json.loads(pos_path.read_text(encoding="utf-8"))
    first_po = purchase_orders[0] if purchase_orders else None
    print("Purchase Order #1:")
    print(json.dumps(first_po, indent=2))
    print()


def print_first_purchase_order_from_database(database_url: str) -> None:
    first_po = first_purchase_order_from_db(database_url)
    print("Purchase Order #1:")
    print(json.dumps(first_po, indent=2, default=str))
    print()


def build_database_url(args: argparse.Namespace) -> str:
    return str(args.db_path)


if __name__ == "__main__":
    main()
