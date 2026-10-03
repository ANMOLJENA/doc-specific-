import { useCallback, useEffect, useMemo, useState } from "react";
import { api, originalUrl, pretty, seconds } from "./lib.js";

const activeStatuses = new Set(["queued", "processing", "quality_check", "enhancing"]);

function flattenCases(cases) {
  return cases.flatMap(bundle => bundle.pages.map(page => ({
    ...page,
    caseId: bundle.case_id,
    createdAt: bundle.created_at,
    caseStatus: bundle.status,
    requestedEngine: bundle.requested_ocr_engine,
  })));
}

function queueState(row) {
  if (row.status === "failed" || row.caseStatus === "failed") return "retrying";
  if (row.status === "processing" || row.caseStatus === "processing") return "extracting";
  if (row.quality_gate?.forwarded_with_quality_warning || row.review_required) return "quality";
  if (row.status === "processed") return "archived";
  return "intake";
}

function stageText(row) {
  const state = queueState(row);
  if (state === "quality") return "OCR complete · quality warning";
  if (state === "extracting") return `${row.requestedEngine === "surya" ? "Surya 2" : "PaddleOCR"} extraction`;
  if (state === "retrying") return "Processing failed";
  if (state === "archived") return "Archived to database";
  return "Queued for worker";
}

function shortId(value, prefix) {
  return `${prefix}-${String(value || "").slice(0, 8).toUpperCase()}`;
}

