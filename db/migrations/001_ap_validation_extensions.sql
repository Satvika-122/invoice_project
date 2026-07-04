CREATE TABLE IF NOT EXISTS purchase_order_line_items (
    po_line_id        TEXT PRIMARY KEY,
    po_id             TEXT NOT NULL REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    item_name         TEXT NOT NULL,
    ordered_quantity NUMERIC(12,2) NOT NULL CHECK (ordered_quantity >= 0),
    unit_price        NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0)
);
CREATE INDEX IF NOT EXISTS idx_po_line_items_po ON purchase_order_line_items(po_id);

INSERT INTO purchase_order_line_items (po_line_id, po_id, item_name, ordered_quantity, unit_price) VALUES
('PO-9001-1', 'PO-9001', 'Laptop', 10.00, 45000.00),
('PO-9001-2', 'PO-9001', 'Mobile Phone', 20.00, 10000.00)
ON CONFLICT (po_line_id) DO UPDATE SET
    po_id = EXCLUDED.po_id,
    item_name = EXCLUDED.item_name,
    ordered_quantity = EXCLUDED.ordered_quantity,
    unit_price = EXCLUDED.unit_price;

CREATE TABLE IF NOT EXISTS goods_receipts (
    receipt_id        TEXT PRIMARY KEY,
    po_id             TEXT NOT NULL REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    item              TEXT NOT NULL,
    quantity_received NUMERIC(12,2) NOT NULL CHECK (quantity_received >= 0),
    receipt_date      DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goods_receipts_po ON goods_receipts(po_id);

INSERT INTO goods_receipts (receipt_id, po_id, item, quantity_received, receipt_date) VALUES
('GR-7001', 'PO-9001', 'Laptop', 10.00, '2026-06-20'),
('GR-7002', 'PO-9001', 'Mobile Phone', 20.00, '2026-06-09')
ON CONFLICT (receipt_id) DO UPDATE SET
    po_id = EXCLUDED.po_id,
    item = EXCLUDED.item,
    quantity_received = EXCLUDED.quantity_received,
    receipt_date = EXCLUDED.receipt_date;

CREATE TABLE IF NOT EXISTS approval_thresholds (
    threshold_id BIGSERIAL PRIMARY KEY,
    min_amount   NUMERIC(14,2) NOT NULL CHECK (min_amount >= 0),
    max_amount   NUMERIC(14,2) NOT NULL CHECK (max_amount >= min_amount),
    role         TEXT NOT NULL
);

TRUNCATE TABLE approval_thresholds;
INSERT INTO approval_thresholds (min_amount, max_amount, role) VALUES
(0.00, 49999.99, 'AP Automation'),
(50000.00, 500000.00, 'Finance Manager'),
(500000.01, 999999999.00, 'Senior Finance Approval');

INSERT INTO rules_config (key, value) VALUES
  ('tax_tolerance_abs', '5.00'),
  ('total_tolerance_abs', '5.00')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
