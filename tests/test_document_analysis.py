import unittest
from unittest.mock import patch

from app.services.analysis import AnalysisUnavailable, _parse_json, propose_documents
from app.services.validation import validate_proposal


class DocumentAnalysisTests(unittest.TestCase):
    def test_provider_commentary_wrapping_json(self):
        self.assertEqual(_parse_json('Here is the result:\n{"documents": []}\nDone.'), {"documents": []})

    @patch("app.services.analysis._call_json")
    def test_field_failure_preserves_classification(self, mock_call):
        mock_call.side_effect = [{"documents": [{"category": "invoice", "pages": [1], "confidence": .9}]}, AnalysisUnavailable("empty provider response")]
        grouped = []
        result = propose_documents(self.pages[:1], on_grouped=lambda proposal: grouped.append(proposal["documents"][0]["category"]))
        self.assertEqual(grouped, ["invoice"])
        self.assertEqual(result["documents"][0]["category"], "invoice")
        self.assertEqual(result["documents"][0]["fields"], [])
        self.assertEqual(len(result["field_errors"]), 1)

    def setUp(self):
        self.pages = [
            {"page": 1, "name": "a.png", "text": "Invoice No: A-100\nTotal: 42.00"},
            {"page": 2, "name": "b.png", "text": "Policy Number: P-441"},
            {"page": 3, "name": "c.png", "text": "Invoice A-100 continued\nTax: 2.00"},
        ]

    def test_non_adjacent_pages_can_form_one_document(self):
        proposal = {"documents": [
            {"category": "invoice", "pages": [1, 3], "confidence": .9,
             "fields": [{"name": "invoice_number", "value": "A-100",
                         "value_type": "identifier", "source_page": 1,
                         "evidence": "Invoice No: A-100", "confidence": .9}]},
            {"category": "insurance_policy", "pages": [2], "confidence": .9, "fields": []},
        ]}
        result = validate_proposal(proposal, self.pages, {"invoice"})
        self.assertEqual(result["documents"][0]["pages"], [1, 3])
        self.assertEqual(result["documents"][0]["fields"][0]["status"], "evidence_checked")
        self.assertEqual(result["documents"][1]["category_status"], "proposed")

    def test_unsupported_value_is_flagged(self):
        proposal = {"documents": [{"category": "invoice", "pages": [1], "confidence": .9,
            "fields": [{"name": "total", "value": "999.00", "value_type": "currency",
                        "source_page": 1, "evidence": "Total: 42.00", "confidence": .95}]}]}
        result = validate_proposal(proposal, self.pages, {"invoice"})
        field = result["documents"][0]["fields"][0]
        self.assertEqual(field["status"], "needs_review")
        self.assertIn("value_not_in_evidence", field["issues"])
        self.assertEqual(len(result["documents"]), 3)  # Unassigned pages are retained.

    @patch("app.services.analysis._call_json")
    def test_grouping_and_fields_use_distinct_ai_calls(self, mock_call):
        mock_call.side_effect = [
            {"documents": [{"category": "invoice", "title": "Invoice", "pages": [1, 3], "confidence": .9},
                           {"category": "policy", "title": "Policy", "pages": [2], "confidence": .8}]},
            {"fields": [{"name": "invoice_number", "value": "A-100", "source_page": 1,
                         "evidence": "Invoice No: A-100", "value_type": "identifier", "confidence": .9}]},
            {"fields": []},
        ]
        result = propose_documents(self.pages)
        self.assertEqual(mock_call.call_count, 3)
        self.assertEqual(result["documents"][0]["fields"][0]["value"], "A-100")


if __name__ == "__main__":
    unittest.main()
