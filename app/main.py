import json
import shutil
import uuid
import re
import os
import io
import zipfile
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.request import urlopen

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import (AI_BASE_URL, AI_MODEL, OCR_BATCH_SIZE, OCR_MIN_CHARS,
                        OCR_RECHECK_CONFIDENCE, PADDLE_HEARTBEAT_KEY,
                        PADDLE_PRIMARY_QUEUE_NAME, PADDLE_PRIMARY_TIMEOUT_SECONDS,
                        PADDLE_QUEUE_NAME, PREVIEW_MODE, QUEUE_NAME, STORAGE_PATH)
from app.database import Base, SessionLocal, engine
from app.models import ApprovedCategory, Case, CaseAnalysis, ExtractionJob, Page
from app.queue import (clear_case_cancel, client as redis_client, enqueue_case, enqueue_extraction,
                       enqueue_paddle_comparison, request_case_cancel)
from app.services.comparisons import comparison_for, set_verdict, update_paddle
from app.services.pdf_ingestion import expand_upload
from app.services.validation import validate_proposal
from app.services.pdf_extraction import load_dictionary, normalise_fields, resolve_fields, results_to_csv, save_dictionary

APP_DIR = Path(__file__).resolve().parent
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".pdf"}


class CategoryChange(BaseModel):
    category: str


class OcrVerdict(BaseModel):
    verdict: Literal["surya", "paddle", "tie", "neither"]


class PageDeletionRequest(BaseModel):
    """The repository deletes individual uploaded images, not just display rows."""

    page_ids: list[str]


class ExtractionJobRequest(BaseModel):
    fields: list[str] = []


class DictionaryEntry(BaseModel):
    labels: list[str]
    type: str = "text"
    pattern: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="DOC Specific OCR Pipeline", version="0.2.0", lifespan=lifespan)
app.mount("/frontend", StaticFiles(directory=APP_DIR / "static" / "react"), name="frontend")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(APP_DIR / "static" / "react" / "index.html")


@app.get("/validation", include_in_schema=False)
@app.get("/exports", include_in_schema=False)
@app.get("/queue", include_in_schema=False)
@app.get("/database", include_in_schema=False)
def portal_page():
    return FileResponse(APP_DIR / "static" / "react" / "index.html")


@app.post("/api/extract/resolve")
def resolve_extraction_fields(payload: ExtractionJobRequest):
    return {"fields": resolve_fields(payload.fields)}


@app.get("/api/extract/dictionary")
def get_extraction_dictionary():
    return load_dictionary()


@app.put("/api/extract/dictionary")
def put_extraction_dictionary(entries: dict[str, DictionaryEntry]):
    dictionary = load_dictionary()
    for key, entry in entries.items():
        normal = normalise_fields([key])
        if not normal:
            raise HTTPException(400, "Dictionary field key is required")
        dictionary[normal[0]] = entry.model_dump(exclude_none=True)
    save_dictionary(dictionary)
    return dictionary


@app.post("/api/extract/jobs", status_code=201)
def create_extraction_job(payload: ExtractionJobRequest, db: Session = Depends(get_db)):
    fields = normalise_fields(payload.fields)
    if not fields:
        raise HTTPException(400, "Choose at least one target field")
    job = ExtractionJob(fields=fields, status="draft", files=[], results=[])
    db.add(job)
    db.commit()
    return extraction_job_payload(job)


