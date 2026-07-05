from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
GLM_OCR_MODEL = "zai-org/GLM-OCR"

NORMALIZED_KEYS = (
    "vendor_name",
    "vendor_id",
    "vendor_bill_no",
    "invoice_number",
    "invoice_date",
    "line_items",
    "subtotal",
    "tax_amount",
    "total_amount",
    "po_reference",
)


def extract_text_from_document(file_path: Path) -> dict[str, Any]:
    """Extract text from a document and report method plus confidence.

    Digital PDFs are handled with PyMuPDF when available. Scanned PDFs fall
    back to OCR if pytesseract/Pillow are installed. If OCR tooling is not
    available, the function returns explicit low-confidence output instead of
    fabricating invoice data.
    """
    suffix = file_path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        gemini_result = extract_text_with_gemini(file_path)
        if gemini_result.get("structured") or gemini_result["text"].strip():
            return gemini_result
        ocr_result = extract_text_from_image(file_path)
        if ocr_result["text"].strip():
            return ocr_result
        hf_result = extract_text_with_glm_ocr(file_path)
        if hf_result["text"].strip():
            return hf_result
        return {"text": "", "method": "image_ocr_unavailable", "confidence": 0.0, "note": hf_result.get("error", ocr_result.get("error", "No image OCR provider available."))}

    if suffix != ".pdf":
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        return {"text": text, "method": "text_file", "confidence": 0.95 if text.strip() else 0.0}

    digital_text = extract_text_with_pymupdf(file_path)
    if enough_embedded_text(digital_text):
        return {"text": digital_text, "method": "digital_pdf", "confidence": 0.98}

    gemini_result = extract_text_with_gemini(file_path)
    if gemini_result.get("structured") or gemini_result["text"].strip():
        return gemini_result

    ocr_result = extract_text_with_ocr(file_path)
    if ocr_result["text"].strip():
        return ocr_result

    hf_result = extract_text_with_glm_ocr(file_path)
    if hf_result["text"].strip():
        return hf_result

    fallback_text = extract_text_pdf(file_path)
    if fallback_text.strip():
        return {"text": fallback_text, "method": "pdf_byte_text_fallback", "confidence": 0.80}

    return {
        "text": "",
        "method": "ocr_unavailable",
        "confidence": 0.0,
        "note": "No embedded PDF text found and OCR dependencies are unavailable or produced no text.",
    }


def extract_invoice_from_pdf(pdf_path: Path) -> dict[str, Any]:
    """Extract normalized invoice fields from a text or scanned PDF.

    Text PDFs use pdfplumber when installed. If no text can be extracted, the
    document is routed through a scanned/OCR fallback path that returns explicit
    nulls and raw metadata instead of failing or fabricating values.
    """
    extraction = extract_text_from_document(pdf_path)
    if isinstance(extraction.get("structured"), dict):
        normalized = ensure_explicit_nulls(extraction["structured"])
        normalized["extraction_method"] = extraction["method"]
        normalized["ocr_confidence"] = extraction["confidence"]
        normalized["raw_extraction"] = extraction
        return normalized

    raw_text = extraction["text"]
    if raw_text.strip():
        normalized = parse_invoice_text(raw_text)
        normalized["extraction_method"] = extraction["method"]
        normalized["ocr_confidence"] = extraction["confidence"]
        normalized["raw_extraction"] = extraction
        return ensure_explicit_nulls(normalized)

    normalized = scanned_pdf_fallback(pdf_path)
    normalized["extraction_method"] = extraction["method"]
    normalized["ocr_confidence"] = extraction["confidence"]
    normalized["raw_extraction"].update(extraction)
    return ensure_explicit_nulls(normalized)


def extract_text_with_pymupdf(pdf_path: Path) -> str:
    try:
        import fitz
    except ImportError:
        return ""

    try:
        with fitz.open(str(pdf_path)) as document:
            return "\n".join(page.get_text("text") for page in document)
    except Exception:
        return ""


