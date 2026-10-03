"""Deterministic label-image gate. Scores are heuristics, NOT OCR accuracy.

Arrays use OpenCV BGR/grayscale uint8 convention. Originals are never mutated.
Defaults are uncalibrated starting points; record config with every decision.
"""

import hashlib
import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)
VERSION = "label-quality-v1"


@dataclass(frozen=True)
class QualityConfig:
    blur_min: float = 100.0
    contrast_min: float = 20.0
    skew_max: float = 3.0
    noise_max: float = 8.0
    text_height_min: float = 12.0
    min_side: int = 200
    min_text_components: int = 5
    max_attempts: int = 1
    score_max_side: int = 1000
    noise_sample_side: int = 640
    denoise_h: float = 7.0
    clahe_clip: float = 2.0
    clahe_tile: int = 8
    sharpen_amount: float = 1.0
    max_skew_correction: float = 30.0
    upscale_max_factor: float = 4.0
    upscale_max_side: int = 3000
    upscale_margin: float = 1.35
    denoise_enabled: bool = True
    deskew_enabled: bool = True
    contrast_enabled: bool = True
    sharpen_enabled: bool = True
    upscale_enabled: bool = True
    calibration_id: str = "uncalibrated"

    def __post_init__(self):
        numeric = [self.blur_min, self.contrast_min, self.skew_max, self.noise_max,
                   self.text_height_min, self.denoise_h, self.clahe_clip,
                   self.sharpen_amount, self.max_skew_correction, self.upscale_max_factor,
                   self.upscale_margin]
        if any(not math.isfinite(v) or v < 0 for v in numeric):
            raise ValueError("Quality thresholds must be finite and nonnegative")
        if not 0 <= self.max_attempts <= 3 or min(self.min_side, self.min_text_components,
                self.score_max_side, self.noise_sample_side, self.clahe_tile) < 1:
            raise ValueError("Invalid quality gate limits")
        if self.upscale_max_factor < 1 or self.upscale_max_side < 1 or self.upscale_margin < 1:
            raise ValueError("Invalid quality gate upscale limits")

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        for name, default in asdict(defaults).items():
            raw = os.getenv("QUALITY_" + name.upper())
            if raw is not None:
                if isinstance(default, bool):
                    if raw.lower() not in {"1", "0", "true", "false"}:
                        raise ValueError(f"Invalid QUALITY_{name.upper()} boolean")
                    values[name] = raw.lower() in {"1", "true"}
                else:
                    values[name] = type(default)(raw)
        return cls(**values)


def _gray(image):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.size == 0:
        raise ValueError("Expected a nonempty uint8 image")
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    raise ValueError("Expected grayscale or BGR image")


def _sample(gray, side):
    scale = min(1.0, side / max(gray.shape))
    if scale == 1:
        return gray, scale
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA), scale


def estimate_skew(gray, config):
    sample, _ = _sample(gray, config.score_max_side)
    edges = cv2.Canny(sample, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=30,
                            minLineLength=max(20, int(sample.shape[1] * .06)), maxLineGap=12)
    candidates = []
    for x1, y1, x2, y2 in (lines[:, 0] if lines is not None else []):
        angle = math.degrees(math.atan2(int(y2-y1), int(x2-x1)))
        angle = (angle + 90) % 180 - 90
        if abs(angle) <= config.max_skew_correction:
            candidates.append((angle, math.hypot(int(x2-x1), int(y2-y1))))
    if len(candidates) < 2:
        return None, len(candidates)
    candidates.sort()
    midpoint = sum(length for _, length in candidates) / 2
    total = 0
    for angle, length in candidates:
        total += length
        if total >= midpoint:
            return round(angle, 3), len(candidates)


