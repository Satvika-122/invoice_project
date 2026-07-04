PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS vendors (
    vendor_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    approved        INTEGER NOT NULL DEFAULT 0,
    tax_id          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO vendors (vendor_id, name, approved, tax_id, created_at) VALUES
('VEND-1001', 'Acme Co',      1, 'US-11-1111111', '2025-10-01T09:00:00Z'),
('VEND-1002', 'Nova Ltd',     1, 'GB-222222222',  '2025-10-05T10:15:00Z'),
('VEND-1003', 'Zeta Inc',     1, 'US-33-3333333', '2025-10-12T11:30:00Z'),
('VEND-1004', 'Orbit LLC',    0, 'US-44-4444444', '2025-11-02T13:45:00Z'),
('VEND-1005', 'Flux GmbH',    1, 'DE-555555555',  '2025-11-18T08:20:00Z'),
('VEND-1006', 'Sable Pvt',    1, 'IN-666666666',  '2025-12-04T15:10:00Z'),
('VEND-1007', 'Halo Corp',    0, 'US-77-7777777', '2026-01-09T09:55:00Z'),
('VEND-1008', 'Ridge Co',     1, 'GB-888888888',  '2026-01-22T12:40:00Z'),
('VEND-1009', 'Pivot Inc',    1, 'US-99-9999999', '2026-02-14T14:05:00Z'),
('VEND-1010', 'Marsh LLC',    0, 'US-10-1010101', '2026-03-01T10:30:00Z')
ON CONFLICT (vendor_id) DO UPDATE SET
    name = excluded.name,
    approved = excluded.approved,
    tax_id = excluded.tax_id,
    created_at = excluded.created_at;

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id           TEXT PRIMARY KEY,
    vendor_id       TEXT NOT NULL REFERENCES vendors(vendor_id),
    po_amount       NUMERIC_TEXT_14_2 NOT NULL CHECK (CAST(po_amount AS NUMERIC) >= 0),
    matched_amount  NUMERIC_TEXT_14_2 NOT NULL DEFAULT '0.00' CHECK (CAST(matched_amount AS NUMERIC) >= 0),
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','partially_matched','closed','fully_matched')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders(vendor_id);

INSERT INTO purchase_orders (po_id, vendor_id, po_amount, matched_amount, status, created_at) VALUES
('PO-9001', 'VEND-1001', '767000.00', '0.00', 'open', '2026-06-01T00:00:00Z'),
('PO-9002', 'VEND-1001',   '5000.00', '0.00', 'open', '2026-06-15T00:00:00Z'),
('PO-9003', 'VEND-1002',  '25000.00', '0.00', 'open', '2026-06-20T09:30:00Z'),
('PO-9004', 'VEND-1003',  '12500.00', '0.00', 'open', '2026-06-22T11:00:00Z'),
('PO-9005', 'VEND-1005',  '80000.00', '0.00', 'open', '2026-06-25T13:45:00Z'),
('PO-9006', 'VEND-1006', '150000.00', '0.00', 'open', '2026-06-28T10:10:00Z'),
('PO-9007', 'VEND-1008',  '42000.00', '0.00', 'open', '2026-07-01T08:00:00Z'),
('PO-9008', 'VEND-1009',  '99000.00', '0.00', 'open', '2026-07-03T12:00:00Z')
ON CONFLICT (po_id) DO UPDATE SET
    vendor_id = excluded.vendor_id,
    po_amount = excluded.po_amount,
    matched_amount = excluded.matched_amount,
    status = excluded.status,
    created_at = excluded.created_at;

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id      TEXT PRIMARY KEY,
    vendor_id       TEXT NOT NULL REFERENCES vendors(vendor_id),
    po_id           TEXT REFERENCES purchase_orders(po_id),
    invoice_number  TEXT,
    invoice_date    TEXT,
    total_amount    NUMERIC_TEXT_14_2,
    dedupe_key      TEXT NOT NULL,
    raw_extraction  TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_id);
CREATE INDEX IF NOT EXISTS idx_invoices_po ON invoices(po_id);
CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(vendor_id, invoice_number);