def extract_text_with_ocr(pdf_path: Path) -> dict[str, Any]:
    try:
        import fitz
        from PIL import Image
    except ImportError:
        return {"text": "", "method": "ocr_unavailable", "confidence": 0.0}

    text_parts: list[str] = []
    confidences: list[float] = []
    try:
        with fitz.open(str(pdf_path)) as document:
            for page in document:
                pixmap = page.get_pixmap(dpi=300, alpha=False)
                image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                result = ocr_image(image)
                text_parts.append(result["text"])
                confidences.extend(result["confidences"])
    except Exception as exc:
        return {"text": "", "method": "ocr", "confidence": 0.0, "error": str(exc)}

    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return {"text": "\n".join(text_parts), "method": "ocr", "confidence": round(confidence, 4)}


def extract_text_from_image(image_path: Path) -> dict[str, Any]:
    try:
        from PIL import Image
    except ImportError:
        return {"text": "", "method": "image_ocr_unavailable", "confidence": 0.0, "error": "Pillow is not installed"}

    try:
        image = Image.open(image_path)
        result = ocr_image(image)
    except Exception as exc:
        return {"text": "", "method": "image_ocr", "confidence": 0.0, "error": str(exc)}

    confidence = sum(result["confidences"]) / len(result["confidences"]) if result["confidences"] else 0.0
    return {"text": result["text"], "method": "image_ocr", "confidence": round(confidence, 4)}


def ocr_image(image: Any) -> dict[str, Any]:
    try:
        import pytesseract
        from PIL import ImageEnhance, ImageFilter, ImageOps
    except ImportError as exc:
        return {"text": "", "confidences": [], "error": str(exc)}

    configure_tesseract(pytesseract)
    prepared = ImageOps.grayscale(image)
    prepared = ImageOps.autocontrast(prepared)
    if prepared.width < 1800:
        prepared = prepared.resize((prepared.width * 2, prepared.height * 2))
    prepared = ImageEnhance.Contrast(prepared).enhance(2.2)
    prepared = prepared.filter(ImageFilter.SHARPEN)
    prepared = prepared.point(lambda value: 255 if value > 180 else 0)

    config = "--oem 3 --psm 6"
    data = pytesseract.image_to_data(prepared, output_type=pytesseract.Output.DICT, config=config)
    words = [word for word in data.get("text", []) if str(word).strip()]
    confidences: list[float] = []
    for value in data.get("conf", []):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric >= 0:
            confidences.append(numeric / 100)
    return {"text": " ".join(words), "confidences": confidences}


def configure_tesseract(pytesseract_module: Any) -> None:
    current = getattr(pytesseract_module.pytesseract, "tesseract_cmd", "")
    if current and Path(current).exists():
        return
    for candidate in (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Users\User\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ):
        if candidate.exists():
            pytesseract_module.pytesseract.tesseract_cmd = str(candidate)
            return


def enough_embedded_text(text: str) -> bool:
    return len(text.strip()) >= 40


def extract_text_with_glm_ocr(file_path: Path) -> dict[str, Any]:
    """Use the optional Hugging Face GLM-OCR model for images/scanned PDFs."""
    try:
        from transformers import pipeline
    except ImportError:
        return {"text": "", "method": "glm_ocr_unavailable", "confidence": 0.0, "error": "transformers is not installed"}

    image_inputs = document_to_image_inputs(file_path)
    if not image_inputs:
        return {"text": "", "method": "glm_ocr_unavailable", "confidence": 0.0, "error": "No image pages could be prepared for OCR"}

    prompt = (
        "Extract the invoice text exactly as visible. Include invoice number, date, vendor, "
        "vendor ID, PO/reference number, line items, subtotal, tax/GST, and total. "
        "Return plain text only."
    )
    try:
        pipe = pipeline("image-text-to-text", model=GLM_OCR_MODEL)
        pages: list[str] = []
        for image_input in image_inputs:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "url": image_input},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
            response = pipe(text=messages)
            pages.append(extract_pipeline_text(response))
    except Exception as exc:
        return {"text": "", "method": "glm_ocr", "confidence": 0.0, "error": str(exc)}

    text = "\n".join(page for page in pages if page.strip())
    return {"text": text, "method": "glm_ocr", "confidence": 0.88 if text.strip() else 0.0}


