"""Deterministic, local PDF claim-field extraction (PyMuPDF + regex, no LLM or network).

This is the logic of the ``extractor.ipynb`` notebook, with one change: the set of fields to
look for is chosen at run time (from the UI) instead of being a fixed list. Every built-in field
keeps the notebook's label wording; fields the notebook does not know are searched by the labels
saved in ``extract_dictionary.json`` (or, failing that, by their own name).
"""

from __future__ import annotations

import copy
import csv
import io
import itertools
import json
import re
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz, process as fuzzy_process

from app.services.llm_extraction import LLM, LLMError, ask_llm, document_text, get_llm

try:
    import pymupdf as fitz
except ImportError:
    import fitz

DICTIONARY_PATH = Path(__file__).resolve().parents[1] / "extract_dictionary.json"

OUTPUT_ISO_DATES = True         # True: 2026-06-30   False: text exactly as in the PDF
OUTPUT_NUMERIC_AMOUNTS = True   # True: 12588        False: text exactly as in the PDF
CLAIM_DATE_FALLBACK_TO_LETTER_DATE = True
LETTER_DATE_TOP_FRACTION = 0.35

# ----------------------------------------------------------------------------
# Built-in field definitions (from the notebook). The first label that yields a VALID value
# wins; other labels that give a DIFFERENT value are reported as alternatives.
# ----------------------------------------------------------------------------
BUILTIN_FIELDS = [
    dict(name="Claim number", type="id", labels=[
        "Claim bearing No", "Claim bearing Number", "Cashless Claim Reference Number",
        "Claim Number", "Claim No", "Claim Reference Number", "Claim Ref No",
        "Claim Reference No", "Claim ID", "Claim Reference",
        "Intimation No", "Intimation Number", "Reference No", "Reference Number",
        "File No", "File Number"]),
    dict(name="Claim date", type="date", labels=[
        "Claim Date", "Date of Claim", "Claim Registration Date", "Claim Registered On",
        "Date of Intimation", "Intimation Date", "Registration Date", "Date of Registration",
        "Claim Received Date", "Received Date", "Date of Submission", "Submission Date",
        "Claim Submitted On"],
        fallback_labels=["Claim Covering Letter Date", "Covering Letter Date", "Letter Date",
                         "Date of Letter", "Letter Dated"]),
    dict(name="Admission date", type="date", range_idx=0, labels=[
        "Date of Admission", "Admission Date", "DOA", "Date of Hospitalization",
        "Date of Hospitalisation", "Hospitalization Date", "Admitted On"]),
    dict(name="Discharge date", type="date", range_idx=1, labels=[
        "Date of Discharge", "Discharge Date", "DOD", "Discharged On"]),
    dict(name="Settlement date", type="date", labels=[
        "Settlement Date", "Date of Settlement", "Approval Date", "Date of Approval",
        "Payment Date", "Date of Payment", "Disbursement Date", "Settled On",
        "Processed Date", "Decision Date"]),
    dict(name="Final Diagnosis", type="text", labels=[
        "Final Diagnosis", "Diagnosis", "Provisional Diagnosis", "Ailment", "Disease",
        "Nature of Illness", "Treatment of", "Treatment for", "Hospitalization for"]),
    dict(name="Claimed amount", type="amount", labels=[
        "Amount Claimed", "Claimed Amount", "Total Claimed Amount", "Claim Amount",
        "Total Claim Amount", "Requested Amount"]),
    dict(name="Final Bill", type="amount", labels=[
        "Net Bill Amount", "Final Bill Amount", "Final Bill", "Net Bill", "Bill Amount",
        "Total Bill Amount", "Total Bill", "Gross Bill Amount", "Gross Bill",
        "Hospital Bill Amount", "Hospital Bill", "Billed Amount", "Total Billed Amount",
        "Bill Total"]),
    dict(name="Settled amount", type="amount", labels=[
        "Settled Amount", "Amount Settled", "Paid Amount", "Amount Paid", "Approved Amount",
        "Amount Approved", "Net Amount Recommended for Payment", "Net Payable Amount",
        "Net Payable", "Payable Amount", "Disbursed Amount", "Total Paid Amount"]),
    dict(name="Not settled amount", type="amount", labels=[
        "Non Pay Amount", "Non Payable Amount", "Not Settled Amount", "Disallowed Amount",
        "Amount Disallowed", "Deducted Amount", "Rejected Amount", "Total Deductions",
        "Deductions", "Deduction"]),
]
# Helper fields: never written to the CSV, only used to check the arithmetic.
AUX_FIELDS = [
    dict(name="Copay", type="amount", aux=True, labels=[
        "Co-pay Amount", "Co-payment", "Co-pay", "Patient Share"]),
    dict(name="TDS", type="amount", aux=True, labels=[
        "Tax Deducted at Source", "TDS Amount", "TDS"]),
    dict(name="Discount", type="amount", aux=True, labels=[
        "Hospital Discount", "Discount Amount"]),
]


def normalise_fields(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,\n]", raw)
    return [re.sub(r"[^a-z0-9_]+", "_", str(item).strip().lower()).strip("_") for item in (raw or []) if str(item).strip()]