CREATE TABLE IF NOT EXISTS invoice_line_items (
    line_item_id    TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    description     TEXT,
    quantity        NUMERIC_TEXT_12_2,
    unit_price      NUMERIC_TEXT_14_2,
    line_amount     NUMERIC_TEXT_14_2,
    tax_amount      NUMERIC_TEXT_14_2
);
CREATE INDEX IF NOT EXISTS idx_line_items_invoice ON invoice_line_items(invoice_id);

CREATE TABLE IF NOT EXISTS purchase_order_line_items (
    po_line_id        TEXT PRIMARY KEY,
    po_id             TEXT NOT NULL REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    item_name         TEXT NOT NULL,
    ordered_quantity NUMERIC_TEXT_12_2 NOT NULL CHECK (CAST(ordered_quantity AS NUMERIC) >= 0),
    unit_price        NUMERIC_TEXT_14_2 NOT NULL CHECK (CAST(unit_price AS NUMERIC) >= 0)
);
CREATE INDEX IF NOT EXISTS idx_po_line_items_po ON purchase_order_line_items(po_id);

INSERT INTO purchase_order_line_items (po_line_id, po_id, item_name, ordered_quantity, unit_price) VALUES
('PO-9001-1', 'PO-9001', 'Laptop', '10.00', '45000.00'),
('PO-9001-2', 'PO-9001', 'Mobile Phone', '20.00', '10000.00')
ON CONFLICT (po_line_id) DO UPDATE SET
    po_id = excluded.po_id,
    item_name = excluded.item_name,
    ordered_quantity = excluded.ordered_quantity,
    unit_price = excluded.unit_price;

CREATE TABLE IF NOT EXISTS goods_receipts (
    receipt_id        TEXT PRIMARY KEY,
    po_id             TEXT NOT NULL REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    item              TEXT NOT NULL,
    quantity_received NUMERIC_TEXT_12_2 NOT NULL CHECK (CAST(quantity_received AS NUMERIC) >= 0),
    receipt_date      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goods_receipts_po ON goods_receipts(po_id);

INSERT INTO goods_receipts (receipt_id, po_id, item, quantity_received, receipt_date) VALUES
('GR-7001', 'PO-9001', 'Laptop', '10.00', '2026-06-20'),
('GR-7002', 'PO-9001', 'Mobile Phone', '20.00', '2026-06-09')
ON CONFLICT (receipt_id) DO UPDATE SET
    po_id = excluded.po_id,
    item = excluded.item,
    quantity_received = excluded.quantity_received,
    receipt_date = excluded.receipt_date;

CREATE TABLE IF NOT EXISTS processing_runs (
    run_id          TEXT PRIMARY KEY,
    invoice_id      TEXT REFERENCES invoices(invoice_id),
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','completed','failed')),
    checks_run      TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES processing_runs(run_id),
    invoice_id      TEXT NOT NULL REFERENCES invoices(invoice_id),
    verdict         TEXT NOT NULL
                    CHECK (verdict IN ('auto_approved','needs_review','rejected')),
    reasons         TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_invoice ON decisions(invoice_id);

CREATE TABLE IF NOT EXISTS rules_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO rules_config (key, value) VALUES
  ('tolerance_pct', '0.02'),
  ('tolerance_abs', '5.00'),
  ('fuzzy_match_amount_window_pct', '0.03'),
  ('fuzzy_match_date_window_days', '7'),
  ('tax_tolerance_abs', '5.00'),
  ('total_tolerance_abs', '5.00')
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now');

CREATE TABLE IF NOT EXISTS approval_thresholds (
    threshold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    min_amount   NUMERIC_TEXT_14_2 NOT NULL CHECK (CAST(min_amount AS NUMERIC) >= 0),
    max_amount   NUMERIC_TEXT_14_2 NOT NULL CHECK (CAST(max_amount AS NUMERIC) >= CAST(min_amount AS NUMERIC)),
    role         TEXT NOT NULL
);

DELETE FROM approval_thresholds;
INSERT INTO approval_thresholds (min_amount, max_amount, role) VALUES
('0.00', '49999.99', 'AP Automation'),
('50000.00', '500000.00', 'Finance Manager'),
('500000.01', '999999999.00', 'Senior Finance Approval');