def extract_text_with_gemini(file_path: Path) -> dict[str, Any]:
    """Use Gemini Vision for optional image/PDF invoice extraction."""
    api_key = gemini_api_key()
    if not api_key:
        return {"text": "", "method": "gemini_unavailable", "confidence": 0.0, "error": "GEMINI_API_KEY is not set"}

    try:
        import google.generativeai as genai
    except ImportError:
        return {"text": "", "method": "gemini_unavailable", "confidence": 0.0, "error": "google-generativeai is not installed"}

    prompt = (
        "Extract this vendor invoice into strict JSON only. Use this exact schema: "
        '{"vendor_name": string|null, "vendor_id": string|null, "vendor_bill_no": string|null, "invoice_number": string|null, '
        '"invoice_date": "YYYY-MM-DD"|null, "po_reference": string|null, "subtotal": string|null, '
        '"tax_amount": string|null, "total_amount": string|null, "line_items": ['
        '{"description": string|null, "quantity": string|null, "unit_price": string|null, '
        '"line_amount": string|null, "tax_amount": string|null}]}'
    )
    import traceback

    try:
        print("=" * 80)
        print("Starting Gemini extraction")
        print(f"File: {file_path}")
        print(f"Exists: {file_path.exists()}")
        print(f"Size: {file_path.stat().st_size} bytes")
        print(f"Model: {os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')}")
        print("=" * 80)
    
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        )
    
        response = model.generate_content(
            [
                prompt,
                {
                    "mime_type": mime_type_for(file_path),
                    "data": file_path.read_bytes(),
                },
            ],
            request_options={"timeout": 30},
        )
    
        text = getattr(response, "text", "") or ""
    
        print("========== GEMINI RAW RESPONSE ==========")
        print(text)
        print("=========================================")
    
        structured = parse_gemini_json(text)
    
        if structured:
            return {
                "text": text,
                "structured": structured,
                "method": "gemini",
                "confidence": 0.92,
            }
    
        return {
            "text": text,
            "method": "gemini",
            "confidence": 0.75 if text.strip() else 0.0,
            "error": "Gemini response was not parseable JSON",
        }
    
    except Exception as exc:
        print("=" * 80)
        print("GEMINI EXCEPTION")
        print(exc)
        traceback.print_exc()
        print("=" * 80)
    
        return {
            "text": "",
            "method": "gemini",
            "confidence": 0.0,
            "error": str(exc),
        }


def gemini_api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        import streamlit as st

        value = st.secrets.get("GEMINI_API_KEY")
        return str(value) if value else None
    except Exception:
        return None


def parse_gemini_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return normalize_gemini_payload(payload) if isinstance(payload, dict) else None


def normalize_gemini_payload(payload: dict[str, Any]) -> dict[str, Any]:
    line_items = payload.get("line_items")
    bill_number = clean_optional(payload.get("vendor_bill_no") or payload.get("bill_id") or payload.get("invoice_number"))
    return {
        "vendor_name": clean_optional(payload.get("vendor_name") or payload.get("vendor")),
        "vendor_id": normalize_reference(clean_optional(payload.get("vendor_id"))),
        "vendor_bill_no": bill_number,
        "invoice_number": bill_number,
        "invoice_date": clean_optional(payload.get("invoice_date")),
        "line_items": [normalize_gemini_line(line) for line in line_items] if isinstance(line_items, list) else [],
        "subtotal": normalize_amount(clean_optional(payload.get("subtotal"))),
        "tax_amount": normalize_amount(clean_optional(payload.get("tax_amount") or payload.get("tax"))),
        "total_amount": normalize_amount(clean_optional(payload.get("total_amount") or payload.get("total"))),
        "po_reference": normalize_reference(clean_optional(payload.get("po_reference") or payload.get("po_id") or payload.get("po_number"))),
        "po_reference_confidence": "high" if payload.get("po_reference") or payload.get("po_id") or payload.get("po_number") else "none",
    }


def normalize_gemini_line(line: Any) -> dict[str, Any]:
    item = line if isinstance(line, dict) else {}
    return {
        "description": clean_optional(item.get("description") or item.get("item_name")),
        "quantity": normalize_amount(clean_optional(item.get("quantity"))),
        "unit_price": normalize_amount(clean_optional(item.get("unit_price"))),
        "line_amount": normalize_amount(clean_optional(item.get("line_amount") or item.get("amount"))),
        "tax_amount": normalize_amount(clean_optional(item.get("tax_amount") or item.get("tax"))),
    }


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return clean(text) if text else None


