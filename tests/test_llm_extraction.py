import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.services.llm_extraction import LLMError
from app.services.pdf_extraction import extract_pdf


def fake_llm(answers):
    """A stand-in for the local model: returns fixed answers and records what it was asked."""
    calls = []

    def llm(messages, schema):
        calls.append(schema)
        return json.dumps({key: answers.get(key) for key in schema["properties"]})

    llm.calls = calls
    return llm


def unreachable_llm(messages, schema):
    raise LLMError("local LLM at http://qwen:8080/v1 unavailable: connection refused")


class RegexPlusLlmTests(unittest.TestCase):
    def make_pdf(self, text: str) -> Path:
        document = pymupdf.open()
        document.new_page().insert_text((72, 72), text)
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        handle.write(document.tobytes())
        handle.close()
        document.close()
        return Path(handle.name)

    def run_extract(self, text, fields, llm):
        path = self.make_pdf(text)
        try:
            return extract_pdf(path, fields, llm=llm)
        finally:
            path.unlink(missing_ok=True)

    def test_llm_fills_fields_the_regex_cannot_find_and_flags_them(self):
        llm = fake_llm({"final_diagnosis": "Acute appendicitis with peritonitis", "claim_number": "99887766"})
        result = self.run_extract(
            "Case Ref. 99887766\nIllness Description: Acute appendicitis with peritonitis",
            ["claim_number", "final_diagnosis"], llm)
        self.assertEqual(result["data"], {"claim_number": "99887766",
                                          "final_diagnosis": "Acute appendicitis with peritonitis"})
        self.assertEqual(result["evidence"]["final_diagnosis"]["strategy"], "llm")
        self.assertEqual(result["status"], "warning")
        self.assertTrue(any("filled by the local LLM" in w for w in result["warnings"]))

    def test_llm_reads_every_requested_field_not_only_the_missing_ones(self):
        llm = fake_llm({})
        self.run_extract("Policy No: POL-123\nWard: general", ["policy_number", "final_diagnosis"], llm)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(list(llm.calls[0]["properties"]), ["policy_number", "final_diagnosis"])

    def test_agreement_raises_confidence_without_a_warning(self):
        result = self.run_extract("Policy No: POL-123", ["policy_number"], fake_llm({"policy_number": "POL-123"}))
        self.assertEqual(result["data"]["policy_number"], "POL-123")
        self.assertEqual(result["confidence"]["policy_number"], 0.98)
        self.assertEqual(result["evidence"]["policy_number"]["strategy"], "inline")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["status"], "done")

    def test_disagreement_keeps_regex_value_and_flags_it(self):
        llm = fake_llm({"claim_number": "99887766"})
        result = self.run_extract("Claim Number: 111222333\nCase Ref. 99887766", ["claim_number"], llm)
        self.assertEqual(result["data"]["claim_number"], "111222333")
        self.assertTrue(any("regex and local LLM disagree" in w for w in result["warnings"]))
        alternatives = result["evidence"]["claim_number"]["alternatives"]
        self.assertIn("99887766", [a["value"] for a in alternatives])

    def test_rejects_values_that_are_not_in_the_pdf(self):
        llm = fake_llm({"final_diagnosis": "Dengue fever", "claim_number": "12345678"})
        result = self.run_extract("Case Ref. 99887766", ["claim_number", "final_diagnosis"], llm)
        self.assertEqual(result["data"], {"claim_number": None, "final_diagnosis": None})
        self.assertTrue(any("value not found" in w for w in result["warnings"]))

    def test_typed_values_still_go_through_validation(self):
        llm = fake_llm({"settlement_date": "sometime in June"})
        result = self.run_extract("Processed in sometime in June", ["settlement_date"], llm)
        self.assertIsNone(result["data"]["settlement_date"])

    def test_unreachable_llm_is_reported_and_regex_values_survive(self):
        result = self.run_extract("Policy No: POL-123", ["policy_number"], unreachable_llm)
        self.assertEqual(result["data"]["policy_number"], "POL-123")
        self.assertEqual(result["status"], "warning")
        self.assertTrue(any("NOT cross-checked" in w for w in result["warnings"]))

    def test_non_json_reply_is_reported_like_an_outage(self):
        result = self.run_extract("Policy No: POL-123", ["policy_number"], lambda messages, schema: "not json")
        self.assertEqual(result["data"]["policy_number"], "POL-123")
        self.assertTrue(any("NOT cross-checked" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
