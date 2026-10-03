import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Case, CaseAnalysis, Page
from app.paddle_worker import handle_comparison
from app.services.processing import ProcessedPage
from app.worker import handle_case


class QualityRoutingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_bundle_excludes_held_image_and_uses_approved_derivative(self):
        with self.sessions() as db:
            db.add(Case(id="bundle"))
            db.add(CaseAnalysis(case_id="bundle", result={"ocr_engine": "paddle"}))
            db.add_all([Page(case_id="bundle", sequence=n, original_name=f"{n}.png",
                             storage_key=f"bundle/{n}.png") for n in (1, 2)])
            db.commit()
        derivative = Path("bundle/quality-approved.png")

        def gate(path):
            if path.name == "1.png":
                return None, {"decision": "flagged_for_review", "failure_reasons": ["blur"]}
            return derivative, {"decision": "passed_after_enhancement", "failure_reasons": []}

        proposal = {"documents": [], "review_required": False}
        reading = ProcessedPage("Approved label", .95, "paddle", False, False)
        with patch("app.worker.SessionLocal", self.sessions), patch("app.worker.prepare_ocr_input", side_effect=gate), \
             patch("app.worker.process_images_primary", return_value=[reading]) as ocr, \
             patch("app.worker.propose_documents", return_value={}) as grouping, \
             patch("app.worker.validate_proposal", return_value=proposal):
            handle_case("bundle")
        ocr.assert_called_once()
        self.assertEqual(ocr.call_args.args[0], [derivative])
        self.assertIn("cancel_check", ocr.call_args.kwargs)
        self.assertEqual([item["page"] for item in grouping.call_args.args[0]], [2])
        with self.sessions() as db:
            result = db.get(CaseAnalysis, "bundle").result
            self.assertEqual(result["failed_pages"], [1])
            self.assertEqual(len(result["ocr_pages"]), 2)
            self.assertEqual(db.get(Case, "bundle").status, "needs_review")

    def test_comparison_gate_rejection_never_loads_paddle(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "image.png").touch()
            with self.sessions() as db:
                db.add(Case(id="legacy", status="completed"))
                db.add(CaseAnalysis(case_id="legacy", result={}))
                db.add(Page(case_id="legacy", sequence=1, original_name="image.png", storage_key="image.png",
                            status="processed", extracted_text="Historical original OCR"))
                db.commit()
            held = {"decision": "flagged_for_review", "failure_reasons": ["blur"]}
            with patch("app.paddle_worker.SessionLocal", self.sessions), patch("app.paddle_worker.STORAGE_PATH", folder), \
                 patch("app.paddle_worker.prepare_ocr_input", return_value=(None, held)), \
                 patch("app.paddle_worker.PaddleOcrEngine", side_effect=AssertionError("Held images cannot load OCR")):
                handle_comparison("legacy", 1)
            with self.sessions() as db:
                result = db.get(CaseAnalysis, "legacy").result
                self.assertEqual(result["ocr_comparisons"]["1"]["paddle"]["status"], "failed")
                self.assertEqual(db.query(Page).one().extracted_text, "Historical original OCR")