def mime_type_for(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(suffix, "application/octet-stream")


def document_to_image_inputs(file_path: Path) -> list[str]:
    suffix = file_path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [file_path.resolve().as_uri()]
    if suffix != ".pdf":
        return []

    try:
        import fitz
    except ImportError:
        return []

    output_dir = file_path.parent / ".ocr_pages"
    output_dir.mkdir(exist_ok=True)
    image_paths: list[str] = []
    try:
        with fitz.open(str(file_path)) as document:
            for index, page in enumerate(document):
                pixmap = page.get_pixmap(dpi=200)
                image_path = output_dir / f"{file_path.stem}_page_{index + 1}.png"
                pixmap.save(str(image_path))
                image_paths.append(image_path.resolve().as_uri())
    except Exception:
        return []
    return image_paths


def extract_pipeline_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, list) and response:
        return extract_pipeline_text(response[0])
    if isinstance(response, dict):
        for key in ("generated_text", "text", "answer"):
            value = response.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                text_parts = []
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("content"), str):
                        text_parts.append(item["content"])
                    elif isinstance(item, str):
                        text_parts.append(item)
                if text_parts:
                    return "\n".join(text_parts)
    return str(response or "")


def extract_text_pdf(pdf_path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:
            return ""

    raw = pdf_path.read_bytes()
    try:
        decoded = raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    token_text = extract_pdf_text_tokens(decoded)
    if has_invoice_text(token_text):
        return token_text
    return decoded if has_invoice_text(decoded) else ""


def extract_pdf_text_tokens(decoded_pdf: str) -> str:
    """Extract simple PDF text-showing tokens from uncompressed PDFs."""
    tokens = re.findall(r"\(([^()]*)\)\s*Tj", decoded_pdf, flags=re.DOTALL)
    cleaned: list[str] = []
    for token in tokens:
        token = token.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
        token = token.strip()
        if token:
            cleaned.append(token)
    return "\n".join(cleaned)


def has_invoice_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("invoice number", "vendor:", "vendor id", "total:", "purchase order", "ref:"))


def parse_invoice_text(text: str) -> dict[str, Any]:
    text = normalize_ocr_text(text)
    invoice_number = (
        first_match(text, r"(?:(?:Vendor\s*)?Bill\s*(?:No\.?|Number|#)\s*[:\.\-]?\s*)([A-Z0-9\-\/]+)")
        or first_match(text, r"(?:Invoice\s*(?:No\.?|Number|#)\s*[:\.\-]?\s*)([A-Z0-9\-\/]+)")
        or first_match(text, r"\b(INV[-A-Z0-9\/]+)\b")
        or first_match(text, r"\b(BILL[-A-Z0-9\/]+)\b")
    )
    invoice_date = first_match(text, r"(?:Invoice\s*Date|Date)\s*[:\.\-]?\s*(\d{4}-\d{2}-\d{2})") or first_match(text, r"\b(\d{4}-\d{2}-\d{2})\b")
    po_reference = (
        first_match(text, r"(?:PO|Purchase\s*Order)\s*(?:No\.?|Number|#)?\s*[:\-]?\s*(PO[-\s]?\d+)")
        or first_match(text, r"(?:Ref|Reference)\s*[:\-]?\s*(PO[-\s]?\d+)")
        or first_match(text, r"\b(PO[-\s]?\d+)\b")
    )
    total = first_match(text, r"(?:Total|Amount\s*Due)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{2})?)")
    tax = first_match(text, r"(?:Tax|GST)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{2})?)")
    subtotal = first_match(text, r"(?:Subtotal|Sub Total)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{2})?)")
    total = last_match(text, r"(?:^|\n)(?:Total|Amount\s*Due)\s*[:\-]?\s*(?:INR|Rs\.?)?\s*([0-9,]+(?:\.\d{2})?)") or total
    tax = last_match(text, r"(?:^|\n)(?:Tax|GST)\s*[:\-]?\s*(?:INR|Rs\.?)?\s*([0-9,]+(?:\.\d{2})?)") or tax
    subtotal = last_match(text, r"(?:^|\n)(?:Subtotal|Sub Total)\s*[:\-]?\s*(?:INR|Rs\.?)?\s*([0-9,]+(?:\.\d{2})?)") or subtotal
    vendor_name = (
        first_match(text, r"Vendor\s*[:\.\-]?\s*([A-Za-z0-9 .,&]+?)\s+(?:Vendor\s*ID|VEND[-\s]?\d+|Ref|PO[-\s]?\d+)")
        or first_match(text, r"Vendor\s*[:\.\-]?\s*([A-Za-z0-9 .,&]+)")
    )
    vendor_id = first_match(text, r"Vendor\s*ID\s*[:\.\-]?\s*([A-Z0-9\-]+)") or first_match(text, r"\b(VEND[-\s]?\d+)\b")

    line_items = extract_line_items(text)
    if not line_items and total:
        line_items = [
            {
                "description": "Bundled invoice total",
                "quantity": None,
                "unit_price": None,
                "line_amount": normalize_amount(total),
                "tax_amount": normalize_amount(tax),
            }
        ]

    return {
        "vendor_name": clean(vendor_name),
        "vendor_id": normalize_reference(clean(vendor_id)),
        "vendor_bill_no": clean(invoice_number),
        "invoice_number": clean(invoice_number),
        "invoice_date": clean(invoice_date),
        "line_items": line_items,
        "subtotal": normalize_amount(subtotal),
        "tax_amount": normalize_amount(tax),
        "total_amount": normalize_amount(total),
        "po_reference": normalize_reference(clean(po_reference)) if po_reference else None,
        "po_reference_confidence": "medium" if po_reference else "none",
    }


