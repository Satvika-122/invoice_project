from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


OUTPUT_DIR = Path("data/test_inputs")


INVOICE_LINES = [
    ("Invoice Number:", "INV-PDF-2026-002"),
    ("Date:", "2026-06-27"),
    ("Vendor:", "Acme Co"),
    ("Vendor ID:", "VEND-1001"),
    ("Ref:", "PO-9001"),
    ("Currency:", "INR"),
]


def generate_digital_pdf(path: Path) -> None:
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        pageCompression=0,
    )
    story = [
        Paragraph("Tax Invoice", styles["Title"]),
        Spacer(1, 6 * mm),
        Paragraph("Acme Co", styles["Heading2"]),
        Paragraph("Supplier invoice for AP automation testing", styles["Normal"]),
        Spacer(1, 6 * mm),
    ]

    header_table = Table(INVOICE_LINES, colWidths=[42 * mm, 90 * mm])
    header_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 8 * mm))

    line_table = Table(
        [
            ["Description", "Quantity", "Unit Price", "Line Amount"],
            ["Laptop", "1", "45000.00", "45000.00"],
        ],
        colWidths=[70 * mm, 28 * mm, 35 * mm, 38 * mm],
    )
    line_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9eaf7")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("PADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    story.append(line_table)
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("Subtotal: INR 45000.00", styles["Normal"]))
    story.append(Paragraph("GST: INR 8100.00", styles["Normal"]))
    story.append(Paragraph("Total: INR 53100.00", styles["Heading2"]))
    document.build(story)


def generate_invoice_image(path: Path) -> None:
    image = Image.new("RGB", (1600, 2100), "white")
    draw = ImageDraw.Draw(image)
    title_font = load_font(78)
    heading_font = load_font(52)
    body_font = load_font(42)
    small_font = load_font(38)

    x = 110
    y = 90
    draw.text((x, y), "Tax Invoice", fill="black", font=title_font)
    y += 105
    draw.text((x, y), "Acme Co", fill="black", font=heading_font)
    y += 70
    draw.text((x, y), "Supplier invoice for OCR testing", fill="black", font=body_font)
    y += 100

    for label, value in INVOICE_LINES:
        draw.text((x, y), label, fill="black", font=body_font)
        draw.text((x + 430, y), value, fill="black", font=body_font)
        y += 68

    y += 55
    table_right = 1490
    draw.rectangle((x, y, table_right, y + 84), outline="black", width=3, fill=(230, 241, 249))
    draw.text((x + 24, y + 22), "Description", fill="black", font=small_font)
    draw.text((x + 700, y + 22), "Quantity", fill="black", font=small_font)
    draw.text((x + 930, y + 22), "Unit Price", fill="black", font=small_font)
    draw.text((x + 1230, y + 22), "Amount", fill="black", font=small_font)
    y += 84
    draw.rectangle((x, y, table_right, y + 84), outline="black", width=3)
    draw.text((x + 24, y + 22), "Laptop", fill="black", font=small_font)
    draw.text((x + 750, y + 22), "1", fill="black", font=small_font)
    draw.text((x + 955, y + 22), "45000.00", fill="black", font=small_font)
    draw.text((x + 1240, y + 22), "45000.00", fill="black", font=small_font)
    y += 125

    draw.text((x + 790, y), "Subtotal: INR 45000.00", fill="black", font=body_font)
    y += 68
    draw.text((x + 790, y), "GST: INR 8100.00", fill="black", font=body_font)
    y += 78
    draw.text((x + 790, y), "Total: INR 53100.00", fill="black", font=heading_font)
    image.save(path)


def load_font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "calibri.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    digital_pdf = OUTPUT_DIR / "sample_digital_invoice.pdf"
    image_path = OUTPUT_DIR / "sample_scanned_invoice.png"
    scanned_pdf = OUTPUT_DIR / "sample_scanned_invoice.pdf"

    generate_digital_pdf(digital_pdf)
    generate_invoice_image(image_path)
    Image.open(image_path).save(scanned_pdf, "PDF", resolution=200)

    print(f"Generated {digital_pdf}")
    print(f"Generated {image_path}")
    print(f"Generated {scanned_pdf}")


if __name__ == "__main__":
    main()
