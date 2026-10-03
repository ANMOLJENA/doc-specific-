import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from app.services.quality_gate import (QualityConfig, deskew, enhance_image,
                                      prepare_ocr_input, process_image, score_image_quality)


def sample_label():
    image = np.full((400, 800, 3), 255, np.uint8)
    for y, text in zip([75, 145, 215, 285, 350], [
        "SAMPLE LABEL", "Medicine 500 mg", "LOT A123 EXP 2028", "Dose 0-1-1", "Store below 25 C"]):
        cv2.putText(image, text, (40, y), cv2.FONT_HERSHEY_SIMPLEX, 1.25, (0, 0, 0), 2, cv2.LINE_AA)
    return image


class QualityGateTests(unittest.TestCase):
    def test_good_label_passes_without_changing_pixels(self):
        image = sample_label()
        before = image.copy()
        output, report = process_image(image)
        self.assertEqual(report["decision"], "passed_direct", report)
        self.assertEqual(report["enhancements"], [])
        np.testing.assert_array_equal(before, image)
        np.testing.assert_array_equal(before, output)

    def test_blur_contrast_noise_metrics_respond_to_degradation(self):
        image = sample_label()
        good = score_image_quality(image)["scores"]
        blurry = cv2.GaussianBlur(image, (21, 21), 6)
        flat = (image.astype(float) * .12 + 110).astype(np.uint8)
        noise = np.random.default_rng(42).normal(0, 8, image.shape)
        noisy = np.clip(image.astype(float) + noise, 0, 255).astype(np.uint8)
        self.assertLess(score_image_quality(blurry)["scores"]["blur"], good["blur"])
        self.assertLess(score_image_quality(flat)["scores"]["contrast"], good["contrast"])
        self.assertGreater(score_image_quality(noisy)["scores"]["noise"], good["noise"])

    def test_deskew_sign_and_recheck(self):
        tilted = deskew(sample_label(), -8)
        before = score_image_quality(tilted)
        self.assertTrue(before["skew_failed"], before)
        output, report = process_image(tilted)
        self.assertIn("deskew", report["enhancements"])
        self.assertEqual(report["decision"], "passed_after_enhancement")
        self.assertLess(abs(score_image_quality(output)["scores"]["skew"]), 3)

    def test_dispatch_applies_only_failed_metrics_in_order(self):
        image = sample_label()
        report = score_image_quality(image)
        for metric in ["blur", "contrast", "skew", "noise"]:
            report[metric + "_failed"] = True
        report["scores"]["skew"] = 5
        _, operations = enhance_image(image, report)
        self.assertEqual(operations, ["denoise", "deskew", "clahe", "sharpen"])
        report = score_image_quality(image)
        report["contrast_failed"] = True
        _, operations = enhance_image(image, report)
        self.assertEqual(operations, ["clahe"])

    def test_persistently_bad_image_is_held_after_one_attempt(self):
        image = np.full((400, 800, 3), 180, np.uint8)
        _, report = process_image(image)
        self.assertEqual(report["decision"], "flagged_for_review")
        self.assertEqual(len(report["attempts"]), 1)
        self.assertIn("resolution_unmeasurable", report["failure_reasons"])

    def test_valid_but_failed_quality_image_is_forwarded_with_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "flat.png"
            cv2.imwrite(str(source), np.full((400, 800, 3), 180, np.uint8))
            accepted, report = prepare_ocr_input(source)
            self.assertIsNotNone(accepted)
            self.assertTrue(accepted.is_file())
            self.assertEqual(report["decision"], "flagged_for_review")
            self.assertTrue(report["forwarded_to_ocr"])
            self.assertTrue(report["forwarded_with_quality_warning"])
            self.assertTrue(report["review_recommended"])

    def test_resolution_beyond_upscale_limit_is_still_held(self):
        image = sample_label()
        config = replace(QualityConfig(), text_height_min=100)
        _, report = process_image(image, config)
        self.assertEqual(report["decision"], "flagged_for_review")
        self.assertIn("resolution_threshold", report["failure_reasons"])
        self.assertTrue(any(item.startswith("upscale_") for item in report["enhancements"]))

    def test_low_resolution_is_upscaled_rechecked_and_marked_for_verification(self):
        image = cv2.resize(sample_label(), None, fx=.4, fy=.4, interpolation=cv2.INTER_AREA)
        output, report = process_image(image)
        self.assertEqual(report["decision"], "passed_after_enhancement", report)
        self.assertTrue(any(item.startswith("upscale_") for item in report["enhancements"]))
        self.assertGreater(output.shape[0], image.shape[0])
        self.assertTrue(report["post_enhancement"]["passes"]["resolution"])
        self.assertTrue(report["review_recommended"])
        self.assertIn("source_resolution_was_below_threshold", report["warnings"])

    def test_accepted_derivative_does_not_overwrite_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "original.png"
            image = sample_label()
            cv2.imwrite(str(source), image)
            original = source.read_bytes()
            report = {"decision": "passed_after_enhancement", "failure_reasons": [], "enhancements": ["clahe"]}
            with patch("app.services.quality_gate.process_image", return_value=(image, report)):
                path, saved = prepare_ocr_input(source)
            self.assertNotEqual(path, source)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(path.is_file())
            self.assertIn("source_sha256", saved)

    def test_missing_or_invalid_image_is_held(self):
        path, report = prepare_ocr_input(Path("missing-test-image.png"))
        self.assertIsNone(path)
        self.assertEqual(report["decision"], "flagged_for_review")

    def test_save_failure_retains_scores_and_does_not_forward_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "original.png"
            cv2.imwrite(str(source), sample_label())
            original_bytes = source.read_bytes()
            enhanced, report = process_image(deskew(sample_label(), -8))
            with patch("app.services.quality_gate.process_image", return_value=(enhanced, report)), \
                 patch("app.services.quality_gate.cv2.imwrite", return_value=False):
                path, saved = prepare_ocr_input(source)
            self.assertIsNone(path)
            self.assertEqual(saved["decision"], "flagged_for_review")
            self.assertIn("original", saved)
            self.assertIn("quality_check_error:OSError", saved["failure_reasons"])
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_disabled_operation_does_not_disable_gate(self):
        config = replace(QualityConfig(), contrast_min=200, contrast_enabled=False)
        _, report = process_image(sample_label(), config)
        self.assertEqual(report["decision"], "flagged_for_review")
        self.assertNotIn("clahe", report["enhancements"])

    def test_threshold_config_validation(self):
        with self.assertRaises(ValueError):
            QualityConfig(blur_min=float("nan"))
        with self.assertRaises(ValueError):
            QualityConfig(max_attempts=100)


if __name__ == "__main__":
    unittest.main()
