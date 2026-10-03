"""OCR adapters for accepted quality-gate inputs; no handwriting second model."""
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.config import OCR_MIN_CHARS, OCR_RECHECK_CONFIDENCE
from app.queue import CaseCancelled, request_paddle_ocr
from app.services.surya_ocr import OcrResult, SuryaOcrEngine

_engine: SuryaOcrEngine | None = None
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessedPage:
    text: str
    confidence: float
    method: str
    rechecked: bool
    needs_review: bool


def _get_engine():
    global _engine
    if _engine is None:
        _engine = SuryaOcrEngine()
    return _engine


def _character_count(result):
    return sum(character.isalnum() for character in result.text)


def _weak(result):
    return result.confidence < OCR_RECHECK_CONFIDENCE or _character_count(result) < OCR_MIN_CHARS


def _prefer_recheck(primary, recheck):
    if not recheck.text.strip():
        return False
    if not primary.text.strip():
        return True
    return recheck.confidence >= primary.confidence + .03 or (
        _character_count(primary) < OCR_MIN_CHARS
        and _character_count(recheck) > _character_count(primary)
        and recheck.confidence >= primary.confidence)


def process_images(paths: list[Path], cancel_check=None) -> list[ProcessedPage]:
    """Surya extraction/recheck on the same already-approved input pixels."""
    images = []
    try:
        for path in paths:
            with Image.open(path) as source:
                images.append(source.convert("RGB"))
        if not images:
            return []
        engine = _get_engine()
        done = threading.Event()
        if cancel_check:
            if cancel_check():
                raise CaseCancelled("OCR run cancelled by user")
            def watch_cancel():
                while not done.wait(.25):
                    if cancel_check():
                        try:
                            engine.cancel_current()
                        except Exception:
                            logger.exception("Could not close the active Surya request")
                        return
            threading.Thread(target=watch_cancel, name="surya-cancel-watch", daemon=True).start()
        try:
            primary = engine.read_batch(images, mode="layout")
            if cancel_check and cancel_check():
                raise CaseCancelled("OCR run cancelled by user")
        except Exception as error:
            if cancel_check and cancel_check():
                raise CaseCancelled("OCR run cancelled by user") from error
            raise
        if len(primary) != len(images):
            raise RuntimeError("Surya returned an incomplete batch")
        indices = [i for i, result in enumerate(primary) if _weak(result)]
        try:
            rechecks = engine.read_batch([images[i] for i in indices], mode="full_page") if indices else []
            if cancel_check and cancel_check():
                raise CaseCancelled("OCR run cancelled by user")
        except Exception as error:
            if cancel_check and cancel_check():
                raise CaseCancelled("OCR run cancelled by user") from error
            raise
        if len(rechecks) != len(indices):
            raise RuntimeError("Surya returned an incomplete recheck")
        selected = list(primary)
        methods = ["layout"] * len(images)
        for i, candidate in zip(indices, rechecks):
            if _prefer_recheck(primary[i], candidate):
                selected[i] = candidate
                methods[i] = "full_page_recheck"
        return [ProcessedPage(result.text, result.confidence, methods[i], i in indices, _weak(result))
                for i, result in enumerate(selected)]
    finally:
        if 'done' in locals():
            done.set()
        for image in images:
            image.close()


def process_images_primary(paths: list[Path], cancel_check=None) -> list[ProcessedPage]:
    """Paddle first; Surya fallback stays on the same approved image, never original."""
    selected = [None] * len(paths)
    fallback_indices = []
    for i, path in enumerate(paths):
        try:
            response = request_paddle_ocr(path.parent.name + "/" + path.name, cancel_check=cancel_check)
            text = str(response.get("text") or "").strip()
            if not text:
                raise ValueError("Paddle returned no text")
            score = float(response.get("confidence") or 0)
            weak = _weak(OcrResult(text, score))
            selected[i] = ProcessedPage(text, score, "paddle", False, weak)
        except CaseCancelled:
            raise
        except Exception as error:
            logger.warning("Paddle unavailable for page index=%s (%s); trying Surya", i, type(error).__name__)
            fallback_indices.append(i)
    if fallback_indices:
        fallback_paths = [paths[i] for i in fallback_indices]
        fallback = process_images(fallback_paths, cancel_check=cancel_check) if cancel_check else process_images(fallback_paths)
        if len(fallback) != len(fallback_indices):
            raise RuntimeError("Surya returned an incomplete fallback batch")
        for i, result in zip(fallback_indices, fallback):
            selected[i] = ProcessedPage(result.text, result.confidence, "surya_fallback", result.rechecked, True)
    if any(result is None for result in selected):
        raise RuntimeError("An OCR page was not processed")
    return selected
