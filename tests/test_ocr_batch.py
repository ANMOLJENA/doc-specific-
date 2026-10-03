import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services.processing import ProcessedPage, process_images, process_images_primary
from app.services.surya_ocr import OcrResult


class FakeSurya:
    def __init__(self, recheck: OcrResult):
        self.calls = []
        self.recheck = recheck

    def read_batch(self, images, mode="layout"):
        self.calls.append((mode, len(images), [image.size for image in images]))
        if mode == "layout":
            return [
                OcrResult("Invoice number A-100", .93),
                OcrResult("0-1", .48),
                OcrResult("Policy number P-441", .91),
            ]
        return [self.recheck]


class BatchOcrTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.paths = [Path(self.folder.name) / f"page-{i}.png" for i in range(3)]
        for path in self.paths:
            Image.new("RGB", (80, 50), "white").save(path)

    def tearDown(self):
        self.folder.cleanup()

    def test_only_weak_page_is_rechecked_on_original_image(self):
        before = [path.read_bytes() for path in self.paths]
        engine = FakeSurya(OcrResult("0-1-1 for five days", .88))
        with patch("app.services.processing._engine", engine):
            results = process_images(self.paths)
        self.assertEqual(engine.calls, [
            ("layout", 3, [(80, 50)] * 3),
            ("full_page", 1, [(80, 50)]),
        ])
        self.assertEqual(results[1].text, "0-1-1 for five days")
        self.assertEqual(results[1].method, "full_page_recheck")
        self.assertTrue(results[1].rechecked)
        self.assertFalse(results[0].rechecked)
        self.assertEqual([path.read_bytes() for path in self.paths], before)

    def test_weaker_second_pass_is_not_adopted(self):
        engine = FakeSurya(OcrResult("unreadable", .22))
        with patch("app.services.processing._engine", engine):
            results = process_images(self.paths)
        self.assertEqual(results[1].text, "0-1")
        self.assertEqual(results[1].method, "layout")
        self.assertTrue(results[1].needs_review)

    def test_paddle_primary_does_not_call_surya_when_text_is_returned(self):
        with patch("app.services.processing.request_paddle_ocr", return_value={"text": "Sample document text", "confidence": 0.91}), patch(
            "app.services.processing.process_images", side_effect=AssertionError("Surya must not run")
        ):
            results = process_images_primary(self.paths[:1])
        self.assertEqual(results[0].method, "paddle")
        self.assertEqual(results[0].text, "Sample document text")
        self.assertFalse(results[0].needs_review)

    def test_only_empty_paddle_page_uses_surya_fallback(self):
        surya_result = ProcessedPage("Fallback text", 0.9, "layout", False, False)
        with patch("app.services.processing.request_paddle_ocr", side_effect=[
            {"text": "First page text", "confidence": 0.9},
            {"text": "", "confidence": None},
        ]), patch("app.services.processing.process_images", return_value=[surya_result]) as surya:
            results = process_images_primary(self.paths[:2])
        surya.assert_called_once_with([self.paths[1]])
        self.assertEqual([page.method for page in results], ["paddle", "surya_fallback"])
        self.assertEqual(results[1].text, "Fallback text")
        self.assertTrue(results[1].needs_review)

    def test_paddle_failure_uses_surya_fallback(self):
        surya_result = ProcessedPage("Fallback text", 0.9, "layout", False, False)
        with patch("app.services.processing.request_paddle_ocr", side_effect=TimeoutError("test timeout")), patch(
            "app.services.processing.process_images", return_value=[surya_result]
        ):
            result = process_images_primary(self.paths[:1])[0]
        self.assertEqual(result.method, "surya_fallback")


if __name__ == "__main__":
    unittest.main()