def estimate_text_height(gray, config):
    # Lower quartile character-component height targets small label text.
    # This is not a detector: logos/barcodes can fool it; calibrate on label ROIs.
    sample, scale = _sample(gray, config.score_max_side)
    _, mask = cv2.threshold(sample, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    heights = [h / scale for _, _, w, h, area in stats[1:]
               if 2 <= h <= sample.shape[0] * .12 and 1 <= w <= h * 3
               and area >= 3 and .08 <= area / (w*h) <= .95]
    if len(heights) < config.min_text_components:
        return None, len(heights)
    return round(float(np.percentile(heights, 25)), 2), len(heights)


def score_image_quality(image, config=None) -> dict:
    config = config or QualityConfig.from_env()
    gray = _gray(image)
    angle, line_count = estimate_skew(gray, config)
    text_height, component_count = estimate_text_height(gray, config)
    sample, _ = _sample(gray, config.noise_sample_side)
    denoised = cv2.fastNlMeansDenoising(sample, None, config.denoise_h, 7, 21)
    scores = {"blur": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 3),
              "contrast": round(float(gray.std()), 3), "skew": angle,
              "noise": round(float(cv2.absdiff(sample, denoised).mean()), 3),
              "text_height": text_height, "width": int(gray.shape[1]), "height": int(gray.shape[0]),
              "skew_line_count": line_count, "text_component_count": component_count}
    passed = {"blur": scores["blur"] >= config.blur_min,
              "contrast": scores["contrast"] >= config.contrast_min,
              "skew": angle is not None and abs(angle) <= config.skew_max,
              "noise": scores["noise"] <= config.noise_max,
              "resolution": text_height is not None and text_height >= config.text_height_min
                            and min(gray.shape) >= config.min_side}
    return {"scores": scores, "passes": passed, "passed": all(passed.values()),
            "failure_reasons": [name + ("_unmeasurable" if name == "skew" and angle is None
                or name == "resolution" and text_height is None else "_threshold")
                for name, value in passed.items() if not value],
            **{name + "_failed": not value for name, value in passed.items()}}


