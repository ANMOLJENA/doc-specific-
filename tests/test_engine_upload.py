import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import app, get_db


class EngineUploadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        sessions = sessionmaker(bind=self.engine)

        def db_override():
            with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = db_override
        self.client = TestClient(app)
        self.storage_patch = patch("app.main.STORAGE_PATH", Path(self.folder.name))
        self.preview_patch = patch("app.main.PREVIEW_MODE", True)
        self.storage_patch.start()
        self.preview_patch.start()

    def tearDown(self):
        self.preview_patch.stop()
        self.storage_patch.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.folder.cleanup()

    def test_default_engine_is_paddle(self):
        response = self.client.post("/cases", files=[
            ("files", ("sample.png", b"synthetic", "image/png")),
        ])
        self.assertEqual(response.status_code, 201)
        case = self.client.get(f"/cases/{response.json()['case_id']}").json()
        self.assertEqual(case["requested_ocr_engine"], "paddle")

    def test_selected_engine_is_saved(self):
        response = self.client.post("/cases", data={"ocr_engine": "surya"}, files=[
            ("files", ("sample.png", b"synthetic", "image/png")),
        ])
        self.assertEqual(response.status_code, 201)
        case = self.client.get(f"/cases/{response.json()['case_id']}").json()
        self.assertEqual(case["requested_ocr_engine"], "surya")

    def test_digital_pdf_page_uses_pymupdf_text(self):
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Medical bill INV-2048")
        content = document.tobytes()
        document.close()
        response = self.client.post("/cases", files=[("files", ("bill.pdf", content, "application/pdf"))])
        self.assertEqual(response.status_code, 201)
        case = self.client.get(f"/cases/{response.json()['case_id']}").json()
        self.assertEqual(case["page_count"] if "page_count" in case else len(case["pages"]), 1)
        self.assertEqual(case["pages"][0]["ocr_method"], "pymupdf")
        self.assertIn("INV-2048", case["pages"][0]["ocr_text"])

    def test_invalid_engine_is_rejected_before_saving(self):
        response = self.client.post("/cases", data={"ocr_engine": "invalid"}, files=[
            ("files", ("sample.png", b"synthetic", "image/png")),
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_bundle_limit_is_ten(self):
        files = [("files", (f"page-{i}.png", b"synthetic", "image/png")) for i in range(10)]
        accepted = self.client.post("/cases", files=files)
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.json()["page_count"], 10)
        rejected = self.client.post("/cases", files=files + [("files", ("page-11.png", b"synthetic", "image/png"))])
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("1 to 10", rejected.json()["detail"])

    def test_selected_page_can_be_permanently_deleted(self):
        created = self.client.post("/cases", files=[
            ("files", ("first.png", b"first", "image/png")),
            ("files", ("second.png", b"second", "image/png")),
        ]).json()
        case_id = created["case_id"]
        pages = self.client.get(f"/cases/{case_id}").json()["pages"]
        first_id = pages[0]["image_id"]

        deleted = self.client.request("DELETE", "/pages", json={"page_ids": [first_id]})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deleted_pages"], 1)
        remaining = self.client.get(f"/cases/{case_id}").json()["pages"]
        self.assertEqual([page["name"] for page in remaining], ["second.png"])

        last = self.client.request("DELETE", "/pages", json={"page_ids": [remaining[0]["image_id"]]})
        self.assertEqual(last.status_code, 200)
        self.assertEqual(last.json()["deleted_case_ids"], [case_id])
        self.assertEqual(self.client.get(f"/cases/{case_id}").status_code, 404)



if __name__ == "__main__":
    unittest.main()