def extract_line_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pattern = re.compile(r"(?P<description>[A-Za-z][A-Za-z0-9 \-]+)\s+(?P<quantity>\d+(?:\.\d+)?)\s+(?P<unit>[0-9,]+(?:\.\d{2})?)\s+(?P<amount>[0-9,]+(?:\.\d{2})?)")
    for match in pattern.finditer(text):
        items.append(
            {
                "description": clean_line_description(match.group("description")),
                "quantity": normalize_amount(match.group("quantity")),
                "unit_price": normalize_amount(match.group("unit")),
                "line_amount": normalize_amount(match.group("amount")),
                "tax_amount": None,
            }
        )
    return items


def clean_line_description(value: str | None) -> str | None:
    description = clean(value)
    if not description:
        return None
    header = "Description Quantity Unit Price Amount"
    if description.lower().startswith(header.lower()):
        description = description[len(header) :].strip()
    return description or None


def scanned_pdf_fallback(pdf_path: Path) -> dict[str, Any]:
    return {
        "vendor_name": None,
        "vendor_id": None,
        "invoice_number": None,
        "invoice_date": None,
        "line_items": [],
        "subtotal": None,
        "tax_amount": None,
        "total_amount": None,
        "po_reference": None,
        "po_reference_confidence": "none",
        "raw_extraction": {
            "source_file": str(pdf_path),
            "confidence": "low",
            "note": "Scanned/image PDF routed to OCR fallback. Configure pytesseract or a vision API for production extraction.",
        },
    }


def ensure_explicit_nulls(payload: dict[str, Any]) -> dict[str, Any]:
    for key in NORMALIZED_KEYS:
        payload.setdefault(key, None)
    if payload["line_items"] is None:
        payload["line_items"] = []
    payload.setdefault("raw_extraction", {})
    return payload


def first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def last_match(text: str, pattern: str) -> str | None:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    return matches[-1].strip() if matches else None


def clean(value: str | None) -> str | None:
    return " ".join(value.split()) if value else None


def normalize_amount(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace(",", "")


def normalize_ocr_text(text: str) -> str:
    normalized = text.replace("‘", "").replace("’", "").replace("“", "").replace("”", "")
    replacements = {
        "Invoiee": "Invoice",
        "Involee": "Invoice",
        "Inv-PDF": "INV-PDF",
        "INv-": "INV-",
        "Subtotat": "Subtotal",
        "Subtotai": "Subtotal",
        "@sr": "GST",
        "GsT": "GST",
        "Po-": "PO-",
        "P0-": "PO-",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\b(Vendor Bill No|Bill Number|Bill No|Invoice Number|Date|Vendor|Vendor ID|Ref|Currency|Description|Subtotal|GST|Total)\b", r"\n\1", normalized, flags=re.IGNORECASE)
    return normalized


def normalize_reference(value: str | None) -> str | None:
    if not value:
        return None
    return value.upper().replace(" ", "-")
