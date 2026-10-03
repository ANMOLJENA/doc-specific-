import unittest

from scripts.benchmark_ocr import edit_distance, normalize


class BenchmarkMetricTests(unittest.TestCase):
    def test_normalize_ignores_case_and_punctuation(self):
        self.assertEqual(normalize("Dosage: 0-1-1!"), "dosage 0 1 1")

    def test_character_distance(self):
        self.assertEqual(edit_distance("tablet", "tablets"), 1)

    def test_word_distance(self):
        self.assertEqual(edit_distance(["take", "after", "food"], ["take", "with", "food"]), 1)


if __name__ == "__main__":
    unittest.main()
