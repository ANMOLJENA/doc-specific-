import unittest

import fitz

from app.services.pdf_ingestion import expand_pdf


class PdfIngestionTests(unittest.TestCase):
    def test_selectable_text_is_preserved_and_page_is_rendered(self):
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Medical bill INV-2048")
        content = document.tobytes()
        document.close()

        pages = expand_pdf("bill.pdf", content)
        self.assertEqual(len(pages), 1)
        self.assertIn("INV-2048", pages[0].digital_text)
        self.assertTrue(pages[0].image_bytes.startswith(b"\x89PNG"))

    def test_pdf_page_limit_is_enforced(self):
        document = fitz.open()
        for _ in range(11):
            document.new_page()
        content = document.tobytes()
        document.close()
        with self.assertRaisesRegex(ValueError, "limited to 10"):
            expand_pdf("large.pdf", content)


if __name__ == "__main__":
    unittest.main()