export default function QueuePage({ system }) {
  const [payload, setPayload] = useState({ total: 0, cases: [] });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("all");
  const [query, setQuery] = useState("");
  const [priority, setPriority] = useState(false);
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    try {
      const result = await api("/cases?limit=100");
      setPayload(result);
      setError("");
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  const rows = useMemo(() => flattenCases(payload.cases), [payload]);
  const counts = useMemo(() => ({
    intake: rows.filter(row => queueState(row) === "intake").length,
    quality: rows.filter(row => queueState(row) === "quality").length,
    extracting: rows.filter(row => queueState(row) === "extracting").length,
    retrying: rows.filter(row => queueState(row) === "retrying").length,
    archived: rows.filter(row => queueState(row) === "archived").length,
    active: rows.filter(row => activeStatuses.has(row.status) || activeStatuses.has(row.caseStatus)).length,
  }), [rows]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = rows.filter(row => {
      if (tab !== "all" && queueState(row) !== tab) return false;
      return !needle || [row.name, row.caseId, row.image_id, row.category, row.status].some(value => String(value || "").toLowerCase().includes(needle));
    });
    return filtered.sort((a, b) => priority
      ? Number(queueState(b) === "quality" || queueState(b) === "retrying") - Number(queueState(a) === "quality" || queueState(a) === "retrying")
      : Date.parse(b.createdAt) - Date.parse(a.createdAt));
  }, [rows, query, tab, priority]);

  const queueDepth = system?.queued_cases ?? counts.active;
  return <main className="enterprise-main queue-page">
    <section className="enterprise-page-head">
      <div><div className="title-line"><h1>Ingestion Queue &amp; Processing Telemetry</h1><span className={`live-badge ${system?.redis === "reachable" ? "" : "warn"}`}><i />{system?.redis === "reachable" ? "QUEUE ONLINE" : "QUEUE STATUS UNKNOWN"}</span></div><p>Real-time intake across the quality gate, OCR extraction, and the searchable document archive.</p></div>
      <div className="page-actions"><button disabled title="Queue pause needs an administrative backend endpoint">Ⅱ Pause Queue</button><button disabled title="Prioritization is not implemented yet">! Prioritize Flagged</button><a className="primary-action" href="/">＋ Add Upload</a></div>
    </section>

    <section className="telemetry-ribbon" aria-label="Live queue summary">
      <span><b>Worker:</b> {pretty(system?.worker || "checking")}</span><span><b>Queue depth:</b> {queueDepth ?? "—"}</span><span className="flow"><em>{counts.intake} intake</em> → <em className="amber">{counts.quality} quality warnings</em> → <em className="blue">{counts.extracting} extracting</em> → <em>{counts.archived} archived</em></span>
    </section>
    {error && <div className="enterprise-alert error" role="alert">Could not load the queue: {error}</div>}

    <section className="pipeline-cards">
      <article><div><span className="eyebrow">⇥ Intake stream</span><strong>{counts.intake}</strong><p>Saved pages waiting for a worker</p></div><small>Redis queue: {queueDepth ?? "—"}</small></article>
      <article className="warning-card"><div><span className="eyebrow">◇ Pre-OCR quality gate</span><strong>{counts.quality}</strong><p>Processed with a visible quality warning</p></div><small>Automatic targeted enhancement</small></article>
      <article className="active-card"><div><span className="eyebrow">▤ OCR extraction</span><strong>{counts.extracting}</strong><p>Pages currently in OCR processing</p></div><small>{system?.primary_ocr || "PaddleOCR PP-OCRv6"}</small></article>
      <article className="success-card"><div><span className="eyebrow">✓ Archived to database</span><strong>{counts.archived}</strong><p>Completed pages in the loaded index</p></div><small>{counts.retrying ? `${counts.retrying} failed` : "No failures in this view"}</small></article>
    </section>

    <section className="enterprise-panel queue-panel">
      <div className="queue-toolbar">
        <div className="segmented" role="tablist" aria-label="Queue status filters">
          {[["all", "All activity", rows.length], ["quality", "Quality warnings", counts.quality], ["extracting", "In extraction", counts.extracting], ["retrying", "Failed", counts.retrying]].map(([id, label, count]) => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label} <b>{count}</b></button>)}
        </div>
        <div className="filter-actions"><label className="search-control"><span>⌕</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Filter by bundle, image or filename…" /></label><button className={priority ? "active-sort" : ""} onClick={() => setPriority(value => !value)}>↕ Priority</button><button onClick={load} aria-label="Refresh queue">↻</button></div>
      </div>
      <div className="enterprise-table-wrap">
        <table className="enterprise-table queue-table">
          <thead><tr><th>Pos</th><th>Priority</th><th>Document &amp; thumbnail</th><th>Bundle ID</th><th>Source</th><th>Current stage</th><th>Wait / elapsed</th><th>Review</th><th>Actions</th></tr></thead>
          <tbody>{visible.map((row, index) => {
            const state = queueState(row);
            const qualityFailures = row.quality_gate?.failure_reasons || [];
            return <tr key={row.image_id} className={state === "quality" ? "warning-row" : ""}>
              <td className="mono muted">{String(index + 1).padStart(2, "0")}</td>
              <td><span className={`priority-tag ${state === "quality" || state === "retrying" ? "high" : ""}`}>{state === "quality" || state === "retrying" ? "HIGH" : "NORM"}</span></td>
              <td><div className="doc-cell">{originalUrl(row) ? <img src={originalUrl(row)} alt="" /> : <span className="thumb-placeholder">▧</span>}<div><button className="link-button mono" onClick={() => setSelected(row)}>{shortId(row.image_id, "DOC")}</button><small>{row.name}</small></div></div></td>
              <td><b className="mono">{shortId(row.caseId, "BATCH")}</b><small>{new Date(row.createdAt).toLocaleString()}</small></td>
              <td><b>Web upload</b><small>Image {row.page} of bundle</small></td>
              <td><span className={`stage-pill ${state}`}>{stageText(row)}</span><small>{qualityFailures.length ? qualityFailures.map(pretty).join(" · ") : pretty(row.quality_gate?.decision || row.status)}</small></td>
              <td><b className="mono">{seconds(row.createdAt)}</b><small>{activeStatuses.has(row.status) ? "Active" : pretty(row.status)}</small></td>
              <td className="mono">{row.review_required ? "Required" : "—"}</td>
              <td><button className="table-button" onClick={() => setSelected(row)}>Inspect</button></td>
            </tr>;
          })}{!visible.length && <tr><td colSpan="9" className="enterprise-empty">{loading ? "Loading queue activity…" : "No pages match this queue view."}</td></tr>}</tbody>
        </table>
      </div>
      <div className="panel-footer"><span>Showing <b>{visible.length}</b> of {rows.length} indexed pages</span><span>Auto-refresh: 4 seconds</span></div>
    </section>

    {selected && <div className="enterprise-modal-backdrop" role="presentation" onMouseDown={() => setSelected(null)}><section className="enterprise-modal" role="dialog" aria-modal="true" aria-labelledby="queue-inspect-title" onMouseDown={event => event.stopPropagation()}><header><div><span className="eyebrow">PROCESSING RECORD</span><h2 id="queue-inspect-title">{selected.name}</h2></div><button onClick={() => setSelected(null)} aria-label="Close">×</button></header><div className="modal-grid"><div>{originalUrl(selected) ? <img src={originalUrl(selected)} alt={`Original ${selected.name}`} /> : <div className="image-empty">Original unavailable</div>}</div><div className="record-details"><dl><dt>Bundle</dt><dd>{selected.caseId}</dd><dt>Status</dt><dd>{stageText(selected)}</dd><dt>OCR engine</dt><dd>{pretty(selected.ocr_method || selected.requestedEngine || "pending")}</dd><dt>Quality decision</dt><dd>{pretty(selected.quality_gate?.decision || "not recorded")}</dd></dl><h3>OCR text</h3><pre>{selected.ocr_text || selected.error || "OCR text is not available yet."}</pre></div></div><footer><a className="primary-action" href={`/?case=${encodeURIComponent(selected.caseId)}`}>Open full ingestion record</a></footer></section></div>}
  </main>;
}
