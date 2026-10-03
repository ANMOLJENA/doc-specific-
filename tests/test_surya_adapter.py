import unittest
from types import SimpleNamespace

from app.services.surya_ocr import SuryaOcrEngine


class SuryaAdapterTests(unittest.TestCase):
    def test_orders_blocks_and_keeps_dosage_notation(self):
        prediction = SimpleNamespace(blocks=[
            SimpleNamespace(reading_order=1, html="<p>Take after food<br/>0-1-1 for 5 days</p>", confidence=.8, skipped=False, error=False),
            SimpleNamespace(reading_order=0, html="<h2>Prescription</h2>", confidence=.9, skipped=False, error=False),
            SimpleNamespace(reading_order=2, html="<p>unreadable</p>", confidence=.1, skipped=False, error=True),
        ])
        result = SuryaOcrEngine._result(prediction)
        self.assertEqual(result.text, "Prescription\nTake after food\n0-1-1 for 5 days")
        self.assertEqual(result.confidence, .85)


if __name__ == "__main__":
    unittest.main()
