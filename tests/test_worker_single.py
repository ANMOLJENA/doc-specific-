import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Case, CaseAnalysis, Page
from app.services.processing import ProcessedPage
from app.worker import handle_case


class SingleImageWorkerTests(unittest.TestCase):
    def test_bundle_persists_each_page_before_next_paddle_read(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            case = Case()
            db.add(case)
            db.flush()
            case_id = case.id
            for number in (1, 2):
                db.add(Page(case_id=case_id, sequence=number, original_name=f"{number}.png", storage_key=f"{number}.png"))
            db.commit()

        def fake_ocr(paths, **_):
            with sessions() as check:
                first = check.query(Page).filter_by(case_id=case_id, sequence=1).one()
                if paths[0].name == "2.png":
                    self.assertEqual(first.status, "processed")
                    self.assertEqual(first.extracted_text, "Page 1 text")
                    self.assertIn("ocr_elapsed_seconds", check.get(CaseAnalysis, case_id).result["ocr_pages"][0])
            return [ProcessedPage(f"Page {paths[0].stem} text", .95, "paddle", False, False)]

        with patch("app.worker.SessionLocal", sessions), patch("app.worker.prepare_ocr_input", side_effect=lambda path: (path, {"decision": "passed_direct", "failure_reasons": []})), patch("app.worker.process_images_primary", side_effect=fake_ocr), patch("app.worker.propose_documents", return_value={"documents": []}):
            handle_case(case_id)
        engine.dispose()

    def test_unreadable_input_never_calls_either_ocr_and_retry_rechecks(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            case = Case()
            db.add(case)
            db.flush()
            case_id = case.id
            db.add(Page(case_id=case_id, sequence=1, original_name="bad.png", storage_key="bad.png"))
            db.add(CaseAnalysis(case_id=case_id, result={"ocr_engine": "paddle"}))
            db.commit()
        report = {"decision": "flagged_for_review", "failure_reasons": ["blur_threshold"]}
        with patch("app.worker.SessionLocal", sessions), patch("app.worker.prepare_ocr_input", return_value=(None, report)) as gate, patch(
            "app.worker.process_images", side_effect=AssertionError("No Surya bypass")
        ), patch("app.worker.process_images_primary", side_effect=AssertionError("No Paddle bypass")):
            handle_case(case_id)
            with sessions() as db:
                db.query(Page).filter_by(case_id=case_id).one().status = "queued"
                db.commit()
            handle_case(case_id)
            self.assertEqual(gate.call_count, 2)
        with sessions() as db:
            page = db.query(Page).filter_by(case_id=case_id).one()
            self.assertEqual(page.status, "quality_review")
            self.assertIsNone(page.extracted_text)
            self.assertEqual(db.get(Case, case_id).status, "needs_review")
            self.assertEqual(len(db.get(CaseAnalysis, case_id).result["ocr_pages"][0]["quality_history"]), 2)
        engine.dispose()

    @patch("app.worker.propose_documents", return_value={"documents": []})
    def test_quality_warning_is_forwarded_to_ocr_and_remains_review_required(self, _analysis):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        report = {"decision": "flagged_for_review", "failure_reasons": ["resolution_threshold"],
                  "review_recommended": True, "forwarded_with_quality_warning": True}
        with sessions() as db:
            case = Case(status="queued")
            db.add(case); db.flush(); case_id = case.id
            db.add(Page(case_id=case_id, sequence=1, original_name="warning.png", storage_key="warning.png"))
            db.add(CaseAnalysis(case_id=case_id, result={"ocr_engine": "paddle"}))
            db.commit()
        reading = ProcessedPage("Warning OCR text", .71, "paddle", False, False)
        with patch("app.worker.SessionLocal", sessions), patch(
            "app.worker.prepare_ocr_input", side_effect=lambda path: (path, report)
        ), patch("app.worker.process_images_primary", return_value=[reading]) as ocr:
            handle_case(case_id)
        self.assertEqual(ocr.call_args.args[0][0].name, "warning.png")
        with sessions() as db:
            page = db.query(Page).filter_by(case_id=case_id).one()
            metric = db.get(CaseAnalysis, case_id).result["ocr_pages"][0]
            self.assertEqual(page.status, "processed")
            self.assertEqual(page.extracted_text, "Warning OCR text")
            self.assertTrue(page.review_required)
            self.assertTrue(metric["ocr_needs_review"])
        engine.dispose()

    @patch("app.worker.propose_documents", return_value={"documents": []})
    def test_surya_choice_is_used_and_preserved(self, _analysis):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            case = Case()
            db.add(case)
            db.flush()
            case_id = case.id
            db.add(Page(case_id=case_id, sequence=1, original_name="sample.png", storage_key="sample.png"))
            db.add(CaseAnalysis(case_id=case_id, result={"ocr_engine": "surya"}))
            db.commit()
        result = ProcessedPage("Sample text", 0.96, "layout", False, False)
        with patch("app.worker.prepare_ocr_input", side_effect=lambda path: (path, {"decision": "passed_direct", "failure_reasons": []})), patch("app.worker.SessionLocal", sessions), patch("app.worker.process_images", return_value=[result]) as surya, patch(
            "app.worker.process_images_primary", side_effect=AssertionError("Do not try Paddle for Surya choice")
        ):
            handle_case(case_id)
        surya.assert_called_once()
        with sessions() as db:
            analysis = db.get(CaseAnalysis, case_id).result
            self.assertEqual(analysis["ocr_engine"], "surya")
            self.assertEqual(analysis["ocr_pages"][0]["ocr_method"], "surya")
        engine.dispose()

    def test_single_image_runs_ai_grouping_and_assigns_category(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            case = Case()
            db.add(case)
            db.flush()
            db.add(Page(case_id=case.id, sequence=1, original_name="example.png", storage_key="example.png"))
            db.commit()
            case_id = case.id

        def fake_ocr(paths, **_):
            self.assertEqual(len(paths), 1)
            with sessions() as db:
                page = db.query(Page).filter_by(case_id=case_id).one()
                self.assertEqual(page.status, "processing")
            return [ProcessedPage(text="Example dosage 0-1-1", confidence=0.96,
                                  method="layout", rechecked=False, needs_review=False)]

        with patch("app.worker.prepare_ocr_input", side_effect=lambda path: (path, {"decision": "passed_direct", "failure_reasons": []})), patch("app.worker.SessionLocal", sessions), patch("app.worker.process_images_primary", side_effect=fake_ocr), patch(
            "app.worker.propose_documents", return_value={"documents": [{"category": "prescription", "title": "Prescription", "pages": [1], "confidence": 0.9}]}
        ):
            handle_case(case_id)

        with sessions() as db:
            case = db.get(Case, case_id)
            page = db.query(Page).filter_by(case_id=case_id).one()
            analysis = db.get(CaseAnalysis, case_id).result
            self.assertEqual(case.status, "needs_review")
            self.assertEqual(page.status, "processed")
            self.assertEqual(analysis["documents"][0]["category"], "prescription")
            self.assertEqual(page.document_type, "prescription")
            self.assertEqual(len(analysis["ocr_pages"]), 1)
            self.assertLessEqual(analysis["ocr_started_at"], analysis["ocr_finished_at"])


if __name__ == "__main__":
    unittest.main()