@app.post("/api/extract/jobs/{job_id}/files")
async def add_extraction_files(job_id: str, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    job = db.get(ExtractionJob, job_id)
    if not job:
        raise HTTPException(404, "Extraction job not found")
    if job.status not in {"draft", "queued"}:
        raise HTTPException(409, "Files cannot be changed after extraction starts")
    current = list(job.files or [])
    if len(current) + len(files) > 10:
        raise HTTPException(400, "Add at most 10 PDF files")
    job_folder = STORAGE_PATH / "extract_jobs" / job.id
    job_folder.mkdir(parents=True, exist_ok=True)
    for upload in files:
        filename = Path(upload.filename or "document.pdf").name
        if Path(filename).suffix.lower() != ".pdf":
            raise HTTPException(400, "PDF Data Extraction accepts PDF files only")
        content = await upload.read()
        if len(content) > 100 * 1024 * 1024:
            raise HTTPException(413, f"{filename} exceeds the 100 MB limit")
        from app.services.pdf_ingestion import expand_pdf
        try:
            pages = expand_pdf(filename, content, max_pages=100)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if sum(item.get("pages", 0) for item in current) + len(pages) > 100:
            raise HTTPException(400, "Extraction jobs are limited to 100 PDF pages")
        key = job_folder / f"{uuid.uuid4().hex}-{filename}"
        key.write_bytes(content)
        text_pages = sum(1 for page in pages if page.digital_text)
        layer = "text" if text_pages == len(pages) else "scanned" if text_pages == 0 else "mixed"
        current.append({"name": filename, "storage_key": str(key), "pages": len(pages), "size": len(content), "layer": layer})
    job.files = current
    job.total_pages = sum(item["pages"] for item in current)
    db.commit()
    return extraction_job_payload(job)


@app.post("/api/extract/jobs/{job_id}/run", status_code=202)
def run_extraction_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ExtractionJob, job_id)
    if not job:
        raise HTTPException(404, "Extraction job not found")
    if not job.fields or not job.files:
        raise HTTPException(400, "Add target fields and at least one PDF first")
    if job.status in {"queued", "processing"}:
        raise HTTPException(409, "Extraction is already running")
    job.status = "queued"
    job.current_page = 0
    job.results = []
    db.commit()
    try:
        enqueue_extraction(job.id)
    except Exception as error:
        job.status = "failed"
        job.error = str(error)
        db.commit()
        raise HTTPException(503, "Extraction queue is unavailable") from error
    return extraction_job_payload(job)


@app.get("/api/extract/jobs/{job_id}")
def get_extraction_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ExtractionJob, job_id)
    if not job:
        raise HTTPException(404, "Extraction job not found")
    return extraction_job_payload(job)


