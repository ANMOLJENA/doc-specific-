import { useCallback, useEffect, useMemo, useState } from "react";
import { api, csv, downloadFile, originalUrl, pretty } from "./lib.js";

function flattenCases(cases) {
  return cases.flatMap(bundle => bundle.pages.map(page => ({
    ...page,
    caseId: bundle.case_id,
    createdAt: bundle.created_at,
    caseStatus: bundle.status,
    requestedEngine: bundle.requested_ocr_engine,
    analysisError: bundle.analysis?.error,
  })));
}

function documentId(row) {
  return `DOC-${String(row.image_id || "").slice(0, 8).toUpperCase()}`;
}

function batchId(row) {
  return `BATCH-${String(row.caseId || "").slice(0, 8).toUpperCase()}`;
}

function confidence(row) {
  const value = Number(row.ocr_confidence);
  return Number.isFinite(value) ? value : null;
}

function qualityKind(row) {
  if (row.status === "failed") return "failed";
  if (row.quality_gate?.forwarded_with_quality_warning || row.review_required) return "review";
  if (row.quality_gate?.enhancements?.length) return "enhanced";
  if (row.status === "processed") return "direct";
  return "pending";
}

function qualityLabel(row) {
  const kind = qualityKind(row);
  if (kind === "direct") return "Clean first pass";
  if (kind === "enhanced") return `Enhanced · ${row.quality_gate.enhancements.join(" + ")}`;
  if (kind === "review") return "OCR complete · review suggested";
  if (kind === "failed") return "Processing failed";
  return pretty(row.status);
}

function snippet(row) {
  const text = String(row.ocr_text || row.error || "OCR output not available").replace(/\s+/g, " ").trim();
  return text.length > 125 ? `${text.slice(0, 125)}…` : text;
}

