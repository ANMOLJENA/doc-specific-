"""Local PaddleOCR comparison adapter; never changes primary case OCR text."""

from dataclasses import dataclass
from pathlib import Path


MODEL_NAME = "PP-OCRv6"


@dataclass(frozen=True)
class PaddleRead:
    text: str
    confidence: float | None
    lines: list[dict]


def _plain_result(result) -> dict:
    data = getattr(result, "json", None)
    if callable(data):
        data = data()
    if data is None and isinstance(result, dict):
        data = result
    if not isinstance(data, dict):
        raise ValueError("PaddleOCR returned an unsupported result")
    return data.get("res", data)


def _as_box(raw) -> list[int] | None:
    if raw is None:
        return None
    values = raw.tolist() if hasattr(raw, "tolist") else raw
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        return [int(value) for value in values]
    except (TypeError, ValueError):
        return None


def parse_prediction(results) -> PaddleRead:
    lines: list[dict] = []
    for result in results:
        page = _plain_result(result)
        texts = page.get("rec_texts", [])
        scores = page.get("rec_scores", [])
        boxes = page.get("rec_boxes", [])
        if hasattr(scores, "tolist"):
            scores = scores.tolist()
        if hasattr(boxes, "tolist"):
            boxes = boxes.tolist()
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                continue
            score = float(scores[index]) if index < len(scores) else None
            box = _as_box(boxes[index]) if index < len(boxes) else None
            lines.append({"text": text.strip(), "score": round(score, 4) if score is not None else None,
                          "bbox": box})
    confidences = [line["score"] for line in lines if line["score"] is not None]
    return PaddleRead(
        text="\n".join(line["text"] for line in lines),
        confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        lines=lines,
    )


class PaddleOcrEngine:
    """One lazy CPU model instance in the optional comparison worker."""

    def __init__(self):
        from paddleocr import PaddleOCR

        self.model = PaddleOCR(
            ocr_version=MODEL_NAME,
            device="cpu",
            engine="paddle",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    def read(self, path: Path) -> PaddleRead:
        return parse_prediction(self.model.predict(str(path)))
