"""Case-level worker: OCR the bundle, propose document groups, validate evidence."""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import EXTRACTION_QUEUE_NAME, OCR_BATCH_SIZE, QUEUE_NAME, STORAGE_PATH
from app.database import Base, SessionLocal, engine
from app.models import ApprovedCategory, Case, CaseAnalysis, Page
from app.queue import CaseCancelled, case_cancelled, client
from app.services.analysis import AnalysisUnavailable, propose_documents
from app.services.processing import process_images, process_images_primary
from app.services.quality_gate import prepare_ocr_input
from app.services.validation import validate_proposal
from app.extraction_worker import handle_extraction_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _raise_if_cancelled(case_id: str) -> None:
    if case_cancelled(case_id):
        raise CaseCancelled(f"Case {case_id} was cancelled")


def _save_analysis(db, case_id: str, result: dict) -> None:
    record = db.get(CaseAnalysis, case_id)
    if record:
        result.setdefault("ocr_engine", (record.result or {}).get("ocr_engine", "paddle"))
        if "ocr_comparisons" in (record.result or {}):
            result.setdefault("ocr_comparisons", record.result["ocr_comparisons"])
        record.result = result
    else:
        db.add(CaseAnalysis(case_id=case_id, result=result))


def _process_batch(db, case_id: str, batch: list[Page], metrics: dict[int, dict], requested_engine: str = "paddle") -> None:
    processor = process_images if requested_engine == "surya" else process_images_primary
    accepted = []
    paths = []
    for page in batch:
        page.status = "quality_check"
    db.commit()
    for page in batch:
        _raise_if_cancelled(case_id)
        existing = dict(metrics.get(page.sequence, {}))
        path, report = prepare_ocr_input(Path(STORAGE_PATH) / page.storage_key)
        history = list(existing.get("quality_history", []))
        history.append(report)
        metrics[page.sequence] = {**existing, "page": page.sequence, "quality_gate": report,
                                 "quality_history": history, "ocr_method": None,
                                 "ocr_needs_review": path is None or report.get("review_recommended", False)}
        logger.info("quality_decision case=%s image=%s page=%s decision=%s reasons=%s",
                    case_id, page.id, page.sequence, report["decision"], report["failure_reasons"])
        if path is None:
            page.status = "quality_review"
            page.error = "Quality gate held image: " + ", ".join(report["failure_reasons"])
            page.review_required = True
        else:
            page.status = "processing"
            accepted.append(page)
            paths.append(path)
    record = db.get(CaseAnalysis, case_id)
    current = dict(record.result or {}) if record else {}
    current["ocr_pages"] = list(metrics.values())
    _save_analysis(db, case_id, current)
    db.commit()
    if not accepted:
        return
    def save_page(page, started, result=None, error=None):
        elapsed = round(time.perf_counter() - started, 2)
        metrics[page.sequence]["ocr_elapsed_seconds"] = elapsed
        metrics[page.sequence]["ocr_finished_at"] = datetime.now(timezone.utc).isoformat()
        if error is not None or result is None or not result.text.strip():
            page.status = "failed"
            page.error = "OCR extraction failed; inspect worker logs" if error else "The selected OCR route returned no text"
            page.review_required = True
            if error:
                logger.error("OCR failed for case=%s page=%s (%s)", case_id, page.sequence, type(error).__name__)
        else:
            page.extracted_text = result.text
            page.status = "processed"
            page.error = None
            quality_review = metrics[page.sequence].get("quality_gate", {}).get("review_recommended", False)
            page.review_required = result.needs_review or quality_review
            metrics[page.sequence].update({
                "ocr_confidence": result.confidence,
                "ocr_method": "surya" if requested_engine == "surya" else result.method,
                "ocr_rechecked": result.rechecked,
                "ocr_needs_review": result.needs_review or quality_review,
            })
        record = db.get(CaseAnalysis, case_id)
        current = dict(record.result or {})
        current["ocr_pages"] = list(metrics.values())
        _save_analysis(db, case_id, current)
        db.commit()
        logger.info("ocr_page_done case=%s page=%s method=%s status=%s elapsed=%.2fs",
                    case_id, page.sequence, metrics[page.sequence].get("ocr_method"), page.status, elapsed)

    if requested_engine != "surya":
        # Paddle's local worker already handles one page at a time. Persist each
        # result immediately so later pages cannot hide completed OCR output.
        for page, path in zip(accepted, paths):
            _raise_if_cancelled(case_id)
            started = time.perf_counter()
            metrics[page.sequence]["ocr_started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                results = processor([path], cancel_check=lambda: case_cancelled(case_id))
                if len(results) != 1:
                    raise RuntimeError("OCR returned an incomplete page")
                save_page(page, started, result=results[0])
            except CaseCancelled:
                raise
            except Exception as error:
                logger.exception("OCR failed for case=%s page=%s", case_id, page.sequence)
                save_page(page, started, error=error)
        return

    started = time.perf_counter()
    try:
        results = processor(paths, cancel_check=lambda: case_cancelled(case_id))
        _raise_if_cancelled(case_id)
        if len(results) != len(accepted):
            raise RuntimeError("Surya returned an incomplete batch")
        for page, result in zip(accepted, results):
            save_page(page, started, result=result)
    except CaseCancelled:
        raise
    except Exception:
        logger.exception("Surya batch failed for case=%s; isolating approved pages", case_id)
        for page, path in zip(accepted, paths):
            _raise_if_cancelled(case_id)
            started = time.perf_counter()
            try:
                results = processor([path], cancel_check=lambda: case_cancelled(case_id))
                _raise_if_cancelled(case_id)
                if len(results) != 1:
                    raise RuntimeError("Surya returned an incomplete page")
                save_page(page, started, result=results[0])
            except Exception as error:
                save_page(page, started, error=error)


def handle_case(case_id: str) -> None:
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        if not case:
            return
        if case.status == "cancelled" or case_cancelled(case_id):
            logger.info("Skipping cancelled case=%s", case_id)
            return
        pages = db.scalars(select(Page).where(Page.case_id == case_id).order_by(Page.sequence)).all()
        previous = db.get(CaseAnalysis, case_id)
        requested_engine = previous.result.get("ocr_engine", "paddle") if previous else "paddle"
        metrics = {
            item["page"]: item
            for item in (previous.result.get("ocr_pages", []) if previous else [])
            if isinstance(item, dict) and "page" in item
        }
        started_at = datetime.now(timezone.utc).isoformat()
        _save_analysis(db, case_id, {
            "documents": [], "ocr_pages": list(metrics.values()),
            "ocr_started_at": started_at,
        })
        case.status = "processing"
        db.commit()
        pending = [page for page in pages if page.extracted_text is None]
        for start in range(0, len(pending), OCR_BATCH_SIZE):
            _raise_if_cancelled(case_id)
            _process_batch(db, case_id, pending[start:start + OCR_BATCH_SIZE], metrics, requested_engine)
        _raise_if_cancelled(case_id)
        page_inputs: list[dict] = []
        for page in pages:
            if page.status == "processed" and page.extracted_text is not None:
                ocr = metrics.get(page.sequence, {})
                page_inputs.append({
                    "page": page.sequence, "name": page.original_name, "text": page.extracted_text,
                    "ocr_confidence": ocr.get("ocr_confidence"),
                    "ocr_method": ocr.get("ocr_method"),
                    "ocr_rechecked": ocr.get("ocr_rechecked", False),
                    "ocr_needs_review": ocr.get("ocr_needs_review", False),
                })
        timing = {
            "ocr_started_at": started_at,
            "ocr_finished_at": datetime.now(timezone.utc).isoformat(),
        }

        if not page_inputs:
            case.status = "needs_review" if any(page.status == "quality_review" for page in pages) else "failed"
            _save_analysis(db, case_id, {
                "documents": [], "ocr_pages": list(metrics.values()),
                "error": "No pages eligible for extraction; inspect quality reports and errors", "review_required": True, **timing,
            })
            db.commit()
            return

        try:
            categories = set(db.scalars(select(ApprovedCategory.name)).all())
            def save_groups(proposal):
                _raise_if_cancelled(case_id)
                grouped = validate_proposal(proposal, page_inputs, categories)
                grouped.update(timing)
                grouped.update({"ocr_pages": list(metrics.values()), "analysis_stage": "extracting_fields"})
                for page in pages:
                    related = [doc for doc in grouped["documents"] if page.sequence in doc["pages"]]
                    if related:
                        page.document_type = related[0]["category"]
                        page.classification_confidence = related[0]["confidence"]
                _save_analysis(db, case_id, grouped)
                db.commit()

            _save_analysis(db, case_id, {"documents": [], "ocr_pages": list(metrics.values()), **timing,
                                         "analysis_stage": "classifying", "analysis_started_at": datetime.now(timezone.utc).isoformat()})
            db.commit()
            logger.info("AI classification started for case=%s", case_id)
            proposal = propose_documents(page_inputs, on_grouped=save_groups)
            _raise_if_cancelled(case_id)
            result = validate_proposal(proposal, page_inputs, categories)
            result.update(timing)
            result["analysis_stage"] = "completed"
            if proposal.get("field_errors"):
                result["field_errors"] = proposal["field_errors"]
                result["review_required"] = True
            result["ocr_pages"] = list(metrics.values())
            failed_pages = [page.sequence for page in pages if page.status in {"failed", "quality_review"}]
            if failed_pages:
                result["failed_pages"] = failed_pages
                result["review_required"] = True
            if any(item["ocr_needs_review"] for item in page_inputs):
                result["review_required"] = True
            for page in pages:
                related = [doc for doc in result["documents"] if page.sequence in doc["pages"]]
                if related:
                    page.document_type = related[0]["category"]
                    page.classification_confidence = related[0]["confidence"]
                    page.review_required = related[0]["review_required"] or metrics.get(page.sequence, {}).get("ocr_needs_review", False)
            _save_analysis(db, case_id, result)
            case.status = "needs_review" if result["review_required"] or any(page.review_required for page in pages) else "completed"
        except CaseCancelled:
            raise
        except AnalysisUnavailable as error:
            _save_analysis(db, case_id, {"documents": [], "ocr_pages": list(metrics.values()), "error": str(error), "analysis_stage": "failed", "review_required": True, **timing})
            case.status = "awaiting_ai_configuration"
            logger.warning("Analysis unavailable for case=%s: %s", case_id, error)
        except Exception as error:
            logger.exception("Analysis failed for case=%s", case_id)
            _save_analysis(db, case_id, {"documents": [], "ocr_pages": list(metrics.values()), "error": str(error), "review_required": True, **timing})
            case.status = "failed"
        db.commit()


def run() -> None:
    redis_client = client()
    logger.info("Case worker started; queue=%s", QUEUE_NAME)
    while True:
        popped = redis_client.blpop([QUEUE_NAME, EXTRACTION_QUEUE_NAME], timeout=1)
        if not popped:
            continue
        _, raw_job = popped
        try:
            payload = json.loads(raw_job)
            if "job_id" in payload:
                handle_extraction_job(payload["job_id"])
            else:
                handle_case(payload["case_id"])
        except CaseCancelled as error:
            logger.info("%s", error)
        except Exception:
            logger.exception("Unexpected case job failure: %s", raw_job)


if __name__ == "__main__":
    run()
