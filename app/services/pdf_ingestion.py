"""Expand PDFs into page images and detect selectable digital text."""

from dataclasses import dataclass

try:
    import pymupdf as fitz
except ImportError:  # Older PyMuPDF releases expose the legacy module name.
    import fitz


MIN_DIGITAL_TEXT_CHARS = 8


@dataclass(frozen=True)
class PreparedPage:
    name: str
    image_bytes: bytes
    digital_text: str | None


def expand_pdf(filename: str, content: bytes, max_pages: int = 10) -> list[PreparedPage]:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as error:
        raise ValueError(f"Could not read PDF {filename}: {error}") from error
    try:
        if document.page_count < 1:
            raise ValueError(f"PDF {filename} has no pages")
        if document.page_count > max_pages:
            raise ValueError(f"PDF {filename} has {document.page_count} pages; bundles are limited to {max_pages} pages")
        pages = []
        for index in range(document.page_count):
            page = document.load_page(index)
            text = page.get_text("text").strip()
            digital_text = text if sum(character.isalnum() for character in text) >= MIN_DIGITAL_TEXT_CHARS else None
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            pages.append(PreparedPage(f"{filename} · page {index + 1}", pixmap.tobytes("png"), digital_text))
        return pages
    finally:
        document.close()


def expand_upload(filename: str, content: bytes, max_pages: int = 10) -> list[PreparedPage]:
    if filename.lower().endswith(".pdf"):
        return expand_pdf(filename, content, max_pages=max_pages)
    return [PreparedPage(filename, content, None)]