def _key(name: str) -> str:
    return normalise_fields([name])[0]


BUILTIN_BY_KEY = {_key(f["name"]): f for f in BUILTIN_FIELDS}


def labels_of(f):
    """All wordings that count as a label of this field (main + fallback)."""
    return list(f["labels"]) + list(f.get("fallback_labels", []))


def load_dictionary() -> dict:
    return json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))


def save_dictionary(data: dict) -> None:
    DICTIONARY_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def guess_type(name: str) -> str:
    if any(token in name for token in ("amount", "total", "price", "value", "bill", "premium")): return "amount"
    if "date" in name: return "date"
    if "phone" in name or "mobile" in name: return "phone"
    if "email" in name: return "email"
    if any(token in name for token in ("number", "reference", "_id", "policy", "claim", "invoice", "_no")): return "id"
    return "text"


def _merge_labels(*groups) -> list[str]:
    seen, out = set(), []
    for group in groups:
        for label in group or []:
            clean = str(label).strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                out.append(clean)
    return out


def resolve_fields(raw, dictionary: dict | None = None) -> list[dict]:
    """Tell the UI how each requested field will be searched for."""
    dictionary = dictionary if dictionary is not None else load_dictionary()
    all_labels = _merge_labels(*(e.get("labels", []) for e in dictionary.values()),
                               *(labels_of(f) for f in BUILTIN_FIELDS))
    mappings = []
    for key in normalise_fields(raw):
        entry = dictionary.get(key) or {}
        custom = entry.get("labels", [])
        builtin = BUILTIN_BY_KEY.get(key)
        if builtin:
            mappings.append({"field": key, "type": builtin["type"], "matched_labels": _merge_labels(labels_of(builtin), custom),
                             "custom_labels": custom, "status": "known", "confidence": 1.0})
        elif entry:
            mappings.append({"field": key, "type": entry.get("type", "text"), "matched_labels": custom,
                             "custom_labels": custom, "status": "known", "confidence": 1.0})
        else:
            match = fuzzy_process.extractOne(key.replace("_", " "), all_labels, scorer=fuzz.token_set_ratio)
            if match and match[1] >= 65:
                mappings.append({"field": key, "type": guess_type(key), "matched_labels": [match[0]], "custom_labels": [],
                                 "status": "fuzzy", "confidence": round(match[1] / 100, 2)})
            else:
                mappings.append({"field": key, "type": guess_type(key), "matched_labels": [], "custom_labels": [],
                                 "status": "unknown", "confidence": 0.0, "warning": "No dictionary label matched"})
    return mappings


def display_name(key: str, dictionary: dict) -> str:
    builtin = BUILTIN_BY_KEY.get(key)
    if builtin:
        return builtin["name"]
    return (dictionary.get(key) or {}).get("display_name") or key.replace("_", " ").capitalize()


def build_field_defs(requested: list[str], dictionary: dict) -> list[dict]:
    """One field definition per requested key, in the requested order."""
    defs = []
    for key in requested:
        entry = dictionary.get(key) or {}
        builtin = BUILTIN_BY_KEY.get(key)
        if builtin:
            fd = copy.deepcopy(builtin)
            fd["labels"] = _merge_labels(fd["labels"], entry.get("labels", []))
        else:
            fd = dict(type=entry.get("type") or guess_type(key), labels=[])
            labels = _merge_labels(entry.get("labels", []))
            if not labels:
                # Last resort: look for the field by its own name, as typed in the UI.
                labels = [display_name(key, dictionary), key.replace("_", " ")]
            fd["labels"] = _merge_labels(labels)
        fd["key"] = key
        fd["name"] = display_name(key, dictionary)
        defs.append(fd)
    return defs


# ----------------------------------------------------------------------------
# Regex building blocks
# ----------------------------------------------------------------------------
MONTH = r"[A-Za-z]{3,9}"
DATE_PAT = (r"(?:\d{4}-\d{1,2}-\d{1,2}"
            r"|\d{1,2}[\s\-/.]+(?:\d{1,2}|" + MONTH + r")[\s\-/.,]+\d{2,4}"
            r"|" + MONTH + r"\.?\s+\d{1,2},?\s+\d{4})")
AMT_PAT = r"(?:(?:INR|Rs\.?|\u20b9)\s*[:\-]?\s*)?(\d[\d,]*(?:\.\d+)?)(?!\s*[-/]\s*\d)"
ID_PAT = r"\(?([A-Za-z0-9][A-Za-z0-9/\-_.]{2,}[A-Za-z0-9])\)?"
EMAIL_PAT = r"([^\s@<>()]+@[^\s@<>()]+\.[A-Za-z]{2,})"
PHONE_PAT = r"(\+?\d[\d\s\-()]{5,}\d)"
FILL = r"(?:[\s:=\-\u2013]|\bfor\b|\bof\b|\bis\b|\bwas\b|\bon\b|\bdated\b)*"
CURRENCY_ONLY = re.compile(r"(?:INR|Rs\.?|\u20b9)", re.I)
TEXT_STOP = re.compile(r"\s+(?:at|At|AT)\s+(?=[A-Z])|\s{3,}|"
                       r"\s+(?:for the period|for the|during|between)\b")
