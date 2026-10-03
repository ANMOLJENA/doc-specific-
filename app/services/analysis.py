"""AI proposals for whole-case document grouping and flexible field extraction."""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import AI_API_KEY, AI_BASE_URL, AI_MODEL, AI_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


GROUP_PROMPT = """Organize a mixed bundle of document-image OCR pages. Return only JSON:
{"documents":[{"category":"short_snake_case","title":"short title","pages":[1,2],"confidence":0.0}]}.
Infer categories from content, with no fixed list or domain. Group pages of one logical document
even if they are not adjacent. Separate different invoices/reports by their identifiers or entities.
Assign every page exactly once. Use category unknown when evidence is weak.
Do not extract fields in this step."""

FIELD_PROMPT = """Extract useful searchable fields from OCR pages of one proposed document.
Return only JSON: {"fields":[{"name":"short_snake_case","value":"exact printed value",
"value_type":"text|number|date|currency|schedule|identifier","source_page":1,
"evidence":"exact substring copied from that page's OCR text","confidence":0.0}]}.
No fixed field list is assumed. Do not invent values or fill missing information.
Keep schedules such as 0-1-1 exactly as printed; do not interpret them.
Each field must have an exact evidence quotation and its original page number."""


class AnalysisUnavailable(RuntimeError):
    pass


def _parse_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        # Some providers wrap a valid JSON object in explanatory text.
        decoder = json.JSONDecoder()
        for offset, character in enumerate(content):
            if character != "{":
                continue
            try:
                result, _ = decoder.raw_decode(content[offset:])
                break
            except json.JSONDecodeError:
                continue
        else:
            raise ValueError("AI response contained no valid JSON object")
    if not isinstance(result, dict):
        raise ValueError("AI response must be a JSON object")
    return result


def _call_json(system_prompt: str, data: dict, _retry: bool = True) -> dict:
    if not AI_BASE_URL or not AI_MODEL:
        raise AnalysisUnavailable("Set AI_BASE_URL and AI_MODEL to enable document analysis")
    parameters = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
    }
    if "openrouter.ai" in AI_BASE_URL:
        parameters["max_tokens"] = 4096
        parameters["reasoning"] = {"enabled": False, "exclude": True}
        parameters["provider"] = {"require_parameters": True}
    body = json.dumps(parameters).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if AI_API_KEY:
        headers["Authorization"] = f"Bearer {AI_API_KEY}"
    request = Request(f"{AI_BASE_URL}/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=AI_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError) as error:
        raise AnalysisUnavailable(f"AI service request failed: {error}") from error
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AI returned empty final content")
        return _parse_json(content)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        if _retry:
            logger.warning("AI returned invalid JSON; retrying once (model=%s)", payload.get("model", AI_MODEL))
            return _call_json(system_prompt + "\nReturn a complete JSON object only, without commentary.", data, _retry=False)
        raise AnalysisUnavailable(f"AI service returned invalid document JSON: {error}; model={payload.get('model', AI_MODEL)}") from error


def _page_chunks(pages: list[dict], max_chars: int = 24000) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for page in pages:
        page_data = {"page": page["page"], "ocr_text": page["text"][:max_chars]}
        length = len(page_data["ocr_text"])
        if current and size + length > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(page_data)
        size += length
    if current:
        chunks.append(current)
    return chunks


def propose_documents(pages: list[dict], on_grouped=None) -> dict:
    """Group the bundle, then extract dynamic fields from manageable page chunks."""
    previews = [
        {"page": page["page"], "file_name": page["name"],
         "ocr_excerpt": page["text"][:1400] + ("\n...\n" + page["text"][-500:] if len(page["text"]) > 1900 else "")}
        for page in pages
    ]
    proposal = _call_json(GROUP_PROMPT, {"pages": previews})
    documents = proposal.get("documents")
    if not isinstance(documents, list):
        raise AnalysisUnavailable("AI grouping response is missing its documents array")
    if not documents:
        raise AnalysisUnavailable("AI grouping returned no document categories")
    if on_grouped:
        on_grouped(proposal)
    page_map = {page["page"]: page for page in pages}
    for document in documents:
        if not isinstance(document, dict):
            continue
        selected_pages = []
        for number in document.get("pages", []):
            try:
                source = page_map.get(int(number))
            except (TypeError, ValueError):
                source = None
            if source and source not in selected_pages:
                selected_pages.append(source)
        fields: list[dict] = []
        for chunk in _page_chunks(selected_pages):
            try:
                response = _call_json(FIELD_PROMPT, {"category": document.get("category"), "pages": chunk})
                if not isinstance(response.get("fields"), list):
                    raise AnalysisUnavailable("AI field response is missing its fields array")
                fields.extend(response["fields"])
            except AnalysisUnavailable as error:
                proposal.setdefault("field_errors", []).append({"pages": [page["page"] for page in chunk], "error": str(error)})
                logger.warning("AI fields unavailable; retaining document classification: %s", error)
        document["fields"] = fields
    return proposal
