# Invoice Processing Pipeline

PS-1 invoice processing implementation focused on the documented decision path:
extract or read an invoice, normalize it, match it to vendors and purchase
orders, apply duplicate/tolerance/completeness checks, and return a decision
with human-readable reasons.

## Run

```bash
python -m invoice_processing.cli
```

Outputs are written to:

- `outputs/decision.json`
- `outputs/audit_report.json`
- `outputs/execution_trace.json`

## Current Pipeline

The active pipeline lives in `invoice_processing/app.py`.

1. Extract/read invoice input.
2. Normalize invoice and build corrected `dedupe_key`.
3. Run missing critical field check.
4. Short-circuit terminal failures.
5. Run exact duplicate check.
6. Run reused invoice number check.
7. Run vendor approval check.
8. Match PO by exact `po_id`, then fuzzy vendor + amount + date window.
9. Aggregate matched amount for split invoices.
10. Run tolerance check using `config/rules_config.json`.
11. Return `decision`, `reasons[]`, and `checks_run[]`.

## Extraction Note

`invoice_processing/extraction.py` is the upstream PDF extraction stage. When
`pdfplumber` is installed it is used for text-based PDFs. If a PDF has no
extractable text, the file is routed to an OCR/vision fallback path.

For this local implementation, the scanned-PDF fallback returns an explicit-null
normalized object with raw metadata rather than guessing values. In production,
that fallback can be replaced with `pytesseract`, Claude Vision, Gemini Vision,
or another approved OCR provider without changing the downstream decision
pipeline.

## Database Artifacts

`db/schema.sql` contains the original PostgreSQL reference schema.

`db/schema_sqlite.sql` contains the active local SQLite schema. When `--use-db`
is passed, `data/invoice_processing.db` is created automatically on first run
from this schema, so no external database setup is required.

Duplicate detection recomputes dedupe keys using:

```text
vendor_id | invoice_number | amount | invoice_date
```