HOSPITAL_CUT = re.compile(r"\s+at\s+\S.{0,70}?(?:Hospital|Clinic|Nursing|Medical|Centre|Center|"
                          r"Healthcare|Institute|Sanatorium)", re.I)
TYPED = {"date": r"(" + DATE_PAT + r")", "amount": AMT_PAT, "id": ID_PAT,
         "email": EMAIL_PAT, "phone": PHONE_PAT}
RANGE_PAT = re.compile(
    r"(?:period\s+(?:from\s+)?|from\s+|between\s+|hospitali[sz]ed\s+from\s+)"
    r"(" + DATE_PAT + r")(?:\s+\d{1,2}:\d{2}(?:\s*(?:hrs|AM|PM))?)?"
    r"\s+(?:to|and|till|until)\s+(" + DATE_PAT + r")", re.I)
TOTAL_ROW = re.compile(r"(?:grand\s+)?total\s*(?:[\d,.\s]|INR|Rs\.?|\u20b9|-)*", re.I)
BARE_DATE = re.compile(r"(?<![A-Za-z0-9])Dated?(?![A-Za-z])", re.I)


def label_pat(label):
    toks = [t for t in re.split(r"[\s\-_]+", label.strip()) if t]
    body = r"[\s\-_]*".join(re.escape(t) for t in toks)
    return (r"(?<![A-Za-z0-9])" + body +
            r"(?![A-Za-z])\.?(?:\s*\((?:INR|Rs\.?|\u20b9)\))?")


class Ctx:
    """Everything that depends on the field list (the notebook kept these as module globals).

    `fields` are the fields to extract. `context` are extra fields (the remaining built-ins)
    whose labels are only used to recognise where one label block ends and another begins, so a
    field behaves the same no matter which other fields were requested."""

    def __init__(self, fields, context=()):
        self.fields = list(fields)
        everything = self.fields + [c for c in context if c["name"] not in {f["name"] for f in self.fields}]
        all_labels = sorted({l for f in everything for l in labels_of(f)}, key=len, reverse=True)
        self.all_label_full = [re.compile(label_pat(l) + r"\s*[:\-\u2013]?", re.I)
                               for f in everything for l in labels_of(f)]
        self.labels_at_end = re.compile("(?:" + "|".join(label_pat(l) for l in all_labels)
                                        + r")[\s:\-\u2013]*$", re.I)
        self.label_at_start = re.compile("^(?:" + "|".join(label_pat(l) for l in all_labels) + ")", re.I)
        self.everything = everything
        self.other_labels = {}
        for f in everything:
            labs = sorted((l for g in everything if g["name"] != f["name"] for l in labels_of(g)),
                          key=len, reverse=True)
            self.other_labels[f["name"]] = re.compile("|".join(label_pat(l) for l in labs), re.I) if labs else None

    def follows_label(self, text, pos):
        """True if the text just before `pos` ends with another known label (a header block)."""
        return bool(self.labels_at_end.search(text[max(0, pos - 60):pos]))

    def is_label_line(self, line):
        line = line.strip()
        return any(p.fullmatch(line) for p in self.all_label_full)


