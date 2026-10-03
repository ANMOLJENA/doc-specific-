"""Redis worker for deterministic PDF extraction jobs."""

import json
import logging
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from app.config import EXTRACTION_QUEUE_NAME, STORAGE_PATH
from app.database import Base, SessionLocal, engine
from app.models import ExtractionJob
from app.queue import client
from app.services.pdf_extraction import extract_pdf
from app.services.processing import process_images_primary

logger = logging.getLogger(__name__)


def handle_extraction_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(ExtractionJob, job_id)
        if not job or job.status not in {"queued", "processing"}:
            return
        job.status = "processing"
        job.error = None
        db.commit()
        results = []
        try:
            for item in job.files or []:
                path = Path(item["storage_key"])
                try:
                    ocr_folder = path.parent / "ocr-pages"
                    ocr_folder.mkdir(parents=True, exist_ok=True)
                    def read_scanned_page(pdf_page, page_number):
                        rendered = ocr_folder / f"{path.stem}-{page_number + 1}.png"
                        pdf_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(rendered)
                        results = process_images_primary([rendered])
                        return results[0].text if results else ""
                    result = extract_pdf(path, list(job.fields or []), ocr_reader=read_scanned_page, progress=lambda current, total: _progress(db, job, current, total))
                    result["file"] = item.get("name", path.name)
                    result.setdefault("output", {})["Source PDF"] = result["file"]
                except Exception as error:
                    logger.exception("PDF extraction failed for %s", path)
                    result = {"file": item.get("name", path.name), "status": "failed", "pages": item.get("pages", 0), "extraction_method": [], "data": {field: None for field in job.fields or []}, "output": {"Source PDF": item.get("name", path.name)}, "evidence": {}, "confidence": {}, "warnings": [str(error)], "fields_found": 0, "fields_total": len(job.fields or [])}
                results.append(result)
                job.results = list(results)
                db.commit()
            job.current_page = job.total_pages
            job.status = "done" if all(item.get("status") == "done" for item in results) else "warning"
        except Exception as error:
            job.status = "failed"
            job.error = str(error)
        db.commit()


def _progress(db, job, current: int, total: int) -> None:
    job.current_page = min(job.total_pages, current)
    db.commit()


def run() -> None:
    redis = client()
    logger.info("PDF extraction worker started; queue=%s", EXTRACTION_QUEUE_NAME)
    while True:
        _, raw = redis.blpop(EXTRACTION_QUEUE_NAME)
        handle_extraction_job(json.loads(raw)["job_id"])


if __name__ == "__main__":
    run()
