"""Print Surya OCR from an image available inside the worker container."""

import sys
from pathlib import Path

from app.services.processing import process_images
from app.services.quality_gate import prepare_ocr_input


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/smoke_ocr.py /app/image.png", file=sys.stderr)
        return 2
    image_path = Path(sys.argv[1])
    if not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 2
    accepted, report = prepare_ocr_input(image_path)
    print(f"quality_decision={report['decision']} reasons={report['failure_reasons']}")
    if accepted is None:
        return 1
    result = process_images([accepted])[0]
    print(f"method={result.method} confidence={result.confidence} rechecked={result.rechecked} needs_review={result.needs_review}")
    print(result.text)
    return 0 if result.text.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
