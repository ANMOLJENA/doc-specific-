"""Score a labelled CSV manifest locally; no OCR or source-image mutation."""
import argparse
import csv
import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.services.quality_gate import QualityConfig, process_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="CSV columns: id,path,quality_label (good/bad)")
    parser.add_argument("--disable", nargs="*", choices=["denoise", "deskew", "contrast", "sharpen", "upscale"], default=[])
    parser.add_argument("--disable-all-enhancements", action="store_true")
    args = parser.parse_args()
    config = QualityConfig.from_env()
    config = replace(config, **{name + "_enabled": False for name in args.disable})
    if args.disable_all_enhancements:
        config = replace(config, max_attempts=0)
    with args.manifest.open(encoding="utf-8-sig", newline="") as manifest:
        for row in csv.DictReader(manifest):
            if not row.get("id") or row.get("quality_label") not in {"good", "bad"}:
                parser.error("Each row needs a non-sensitive id and quality_label good/bad")
            try:
                path = args.manifest.parent / row["path"]
                with Image.open(path) as source:
                    if getattr(source, "n_frames", 1) != 1:
                        raise ValueError("Split multi-frame images first")
                    image = cv2.cvtColor(np.array(ImageOps.exif_transpose(source).convert("RGB")), cv2.COLOR_RGB2BGR)
                _, report = process_image(image, config)
            except Exception as error:
                report = {"decision": "flagged_for_review", "failure_reasons": [type(error).__name__]}
            print(json.dumps({"id": row["id"], "quality_label": row["quality_label"], **report}, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
