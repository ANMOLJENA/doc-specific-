"""Local-LLM half of PDF field extraction (llama.cpp server running Qwen3-8B, no cloud).

Every extraction asks the model for all requested fields; ``pdf_extraction`` then reconciles its
answers with the regex pass. The model is a normal part of the pipeline: if the server cannot be
reached, ``LLMError`` is raised and the caller reports it on the result instead of hiding it.
Nothing here validates answers: ``pdf_extraction.llm_fill`` checks each value against the PDF.
"""

from __future__ import annotations

import json
import logging
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import LLM_BASE_URL, LLM_MAX_CHARS, LLM_MODEL, LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# (messages, json_schema) -> the model's raw reply text
LLM = Callable[[list, dict], str]

_SYSTEM = (
    "You extract fields from the text of one document. Reply with JSON only.\n"
    "Rules: copy each value EXACTLY as printed in the document (same spelling, digits, "
    "separators); never reformat, translate, calculate or guess; use null when the document does "
    "not contain the field. Labels differ between documents, so use the listed alternative "
    "labels and your judgement about meaning."
)


class LLMError(RuntimeError):
    """The local model could not be reached or did not return usable JSON."""


class LocalLLM:
    """OpenAI-compatible chat endpoint of a local llama.cpp server (also works with Ollama/LM Studio)."""

    def __init__(self, base_url: str, model: str, timeout: int):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout

    def __call__(self, messages: list, schema: dict) -> str:
        body = json.dumps({
            "model": self.model, "messages": messages, "temperature": 0, "max_tokens": 700,
            "response_format": {"type": "json_object", "schema": schema},
            "chat_template_kwargs": {"enable_thinking": False},  # Qwen3: answer directly, no <think> block
        }).encode()
        request = Request(f"{self.base_url}/chat/completions", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)["choices"][0]["message"]["content"]
        except HTTPError as error:
            raise LLMError(f"local LLM returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, KeyError, ValueError) as error:
            raise LLMError(f"local LLM at {self.base_url} unavailable: {error}") from error


def get_llm() -> LocalLLM:
    return LocalLLM(LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS)


def document_text(pages: list[dict], limit: int = LLM_MAX_CHARS) -> str:
    """The PDF's lines in reading order with page markers, cut to what fits the context."""
    parts = [f"[page {pg['no']}]\n" + "\n".join(pg["lines"]) for pg in pages]
    return "\n".join(parts)[:limit]


def ask_llm(llm: LLM, text: str, fields: list[dict]) -> dict[str, str | None]:
    """Ask for `fields` (dicts with key/name/type/labels). -> {field key: printed value or None}.
    Raises LLMError when the model is unreachable or its reply is not a JSON object."""
    schema = {
        "type": "object",
        "properties": {fd["key"]: {"type": ["string", "null"]} for fd in fields},
        "required": [fd["key"] for fd in fields],
        "additionalProperties": False,
    }
    wanted = "\n".join(
        f'- "{fd["key"]}" ({fd["type"]}): {fd["name"]}; may be labelled '
        + ", ".join(f'"{l}"' for l in fd["labels"][:12])
        for fd in fields)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Fields to extract:\n{wanted}\n\nDOCUMENT:\n{text}"},
    ]
    try:
        reply = json.loads(llm(messages, schema))
    except LLMError:
        raise
    except (ValueError, TypeError) as error:
        raise LLMError(f"local LLM did not return JSON: {error}") from error
    if not isinstance(reply, dict):
        raise LLMError("local LLM did not return a JSON object")
    return {fd["key"]: (str(reply[fd["key"]]).strip() or None) if reply.get(fd["key"]) is not None else None
            for fd in fields}
