import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

from app.services.pdf_extraction import extract_pdf, resolve_fields


class PdfExtractionTests(unittest.TestCase):
    def setUp(self):
        # These tests cover the regex pass only: the LLM is replaced by one that finds nothing, so no
        # model server is needed. The regex-plus-LLM behaviour is tested in test_llm_extraction.py.
        silent = lambda messages, schema: json.dumps({key: None for key in schema["properties"]})
        patcher = mock.patch("app.services.pdf_extraction.get_llm", return_value=silent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_pdf(self, text: str) -> Path:
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), text)
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        handle.write(document.tobytes())
        handle.close()
        document.close()
        return Path(handle.name)

    def test_resolve_known_and_unknown_fields(self):
        mapping = resolve_fields(["policy_number", "unknown_field"])
        self.assertEqual(mapping[0]["status"], "known")
        self.assertEqual(mapping[1]["status"], "unknown")

    def test_extracts_typed_values_and_evidence(self):
        path = self.make_pdf("Policy No: POL-123\nSettlement Amount: Rs 4,25,000\nSettlement Date: 24-Oct-2024")
        try:
            result = extract_pdf(path, ["policy_number", "settlement_amount", "settlement_date"])
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["extraction_method"], ["text"])
        self.assertEqual(result["data"]["policy_number"], "POL-123")
        self.assertEqual(result["data"]["settlement_amount"], 425000)
        self.assertEqual(result["data"]["settlement_date"], "2024-10-24")
        self.assertEqual(result["evidence"]["policy_number"]["strategy"], "inline")

    def test_settlement_fields_stay_anchored_to_their_own_labels(self):
        path = self.make_pdf(
            "Date: 30-JUN-2026\n"
            "Claim ID\n8720632\n"
            "Claim Number\nOC-27-1002-8402-00003527\n"
            "DOA: 15-JUN-2026 DOD: 17-JUN-2026\n"
            "Approval Date\n30-JUN-2026\n"
            "Bill Amount\nINR 20534\n"
            "Paid Amount\nINR 12588\n"
            "Disallowed Amount\nINR 6547"
        )
        fields = [
            "claim_number", "claim_date", "admission_date", "discharge_date",
            "settlement_date", "final_diagnosis", "claimed_amount", "final_bill",
            "settled_amount", "not_settled_amount",
        ]
        try:
            result = extract_pdf(path, fields)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["output"], {
            "Source PDF": path.name,
            "Claim number": "OC-27-1002-8402-00003527",
            "Claim date": "2026-06-30",   # no claim-date label: letter date is used and flagged
            "Admission date": "2026-06-15",
            "Discharge date": "2026-06-17",
            "Settlement date": "2026-06-30",
            "Final Diagnosis": None,
            "Claimed amount": None,
            "Final Bill": 20534,
            "Settled amount": 12588,
            "Not settled amount": 6547,
            "Amounts check": "MISMATCH",   # 20534 - 6547 != 12588: flagged for a human look
        })
        self.assertTrue(any("LETTER date" in w for w in result["warnings"]))
        self.assertTrue(any("do not reconcile" in w for w in result["warnings"]))

    def test_prose_settlement_letter_extracts_claim_and_diagnosis(self):
        path = self.make_pdf(
            "Cashless Claim Reference Number: (143598222)\n"
            "Claim bearing No 143598222 has been settled for Rs 14911\n"
            "Amount Claimed for Rs 24300\n"
            "treatment of Noninfective gastroenteritis and colitis, unspecified at Hospital\n"
            "for the period from 10 Jul 2026 12:00 PM to 12 Jul 2026 12:00 PM.\n"
            "Settled Amount (INR)\n14911\nSettlement Date\n30-07-2026 00:00:00\n"
            "Net amount recommended for\npayment\n14911"
        )
        fields = [
            "claim_number", "claim_date", "admission_date", "discharge_date",
            "settlement_date", "final_diagnosis", "claimed_amount", "final_bill",
            "settled_amount", "not_settled_amount",
        ]
        try:
            result = extract_pdf(path, fields)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["output"]["Claim number"], "143598222")
        self.assertEqual(result["output"]["Admission date"], "2026-07-10")
        self.assertEqual(result["output"]["Discharge date"], "2026-07-12")
        self.assertEqual(result["output"]["Final Diagnosis"], "Noninfective gastroenteritis and colitis, unspecified")
        self.assertEqual(result["output"]["Claimed amount"], 24300)
        self.assertEqual(result["output"]["Settled amount"], 14911)

    def test_custom_field_uses_saved_labels_or_its_own_name(self):
        from unittest import mock
        path = self.make_pdf("Patient Name: Asha Rao\nEmail: asha@example.com\nTPA Ref: TPA-445566")
        dictionary = {"tpa_reference": {"labels": ["TPA Ref"], "type": "id", "display_name": "TPA reference"}}
        try:
            with mock.patch("app.services.pdf_extraction.load_dictionary", return_value=dictionary):
                result = extract_pdf(path, ["tpa_reference", "patient_name", "email"])
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["output"]["TPA reference"], "TPA-445566")
        self.assertEqual(result["output"]["Patient name"], "Asha Rao")
        self.assertEqual(result["output"]["Email"], "asha@example.com")
        self.assertNotIn("Amounts check", result["output"])

    def test_csv_has_one_row_per_pdf(self):
        from app.services.pdf_extraction import results_to_csv
        rows = results_to_csv([{"file": "a.pdf", "output": {"Source PDF": "a.pdf", "Claim number": "1234"}},
                               {"file": "b.pdf", "output": {"Source PDF": "b.pdf", "Claim number": None}}])
        self.assertEqual(rows.splitlines(), ["Source PDF,Claim number", "a.pdf,1234", "b.pdf,"])


if __name__ == "__main__":
    unittest.main()
