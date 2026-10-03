"""Local PaddleOCR worker for primary reads and legacy side-by-side comparisons."""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import (PADDLE_HEARTBEAT_KEY, PADDLE_PRIMARY_QUEUE_NAME,
                        PADDLE_QUEUE_NAME, STORAGE_PATH)
from app.database import SessionLocal
from app.models import CaseAnalysis, Page
from app.queue import client
from app.services.comparisons import update_paddle
from app.services.paddle_ocr import MODEL_NAME, PaddleOcrEngine
from app.services.quality_gate import prepare_ocr_input

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_model: PaddleOcrEngine | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _heartbeat() -> None:
    redis = client()
    while True:
        try:
            redis.setex(PADDLE_HEARTBEAT_KEY, 15, "ready")
        except Exception:
            logger.exception("Paddle comparison heartbeat could not reach Redis")
        time.sleep(5)


def handle_comparison(case_id: str, page_number: int) -> None:
    global _model
    with SessionLocal() as db:
        page = db.scalar(select(Page).where(Page.case_id == case_id, Page.sequence == page_number))
        analysis = db.get(CaseAnalysis, case_id, with_for_update=True)
        if not page or not analysis or page.status != "processed":
            logger.warning("Skipped comparison for missing or unprocessed case=%s page=%s", case_id, page_number)
            return
        path = Path(STORAGE_PATH) / page.storage_key
        if not path.is_file():
            update_paddle(analysis, page_number, status="failed", error="Original image is unavailable")
            db.commit()
            return
        update_paddle(analysis, page_number, status="processing", started_at=_now(), error=None)
        db.commit()

    started = time.perf_counter()
    path, quality_report = prepare_ocr_input(path)
    if path is None:
        with SessionLocal() as db:
            analysis = db.get(CaseAnalysis, case_id, with_for_update=True)
            if analysis:
                update_paddle(analysis, page_number, status="failed", quality_gate=quality_report,
                              error="Quality gate held this comparison for manual review", completed_at=_now())
                db.commit()
        return
    try:
        if _model is None:
            _model = PaddleOcrEngine()
        reading = _model.read(path)
        values = {
            "status": "completed", "model": MODEL_NAME,
            "text": reading.text, "confidence": reading.confidence,
            "lines": reading.lines, "elapsed_seconds": round(time.perf_counter() - started, 2),
            "completed_at": _now(), "error": None,
            "quality_gate": quality_report,
        }
    except Exception:
        logger.exception("Paddle comparison failed for case=%s page=%s", case_id, page_number)
        values = {
            "status": "failed", "model": MODEL_NAME,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "completed_at": _now(), "error": "Local PaddleOCR comparison failed; inspect worker logs",
            "quality_gate": quality_report,
        }
    with SessionLocal() as db:
        analysis = db.get(CaseAnalysis, case_id, with_for_update=True)
        if analysis:
            update_paddle(analysis, page_number, **values)
            db.commit()


def handle_primary(payload: dict) -> None:
    global _model
    reply_key = payload["reply_key"]
    if time.time() > float(payload["expires_at"]):
        return
    try:
        root = Path(STORAGE_PATH).resolve()
        path = (root / payload["storage_key"]).resolve()
        path.relative_to(root)
        if not path.is_file():
            raise FileNotFoundError("Original image is unavailable")
        if _model is None:
            _model = PaddleOcrEngine()
        reading = _model.read(path)
        response = {"text": reading.text, "confidence": reading.confidence,
                    "lines": reading.lines, "model": MODEL_NAME}
    except Exception:
        logger.exception("Paddle primary read failed")
        response = {"error": "Local Paddle OCR failed"}
    redis = client()
    redis.rpush(reply_key, json.dumps(response))
    redis.expire(reply_key, 300)


def run() -> None:
    threading.Thread(target=_heartbeat, name="paddle-heartbeat", daemon=True).start()
    redis = client()
    logger.info("Paddle worker started; primary=%s comparison=%s", PADDLE_PRIMARY_QUEUE_NAME, PADDLE_QUEUE_NAME)
    while True:
        try:
            job = redis.blpop([PADDLE_PRIMARY_QUEUE_NAME, PADDLE_QUEUE_NAME], timeout=10)
            if job:
                payload = json.loads(job[1])
                if job[0] == PADDLE_PRIMARY_QUEUE_NAME:
                    handle_primary(payload)
                else:
                    handle_comparison(payload["case_id"], int(payload["page"]))
        except Exception:
            logger.exception("Unexpected Paddle comparison worker error")
            time.sleep(2)


if __name__ == "__main__":
    run()
