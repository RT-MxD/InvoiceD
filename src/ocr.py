"""
OCR helpers — read text out of scanned image invoices and image-only PDFs.

Everything here is free and local:
  * Tesseract OCR engine (via the `pytesseract` wrapper)
  * Pillow for light image preprocessing
  * pdf2image (Poppler) to rasterize scanned PDF pages

All heavy imports are lazy and every entry point degrades gracefully: if the
Tesseract/Poppler binaries aren't installed, we raise a clear, actionable
`OCRUnavailable` instead of a cryptic import error.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from . import config

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


class OCRUnavailable(RuntimeError):
    """Raised when OCR is requested but the engine/binaries are missing."""


def ocr_available() -> Tuple[bool, str]:
    """Return (is_available, human_message). Cheap to call."""
    if not config.OCR_ENABLED:
        return False, "OCR is disabled (set OCR_ENABLED=true to enable)."
    try:
        import pytesseract  # noqa: WPS433
    except ImportError:
        return False, "pytesseract not installed (`pip install pytesseract`)."
    try:
        version = pytesseract.get_tesseract_version()
    except Exception:  # pytesseract raises if the tesseract binary is absent
        return False, (
            "Tesseract binary not found. Install it — macOS: `brew install tesseract`, "
            "Debian/Ubuntu: `apt-get install tesseract-ocr` (already in the Docker image)."
        )
    return True, f"Tesseract {version} ready."


def _preprocess(img):
    """Grayscale + autocontrast to make OCR more reliable on noisy scans."""
    from PIL import ImageOps  # noqa: WPS433

    img = img.convert("L")            # grayscale
    img = ImageOps.autocontrast(img)  # normalize brightness/contrast
    return img


def image_to_text(path: Path) -> str:
    """OCR a single image file into text."""
    ok, msg = ocr_available()
    if not ok:
        raise OCRUnavailable(msg)

    import pytesseract  # noqa: WPS433
    from PIL import Image  # noqa: WPS433

    with Image.open(path) as img:
        text = pytesseract.image_to_string(_preprocess(img), lang=config.OCR_LANGUAGE)
    return text.strip()


def pdf_to_text_ocr(path: Path) -> str:
    """Rasterize each page of a scanned PDF and OCR it."""
    ok, msg = ocr_available()
    if not ok:
        raise OCRUnavailable(msg)

    import pytesseract  # noqa: WPS433
    try:
        from pdf2image import convert_from_path  # noqa: WPS433
    except ImportError as exc:
        raise OCRUnavailable("pdf2image not installed (`pip install pdf2image`).") from exc

    try:
        pages = convert_from_path(str(path), dpi=config.OCR_DPI)
    except Exception as exc:  # typically: Poppler not installed
        raise OCRUnavailable(
            f"Could not rasterize PDF (is Poppler installed? "
            f"macOS: `brew install poppler`): {exc}"
        ) from exc

    parts: List[str] = []
    for page in pages:
        parts.append(pytesseract.image_to_string(_preprocess(page), lang=config.OCR_LANGUAGE))
    return "\n".join(parts).strip()
