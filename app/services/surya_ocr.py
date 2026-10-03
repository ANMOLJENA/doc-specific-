"""Surya 2 adapter, isolated from the rest of the processing pipeline."""

import html
import re
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float


def _html_to_text(content: str) -> str:
    content = re.sub(r"<(?:br|/p|/div|/tr|/li|/h[1-6])\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", " ", content)
    return re.sub(r"[ \t]+", " ", html.unescape(content)).strip()


class SuryaOcrEngine:
    """One long-lived Surya manager per worker process."""

    def __init__(self) -> None:
        try:
            from surya.inference import SuryaInferenceManager
            from surya.layout import LayoutPredictor
            from surya.recognition import RecognitionPredictor
        except ImportError as error:
            raise RuntimeError("Surya 2 is not installed. Rebuild the OCR worker image.") from error

        self.manager = SuryaInferenceManager()
        self.layout = LayoutPredictor(self.manager)
        self.recognition = RecognitionPredictor(self.manager)

    @staticmethod
    def _result(prediction) -> OcrResult:
        blocks = sorted(getattr(prediction, "blocks", []), key=lambda block: getattr(block, "reading_order", 0))
        readable = [block for block in blocks if not getattr(block, "skipped", False) and not getattr(block, "error", False)]
        text = "\n".join(plain for block in readable if (plain := _html_to_text(getattr(block, "html", ""))))
        confidences = [float(getattr(block, "confidence", 0.0)) for block in readable]
        return OcrResult(text=text, confidence=round(sum(confidences) / len(confidences), 3) if confidences else 0.0)

    def read_batch(self, images: list[Image.Image], mode: str = "layout") -> list[OcrResult]:
        if not images:
            return []
        if mode == "layout":
            layouts = self.layout(images)
            predictions = self.recognition(images, layouts)
        elif mode == "full_page":
            predictions = self.recognition(images)
        else:
            raise ValueError(f"Unsupported Surya OCR mode: {mode}")
        if len(predictions) != len(images):
            raise RuntimeError("Surya returned a different number of pages than requested")
        return [self._result(prediction) for prediction in predictions]

    def cancel_current(self) -> None:
        """Close the active HTTP client so llama.cpp cancels its current task."""
        backend = getattr(self.manager, "backend", None)
        active_client = getattr(backend, "_client", None)
        if active_client is not None:
            active_client.close()
        self.manager.stop()
