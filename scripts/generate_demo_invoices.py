from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


OUT_DIR = Path("data/demo_invoices")
TAX_RATE = Decimal("0.18")


@dataclass(frozen=True)
class Line:
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_amount: Decimal


@dataclass(frozen=True)
class InvoiceFixture:
    slug: str
    scenario: str
    bill_number: str | None
    invoice_date: str
    vendor_id: str
    vendor_name: str
    po_id: str
    lines: tuple[Line, ...]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    image: bool = False


def money(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def tax_for(subtotal: Decimal) -> Decimal:
    return money(subtotal * TAX_RATE)


def fixture(
    slug: str,
    scenario: str,
    bill_number: str | None,
    invoice_date: str,
    vendor_id: str,
    vendor_name: str,
    po_id: str,
    description: str,
    quantity: str,
    unit_price: str,
    subtotal: str | None = None,
    tax: str | None = None,
    total: str | None = None,
    image: bool = False,
) -> InvoiceFixture:
    qty = Decimal(quantity)
    price = money(unit_price)
    line_amount = money(subtotal) if subtotal is not None else money(qty * price)
    sub = line_amount
    tax_amount = money(tax) if tax is not None else tax_for(sub)
    total_amount = money(total) if total is not None else money(sub + tax_amount)
    return InvoiceFixture(
        slug=slug,
        scenario=scenario,
        bill_number=bill_number,
        invoice_date=invoice_date,
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        po_id=po_id,
        lines=(Line(description, qty, price, line_amount),),
        subtotal=sub,
        tax=tax_amount,
        total=total_amount,
        image=image,
    )


def all_fixtures() -> list[InvoiceFixture]:
    return [
        fixture("mobile_01_clean_full_quantity", "Group A - Mobile Phone clean full quantity", "DEMO-MOB-2026-001", "2026-06-10", "VEND-1001", "Acme Co", "PO-9001", "Mobile Phone", "20", "10000.00"),
        fixture("mobile_02_second_clean_same_po", "Group A - Second legitimate Mobile Phone invoice, different bill number", "DEMO-MOB-2026-002", "2026-06-11", "VEND-1001", "Acme Co", "PO-9001", "Mobile Phone", "20", "10000.00"),
        fixture("mobile_03_partial_quantity_15", "Group A - Mobile Phone partial quantity 15 of 20", "DEMO-MOB-2026-003", "2026-06-12", "VEND-1001", "Acme Co", "PO-9001", "Mobile Phone", "15", "10000.00"),
        fixture("laptop_01_happy_path_full_po_match", "Group B01 - Laptop control, full PO quantity", "DEMO-LAP-2026-001", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "10", "45000.00"),
        fixture("laptop_02_duplicate_same_as_existing", "Group B02 - Duplicate candidate, same bill/date/amount as seeded invoice", "DEMO-LAP-DUP-2026-001", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "10", "45000.00"),
        fixture("laptop_03_vendor_name_mismatch", "Group B03 - Vendor name mismatch", "DEMO-LAP-2026-003", "2026-06-27", "VEND-1001", "Ace Co", "PO-9001", "Laptop", "10", "45000.00", image=True),
        fixture("laptop_04_missing_invoice_number", "Group B04 - Missing invoice number", None, "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "10", "45000.00"),
        fixture("laptop_05_po_not_found", "Group B05 - PO not found", "DEMO-LAP-2026-005", "2026-06-27", "VEND-1001", "Acme Co", "PO-9999", "Laptop", "10", "45000.00"),
        fixture("laptop_06_quantity_over_ordered", "Group B06 - Quantity over ordered", "DEMO-LAP-2026-006", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "11", "45000.00"),
        fixture("laptop_07_quantity_less_than_received", "Group B07 - Quantity less than received", "DEMO-LAP-2026-007", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "8", "45000.00"),
        fixture("laptop_08_unit_price_mismatch", "Group B08 - Unit price mismatch", "DEMO-LAP-2026-008", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "10", "46000.00", image=True),
        fixture("laptop_09_tax_total_mismatch", "Group B09 - Tax/total mismatch", "DEMO-LAP-2026-009", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "10", "45000.00", tax="81000.00", total="500000.00"),
        fixture("laptop_10_po_amount_over_tolerance", "Group B10 - PO amount over tolerance", "DEMO-LAP-2026-010", "2026-06-27", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "20", "45000.00"),
        fixture("laptop_11_reused_invoice_number_different_amount", "Group B11 - Reused invoice number with different amount", "DEMO-LAP-DUP-2026-001", "2026-06-28", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "9", "45000.00"),
        fixture("laptop_12_unapproved_vendor", "Group B12 - Unapproved vendor", "DEMO-LAP-2026-012", "2026-06-27", "VEND-1004", "Orbit LLC", "PO-9001", "Laptop", "10", "45000.00"),
        fixture("split_po_invoice1_of_2", "Group C - Split PO invoice 1 of 2, six laptops", "DEMO-SPLIT-2026-001", "2026-06-29", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "6", "45000.00"),
        fixture("split_po_invoice2_of_2", "Group C - Split PO invoice 2 of 2, remainder plus small within-tolerance freight embedded", "DEMO-SPLIT-2026-002", "2026-06-30", "VEND-1001", "Acme Co", "PO-9001", "Laptop", "4", "45000.00", subtotal="195000.00"),
    ]


def as_json_payload(item: InvoiceFixture) -> dict[str, Any]:
    return {
        "vendor_bill_no": item.bill_number,
        "invoice_number": item.bill_number,
        "invoice_date": item.invoice_date,
        "vendor_id": item.vendor_id,
        "vendor_name": item.vendor_name,
        "po_id": item.po_id,
        "subtotal": str(item.subtotal),
        "tax": str(item.tax),
        "total": str(item.total),
        "currency": "INR",
        "source_file": f"{item.slug}.pdf",
        "line_items": [
            {
                "description": line.description,
                "quantity": str(line.quantity.normalize()),
                "unit_price": str(line.unit_price),
                "line_amount": str(line.line_amount),
                "tax_amount": None,
            }
            for line in item.lines
        ],
    }


def invoice_text_lines(item: InvoiceFixture) -> list[str]:
    lines = [
        "TAX INVOICE",
        f"Vendor: {item.vendor_name}",
        f"Vendor ID: {item.vendor_id}",
    ]
    if item.bill_number:
        lines.append(f"Bill No: {item.bill_number}")
        lines.append(f"Invoice Number: {item.bill_number}")
    lines.extend(
        [
            f"Date: {item.invoice_date}",
            f"PO: {item.po_id}",
            "Currency: INR",
            "",
            "Description Quantity Unit Price Amount",
        ]
    )
    for line in item.lines:
        lines.append(f"{line.description} {line.quantity.normalize()} {line.unit_price} {line.line_amount}")
    lines.extend(
        [
            "",
            f"Subtotal: INR {item.subtotal}",
            f"GST: INR {item.tax}",
            f"Total: INR {item.total}",
        ]
    )
    return lines


def write_pdf(item: InvoiceFixture, path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(0.75 * inch, height - 0.75 * inch, "Tax Invoice")
    c.setFont("Helvetica", 10)
    c.drawRightString(width - 0.75 * inch, height - 0.75 * inch, item.scenario)
    y = height - 1.25 * inch
    for text in invoice_text_lines(item)[1:]:
        if text == "":
            y -= 12
            continue
        c.setFont("Helvetica-Bold" if text.startswith(("Vendor:", "Bill No:", "Invoice Number:", "PO:", "Subtotal:", "GST:", "Total:")) else "Helvetica", 11)
        c.drawString(0.75 * inch, y, text)
        y -= 18
    c.setStrokeColor(colors.HexColor("#d1d5db"))
    c.line(0.75 * inch, 0.65 * inch, width - 0.75 * inch, 0.65 * inch)
    c.setFont("Helvetica", 8)
    c.drawString(0.75 * inch, 0.45 * inch, "Demo fixture generated for AP invoice processing.")
    c.save()


def write_image(item: InvoiceFixture, path: Path) -> None:
    image = Image.new("RGB", (1300, 1700), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("arial.ttf", 46)
        normal_font = ImageFont.truetype("arial.ttf", 32)
        small_font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        title_font = ImageFont.load_default()
        normal_font = ImageFont.load_default()
        small_font = ImageFont.load_default()

    y = 80
    draw.text((80, y), "Tax Invoice", fill=(17, 24, 39), font=title_font)
    y += 85
    for text in invoice_text_lines(item)[1:]:
        if text == "":
            y += 24
            continue
        draw.text((80, y), text, fill=(17, 24, 39), font=normal_font)
        y += 46
    draw.text((80, 1600), "Scanned-style demo fixture", fill=(107, 114, 128), font=small_font)
    image.save(path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    for item in all_fixtures():
        pdf_path = OUT_DIR / f"{item.slug}.pdf"
        json_path = OUT_DIR / f"{item.slug}.json"
        write_pdf(item, pdf_path)
        json_path.write_text(json.dumps(as_json_payload(item), indent=2), encoding="utf-8")
        outputs = [str(pdf_path), str(json_path)]
        if item.image:
            image_path = OUT_DIR / f"{item.slug}.png"
            write_image(item, image_path)
            outputs.append(str(image_path))
        manifest.append(
            {
                "slug": item.slug,
                "scenario": item.scenario,
                "bill_number": item.bill_number,
                "files": outputs,
            }
        )
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Generated {len(manifest)} invoice fixtures under {OUT_DIR}")


if __name__ == "__main__":
    main()