export default function RepositoryPage() {
  const [payload, setPayload] = useState({ total: 0, cases: [] });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [quality, setQuality] = useState("all");
  const [minConfidence, setMinConfidence] = useState("all");
  const [view, setView] = useState("table");
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [selected, setSelected] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [page, setPage] = useState(1);
  const pageSize = 10;

  const load = useCallback(async () => {
    try {
      const result = await api("/cases?limit=100");
      setPayload(result);
      setError("");
    } catch (loadError) {
      setError(loadError.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(); }, 3000);
    return () => window.clearInterval(timer);
  }, [load]);
  const rows = useMemo(() => flattenCases(payload.cases), [payload]);
  useEffect(() => { if (!selected && rows.length) setSelected(rows[0]); }, [rows, selected]);
  useEffect(() => {
    setSelected(current => current ? rows.find(row => row.image_id === current.image_id) || current : null);
  }, [rows]);

  const categories = useMemo(() => [...new Set(rows.map(row => row.category).filter(Boolean))].sort(), [rows]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter(row => {
      if (category !== "all" && (row.category || "uncategorized") !== category) return false;
      if (quality !== "all" && qualityKind(row) !== quality) return false;
      if (minConfidence !== "all" && (confidence(row) == null || confidence(row) < Number(minConfidence))) return false;
      return !needle || [row.name, row.caseId, row.image_id, row.category, row.ocr_text, row.status].some(value => String(value || "").toLowerCase().includes(needle));
    });
  }, [rows, query, category, quality, minConfidence]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  useEffect(() => { setPage(1); }, [query, category, quality, minConfidence]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);
  const visible = filtered.slice((page - 1) * pageSize, page * pageSize);
  const processed = rows.filter(row => row.status === "processed");
  const passCount = processed.filter(row => !row.review_required && !row.quality_gate?.forwarded_with_quality_warning).length;
  const passRate = processed.length ? Math.round((passCount / processed.length) * 1000) / 10 : null;
  const linked = selected ? rows.filter(row => row.caseId === selected.caseId) : [];

  const toggle = id => setSelectedIds(current => {
    const next = new Set(current);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const exportRows = () => {
    const chosen = selectedIds.size ? rows.filter(row => selectedIds.has(row.image_id)) : filtered;
    downloadFile("document-repository.csv", csv([
      ["document_id", "bundle_id", "filename", "created_at", "status", "category", "quality", "ocr_engine", "confidence", "ocr_text"],
      ...chosen.map(row => [documentId(row), row.caseId, row.name, row.createdAt, row.status, row.category, qualityLabel(row), row.ocr_method || row.requestedEngine, confidence(row), row.ocr_text]),
    ]), "text/csv;charset=utf-8");
  };
  const deleteRows = async rowsToDelete => {
    const ids = [...new Set(rowsToDelete.map(row => row.image_id).filter(Boolean))];
    if (!ids.length || deleting) return;
    const description = ids.length === 1 ? "this document" : `${ids.length} selected documents`;
    if (!window.confirm(`Permanently delete ${description}? This removes its OCR record and saved original image. This cannot be undone.`)) return;
    setDeleting(true);
    try {
      await api("/pages", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ page_ids: ids }) });
      setSelectedIds(current => new Set([...current].filter(id => !ids.includes(id))));
      setSelected(current => current && ids.includes(current.image_id) ? null : current);
      await load();
    } catch (deleteError) {
      setError(`Could not delete the selected document${ids.length === 1 ? "" : "s"}: ${deleteError.message}`);
    } finally {
      setDeleting(false);
    }
  };

  return <main className="enterprise-main repository-page">
    {rows.some(row => row.caseStatus === "processing" && !row.category) && <div className="alert" role="status">Document classification is still processing. Categories refresh automatically when the AI finishes.</div>}
    {payload.cases.slice(0, 1).map(bundle => bundle.analysis?.error && <div className="alert error" role="alert" key={bundle.case_id}>Latest upload: AI classification failed — {bundle.analysis.error}</div>)}
    <div className="repo-breadcrumbs">⌂ Workspace <span>/</span> Repositories <span>/</span> <b>All Documents Archive</b></div>
    <section className="enterprise-panel repository-hero">
      <div className="repository-title-row"><div><h1>Document Repository</h1><p>Searchable document store with OCR lineage, quality decisions, source images, and extracted text.</p></div><div className="page-actions"><button onClick={exportRows}>⇩ Export CSV</button><button className="danger-action" disabled={!selectedIds.size || deleting} onClick={() => void deleteRows(rows.filter(row => selectedIds.has(row.image_id)))}>⌫ {deleting ? "Deleting…" : `Delete selected${selectedIds.size ? ` (${selectedIds.size})` : ""}`}</button><button disabled title="Re-indexing is not implemented">↻ Batch Re-index</button><button className="primary-button" onClick={() => document.getElementById("repository-query")?.focus()}>▽ Advanced Query</button></div></div>
      <div className="repo-metrics">
        <article><span>Total documents stored</span><strong>{rows.length.toLocaleString()}</strong><small>{payload.total.toLocaleString()} bundles indexed</small></article>
        <article><span>Quality pass rate</span><strong>{passRate == null ? "—" : `${passRate}%`}</strong><small>Completed without a quality warning</small></article>
        <article><span>Review suggested</span><strong>{rows.filter(row => row.review_required).length.toLocaleString()}</strong><small>Verify these against the original</small></article>
      </div>
    </section>
    {error && <div className="enterprise-alert error">Could not load the repository: {error}</div>}

    <section className="enterprise-panel repository-filters">
      <div className="query-row"><label className="query-input"><span>⌕</span><input id="repository-query" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search text, filename, bundle ID, category…" /></label><button className="primary-button" onClick={() => setPage(1)}>Execute query</button><button onClick={() => { setQuery(""); setCategory("all"); setQuality("all"); setMinConfidence("all"); }}>↻ Reset</button></div>
      <div className="filter-row">
        <label>Classification<select value={category} onChange={event => setCategory(event.target.value)}><option value="all">All document types</option><option value="uncategorized">Uncategorized</option>{categories.map(item => <option key={item} value={item}>{pretty(item)}</option>)}</select></label>
        <label>Quality gate<select value={quality} onChange={event => setQuality(event.target.value)}><option value="all">All outcomes</option><option value="direct">Clean first pass</option><option value="enhanced">Enhanced</option><option value="review">Review suggested</option><option value="failed">Failed</option><option value="pending">Pending</option></select></label>
        <label>Confidence<select value={minConfidence} onChange={event => setMinConfidence(event.target.value)}><option value="all">Any confidence</option><option value="0.9">90% and above</option><option value="0.75">75% and above</option></select></label>
        <span className="result-count">{filtered.length} matching pages</span>
        <div className="view-toggle"><button className={view === "table" ? "active" : ""} onClick={() => setView("table")}>▤ Table</button><button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")}>▦ Cards</button></div>
      </div>
    </section>

    <section className="repository-workspace">
      <div className="enterprise-panel records-panel">
        <header className="records-head"><label><input type="checkbox" checked={visible.length > 0 && visible.every(row => selectedIds.has(row.image_id))} onChange={event => setSelectedIds(current => { const next = new Set(current); visible.forEach(row => event.target.checked ? next.add(row.image_id) : next.delete(row.image_id)); return next; })} /> {selectedIds.size} selected</label><span>Density: Compact</span></header>
        {view === "table" ? <div className="enterprise-table-wrap"><table className="enterprise-table repository-table"><thead><tr><th></th><th>Document &amp; preview</th><th>Bundle &amp; timestamp</th><th>Classification</th><th>Quality gate log</th><th>Extracted text</th><th>Confidence</th><th>Action</th></tr></thead><tbody>{visible.map(row => <tr key={row.image_id} className={selected?.image_id === row.image_id ? "selected-row" : ""} onClick={() => setSelected(row)}><td onClick={event => event.stopPropagation()}><input type="checkbox" checked={selectedIds.has(row.image_id)} onChange={() => toggle(row.image_id)} /></td><td><div className="doc-cell">{originalUrl(row) ? <img src={originalUrl(row)} alt="" /> : <span className="thumb-placeholder">▧</span>}<div><b className="mono blue-text">{documentId(row)}</b><small>{row.name}</small></div></div></td><td><b className="mono">{batchId(row)}</b><small>{new Date(row.createdAt).toLocaleString()}</small></td><td><span className="category-chip">{pretty(row.category || "Uncategorized")}</span></td><td><span className={`quality-dot ${qualityKind(row)}`} /> <b>{qualityLabel(row)}</b><small>{pretty(row.status)}</small></td><td className="text-snippet">{snippet(row)}</td><td>{confidence(row) == null ? <span className="muted">—</span> : <span className={`confidence-chip ${confidence(row) < .75 ? "low" : ""}`}>{Math.round(confidence(row) * 1000) / 10}%</span>}</td><td><button className="icon-table-button" onClick={event => { event.stopPropagation(); setSelected(row); }} aria-label={`Inspect ${row.name}`}>◎</button></td></tr>)}{!visible.length && <tr><td colSpan="8" className="enterprise-empty">{loading ? "Loading repository…" : "No documents match these filters."}</td></tr>}</tbody></table></div> : <div className="repository-card-grid">{visible.map(row => <button key={row.image_id} className={selected?.image_id === row.image_id ? "active" : ""} onClick={() => setSelected(row)}>{originalUrl(row) ? <img src={originalUrl(row)} alt="" /> : <span className="card-image-empty">▧</span>}<span className="mono blue-text">{documentId(row)}</span><b>{row.name}</b><small>{pretty(row.category || "Uncategorized")} · {qualityLabel(row)}</small></button>)}{!visible.length && <div className="enterprise-empty">No documents match these filters.</div>}</div>}
        <footer className="records-pagination"><span>Showing {filtered.length ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, filtered.length)} of {filtered.length}</span><div><button disabled={page === 1} onClick={() => setPage(value => value - 1)}>‹</button><span>{page} / {pageCount}</span><button disabled={page === pageCount} onClick={() => setPage(value => value + 1)}>›</button></div></footer>
      </div>

      <aside className="enterprise-panel repository-inspector">
        {selected ? <><header><div><i /><b className="mono">{documentId(selected)} details</b><small>{batchId(selected)}</small></div><a href={`/?case=${encodeURIComponent(selected.caseId)}`} title="Open full record">↗</a></header><div className="inspector-body">{originalUrl(selected) && <img className="inspector-preview" src={originalUrl(selected)} alt={`Original ${selected.name}`} />}<dl><dt>Filename</dt><dd>{selected.name}</dd><dt>Classification</dt><dd>{pretty(selected.category || "Uncategorized")}</dd><dt>OCR engine</dt><dd>{pretty(selected.ocr_method || selected.requestedEngine || "Not recorded")}</dd><dt>Quality</dt><dd>{qualityLabel(selected)}</dd></dl><div className="inspector-text"><span>Extracted OCR text</span><pre>{selected.ocr_text || selected.error || "OCR output is not available yet."}</pre></div><div className="linked-title"><b>Linked bundle records</b><span>{linked.length}</span></div><div className="linked-records">{linked.map(row => <button key={row.image_id} className={row.image_id === selected.image_id ? "active" : ""} onClick={() => setSelected(row)}><span><b className="mono">{documentId(row)}</b><small>{pretty(row.category || row.status)} · Page {row.page}</small></span><span>{confidence(row) == null ? "—" : `${Math.round(confidence(row) * 100)}%`}</span></button>)}</div></div><footer><a className="primary-action" href={`/?case=${encodeURIComponent(selected.caseId)}`}>Open extraction record</a><a className="secondary-action" href={originalUrl(selected) || "#"} target="_blank" rel="noreferrer">Original ↗</a><button className="danger-action" disabled={deleting} onClick={() => void deleteRows([selected])}>⌫ {deleting ? "Deleting…" : "Delete"}</button></footer></> : <div className="enterprise-empty">Select a document to inspect its source and linked bundle.</div>}
      </aside>
    </section>
  </main>;
}
