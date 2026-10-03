import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import app, get_db
from app.models import Case, CaseAnalysis, Page
from app.services.comparisons import comparison_for, set_verdict, update_paddle
from app.services.paddle_ocr import parse_prediction


class PaddleComparisonTests(unittest.TestCase):
    def test_parse_prediction_preserves_lines_boxes_and_mean_score(self):
        read = parse_prediction([{"res": {
            "rec_texts": [" First line ", "Second line", ""],
            "rec_scores": [0.9, 0.7, 0.1],
            "rec_boxes": [[1, 2, 30, 12], [4, 20, 45, 33], [0, 0, 0, 0]],
        }}])
        self.assertEqual(read.text, "First line\nSecond line")
        self.assertEqual(read.confidence, 0.8)
        self.assertEqual(read.lines[1]["bbox"], [4, 20, 45, 33])

    def test_comparison_storage_does_not_change_primary_ocr(self):
        analysis = SimpleNamespace(result={"ocr_pages": {"1": {"text": "primary"}}})
        update_paddle(analysis, 1, status="completed", text="candidate")
        set_verdict(analysis, 1, "neither")
        self.assertEqual(comparison_for(analysis.result, 1)["paddle"]["text"], "candidate")
        self.assertEqual(comparison_for(analysis.result, 1)["verdict"], "neither")
        self.assertEqual(analysis.result["ocr_pages"]["1"]["text"], "primary")

    def test_endpoint_queues_comparison_and_keeps_primary_text(self):
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(Case(id="test-case", status="completed"))
            db.add(Page(case_id="test-case", sequence=1, original_name="sample.png",
                        storage_key="test-case/sample.png", status="processed", extracted_text="primary"))
            db.add(CaseAnalysis(case_id="test-case", result={}))
            db.commit()

        def db_override():
            with sessions() as db:
                yield db

        class RedisReady:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def exists(self, *_):
                return True

        app.dependency_overrides[get_db] = db_override
        try:
            with patch("app.main.PREVIEW_MODE", False), patch("app.main.redis_client", return_value=RedisReady()), patch("app.main.enqueue_paddle_comparison") as enqueue:
                client = TestClient(app)
                response = client.post("/cases/test-case/pages/1/compare/paddle")
                self.assertEqual(response.status_code, 202)
                enqueue.assert_called_once_with("test-case", 1)
                case = client.get("/cases/test-case").json()
                self.assertEqual(case["pages"][0]["ocr_text"], "primary")
                self.assertEqual(case["pages"][0]["ocr_comparison"]["paddle"]["status"], "queued")
                again = client.post("/cases/test-case/pages/1/compare/paddle")
                self.assertEqual(again.status_code, 409)
                early_verdict = client.post("/cases/test-case/pages/1/compare/verdict", json={"verdict": "paddle"})
                self.assertEqual(early_verdict.status_code, 409)
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
