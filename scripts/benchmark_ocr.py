"""Benchmark Surya 2 OCR modes on a non-sensitive image.

Usage:
    python -m scripts.benchmark_ocr work/surya-smoke.png \
        --expected tests/fixtures/surya-smoke.txt --modes layout full_page

This bypasses the case queue and never changes stored uploads or model weights.
The expected text is used only for local metrics and is not printed.
"""

import argparse
import re
import time
from pathlib import Path

from PIL import Image, ImageOps
import cv2
import numpy as np

from app.services.surya_ocr import SuryaOcrEngine
from app.services.quality_gate import process_image


def normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def edit_distance(reference: list[str] | str, candidate: list[str] | str) -> int:
    previous = list(range(len(candidate) + 1))
    for index, left in enumerate(reference, start=1):
        current = [index]
        for column, right in enumerate(candidate, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left != right),
            ))
        previous = current
    return previous[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Surya OCR latency and optional normalized error rates")
    parser.add_argument("image", type=Path)
    parser.add_argument("--expected", type=Path, help="Ground-truth transcription of this image")
    parser.add_argument("--modes", nargs="+", choices=("layout", "full_page"), default=["layout", "full_page"])
    parser.add_argument("--quality-gate", action="store_true", help="Gate in memory before model loading; omit only for explicit raw-image benchmarks")
    args = parser.parse_args()
    if not args.image.is_file():
        parser.error(f"Image not found: {args.image}")
    expected = normalize(args.expected.read_text(encoding="utf-8")) if args.expected else None
    with Image.open(args.image) as source:
        if getattr(source, "n_frames", 1) != 1:
            parser.error("Split multi-frame inputs into separate pages before benchmarking")
        image = ImageOps.exif_transpose(source).convert("RGB")
    try:
        if args.quality_gate:
            output, report = process_image(cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR))
            print(f"quality={report['decision']} gate_seconds={report['elapsed_seconds']} enhancements={report['enhancements']}", flush=True)
            if report["decision"] == "flagged_for_review":
                print(f"held={report['failure_reasons']}")
                return 1
            image.close()
            image = Image.fromarray(cv2.cvtColor(output, cv2.COLOR_BGR2RGB))
        engine = SuryaOcrEngine()
        print(f"pixels={image.width}x{image.height} modes={','.join(args.modes)}")
        for mode in args.modes:
            start = time.perf_counter()
            result = engine.read_batch([image], mode=mode)[0]
            duration = time.perf_counter() - start
            output = normalize(result.text)
            line = f"mode={mode} seconds={duration:.2f} chars={len(output)} score={result.confidence:.3f}"
            if expected is not None:
                cer = edit_distance(expected, output) / max(1, len(expected))
                reference_words, output_words = expected.split(), output.split()
                wer = edit_distance(reference_words, output_words) / max(1, len(reference_words))
                line += f" cer={cer:.3f} wer={wer:.3f}"
            print(line, flush=True)
    finally:
        image.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