def deskew(image, angle):
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width/2, height/2), angle, 1.0)
    cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
    out_w, out_h = math.ceil(height*sine + width*cosine), math.ceil(height*cosine + width*sine)
    matrix[0, 2] += (out_w-width)/2
    matrix[1, 2] += (out_h-height)/2
    return cv2.warpAffine(image, matrix, (out_w, out_h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def enhance_image(image, quality_report, config=None):
    config = config or QualityConfig.from_env()
    output = image.copy()
    applied = []
    if quality_report["noise_failed"] and config.denoise_enabled:
        if output.ndim == 2:
            output = cv2.fastNlMeansDenoising(output, None, config.denoise_h, 7, 21)
        else:
            output = cv2.fastNlMeansDenoisingColored(output, None, config.denoise_h, config.denoise_h, 7, 21)
        applied.append("denoise")
    angle = quality_report["scores"]["skew"]
    if quality_report["skew_failed"] and angle is not None and config.deskew_enabled:
        output = deskew(output, angle)
        applied.append("deskew")
    if quality_report["contrast_failed"] and config.contrast_enabled:
        clahe = cv2.createCLAHE(clipLimit=config.clahe_clip, tileGridSize=(config.clahe_tile,)*2)
        if output.ndim == 2:
            output = clahe.apply(output)
        else:
            lab = cv2.cvtColor(output, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            output = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        applied.append("clahe")
    if quality_report["resolution_failed"] and config.upscale_enabled:
        text_height = quality_report["scores"]["text_height"]
        height, width = output.shape[:2]
        factors = [1.0, config.min_side / min(height, width)]
        if text_height:
            factors.append(config.text_height_min / text_height)
        desired = max(factors)
        factor = min(config.upscale_max_factor, config.upscale_max_side / max(height, width),
                     desired * config.upscale_margin if desired > 1 else 1)
        if factor > 1.01:
            output = cv2.resize(output, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            applied.append(f"upscale_{factor:.2f}x")
    if quality_report["blur_failed"] and config.sharpen_enabled:
        smooth = cv2.GaussianBlur(output, (0, 0), 1.0)
        output = cv2.addWeighted(output, 1 + config.sharpen_amount, smooth, -config.sharpen_amount, 0)
        applied.append("sharpen")
    return output, applied


def process_image(image, config=None):
    config = config or QualityConfig.from_env()
    started = time.perf_counter()
    original = score_image_quality(image, config)
    output = image.copy()
    final = original
    attempts = []
    if not original["passed"]:
        for attempt in range(config.max_attempts):
            output, applied = enhance_image(output, final, config)
            final = score_image_quality(output, config)
            # Laplacian variance is scale-dependent: interpolation lowers it even
            # when the native source was sharp. Preserve the native blur pass and
            # expose that decision instead of manufacturing edges to beat a score.
            if original["passes"]["blur"] and any(op.startswith("upscale_") for op in applied):
                final["passes"]["blur"] = True
                final["blur_failed"] = False
                final["failure_reasons"] = [reason for reason in final["failure_reasons"] if reason != "blur_threshold"]
                final["passed"] = all(final["passes"].values())
                final.setdefault("score_notes", {})["blur"] = "native_source_pass_preserved_after_upscale"
            attempts.append({"attempt": attempt+1, "applied": applied, "scores": final})
            if final["passed"] or not applied:
                break
    decision = "passed_direct" if original["passed"] else "passed_after_enhancement" if final["passed"] else "flagged_for_review"
    source_resolution_warning = not original["passes"]["resolution"]
    warnings = []
    if source_resolution_warning:
        warnings.append("source_resolution_was_below_threshold")
    if not final["passes"]["resolution"]:
        warnings.append("resolution_recheck_failed")
    if decision == "flagged_for_review":
        warnings.append("quality_recheck_failed_ocr_forwarded")
    report = {"version": VERSION, "calibration": config.calibration_id, "config": asdict(config),
              "original": original, "post_enhancement": final, "attempts": attempts,
              "enhancements": [op for item in attempts for op in item["applied"]],
              "decision": decision, "failure_reasons": final["failure_reasons"],
              "warnings": warnings,
              "review_recommended": source_resolution_warning or decision == "flagged_for_review",
              "elapsed_seconds": round(time.perf_counter()-started, 4)}
    return output, report


def prepare_ocr_input(path: Path, config=None):
    """Return accepted original/derived path or None plus auditable telemetry."""
    started = time.perf_counter()
    report = {}
    try:
        with Image.open(path) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("Multi-frame images must be split into separate pages")
            original_rgb = np.array(source.convert("RGB"))
            rgb = np.array(ImageOps.exif_transpose(source).convert("RGB"))
            normalized = not np.array_equal(original_rgb, rgb)
        image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        output, report = process_image(image, config)
        report["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        accepted_path = path
        if report["enhancements"] or normalized:
            accepted_path = path.with_name(f"quality-{uuid.uuid4().hex}.png")
            if not cv2.imwrite(str(accepted_path), output):
                raise OSError("Could not save gated OCR input")
            report["derived_storage_key"] = accepted_path.parent.name + "/" + accepted_path.name
        report["exif_normalized"] = normalized
        report["forwarded_to_ocr"] = True
        report["forwarded_with_quality_warning"] = report["decision"] == "flagged_for_review"
        report["ocr_input_sha256"] = hashlib.sha256(accepted_path.read_bytes()).hexdigest() if accepted_path else None
    except Exception as error:
        logger.warning("Quality gate input error (%s)", type(error).__name__)
        accepted_path = None
        report.update({"version": VERSION, "decision": "flagged_for_review",
                       "failure_reasons": report.get("failure_reasons", []) + ["quality_check_error:" + type(error).__name__],
                       "elapsed_seconds": round(time.perf_counter()-started, 4)})
        report.setdefault("enhancements", [])
        report["forwarded_to_ocr"] = False
        report["forwarded_with_quality_warning"] = False
        report.pop("derived_storage_key", None)
        report["ocr_input_sha256"] = None
    logger.info("quality_gate %s", json.dumps(report, allow_nan=False))
    return accepted_path, report