# ----------------------------------------------------------------------------
# Value parsing / validation
# ----------------------------------------------------------------------------
def parse_date(raw):
    s = re.sub(r"[\s\-/.,]+", " ", raw.strip())
    s = re.sub(r"\bsept\b", "sep", s, flags=re.I)
    for fmt in ("%Y %m %d", "%d %m %Y", "%d %b %Y", "%d %B %Y",
                "%d %m %y", "%d %b %y", "%b %d %Y", "%B %d %Y"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if 1990 <= d.year <= 2100:
            return d.date()
    return None


def parse_amount(raw):
    try:
        v = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


def finish(fd, raw):
    """Validate a raw string for the field type; return the output value or None."""
    t = fd["type"]
    if t == "date":
        d = parse_date(raw)
        if not d:
            return None
        return d.isoformat() if OUTPUT_ISO_DATES else raw.strip()
    if t == "amount":
        v = parse_amount(raw)
        if v is None:
            return None
        return v if OUTPUT_NUMERIC_AMOUNTS else raw.strip()
    if t == "id":
        if not re.search(r"\d", raw) or len(raw) < 4:
            return None
        if re.fullmatch(DATE_PAT, raw):
            return None
        return raw
    if t == "phone":
        digits = re.sub(r"\D", "", raw)
        return raw.strip() if 7 <= len(digits) <= 15 and not re.fullmatch(DATE_PAT, raw.strip()) else None
    return raw  # email / text: validated by the pattern or in clean_text


def clean_text(ctx, fd, text):
    v = text.strip()
    m = HOSPITAL_CUT.search(v)
    if m:
        v = v[:m.start()]
    v = TEXT_STOP.split(v, 1)[0]
    rx = ctx.other_labels.get(fd["name"])
    m = rx.search(v) if rx else None
    if m and m.start() > 0:
        v = v[:m.start()]
    v = v.strip(" :-\u2013,.;|")
    if len(v) < 3 or ctx.is_label_line(v) or not re.search(r"[A-Za-z]", v):
        return None
    return v[:200]


def interpret(ctx, fd, text):
    """text begins at (or just before) the candidate value. -> (raw, value) or None"""
    text = re.sub(r"^" + FILL, "", text)
    if fd["type"] == "text":
        v = clean_text(ctx, fd, text)
        return (v, v) if v else None
    m = re.match(TYPED.get(fd["type"], TYPED["id"]), text)
    if not m:
        return None
    raw = m.group(1)
    val = finish(fd, raw)
    return (raw, val) if val is not None else None


# ----------------------------------------------------------------------------
# PDF to structure
# ----------------------------------------------------------------------------
def build_rows(words, tol=3.0):
    items = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in items:
        yc = (w[1] + w[3]) / 2
        if rows and abs(rows[-1]["yc"] - yc) <= tol:
            rows[-1]["words"].append(w)
        else:
            rows.append({"yc": yc, "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w[0])
        r["text"] = " ".join(w[4] for w in r["words"])
    return rows


def rows_from_text(text):
    """OCR gives text only: lay the lines out on a synthetic grid (one row per line)."""
    rows = []
    for i, line in enumerate(l.strip() for l in text.splitlines() if l.strip()):
        words, x = [], 0.0
        for token in line.split():
            words.append((x, i * 14.0, x + 6.0 * len(token), i * 14.0 + 10.0, token))
            x += 6.0 * (len(token) + 1)
        rows.append({"yc": i * 14.0 + 5.0, "words": words, "text": " ".join(w[4] for w in words)})
    return rows


def page_tables(page):
    out = []
    try:
        for t in page.find_tables().tables:
            data = [[re.sub(r"\s+", " ", c or "").strip() for c in row] for row in t.extract()]
            if data:
                out.append(data)
    except Exception:
        pass
    return out


class Doc:
    def __init__(self, path, ocr_reader=None, progress=None):
        self.path = Path(path)
        self.pages = []
        self.methods = []
        with fitz.open(path) as d:
            if d.needs_pass:
                raise ValueError("PDF is password protected")
            total = d.page_count
            for i, p in enumerate(d):
                text = p.get_text("text")
                if sum(ch.isalnum() for ch in text) < 20 and ocr_reader:
                    text = ocr_reader(p, i) or text
                    self.methods.append("ocr")
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    self.pages.append(dict(no=i + 1, height=max(1.0, len(lines) * 14.0), rows=rows_from_text(text),
                                           lines=lines, tables=[]))
                else:
                    self.methods.append("text")
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    self.pages.append(dict(no=i + 1, height=p.rect.height, rows=build_rows(p.get_text("words")),
                                           lines=lines, tables=page_tables(p)))
                if progress:
                    progress(i + 1, total)
        self.flat = " ".join(" ".join(r["text"] for r in pg["rows"]) for pg in self.pages)
        self.flat_pdf = " ".join(" ".join(pg["lines"]) for pg in self.pages)  # PDF reading order
        self.chars = sum(len(l) for pg in self.pages for l in pg["lines"])


# ----------------------------------------------------------------------------
# Locator strategies
# ----------------------------------------------------------------------------
def _snip(s, a, b):
    return s[max(0, a - 25): b + 70].strip()


def s_inline(ctx, doc, fd, lab):
    """'Label: value' on one physical line."""
    lp = re.compile(label_pat(lab), re.I)
    for pg in doc.pages:
        for row in pg["rows"]:
            for m in lp.finditer(row["text"]):
                if ctx.follows_label(row["text"], m.start()):
                    continue  # header block: the value under this column is elsewhere
                yield row["text"][m.end():], pg["no"], _snip(row["text"], m.start(), m.end())


def s_inline_flat(ctx, doc, fd, lab):
    """Same, but across line breaks (sentences wrapped over several lines)."""
    lp = re.compile(label_pat(lab), re.I)
    for m in lp.finditer(doc.flat):
        if ctx.follows_label(doc.flat, m.start()):
            continue
        yield doc.flat[m.end(): m.end() + 200], 0, _snip(doc.flat, m.start(), m.end())


def s_inline_flat_pdf(ctx, doc, fd, lab):
    """Same as inline_flat, but in the PDF's own reading order (labels wrapped over two rows)."""
    lp = re.compile(label_pat(lab), re.I)
    for m in lp.finditer(doc.flat_pdf):
        if ctx.follows_label(doc.flat_pdf, m.start()):
            continue
        yield doc.flat_pdf[m.end(): m.end() + 200], 0, _snip(doc.flat_pdf, m.start(), m.end())


def s_table(ctx, doc, fd, lab):
    """Ruled tables: value in the cell to the right, or below the header cell."""
    lp = re.compile(label_pat(lab), re.I)
    for pg in doc.pages:
        for data in pg["tables"]:
            for r, row in enumerate(data):
                for c, cell in enumerate(row):
                    m = lp.search(cell) if cell and len(cell) < 80 else None
                    if not m:
                        continue
                    if cell[m.end():].strip(" :-"):
                        yield cell[m.end():], pg["no"], cell
                    for cc in range(c + 1, min(c + 3, len(row))):
                        if row[cc]:
                            yield row[cc], pg["no"], f"{cell} | {row[cc]}"
                    for rr in range(r + 1, min(r + 3, len(data))):
                        if data[rr][c]:
                            yield data[rr][c], pg["no"], f"{cell} / {data[rr][c]}"


def _label_cols(row, m):
    """x-range of the words covered by regex match m on this row."""
    spans, pos = [], 0
    for w in row["words"]:
        spans.append((pos, pos + len(w[4])))
        pos += len(w[4]) + 1
    idx = [k for k, (a, b) in enumerate(spans) if a < m.end() and b > m.start()]
    if not idx:
        return None
    return row["words"][idx[0]][0], row["words"][idx[-1]][2]


def s_total_row(ctx, doc, fd, lab):
    """Category tables: the value is in the 'Total' row, under the column header."""
    if fd["type"] != "amount":
        return
    lp = re.compile(label_pat(lab), re.I)
    for pg in doc.pages:
        rows = pg["rows"]
        totals = [i for i, r in enumerate(rows) if TOTAL_ROW.fullmatch(r["text"].strip())]
        if not totals:
            continue
        for ri, row in enumerate(rows):
            for m in lp.finditer(row["text"]):
                rng = _label_cols(row, m)
                below = [t for t in totals if t > ri and rows[t]["yc"] - row["yc"] < 450]
                if not rng or not below:
                    continue
                x0, x1 = rng
                trow = rows[below[0]]
                label_end = trow["words"][0][2]

                def in_col(w):
                    return w[2] > x0 - 12 and w[0] < x1 + 12 and w[0] > label_end + 2

                cand = [w for w in trow["words"] if in_col(w)]
                if not cand:  # value printed a little above/below the Total line (wrapped note)
                    for r in rows:
                        if r is trow or abs(r["yc"] - trow["yc"]) > 24:
                            continue
                        if min(w[0] for w in r["words"]) <= label_end + 20:
                            continue  # an item row with its own label, not a continuation
                        cand += [w for w in r["words"] if in_col(w)]
                    cand.sort(key=lambda w: (w[1], w[0]))
                if cand:
                    yield (" ".join(w[4] for w in cand), pg["no"],
                           f"{row['text']} // {trow['text']}")


def s_column(ctx, doc, fd, lab):
    """Header row, value row underneath (unruled tables), matched by x-position."""
    lp = re.compile(label_pat(lab), re.I)
    for pg in doc.pages:
        rows = pg["rows"]
        for ri, row in enumerate(rows):
            # labels stacked vertically (one per line): geometry cannot tell columns apart
            if (ri > 0 and ctx.is_label_line(rows[ri - 1]["text"])) or \
               (ri + 1 < len(rows) and ctx.is_label_line(rows[ri + 1]["text"])):
                continue
            if not ctx.label_at_start.match(row["text"]):
                continue  # header with an unknown first column ("Particular", "Charge Type"):
                          # an itemised table, whose value is the Total row, not the first item
            spans, pos = [], 0
            for w in row["words"]:
                spans.append((pos, pos + len(w[4])))
                pos += len(w[4]) + 1
            for m in lp.finditer(row["text"]):
                idx = [k for k, (a, b) in enumerate(spans) if a < m.end() and b > m.start()]
                if not idx:
                    continue
                x0, x1 = row["words"][idx[0]][0], row["words"][idx[-1]][2]
                nxt = (row["words"][idx[-1] + 1][0] if idx[-1] + 1 < len(row["words"])
                       else x1 + 150)
                for rr in rows[ri + 1: ri + 3]:
                    cand = [w for w in rr["words"] if w[2] > x0 - 8 and w[0] < nxt - 4]
                    if cand:
                        txt = " ".join(w[4] for w in cand)
                        yield txt, pg["no"], f"{row['text']} // {rr['text']}"
                        break


def s_next_line(ctx, doc, fd, lab):
    """Label alone on a line, value on the following line (PDF text order)."""
    full = re.compile(label_pat(lab) + r"\s*[:\-\u2013]?", re.I)
    for pg in doc.pages:
        L = pg["lines"]
        for i, ln in enumerate(L[:-1]):
            if not full.fullmatch(ln):
                continue
            nxt = L[i + 1]
            if ctx.is_label_line(nxt) or (i > 0 and ctx.is_label_line(L[i - 1])):
                continue  # part of a label block: handled by column_order
            if CURRENCY_ONLY.fullmatch(nxt) and i + 2 < len(L):
                nxt = nxt + " " + L[i + 2]
            yield nxt, pg["no"], f"{ln} / {nxt}"


def s_column_order(ctx, doc, fd, lab):
    """Flattened table: a block of label lines, followed by a block of values."""
    full = re.compile(label_pat(lab) + r"\s*[:\-\u2013]?", re.I)
    for pg in doc.pages:
        L = pg["lines"]
        for i, ln in enumerate(L):
            if not full.fullmatch(ln):
                continue
            s = i
            while s > 0 and ctx.is_label_line(L[s - 1]):
                s -= 1
            e = i
            while e + 1 < len(L) and ctx.is_label_line(L[e + 1]):
                e += 1
            n = e - s + 1
            if n < 2:
                continue
            vals, k = [], e + 1
            while k < len(L) and len(vals) < n:
                v = L[k]
                if CURRENCY_ONLY.fullmatch(v) and k + 1 < len(L):
                    v = v + " " + L[k + 1]
                    k += 1
                vals.append(v)
                k += 1
            if len(vals) != n:
                continue
            # alignment check: every KNOWN label in the block must get a value of its own type,
            # otherwise the columns are shifted (e.g. an unknown first column "Particular")
            aligned = True
            for j in range(n):
                for other in ctx.everything:
                    if any(re.fullmatch(label_pat(l) + r"\s*[:\-\u2013]?", L[s + j], re.I)
                           for l in labels_of(other)):
                        if other["type"] != "text" and not interpret(ctx, other, vals[j]):
                            aligned = False
                        break
            if aligned:
                yield vals[i - s], pg["no"], f"label block {n} cols, col {i - s + 1}: {vals[i - s]}"


def s_letter_date(doc):
    """A bare 'Date:' in the top part of page 1 (the date printed on the letter itself).
    Rejected when a word sits right before it ('Approval Date', 'Admission Date', ...), so it can
    never be a qualified date label; only 'Date:' at the start of a line or after a reference
    number ('Ref: ABC/12/2026   Date: 03 Aug 2026') counts."""
    pg = doc.pages[0] if doc.pages else None
    if not pg:
        return
    limit = pg["height"] * LETTER_DATE_TOP_FRACTION
    for row in pg["rows"]:
        if row["yc"] > limit:
            break
        for m in BARE_DATE.finditer(row["text"]):
            prev = row["text"][:m.start()].split()
            if prev and re.fullmatch(r"[A-Za-z]+", prev[-1]):
                continue
            yield row["text"][m.end():], pg["no"], _snip(row["text"], m.start(), m.end())


STRATEGIES = [("inline", s_inline), ("inline_flat", s_inline_flat),
              ("inline_pdf_order", s_inline_flat_pdf), ("table", s_table),
              ("total_row", s_total_row), ("column", s_column), ("next_line", s_next_line),
              ("column_order", s_column_order)]
CONF = dict(inline=0.95, inline_flat=0.85, inline_pdf_order=0.85, table=0.90, total_row=0.85,
            column=0.85, next_line=0.85, column_order=0.60, range=0.80, letter_date=0.60, llm=0.50)


def _try_labels(ctx, doc, fd, labels, fallback=False):
    found = []
    for lab in labels:
        hit = None
        for sname, fn in STRATEGIES:
            for text, pg, snip in fn(ctx, doc, fd, lab):
                r = interpret(ctx, fd, text)
                if r:
                    hit = dict(value=r[1], raw=r[0], label=lab, strategy=sname, page=pg,
                               snippet=snip, confidence=CONF[sname])
                    if fallback:
                        hit["fallback"] = True
                        hit["confidence"] = min(hit["confidence"], 0.70)
                    if fd["type"] == "amount":
                        hit["num"] = parse_amount(r[0])
                    break
            if hit:
                break
        if hit:
            found.append(hit)
    return found


def extract_field(ctx, doc, fd):
    """Winner = first label (in priority order) with a valid value. Other labels that also
    give a DIFFERENT valid value are kept as `alternatives`, so conflicts are visible."""
    found = _try_labels(ctx, doc, fd, fd["labels"])
    if not found and "range_idx" in fd:  # "period from X to Y" style sentence
        m = RANGE_PAT.search(doc.flat)
        if m:
            raw = m.group(fd["range_idx"] + 1)
            val = finish(fd, raw)
            if val is not None:
                found.append(dict(value=val, raw=raw, label="period from .. to ..",
                                  strategy="range", page=0,
                                  snippet=_snip(doc.flat, m.start(), m.end()),
                                  confidence=CONF["range"]))
    if not found and fd.get("fallback_labels") and CLAIM_DATE_FALLBACK_TO_LETTER_DATE:
        found = _try_labels(ctx, doc, fd, fd["fallback_labels"], fallback=True)
        if not found:  # bare "Date:" at the top of page 1
            for text, pg, snip in s_letter_date(doc):
                r = interpret(ctx, fd, text)
                if r:
                    found.append(dict(value=r[1], raw=r[0], label="Date (top of letter)",
                                      strategy="letter_date", page=pg, snippet=snip,
                                      confidence=0.60, fallback=True))
                    break
    if not found:
        return None
    win, seen, alts = found[0], {found[0]["value"]}, []
    for h in found[1:]:
        if h["value"] not in seen:
            seen.add(h["value"])
            alts.append(h)
    win["alternatives"] = alts
    return win


def llm_fill(ctx, doc, fields, llm: LLM) -> dict:
    """Ask the local LLM for `fields`. -> {field name: hit}. Raises LLMError if it is unreachable.

    The model may only copy text: an answer is accepted when it is printed in the PDF and, for
    typed fields, passes the same validation as a regex hit. Anything else is dropped."""
    text = document_text(doc.pages)
    fields = [dict(fd, key=fd.get("key") or _key(fd["name"])) for fd in fields]  # helper fields have no key
    hits = {}
    for key, raw in ask_llm(llm, text, fields).items():
        fd = next(f for f in fields if f["key"] == key)
        m = re.search(r"\s+".join(map(re.escape, raw.split())), text, re.I) if raw else None
        if not m:
            continue  # not in the document: the model made it up
        if fd["type"] == "text":
            raw, value = m.group(0), m.group(0)[:200]
        else:
            parsed = interpret(ctx, fd, m.group(0))
            if not parsed:
                continue
            raw, value = parsed
        hit = dict(value=value, raw=raw, label="local LLM", strategy="llm", page=0,
                   snippet=_snip(text, m.start(), m.end()), confidence=CONF["llm"], alternatives=[],
                   note="read by the local LLM; the value is printed in the PDF, but verify it")
        if fd["type"] == "amount":
            hit["num"] = parse_amount(raw)
        hits[fd["name"]] = hit
    return hits


def _same(fd, a, b) -> bool:
    if fd["type"] != "text":
        return a == b
    na, nb = (re.sub(r"\W+", " ", str(v)).strip().casefold() for v in (a, b))
    return na == nb or na in nb or nb in na  # the model may include a few more or fewer words


def reconcile(defs, details, llm_hits, warnings):
    """Combine the regex result (`details`) with the LLM's answers, field by field.

    both agree -> regex hit kept, confidence raised; regex found nothing -> LLM value used and
    flagged; they differ -> regex value kept, LLM value becomes an alternative (so the amounts
    check can still pick it when the arithmetic proves it) and the disagreement is flagged."""
    requested = {fd["name"]: fd for fd in defs}
    for name, hit in llm_hits.items():
        reg, fd = details.get(name), requested.get(name)
        key = fd["key"] if fd else _key(name)
        if reg is None:
            details[name] = hit
            if fd:
                warnings.append(f"{key}: not found by regex, filled by the local LLM: please verify")
        elif _same(fd or dict(type=guess_type(_key(name))), reg["value"], hit["value"]):
            reg["confidence"] = max(reg["confidence"], 0.98)
            reg["confirmed_by_llm"] = True
        else:
            reg["alternatives"].append(hit)
            if fd:
                warnings.append(f"{key}: regex and local LLM disagree (regex {reg['value']!r}, LLM "
                                f"{hit['value']!r}); using regex: please verify")


def check_amounts(details):
    """The 'brain': bill - non-pay - deductions (copay, TDS, discount) must equal the amount paid.
    Tries the winners first, then alternatives, then subsets of the deductions."""
    def hits(name):
        d = details.get(name)
        return [d] + list(d["alternatives"]) if d else [None]

    aux = [details[n]["num"] for n in ("Copay", "TDS", "Discount") if details.get(n)]
    bills = hits("Final Bill")
    if bills == [None] and details.get("Claimed amount"):
        # no Final Bill found: 'Amount Claimed' may be it, but only if the arithmetic proves it
        bills = [details["Claimed amount"]]
    for b, p, n in itertools.product(bills, hits("Settled amount"), hits("Not settled amount")):
        if b is None or p is None:
            continue
        winners = (b is details.get("Final Bill") and p is details.get("Settled amount")
                   and n is details.get("Not settled amount"))
        for k in range(len(aux), -1, -1):
            for sub in itertools.combinations(aux, k):
                if not winners and n is None and not sub:
                    continue  # "bill = paid" built from alternatives proves nothing
                exp = b["num"] - (n["num"] if n else 0) - sum(sub)
                if abs(exp - p["num"]) <= max(2, 0.001 * b["num"]):
                    return (b, p, n), sub
    return None, None


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def extract_pdf(path, requested_fields, ocr_reader=None, progress=None, llm: LLM | None = None) -> dict:
    """Extract `requested_fields` (field keys such as "claim_number") from one PDF.

    The regex pass and the local LLM (Qwen3-8B via `LLM_BASE_URL`, or the `llm` callable passed in)
    both read the PDF and are reconciled field by field: disagreements and LLM-only values are
    flagged in `warnings`, and an unreachable LLM is reported there too.

    Returns the job-result dict used by the API/UI: `output` is the CSV/JSON row keyed by the
    column names, `data`/`evidence`/`confidence` are keyed by field key."""
    path = Path(path)
    dictionary = load_dictionary()
    requested = list(dict.fromkeys(normalise_fields(requested_fields)))
    defs = build_field_defs(requested, dictionary)
    names = {fd["key"]: fd["name"] for fd in defs}
    check_wanted = {"Final Bill", "Settled amount"} <= set(names.values())
    helpers = []
    if check_wanted:  # needed for the arithmetic check, but only shown if the user asked for them
        have = set(names.values())
        helpers = [copy.deepcopy(f) for f in BUILTIN_FIELDS + AUX_FIELDS
                   if f["name"] in {"Not settled amount", "Claimed amount", "Copay", "TDS", "Discount"}
                   and f["name"] not in have]
    context = [copy.deepcopy(f) for f in BUILTIN_FIELDS + AUX_FIELDS]
    ctx = Ctx(defs + helpers, context)

    output = {"Source PDF": path.name}
    warnings, details = [], {}
    base = {"file": path.name, "pages": 0, "extraction_method": [], "evidence": {}, "confidence": {},
            "fields_total": len(requested)}
    try:
        doc = Doc(path, ocr_reader=ocr_reader, progress=progress)
    except Exception as exc:
        for fd in defs:
            output[fd["name"]] = None
        if check_wanted:
            output["Amounts check"] = "incomplete"
        return dict(base, status="failed", data={fd["key"]: None for fd in defs}, output=output,
                    warnings=[f"could not open PDF: {exc}"], fields_found=0)
    if "ocr" not in doc.methods and doc.chars < 20 * max(1, len(doc.pages)):
        warnings.append("very little text: probably a scanned PDF (needs OCR, not this script)")
    for fd in ctx.fields:
        details[fd["name"]] = extract_field(ctx, doc, fd)
    try:  # the local LLM reads every PDF too; its answers are reconciled with the regex ones
        reconcile(defs, details, llm_fill(ctx, doc, ctx.fields, llm or get_llm()), warnings)
    except LLMError as error:
        warnings.append(f"{error}: result is regex-only and was NOT cross-checked by the LLM")
    for fd in defs:
        res = details[fd["name"]]
        output[fd["name"]] = res["value"] if res else None
        if not res:
            warnings.append(f"{fd['key']}: value not found")

    # --- arithmetic check on the amounts -------------------------------------------------
    if check_wanted:
        combo, _sub = check_amounts(details)
        bill_d, paid_d = details.get("Final Bill"), details.get("Settled amount")
        if combo:
            for name, hit in zip(("Final Bill", "Settled amount", "Not settled amount"), combo):
                if hit is not None and hit is not details.get(name):
                    old = details.get(name)
                    note = ("taken from 'Amount Claimed' because the amounts reconcile"
                            if old is None else "chosen because the amounts reconcile")
                    hit = dict(hit, note=note, alternatives=[dict(old, alternatives=[])] if old else [])
                    details[name] = hit
                    if name in output:
                        output[name] = hit["value"]
                    warnings = [w for w in warnings if not w.startswith(_key(name) + ":")]
            output["Amounts check"] = "reconciled"
        elif bill_d and paid_d:
            n = details.get("Not settled amount")
            ded = sum(details[k]["num"] for k in ("Copay", "TDS", "Discount") if details.get(k))
            exp = bill_d["num"] - (n["num"] if n else 0) - ded
            output["Amounts check"] = "MISMATCH" if n else "incomplete (no non-pay amount found)"
            warnings.append(f"amounts do not reconcile: bill {bill_d['num']} - non-pay "
                            f"{n['num'] if n else 0} - deductions {ded} = {round(exp, 2)}, "
                            f"but paid is {paid_d['num']}: check this PDF by eye")
        else:
            output["Amounts check"] = "incomplete"

    cn = details.get("Claim number")
    if cn and cn.get("alternatives") and "Claim number" in names.values():
        warnings.append("several claim numbers found: " + ", ".join(
            [str(cn["value"])] + [str(a["value"]) for a in cn["alternatives"]]) +
            f" (using {cn['value']} from '{cn['label']}')")
    cd = details.get("Claim date")
    if cd and cd.get("fallback") and "Claim date" in names.values():
        warnings.append(f"Claim date is the LETTER date ('{cd['label']}'), not a claim/registration "
                        f"date: no claim-date label exists in this PDF")
    if output.get("Claim date") and output.get("Claim date") == output.get("Settlement date"):
        warnings.append("Claim date equals Settlement date: check the claim-date label")
    if OUTPUT_ISO_DATES and output.get("Admission date") and output.get("Discharge date"):
        if output["Admission date"] > output["Discharge date"]:
            warnings.append("Admission date is after Discharge date")

    data, evidence, confidence = {}, {}, {}
    for fd in defs:
        res = details.get(fd["name"])
        data[fd["key"]] = output[fd["name"]]
        if res:
            confidence[fd["key"]] = res["confidence"]
            evidence[fd["key"]] = {
                "page": res["page"], "strategy": res["strategy"], "label": res["label"],
                "snippet": res["snippet"], "raw": res["raw"], "bbox": None,
                **({"note": res["note"]} if res.get("note") else {}),
                **({"fallback": True} if res.get("fallback") else {}),
                "alternatives": [{"value": a["value"], "label": a["label"], "strategy": a["strategy"]}
                                 for a in res.get("alternatives", [])],
            }
    found = sum(1 for fd in defs if output[fd["name"]] is not None)
    return dict(base, status="warning" if warnings else "done", pages=len(doc.pages),
                extraction_method=doc.methods, data=data, output=output, evidence=evidence,
                confidence=confidence, warnings=warnings, fields_found=found)


def results_to_csv(results: list[dict]) -> str:
    """One CSV row per PDF; columns are the union of the rows' keys, `Source PDF` first."""
    rows = []
    for item in results:
        row = item.get("output")
        if row is None:
            row = {"Source PDF": item.get("file"), **(item.get("data") or {})}
        rows.append(row)
    columns = ["Source PDF"]
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
