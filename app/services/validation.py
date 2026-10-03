"""Validate AI proposals against actual Surya OCR text and page boundaries."""

import re
from datetime import datetime


ALLOWED_TYPES = {"text", "number", "date", "currency", "schedule", "identifier"}


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", _clean(value).lower()).strip("_")[:64] or "unknown"


def _confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _valid_date(value: str) -> bool:
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            datetime.strptime(value, pattern)
            return True
        except ValueError:
            continue
    return False


def _type_reason(value: str, value_type: str) -> str | None:
    if value_type in {"number", "currency"} and not re.fullmatch(r"(?:₹|\$|€|£|Rs\.?\s*)?\s*-?\d[\d,]*(?:\.\d+)?", value):
        return f"invalid_{value_type}_format"
    if value_type == "date" and not _valid_date(value):
        return "unrecognized_date_format"
    if value_type == "schedule" and not re.search(r"\d\s*[-/]\s*\d\s*[-/]\s*\d|daily|weekly|hour|morning|night", value, re.I):
        return "unrecognized_schedule_format"
    return None


def validate_proposal(proposal: dict, pages: list[dict], approved_categories: set[str]) -> dict:
    """Keep unsupported proposals visible for review without treating them as facts."""
    page_map = {int(page["page"]): page for page in pages}
    assigned: set[int] = set()
    documents: list[dict] = []
    if not isinstance(proposal.get("documents"), list):
        raise ValueError("AI proposal is missing its documents array")

    for index, raw_document in enumerate(proposal["documents"]):
        if not isinstance(raw_document, dict):
            continue
        category = _slug(raw_document.get("category"))
        raw_pages = raw_document.get("pages")
        group_pages: list[int] = []
        issues: list[str] = []
        for item in raw_pages if isinstance(raw_pages, list) else []:
            try:
                number = int(item)
            except (TypeError, ValueError):
                issues.append("invalid_page_number")
                continue
            if number not in page_map or number in assigned or number in group_pages:
                issues.append(f"page_{number}_missing_or_duplicate")
                continue
            group_pages.append(number)
        if not group_pages:
            continue
        assigned.update(group_pages)
        category_approved = category in approved_categories
        if not category_approved:
            issues.append("new_category_requires_review")
        if category == "unknown":
            issues.append("unknown_document_type")
        confidence = _confidence(raw_document.get("confidence"))
        if confidence < 0.75:
            issues.append("low_category_confidence")
        fields: list[dict] = []
        seen: set[tuple[str, str, int]] = set()
        for raw_field in raw_document.get("fields", []) if isinstance(raw_document.get("fields"), list) else []:
            if not isinstance(raw_field, dict):
                continue
            name = _slug(raw_field.get("name"))
            value = _clean(raw_field.get("value"))
            evidence = _clean(raw_field.get("evidence"))
            try:
                source_page = int(raw_field.get("source_page"))
            except (TypeError, ValueError):
                source_page = 0
            if not value or (name, value, source_page) in seen:
                continue
            seen.add((name, value, source_page))
            value_type = _slug(raw_field.get("value_type"))
            field_issues: list[str] = []
            if source_page not in group_pages:
                field_issues.append("source_page_outside_document")
            elif not evidence or evidence.casefold() not in _clean(page_map[source_page]["text"]).casefold():
                field_issues.append("evidence_not_found_in_ocr")
            if value.casefold() not in evidence.casefold():
                field_issues.append("value_not_in_evidence")
            if value_type not in ALLOWED_TYPES:
                field_issues.append("unknown_value_type")
            else:
                type_issue = _type_reason(value, value_type)
                if type_issue:
                    field_issues.append(type_issue)
            field_confidence = _confidence(raw_field.get("confidence"))
            if field_confidence < 0.75:
                field_issues.append("low_field_confidence")
            fields.append({
                "name": name, "value": value, "value_type": value_type,
                "source_page": source_page, "evidence": evidence,
                "confidence": field_confidence,
                "status": "needs_review" if field_issues else "evidence_checked",
                "issues": field_issues,
            })
        documents.append({
            "id": f"doc-{index + 1}", "category": category,
            "title": _clean(raw_document.get("title")) or category.replace("_", " ").title(),
            "pages": group_pages, "confidence": confidence,
            "category_status": "approved" if category_approved else "proposed",
            "review_required": bool(issues or any(field["issues"] for field in fields)),
            "issues": issues, "fields": fields,
        })

    for number in sorted(set(page_map) - assigned):
        documents.append({
            "id": f"doc-unassigned-{number}", "category": "unknown", "title": "Unassigned page",
            "pages": [number], "confidence": 0.0, "category_status": "proposed",
            "review_required": True, "issues": ["page_not_grouped_by_ai"], "fields": [],
        })
    return {
        "documents": documents,
        "review_required": any(document["review_required"] for document in documents),
        "page_count": len(page_map),
    }