@app.get("/api/extract/jobs/{job_id}/export")
def export_extraction_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ExtractionJob, job_id)
    if not job:
        raise HTTPException(404, "Extraction job not found")
    results = job.results or []
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        public_results = []
        for index, result in enumerate(results, start=1):
            output = result.get("output", result.get("data", {}))
            public_results.append(output)
            stem = Path(result.get("file") or f"document-{index}").stem
            safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or f"document-{index}"
            bundle.writestr(f"{safe_stem}.json", json.dumps(output, indent=2).encode())
        bundle.writestr("all-results.json", json.dumps(public_results, indent=2).encode())
        bundle.writestr("claim_data.csv", results_to_csv(results).encode("utf-8-sig"))
    return Response(content=archive.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="extraction-{job.id}.zip"'})


@app.get("/api/extract/jobs/{job_id}/export.csv")
def export_extraction_job_csv(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ExtractionJob, job_id)
    if not job:
        raise HTTPException(404, "Extraction job not found")
    if not job.results:
        raise HTTPException(409, "Run the extraction first; there are no results to export yet")
    return Response(content=results_to_csv(job.results or []).encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="claim_data-{job.id}.csv"'})


def extraction_job_payload(job: ExtractionJob) -> dict:
    return {"job_id": job.id, "status": job.status, "fields": job.fields or [], "files": job.files or [], "results": job.results or [], "current_page": job.current_page or 0, "total_pages": job.total_pages or 0, "error": job.error}


@app.get("/pipeline/status")
def pipeline_status(db: Session = Depends(get_db)):
    """Small, read-only status snapshot; does not claim that a worker is healthy."""
    try:
        db.execute(text("SELECT 1"))
        database = "reachable"
    except Exception:
        database = "unavailable"
    try:
        with redis_client() as redis:
            redis.ping()
            queue_depth = redis.llen(QUEUE_NAME)
            paddle_queue_depth = redis.llen(PADDLE_QUEUE_NAME)
            paddle_primary_queue_depth = redis.llen(PADDLE_PRIMARY_QUEUE_NAME)
            paddle_worker = "available" if redis.exists(PADDLE_HEARTBEAT_KEY) else "not_running"
        queue = "reachable"
    except Exception:
        queue_depth = None
        paddle_queue_depth = None
        paddle_primary_queue_depth = None
        paddle_worker = "unavailable"
        queue = "unavailable"
    surya_url = os.getenv("SURYA_INFERENCE_URL", "http://surya:8080/v1").rstrip("/")
    if surya_url.endswith("/v1"):
        surya_url = surya_url[:-3]
    try:
        with urlopen(surya_url + "/health", timeout=2) as response:
            surya = "reachable" if response.status == 200 else "unavailable"
    except Exception:
        surya = "unavailable"
    return {
        "api": "reachable", "database": database, "redis": queue,
        "surya": surya, "queued_cases": queue_depth,
        "paddle_worker": paddle_worker, "paddle_queue_depth": paddle_queue_depth,
        "paddle_primary_queue_depth": paddle_primary_queue_depth,
        "primary_ocr": "PaddleOCR PP-OCRv6", "fallback_ocr": "Surya 2",
        "paddle_primary_timeout_seconds": PADDLE_PRIMARY_TIMEOUT_SECONDS,
        "worker": "not directly monitored", "queue_granularity": "case",
        "ocr_batch_size": OCR_BATCH_SIZE,
        "ocr_recheck_confidence": OCR_RECHECK_CONFIDENCE,
        "ocr_min_chars": OCR_MIN_CHARS,
        "analysis_configured": bool(AI_BASE_URL and AI_MODEL),
        "quality_gate_enabled": True, "quality_thresholds_calibrated": False,
        "preview_mode": PREVIEW_MODE,
    }


@app.get("/demo", include_in_schema=False)
def demo_case():
    """Clearly labeled sample data so the interface can be explored offline."""
    samples = [
        ("clinic-bill.png", "SUNRISE CLINIC\nInvoice No: INV-2048\nDate: 16/09/2026\nTotal Amount: ₹1,240.00"),
        ("prescription-front.png", "Prescription\nDr Mehta\nAmoxicillin 500 mg\n0-1-1 for 5 days"),
        ("prescription-back.png", "Patient instructions\nTake after food\nReview after 5 days"),
        ("insurance-card.png", "HEALTH COVER\nPolicy No: HL-99828\nMember ID: MB-4419"),
        ("travel-ticket.png", "Travel Ticket\nBooking reference: TK72Q9\nDeparture: 21/09/2026"),
    ]
    pages = [{"page": i, "name": name, "text": text, "source_url": None}
             for i, (name, text) in enumerate(samples, 1)]
    proposal = {"documents": [
        {"category": "medical_bill", "title": "Clinic invoice", "pages": [1], "confidence": 0.94, "fields": [
            {"name": "invoice_number", "value": "INV-2048", "value_type": "identifier", "source_page": 1, "evidence": "Invoice No: INV-2048", "confidence": 0.92},
            {"name": "total_amount", "value": "₹1,240.00", "value_type": "currency", "source_page": 1, "evidence": "Total Amount: ₹1,240.00", "confidence": 0.91},
        ]},
        {"category": "prescription", "title": "Prescription and instructions", "pages": [2, 3], "confidence": 0.88, "fields": [
            {"name": "medicine", "value": "Amoxicillin 500 mg", "value_type": "text", "source_page": 2, "evidence": "Amoxicillin 500 mg", "confidence": 0.86},
            {"name": "schedule", "value": "0-1-1", "value_type": "schedule", "source_page": 2, "evidence": "0-1-1 for 5 days", "confidence": 0.84},
        ]},
        {"category": "insurance_card", "title": "Insurance card", "pages": [4], "confidence": 0.91, "fields": [
            {"name": "policy_number", "value": "HL-99828", "value_type": "identifier", "source_page": 4, "evidence": "Policy No: HL-99828", "confidence": 0.9},
        ]},
        {"category": "travel_ticket", "title": "Travel ticket", "pages": [5], "confidence": 0.81, "fields": [
            {"name": "booking_reference", "value": "TK72Q9", "value_type": "identifier", "source_page": 5, "evidence": "Booking reference: TK72Q9", "confidence": 0.83},
        ]},
    ]}
    analysis = validate_proposal(proposal, pages, set())
    return {
        "case_id": "sample-case", "status": "sample", "demo": True,
        "pages": [{"page": page["page"], "name": page["name"], "status": "sample",
                   "category": None, "ocr_confidence": None, "ocr_rechecked": False,
                   "ocr_method": None, "review_required": False,
                   "ocr_text": page["text"], "error": None, "source_url": None} for page in pages],
        "analysis": analysis,
    }


@app.post("/cases", status_code=201)
async def create_case(files: list[UploadFile] = File(...), ocr_engine: str = Form("paddle"), db: Session = Depends(get_db)):
    if ocr_engine not in {"paddle", "surya"}:
        raise HTTPException(400, "Choose ocr_engine as paddle or surya")
    if not files or len(files) > 10:
        raise HTTPException(400, "Upload 1 to 10 document images or PDF files.")
    for upload in files:
        if Path(upload.filename or "").suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"Unsupported image type: {upload.filename or 'unnamed'}")
    upload_data: list[tuple[str, bytes]] = []
    prepared_pages = []
    try:
        for upload in files:
            filename = Path(upload.filename or "document").name
            content = await upload.read()
            upload_data.append((filename, content))
            prepared_pages.extend(expand_upload(filename, content, max_pages=10))
            if len(prepared_pages) > 10:
                raise ValueError("The expanded upload contains more than 10 pages; bundles are limited to 10 pages")
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not prepared_pages:
        raise HTTPException(400, "The upload did not contain any readable pages")
    case = Case(status="queued")
    db.add(case)
    db.flush()
    digital_metrics: list[dict] = []
    case_folder = STORAGE_PATH / case.id
    case_folder.mkdir(parents=True, exist_ok=True)

    for filename, content in upload_data:
        if filename.lower().endswith(".pdf"):
            source_key = f"{case.id}/source-{uuid.uuid4().hex}.pdf"
            (STORAGE_PATH / source_key).write_bytes(content)

    for sequence, prepared in enumerate(prepared_pages, start=1):
        suffix = ".png" if " · page " in prepared.name else Path(prepared.name).suffix.lower()
        storage_key = f"{case.id}/{sequence:03d}-{uuid.uuid4().hex}{suffix}"
        path = STORAGE_PATH / storage_key
        path.write_bytes(prepared.image_bytes)
        page = Page(
            case_id=case.id,
            sequence=sequence,
            original_name=prepared.name,
            storage_key=storage_key,
        )
        if prepared.digital_text:
            page.status = "processed"
            page.extracted_text = prepared.digital_text
            digital_metrics.append({
                "page": sequence, "ocr_confidence": 1.0,
                "ocr_method": "pymupdf", "ocr_rechecked": False,
                "ocr_needs_review": False, "ocr_elapsed_seconds": 0.0,
                "source_type": "digital_pdf",
            })
        db.add(page)
        db.flush()
    db.add(CaseAnalysis(case_id=case.id, result={"ocr_engine": ocr_engine, "ocr_pages": digital_metrics}))
    db.commit()
    if PREVIEW_MODE:
        case.status = "awaiting_services"
        db.commit()
        return {"case_id": case.id, "status": case.status, "page_count": len(prepared_pages)}
    try:
        enqueue_case(case.id)
    except Exception as error:
        case.status = "queue_unavailable"
        db.commit()
        raise HTTPException(503, f"Case was saved but the queue is unavailable: {error}") from error
    return {"case_id": case.id, "status": "queued", "page_count": len(prepared_pages)}


@app.post("/cases/{case_id}/retry")
def retry_case(case_id: str, db: Session = Depends(get_db)):
    if PREVIEW_MODE:
        raise HTTPException(503, "Processing services are not connected in local preview mode")
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status in {"processing", "queued"}:
        raise HTTPException(409, "Case is already processing")
    held_pages = db.scalars(select(Page).where(Page.case_id == case_id, Page.status.in_({"quality_review", "cancelled", "failed"}))).all()
    for page in held_pages:
        page.status = "queued"
        page.error = None
    clear_case_cancel(case_id)
    enqueue_case(case_id)
    case.status = "queued"
    db.commit()
    return {"case_id": case_id, "status": "queued"}


@app.post("/cases/{case_id}/cancel")
def cancel_case(case_id: str, db: Session = Depends(get_db)):
    if PREVIEW_MODE:
        raise HTTPException(503, "Processing services are not connected in local preview mode")
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    if case.status not in {"queued", "processing"}:
        raise HTTPException(409, "Only a queued or processing case can be cancelled")
    request_case_cancel(case_id)
    case.status = "cancelled"
    for page in db.scalars(select(Page).where(Page.case_id == case_id)).all():
        if page.status in {"queued", "quality_check", "processing"}:
            page.status = "cancelled"
            page.error = "Cancelled by user"
    analysis = db.get(CaseAnalysis, case_id)
    if analysis:
        result = dict(analysis.result or {})
        result.update({
            "analysis_stage": "cancelled",
            "cancelled_at": datetime.now(timezone.utc).isoformat(),
            "review_required": True,
        })
        analysis.result = result
    db.commit()
    return {"case_id": case_id, "status": "cancelled"}


def case_payload(case: Case, db: Session) -> dict:
    """Serialize one stored bundle with the page data used by the React workspace."""
    pages = db.scalars(select(Page).where(Page.case_id == case.id).order_by(Page.sequence)).all()
    analysis = db.get(CaseAnalysis, case.id)
    ocr_pages = {
        item["page"]: item for item in (analysis.result.get("ocr_pages", []) if analysis else [])
        if isinstance(item, dict) and "page" in item
    }
    return {
        "case_id": case.id,
        "created_at": case.created_at.isoformat() + "Z",
        "status": case.status,
        "requested_ocr_engine": analysis.result.get("ocr_engine") if analysis else None,
        "pages": [
            {
                "page": page.sequence,
                "image_id": page.id,
                "name": page.original_name,
                "status": page.status,
                "category": page.document_type,
                "ocr_confidence": ocr_pages.get(page.sequence, {}).get("ocr_confidence"),
                "ocr_rechecked": ocr_pages.get(page.sequence, {}).get("ocr_rechecked", False),
                "ocr_method": ocr_pages.get(page.sequence, {}).get("ocr_method"),
                "ocr_elapsed_seconds": ocr_pages.get(page.sequence, {}).get("ocr_elapsed_seconds"),
                "quality_gate": ocr_pages.get(page.sequence, {}).get("quality_gate"),
                "quality_image_url": f"/cases/{case.id}/pages/{page.sequence}/quality-image" if ocr_pages.get(page.sequence, {}).get("quality_gate", {}).get("derived_storage_key") else None,
                "ocr_comparison": comparison_for(analysis.result if analysis else None, page.sequence),
                "review_required": page.review_required,
                "ocr_text": page.extracted_text,
                "error": page.error,
                "source_url": f"/cases/{case.id}/pages/{page.sequence}/original",
            }
            for page in pages
        ],
        "analysis": analysis.result if analysis else None,
    }


@app.get("/cases")
def list_cases(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    """Read-only repository index for queue and archive screens."""
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    total = db.scalar(select(func.count()).select_from(Case)) or 0
    cases = db.scalars(
        select(Case).order_by(Case.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "cases": [case_payload(case, db) for case in cases],
    }


@app.get("/cases/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    return case_payload(case, db)


def _stored_file(storage_key: str | None) -> Path | None:
    """Resolve a storage key only when it stays inside the configured storage root."""
    if not storage_key:
        return None
    root = STORAGE_PATH.resolve()
    candidate = (STORAGE_PATH / storage_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


@app.delete("/pages")
def delete_repository_pages(request: PageDeletionRequest, db: Session = Depends(get_db)):
    """Permanently remove selected repository images and their local OCR artefacts."""
    page_ids = list(dict.fromkeys(page_id for page_id in request.page_ids if page_id.strip()))
    if not page_ids:
        raise HTTPException(400, "Choose at least one document to delete")
    if len(page_ids) > 100:
        raise HTTPException(400, "Delete at most 100 documents at one time")

    pages = db.scalars(select(Page).where(Page.id.in_(page_ids))).all()
    found_ids = {page.id for page in pages}
    if any(page_id not in found_ids for page_id in page_ids):
        raise HTTPException(404, "One or more selected documents no longer exist")

    by_case: dict[str, list[Page]] = {}
    for page in pages:
        by_case.setdefault(page.case_id, []).append(page)

    files_to_remove: set[Path] = set()
    folders_to_remove: set[Path] = set()
    deleted_case_ids: list[str] = []
    for case_id, selected_pages in by_case.items():
        case = db.get(Case, case_id)
        if not case:
            continue
        # Stop a live worker before deleting rows it may otherwise try to update.
        if case.status in {"queued", "processing"}:
            request_case_cancel(case_id)

        removed_sequences = {page.sequence for page in selected_pages}
        all_pages = db.scalars(select(Page).where(Page.case_id == case_id)).all()
        remaining_pages = [page for page in all_pages if page.id not in found_ids]
        analysis = db.get(CaseAnalysis, case_id)
        if analysis:
            previous_result = dict(analysis.result or {})
            previous_ocr_pages = previous_result.get("ocr_pages", [])
            previous_result["ocr_pages"] = [
                item for item in previous_ocr_pages
                if not isinstance(item, dict) or item.get("page") not in removed_sequences
            ]
            for item in previous_ocr_pages:
                if isinstance(item, dict) and item.get("page") in removed_sequences:
                    report = item.get("quality_gate") or {}
                    derived = _stored_file(report.get("derived_storage_key"))
                    if derived:
                        files_to_remove.add(derived)
            documents = []
            for document in previous_result.get("documents", []):
                if not isinstance(document, dict):
                    continue
                kept_pages = [number for number in document.get("pages", []) if number not in removed_sequences]
                if not kept_pages:
                    continue
                updated = dict(document)
                updated["pages"] = kept_pages
                updated["fields"] = [
                    field for field in updated.get("fields", [])
                    if not isinstance(field, dict) or field.get("source_page") not in removed_sequences
                ]
                documents.append(updated)
            previous_result["documents"] = documents
            analysis.result = previous_result

        for page in selected_pages:
            original = _stored_file(page.storage_key)
            if original:
                files_to_remove.add(original)
            db.delete(page)
        if not remaining_pages:
            db.delete(case)
            deleted_case_ids.append(case_id)
            folder = _stored_file(case_id)
            if folder:
                folders_to_remove.add(folder)

    db.commit()
    # Commit first. If a local-file deletion fails, a stale file is safer than a
    # database record pointing at a missing original.
    for file_path in files_to_remove:
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
    for folder in folders_to_remove:
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    return {"deleted_pages": len(pages), "deleted_case_ids": deleted_case_ids,
            "message": "Selected documents were permanently deleted"}


@app.post("/cases/{case_id}/pages/{sequence}/compare/paddle", status_code=202)
def compare_page_with_paddle(case_id: str, sequence: int, db: Session = Depends(get_db)):
    """Queue a legacy/Surya-fallback comparison; saved OCR text is unchanged."""
    if PREVIEW_MODE:
        raise HTTPException(503, "Comparison needs the Docker processing services")
    case = db.get(Case, case_id)
    page = db.scalar(select(Page).where(Page.case_id == case_id, Page.sequence == sequence))
    if not case or not page:
        raise HTTPException(404, "Case page not found")
    if case.status in {"queued", "processing"} or page.status != "processed":
        raise HTTPException(409, "Wait for the original OCR to finish")
    try:
        with redis_client() as redis:
            if not redis.exists(PADDLE_HEARTBEAT_KEY):
                raise HTTPException(503, "Paddle worker is not running. Start it with: docker compose --profile compare up -d paddle-worker")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Redis is unavailable for Paddle comparison") from error
    analysis = db.get(CaseAnalysis, case_id, with_for_update=True)
    if analysis is None:
        analysis = CaseAnalysis(case_id=case_id, result={})
        db.add(analysis)
        db.flush()
    current = comparison_for(analysis.result, sequence).get("paddle", {})
    if current.get("status") in {"queued", "processing", "completed"}:
        raise HTTPException(409, "Paddle comparison is already queued, running, or complete")
    update_paddle(analysis, sequence, status="queued", text=None, lines=[], confidence=None,
                  elapsed_seconds=None, error=None, model="PP-OCRv6")
    set_verdict(analysis, sequence, None)
    db.commit()
    try:
        enqueue_paddle_comparison(case_id, sequence)
    except Exception as error:
        analysis = db.get(CaseAnalysis, case_id, with_for_update=True)
        update_paddle(analysis, sequence, status="failed", error="Redis could not queue Paddle comparison")
        db.commit()
        raise HTTPException(503, "Paddle comparison could not be queued") from error
    return {"case_id": case_id, "page": sequence, "status": "queued"}


@app.post("/cases/{case_id}/pages/{sequence}/compare/verdict")
def record_ocr_verdict(case_id: str, sequence: int, choice: OcrVerdict, db: Session = Depends(get_db)):
    page = db.scalar(select(Page).where(Page.case_id == case_id, Page.sequence == sequence))
    if not page:
        raise HTTPException(404, "Case page not found")
    analysis = db.get(CaseAnalysis, case_id, with_for_update=True)
    if not analysis or comparison_for(analysis.result, sequence).get("paddle", {}).get("status") != "completed":
        raise HTTPException(409, "Complete the Paddle comparison before recording a verdict")
    set_verdict(analysis, sequence, choice.verdict)
    db.commit()
    return {"case_id": case_id, "page": sequence, "verdict": choice.verdict}


@app.get("/cases/{case_id}/fields")
def get_case_fields(
    case_id: str,
    category: str | None = None,
    field_name: str | None = None,
    db: Session = Depends(get_db),
):
    if not db.get(Case, case_id):
        raise HTTPException(404, "Case not found.")
    analysis = db.get(CaseAnalysis, case_id)
    fields = []
    for document in (analysis.result.get("documents", []) if analysis else []):
        if category and document["category"] != category:
            continue
        for field in document.get("fields", []):
            if field_name and field["name"] != field_name:
                continue
            fields.append({
                **field,
                "category": document["category"],
                "document_id": document["id"],
                "source_url": f"/cases/{case_id}/pages/{field['source_page']}/original",
            })
    return {"case_id": case_id, "fields": fields}


@app.post("/cases/{case_id}/documents/{document_id}/approve")
def approve_document(case_id: str, document_id: str, db: Session = Depends(get_db)):
    analysis = db.get(CaseAnalysis, case_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    result = dict(analysis.result)
    documents = [dict(item) for item in result.get("documents", [])]
    target = next((item for item in documents if item["id"] == document_id), None)
    if not target:
        raise HTTPException(404, "Document not found")
    if target["category"] == "unknown":
        raise HTTPException(400, "Give this page a category before approving it")
    if any(field["status"] == "needs_review" for field in target.get("fields", [])):
        raise HTTPException(400, "Fields with evidence or format issues need correction before approval")
    if not db.get(ApprovedCategory, target["category"]):
        db.add(ApprovedCategory(name=target["category"]))
    target["category_status"] = "approved"
    target["issues"] = [issue for issue in target["issues"] if issue not in {
        "new_category_requires_review", "low_category_confidence",
    }]
    target["review_required"] = bool(target["issues"])
    result["documents"] = documents
    result["review_required"] = any(item["review_required"] for item in documents)
    analysis.result = result
    case = db.get(Case, case_id)
    pages = db.scalars(select(Page).where(Page.case_id == case_id)).all()
    ocr_flags = {
        item["page"]: item.get("ocr_needs_review", False)
        for item in result.get("ocr_pages", [])
    }
    for page in pages:
        if page.sequence in target["pages"]:
            page.review_required = target["review_required"] or ocr_flags.get(page.sequence, False)
    case.status = "needs_review" if result["review_required"] or any(page.review_required for page in pages) else "completed"
    db.commit()
    return {"case_id": case_id, "document_id": document_id, "category_status": "approved"}


@app.patch("/cases/{case_id}/documents/{document_id}/category")
def change_document_category(case_id: str, document_id: str, change: CategoryChange, db: Session = Depends(get_db)):
    analysis = db.get(CaseAnalysis, case_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    category = re.sub(r"[^a-z0-9_]+", "_", change.category.lower()).strip("_")[:64]
    if not category or category == "unknown":
        raise HTTPException(400, "Enter a specific document category")
    result = dict(analysis.result)
    documents = [dict(item) for item in result.get("documents", [])]
    target = next((item for item in documents if item["id"] == document_id), None)
    if not target:
        raise HTTPException(404, "Document not found")
    target["category"] = category
    target["category_status"] = "proposed"
    target["issues"] = [issue for issue in target["issues"] if issue not in {
        "unknown_document_type", "page_not_grouped_by_ai", "new_category_requires_review",
    }]
    target["issues"].append("new_category_requires_review")
    target["review_required"] = True
    result["documents"] = documents
    result["review_required"] = True
    analysis.result = result
    for page in db.scalars(select(Page).where(Page.case_id == case_id)).all():
        if page.sequence in target["pages"]:
            page.document_type = category
            page.review_required = True
    db.get(Case, case_id).status = "needs_review"
    db.commit()
    return {"case_id": case_id, "document_id": document_id, "category": category}


@app.get("/cases/{case_id}/pages/{sequence}/original")
def get_original_page(case_id: str, sequence: int, db: Session = Depends(get_db)):
    """Return the source image that supports an extracted result."""
    page = db.scalar(
        select(Page).where(Page.case_id == case_id, Page.sequence == sequence)
    )
    if not page:
        raise HTTPException(404, "Page not found.")
    path = STORAGE_PATH / page.storage_key
    if not path.exists():
        raise HTTPException(404, "Original file is no longer available.")
    return FileResponse(path, filename=page.original_name, content_disposition_type="inline")


@app.get("/cases/{case_id}/pages/{sequence}/quality-image")
def quality_image(case_id: str, sequence: int, db: Session = Depends(get_db)):
    page = db.scalar(select(Page).where(Page.case_id == case_id, Page.sequence == sequence))
    analysis = db.get(CaseAnalysis, case_id)
    if not page or not analysis:
        raise HTTPException(404, "Quality image not found")
    metric = next((item for item in analysis.result.get("ocr_pages", []) if item.get("page") == sequence), {})
    key = metric.get("quality_gate", {}).get("derived_storage_key")
    if not key:
        raise HTTPException(404, "This page has no derived quality image")
    root = STORAGE_PATH.resolve()
    path = (root / key).resolve()
    if path.parent != (root / case_id).resolve() or not path.is_file():
        raise HTTPException(404, "Quality image not found")
    return FileResponse(path, media_type="image/png")
