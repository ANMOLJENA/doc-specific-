"""Store optional OCR experiments without replacing the canonical OCR result."""


def comparison_for(result: dict | None, page: int) -> dict:
    return (result or {}).get("ocr_comparisons", {}).get(str(page), {})


def update_paddle(analysis, page: int, **values) -> dict:
    result = dict(analysis.result or {})
    comparisons = dict(result.get("ocr_comparisons", {}))
    entry = dict(comparisons.get(str(page), {}))
    paddle = dict(entry.get("paddle", {}))
    paddle.update(values)
    entry["paddle"] = paddle
    comparisons[str(page)] = entry
    result["ocr_comparisons"] = comparisons
    analysis.result = result
    return entry


def set_verdict(analysis, page: int, verdict: str | None) -> dict:
    result = dict(analysis.result or {})
    comparisons = dict(result.get("ocr_comparisons", {}))
    entry = dict(comparisons.get(str(page), {}))
    entry["verdict"] = verdict
    comparisons[str(page)] = entry
    result["ocr_comparisons"] = comparisons
    analysis.result = result
    return entry
