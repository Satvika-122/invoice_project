from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st


ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
UI_OUTPUTS_DIR = OUTPUTS_DIR / "streamlit_runs"
UPLOADS_DIR = ROOT / ".streamlit_uploads"
SUPPORTED_EXTENSIONS = [".json", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"]
STAGE_ORDER = ["Ingest", "Extract", "Vendor/PO Match", "Rules & Tolerance", "Decision"]
STAGE_CHECKS = {
    "Ingest": {"data_source", "normalize_invoice", "completeness"},
    "Extract": {"OCR Confidence"},
    "Vendor/PO Match": {"vendor_approval", "duplicate", "reused_invoice_number", "po_match", "po_status"},
    "Rules & Tolerance": {
        "po_aggregate",
        "tolerance",
        "Total Calculation Validation",
        "Tax Validation",
        "Quantity Validation",
        "Unit Price Validation",
        "Goods Receipt Validation",
        "Approval Workflow",
    },
    "Decision": {"short_circuit"},
}


def apply_custom_css() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.6rem; }
        h1, h2, h3 { letter-spacing: 0; }
        .verdict {
            border-radius: 6px;
            padding: 0.85rem 1rem;
            margin: 0.75rem 0 1rem 0;
            font-weight: 700;
            border: 1px solid rgba(0,0,0,0.08);
        }
        .verdict-approved { background: #e8f5ee; color: #0f6b3f; }
        .verdict-review { background: #fff5db; color: #8a5a00; }
        .verdict-rejected { background: #fdeceb; color: #9f1d1d; }
        .reason-panel {
            border-left: 4px solid #d5dae3;
            padding: 0.6rem 0.9rem;
            margin-bottom: 0.75rem;
            background: #fafbfc;
        }
        .runtime-status {
            border: 1px solid #e5e7eb;
            border-radius: 6px;
            padding: 0.65rem 0.8rem;
            background: #fbfcfe;
            margin-top: 0.35rem;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid #e5e7eb;
            border-radius: 6px;
            padding: 0.25rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="AP Invoice Processing", layout="wide")
    apply_custom_css()
    st.title("AP Invoice Processing")
    submit_tab, dashboard_tab, batch_tab = st.tabs(["Submit & Live Run", "Dashboard", "Batch"])
    with submit_tab:
        render_submit_view()
    with dashboard_tab:
        render_dashboard()
    with batch_tab:
        render_batch_view()


def render_submit_view() -> None:
    st.subheader("Submit & Live Run")
    uploaded = st.file_uploader(
        "Upload invoice",
        type=[extension.removeprefix(".") for extension in SUPPORTED_EXTENSIONS],
    )
    left, right = st.columns(2)
    with left:
        use_db = st.checkbox("Use SQLite database", value=True)
        persist_history = st.checkbox("Persist approved invoice history", value=False)
    db_options: dict[str, Any] = {}
    with right:
        if use_db:
            db_options = render_db_options("submit_db_path")
    render_runtime_status(use_db, db_options)

    if st.button("Run", type="primary", disabled=uploaded is None):
        if uploaded is None:
            st.warning("Upload an invoice first.")
            return
        run_uploaded_invoice(uploaded, use_db, persist_history, db_options)


def render_db_options(key: str) -> dict[str, Any]:
    db_path = st.text_input(
        "SQLite DB path",
        value=str(ROOT / "data" / "invoice_processing.db"),
        key=key,
    )
    return {
        "path": db_path,
    }


def render_runtime_status(use_db: bool, db_options: dict[str, Any]) -> None:
    storage = f"SQLite database: `{db_options.get('path', ROOT / 'data' / 'invoice_processing.db')}`" if use_db else "JSON fallback files"
    st.markdown(
        f"""
        <div class="runtime-status">
        <strong>Storage mode</strong><br>{storage}<br><br>
        <strong>Gemini</strong><br>{gemini_status_text()}
        </div>
        """,
        unsafe_allow_html=True,
    )


def gemini_status_text() -> str:
    key_present = bool(os.environ.get("GEMINI_API_KEY") or streamlit_secret("GEMINI_API_KEY"))
    sdk_present = importlib.util.find_spec("google.generativeai") is not None if importlib.util.find_spec("google") else False
    if key_present and sdk_present:
        return "Enabled for image/PDF extraction. JSON invoices do not use Gemini."
    if key_present:
        return "Key is configured, but google-generativeai is not installed in this Python environment; OCR fallback will be used."
    if sdk_present:
        return "SDK is installed, but GEMINI_API_KEY is not configured; OCR fallback will be used."
    return "Not active; key and/or SDK are missing, so OCR fallback will be used."


def streamlit_secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:
        return None
    return str(value) if value else None


def subprocess_environment() -> dict[str, str]:
    env = os.environ.copy()
    if "GEMINI_API_KEY" not in env:
        key = streamlit_secret("GEMINI_API_KEY")
        if key:
            env["GEMINI_API_KEY"] = key
    return env


def run_uploaded_invoice(uploaded: Any, use_db: bool, persist_history: bool, db_options: dict[str, Any]) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    UI_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    input_path = UPLOADS_DIR / f"{run_id}_{safe_name(uploaded.name)}"
    output_dir = UI_OUTPUTS_DIR / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(uploaded.getbuffer())

    command = [
        sys.executable,
        "-m",
        "invoice_processing.cli",
        "--invoice",
        str(input_path),
        "--output-dir",
        str(output_dir),
    ]
    if not persist_history:
        command.append("--no-persist-history")
    if use_db:
        command.extend(["--use-db", "--db-path", str(db_options["path"])])

    st.caption("Running existing CLI: `python -m invoice_processing.cli`")
    stage_placeholders = {stage: st.empty() for stage in STAGE_ORDER}
    progress = st.progress(0)
    log_box = st.empty()
    final_box = st.container()

    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=subprocess_environment(),
    )

    started = time.monotonic()
    while process.poll() is None:
        elapsed = time.monotonic() - started
        render_live_stage_progress(stage_placeholders, progress, elapsed, output_dir)
        time.sleep(0.35)

    stdout, stderr = process.communicate()
    render_live_stage_progress(stage_placeholders, progress, 999, output_dir)
    filtered_stdout = filter_cli_output(stdout)
    if filtered_stdout:
        log_box.code(filtered_stdout, language="text")
    if process.returncode != 0:
        for stage in STAGE_ORDER:
            stage_placeholders[stage].error(f"Failed: {stage}")
        if stderr.strip():
            st.error("Pipeline failed.")
            st.code(stderr.strip())
        else:
            st.error("Pipeline failed without stderr output.")
        return

    decision = read_json(output_dir / "decision.json")
    execution_trace = read_json(output_dir / "execution_trace.json")
    render_completed_stage_progress(stage_placeholders, progress, decision, execution_trace)
    with final_box:
        render_decision(decision)
#        render_output_artifacts(output_dir)


#def render_output_artifacts(output_dir: Path) -> None:
#    for filename in ("decision.json", "audit_report.json", "execution_trace.json"):
#        path = output_dir / filename
#        with st.expander(filename, expanded=False):
#            payload = read_json(path)
 #           if payload is None:
#                st.warning(f"{filename} was not produced.")
#            else:
#                st.json(payload)


def filter_cli_output(stdout: str) -> str:
    hidden_prefixes = (
        "Decision written to ",
        "Audit report written to ",
        "Execution trace written to ",
    )
    visible_lines = [
        line
        for line in stdout.splitlines()
        if not any(line.startswith(prefix) for prefix in hidden_prefixes)
    ]
    return "\n".join(visible_lines).strip()


def render_live_stage_progress(placeholders: dict[str, Any], progress: Any, elapsed: float, output_dir: Path) -> None:
    trace_path = output_dir / "execution_trace.json"
    trace = read_json(trace_path) if trace_path.exists() else None
    if trace:
        decision = read_json(output_dir / "decision.json")
        render_completed_stage_progress(placeholders, progress, decision, trace)
        return

    active_index = min(int(elapsed // 0.7), len(STAGE_ORDER) - 1)
    for index, stage in enumerate(STAGE_ORDER):
        if index < active_index:
            placeholders[stage].success(f"Done: {stage}")
        elif index == active_index:
            placeholders[stage].info(f"Running: {stage}")
        else:
            placeholders[stage].write(f"Pending: {stage}")
    progress.progress(min((active_index + 1) / len(STAGE_ORDER), 0.95))


def render_completed_stage_progress(
    placeholders: dict[str, Any],
    progress: Any,
    decision: dict[str, Any] | None,
    execution_trace: dict[str, Any] | None,
) -> None:
    trace_items = trace_entries(execution_trace)
    checks = {str(item.get("check")) for item in trace_items}
    final_decision = (decision or {}).get("decision", "unknown")
    failed = final_decision == "rejected"
    for stage in STAGE_ORDER:
        summary = stage_summary(stage, checks, decision, trace_items)
        if failed and stage == "Decision":
            placeholders[stage].error(summary)
        else:
            placeholders[stage].success(summary)
    progress.progress(1.0)


def stage_summary(stage: str, checks: set[str], decision: dict[str, Any] | None, trace_items: list[dict[str, Any]]) -> str:
    if stage == "Decision":
        return f"Done: Decision -> {(decision or {}).get('decision', 'unknown')}"
    matched = sorted(STAGE_CHECKS[stage].intersection(checks))
    if matched:
        return f"Done: {stage} ({', '.join(matched[:3])}{'...' if len(matched) > 3 else ''})"
    if stage == "Extract":
        source = first_trace_output(trace_items, "normalize_invoice").get("dedupe_key")
        return "Done: Extract (structured JSON)" if source else "Done: Extract"
    return f"Done: {stage}"


def render_verdict_banner(verdict: str) -> None:
    normalized = verdict.lower()
    if normalized in {"auto_approved", "auto_approve", "approved", "partial_approved"}:
        css_class = "verdict-approved"
    elif normalized in {"rejected", "reject"}:
        css_class = "verdict-rejected"
    else:
        css_class = "verdict-review"
    st.markdown(
        f'<div class="verdict {css_class}">Final verdict: {verdict}</div>',
        unsafe_allow_html=True,
    )


def primary_reasons(decision: dict[str, Any]) -> list[str]:
    verdict = str(decision.get("decision", "")).lower()
    if verdict in {"auto_approved", "auto_approve", "approved"}:
        return ["Invoice approved; no blocking or review reasons were triggered."]

    triggered: list[str] = []
    for reason in decision.get("reasons", []) or []:
        add_unique(triggered, str(reason))

    for validation in decision.get("validations", []) or []:
        status = str(validation.get("status", "")).upper()
        if status in {"PASS", ""}:
            continue
        add_unique(triggered, f"{validation.get('rule')}: {validation.get('reason')}")

    return triggered or ["No specific failure reason was recorded; open Validation details for the full trace."]


def add_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in items:
        items.append(value)


def render_decision(decision: dict[str, Any] | None) -> None:
    if not decision:
        st.error("decision.json was not produced.")
        return
    verdict = str(decision.get("decision", "unknown"))
    render_verdict_banner(verdict)

    st.write(
        {
            "vendor": decision.get("vendor_id"),
            "bill_number": decision.get("invoice_number"),
            "po_id": decision.get("po_id"),
            "dedupe_key": decision.get("dedupe_key"),
        }
    )
    st.markdown("**Primary reasons**")
    st.markdown('<div class="reason-panel">', unsafe_allow_html=True)
    for reason in primary_reasons(decision):
        st.write(f"- {reason}")
    st.markdown("</div>", unsafe_allow_html=True)


def render_dashboard() -> None:
    st.subheader("Dashboard")
    use_db = st.checkbox("Load dashboard from SQLite", value=True, key="dashboard_use_db")
    records: list[dict[str, Any]]
    db_options: dict[str, Any] = {}
    if use_db:
        db_options = render_db_options("dashboard_db_path")
        if st.button("Load DB dashboard"):
            records = load_db_dashboard_records(db_options)
            st.session_state["dashboard_records"] = records
        records = st.session_state.get("dashboard_records", [])
    else:
        records = load_output_dashboard_records(OUTPUTS_DIR)

    history_tab, vendors_tab, pos_tab, lines_tab = st.tabs(["Run History", "Vendors", "Purchase Orders", "PO Line Items"])
    with history_tab:
        if not records:
            st.info("No runs found yet.")
        else:
            verdicts = sorted({record["verdict"] for record in records if record.get("verdict")})
            selected_verdicts = st.multiselect("Filter by verdict", verdicts, default=verdicts)
            filtered = [record for record in records if record.get("verdict") in selected_verdicts]
            render_records_table(filtered)
    with vendors_tab:
        render_reference_table(load_vendor_records(use_db, db_options))
    with pos_tab:
        render_reference_table(load_purchase_order_records(use_db, db_options))
    with lines_tab:
        line_items = load_po_line_item_records(use_db, db_options)
        po_ids = sorted({item.get("po_id") for item in line_items if item.get("po_id")})
        selected_po_ids = st.multiselect("Filter by PO", po_ids, default=po_ids)
        render_reference_table([item for item in line_items if not selected_po_ids or item.get("po_id") in selected_po_ids])


def render_records_table(records: list[dict[str, Any]]) -> None:
    if not records:
        st.info("No runs match the selected verdict filter.")
        return

    table_rows = [
        {
            "timestamp": record.get("timestamp"),
            "vendor": record.get("vendor"),
            "invoice number": record.get("invoice_number"),
            "PO id": record.get("po_id"),
            "amount": record.get("amount"),
            "verdict": record.get("verdict"),
            "stage_reached": record.get("stage_reached"),
        }
        for record in records
    ]
    selected_index = 0
    try:
        event = st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
        )
        selected_rows = event.selection.rows if event and event.selection else []
        if selected_rows:
            selected_index = min(selected_rows[0], len(records) - 1)
    except TypeError:
        st.dataframe(table_rows, use_container_width=True, hide_index=True)
        labels = [record_label(record) for record in records]
        selected_label = st.selectbox("Open run", labels)
        selected_index = labels.index(selected_label)

    selected_index = max(0, min(selected_index, len(records) - 1))
    selected = records[selected_index]
    with st.expander("Full reasoning trace", expanded=True):
        st.markdown("**Reasons**")
        for reason in primary_reasons(selected):
            st.write(f"- {reason}")
        st.markdown("**Checks run**")
        st.write(selected.get("checks_run", []))
        st.markdown("**Trace**")
        st.json(selected.get("trace", []))


def load_output_dashboard_records(outputs_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    decision_paths = sorted(outputs_dir.rglob("*decision.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for decision_path in decision_paths:
        decision = read_json(decision_path)
        if not decision:
            continue
        prefix = decision_path.name.removesuffix("_decision.json").removesuffix("decision.json")
        trace_candidates = [
            decision_path.with_name(decision_path.name.replace("decision.json", "execution_trace.json")),
            decision_path.with_name(f"{prefix}_execution_trace.json") if prefix else outputs_dir / "execution_trace.json",
        ]
        trace = next((read_json(path) for path in trace_candidates if path.exists()), None)
        records.append(record_from_decision(decision, trace, decision_path))
    return records


def load_db_dashboard_records(db_options: dict[str, Any]) -> list[dict[str, Any]]:
    db_path = Path(db_options.get("path") or ROOT / "data" / "invoice_processing.db")
    try:
        from invoice_processing.app import SQLiteDataStore

        SQLiteDataStore(db_path).first_purchase_order()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            rows = conn.execute(
                """
                SELECT
                    COALESCE(pr.finished_at, i.created_at) AS timestamp,
                    i.vendor_id,
                    i.invoice_number,
                    i.po_id,
                    CAST(i.total_amount AS TEXT) AS total_amount,
                    d.verdict,
                    d.reasons,
                    pr.checks_run
                FROM decisions d
                JOIN invoices i ON i.invoice_id = d.invoice_id
                LEFT JOIN processing_runs pr ON pr.run_id = d.run_id
                ORDER BY COALESCE(pr.finished_at, i.created_at) DESC
                LIMIT 100
                """
            ).fetchall()
    except Exception as exc:
        st.error(f"Could not load SQLite dashboard: {exc}")
        return []

    records = []
    for row in rows:
        row_dict = dict(row)
        checks = json_list(row_dict.get("checks_run"))
        records.append(
            {
                "timestamp": str(row_dict.get("timestamp") or ""),
                "vendor": row_dict.get("vendor_id"),
                "invoice_number": row_dict.get("invoice_number"),
                "po_id": row_dict.get("po_id"),
                "amount": str(row_dict.get("total_amount") or ""),
                "verdict": row_dict.get("verdict"),
                "stage_reached": checks[-1] if checks else "decision",
                "reasons": json_list(row_dict.get("reasons")),
                "checks_run": checks,
                "trace": [],
            }
        )
    return records


def load_vendor_records(use_db: bool, db_options: dict[str, Any]) -> list[dict[str, Any]]:
    if use_db:
        return query_sqlite_records(
            db_options,
            """
            SELECT vendor_id, name, tax_id,
                   CASE WHEN approved THEN 'approved' ELSE 'not approved' END AS approved
            FROM vendors
            ORDER BY vendor_id
            """,
        )
    return [
        {
            "vendor_id": vendor.get("vendor_id"),
            "name": vendor.get("name"),
            "tax_id": vendor.get("tax_id"),
            "approved": vendor.get("approved", vendor.get("approval_status")),
        }
        for vendor in read_json_list(ROOT / "data" / "vendors.json")
    ]


def load_purchase_order_records(use_db: bool, db_options: dict[str, Any]) -> list[dict[str, Any]]:
    if use_db:
        return query_sqlite_records(
            db_options,
            """
            SELECT po_id, vendor_id,
                   CAST(po_amount AS TEXT) AS po_amount,
                   CAST(matched_amount AS TEXT) AS matched_amount,
                   status
            FROM purchase_orders
            ORDER BY po_id
            """,
        )
    return [
        {
            "po_id": po.get("po_id") or po.get("po_number"),
            "vendor_id": po.get("vendor_id"),
            "po_amount": po.get("po_amount") or po.get("total_amount"),
            "matched_amount": po.get("matched_amount") or po.get("invoiced_amount"),
            "status": po.get("status"),
        }
        for po in read_json_list(ROOT / "data" / "pos.json")
    ]


def load_po_line_item_records(use_db: bool, db_options: dict[str, Any]) -> list[dict[str, Any]]:
    if use_db:
        return query_sqlite_records(
            db_options,
            """
            SELECT po_line_id, po_id, item_name,
                   CAST(ordered_quantity AS TEXT) AS ordered_quantity,
                   CAST(unit_price AS TEXT) AS unit_price
            FROM purchase_order_line_items
            ORDER BY po_id, po_line_id
            """,
        )

    records: list[dict[str, Any]] = []
    for po in read_json_list(ROOT / "data" / "purchase_orders.json"):
        po_number = po.get("po_id") or po.get("po_number")
        for line in po.get("line_items", []):
            records.append(
                {
                    "po_id": po_number,
                    "item_name": line.get("description") or line.get("item_name"),
                    "ordered_quantity": line.get("quantity") or line.get("ordered_quantity"),
                    "unit_price": line.get("unit_price"),
                }
            )
    return records


def query_sqlite_records(db_options: dict[str, Any], query: str) -> list[dict[str, Any]]:
    db_path = Path(db_options.get("path") or ROOT / "data" / "invoice_processing.db")
    try:
        from invoice_processing.app import SQLiteDataStore

        SQLiteDataStore(db_path).first_purchase_order()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return [dict(row) for row in conn.execute(query).fetchall()]
    except Exception as exc:
        st.error(f"Could not load SQLite records: {exc}")
        return []


def render_reference_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.info("No records found.")
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)


def render_batch_view() -> None:
    st.subheader("Batch")
    uploads = st.file_uploader(
        "Upload batch invoices",
        type=[extension.removeprefix(".") for extension in SUPPORTED_EXTENSIONS],
        accept_multiple_files=True,
        key="batch_uploads",
    )
    left, right = st.columns(2)
    with left:
        use_db = st.checkbox("Use SQLite database", value=True, key="batch_use_db")
        persist_history = st.checkbox("Persist approved invoice history", value=False, key="batch_persist_history")
    db_options: dict[str, Any] = {}
    with right:
        if use_db:
            db_options = render_db_options("batch_db_path")

    if st.button("Run batch", type="primary", disabled=not uploads):
        run_uploaded_batch(uploads, use_db, persist_history, db_options)
        return

    batch_path = Path(st.session_state.get("last_batch_summary_path") or OUTPUTS_DIR / "batch_summary.json")
    summary = read_json(batch_path)
    if not summary:
        st.info("No batch_summary.json found.")
        return
    render_batch_summary(summary)


def run_uploaded_batch(uploads: list[Any], use_db: bool, persist_history: bool, db_options: dict[str, Any]) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    UI_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("batch_%Y%m%d_%H%M%S_%f")
    batch_dir = UPLOADS_DIR / run_id
    output_dir = UI_OUTPUTS_DIR / run_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for upload in uploads:
        (batch_dir / safe_name(upload.name)).write_bytes(upload.getbuffer())

    command = [
        sys.executable,
        "-m",
        "invoice_processing.cli",
        "--batch-dir",
        str(batch_dir),
        "--output-dir",
        str(output_dir),
    ]
    if not persist_history:
        command.append("--no-persist-history")
    if use_db:
        command.extend(["--use-db", "--db-path", str(db_options["path"])])

    with st.spinner("Running batch through existing CLI..."):
        process = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=subprocess_environment(),
        )

    if process.returncode != 0:
        st.error("Batch processing failed.")
        if process.stderr.strip():
            st.code(process.stderr.strip(), language="text")
        return

    summary_path = output_dir / "batch_summary.json"
    st.session_state["last_batch_summary_path"] = str(summary_path)
    summary = read_json(summary_path)
    if summary:
        render_batch_summary(summary)
    if process.stdout.strip():
        st.code(filter_cli_output(process.stdout), language="text")


def render_batch_summary(summary: dict[str, Any]) -> None:
    cols = st.columns(5)
    cols[0].metric("Total", summary.get("total_invoices", 0))
    cols[1].metric("Approved", summary.get("approved", 0))
    cols[2].metric("Partial", summary.get("partial_approved", 0))
    cols[3].metric("Manual review", summary.get("manual_review", 0))
    cols[4].metric("Rejected", summary.get("rejected", 0))
    st.dataframe(summary.get("items", []), use_container_width=True)


def record_from_decision(decision: dict[str, Any], trace: dict[str, Any] | None, path: Path) -> dict[str, Any]:
    trace_items = trace_entries(trace)
    checks_run = decision.get("checks_run") or []
    timestamp = path.stat().st_mtime
    return {
        "timestamp": datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
        "vendor": decision.get("vendor_id"),
        "invoice_number": decision.get("invoice_number"),
        "po_id": decision.get("po_id"),
        "amount": amount_from_decision(decision),
        "verdict": decision.get("decision"),
        "stage_reached": checks_run[-1] if checks_run else (trace_items[-1].get("check") if trace_items else ""),
        "reasons": decision.get("reasons", []),
        "checks_run": checks_run,
        "trace": trace_items,
        "path": str(path),
    }


def amount_from_decision(decision: dict[str, Any]) -> str:
    for validation in decision.get("validations", []):
        details = validation.get("details", {})
        if "actual_total" in details:
            return str(details["actual_total"])
        if "invoice_amount" in details:
            return str(details["invoice_amount"])
    return ""


def first_trace_output(trace_items: list[dict[str, Any]], check: str) -> dict[str, Any]:
    for item in trace_items:
        if item.get("check") == check and isinstance(item.get("output"), dict):
            return item["output"]
    return {}


def trace_entries(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not trace:
        return []
    entries = trace.get("execution_trace", trace)
    return entries if isinstance(entries, list) else []


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def safe_name(name: str) -> str:
    return "".join(character if character.isalnum() or character in ".-_" else "_" for character in name)


def record_label(record: dict[str, Any]) -> str:
    return f"{record.get('timestamp')} | {record.get('invoice_number')} | {record.get('verdict')}"


if __name__ == "__main__":
    main()
