import { useEffect, useMemo, useRef, useState } from "react";
import { api, downloadFile, pretty } from "./lib.js";

const TEMPLATE = "claim_number, claim_date, admission_date, discharge_date, settlement_date, final_diagnosis, claimed_amount, final_bill, settled_amount, not_settled_amount";

function formatSize(bytes) { return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`; }

export default function PdfExtractionPage({ onBack }) {
  const [fieldsText, setFieldsText] = useState(TEMPLATE);
  const [mapping, setMapping] = useState([]);
  const [job, setJob] = useState(null);
  const [selected, setSelected] = useState(0);
  const [tab, setTab] = useState("data");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const input = useRef(null);
  const fields = useMemo(() => fieldsText.split(/[,\n]/).map(item => item.trim()).filter(Boolean), [fieldsText]);

  useEffect(() => {
    const timer = window.setTimeout(() => { api("/api/extract/resolve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields }) }).then(result => setMapping(result.fields || [])).catch(() => {}); }, 300);
    return () => window.clearTimeout(timer);
  }, [fieldsText]);

  const running = ["queued", "processing"].includes(job?.status);
  useEffect(() => {
    if (!job?.job_id || !running) return undefined;
    const timer = window.setInterval(() => api(`/api/extract/jobs/${job.job_id}`).then(setJob).catch(() => {}), 1500);
    return () => window.clearInterval(timer);
  }, [job?.job_id, running]);

  async function stageFiles(fileList) {
    const selectedFiles = Array.from(fileList || []);
    if (!fields.length) return;
    if (!selectedFiles.length) { setError("Choose at least one PDF file."); return; }
    if (selectedFiles.some(file => !/\.pdf$/i.test(file.name) && file.type !== "application/pdf")) { setError("PDF Data Extraction accepts .pdf files only."); return; }
    setBusy(true); setError("");
    try {
      let current = job;
      if (!current?.job_id || ["done", "warning", "failed"].includes(current.status)) current = await api("/api/extract/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields }) });
      const form = new FormData(); selectedFiles.forEach(file => form.append("files", file, file.name));
      setJob(await api(`/api/extract/jobs/${current.job_id}/files`, { method: "POST", body: form }));
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  async function run() { setBusy(true); setError(""); try { setJob(await api(`/api/extract/jobs/${job.job_id}/run`, { method: "POST" })); } catch (e) { setError(e.message); } finally { setBusy(false); } }
  async function saveLabel(item, label, type) {
    if (!label.trim()) return;
    const custom = [...(item.custom_labels || []), label.trim()];
    const entry = { [item.field]: { labels: custom, type: type || item.type } };
    try { await api("/api/extract/dictionary", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(entry) }); setMapping(items => items.map(row => row.field === item.field ? { ...row, type: entry[item.field].type, custom_labels: custom, matched_labels: [...new Set([...(row.status === "known" ? row.matched_labels || [] : []), label.trim()])], status: "known" } : row)); } catch (e) { setError(e.message); }
  }
  async function downloadCsv() {
    try {
      const response = await fetch(`/api/extract/jobs/${job.job_id}/export.csv`);
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "CSV export failed");
      downloadFile(`claim_data-${job.job_id}.csv`, await response.blob(), "text/csv;charset=utf-8");
    } catch (e) { setError(e.message); }
  }
  const tableRows = (job?.results || []).map(item => item.output || { "Source PDF": item.file });
  const tableColumns = tableRows.reduce((cols, row) => { Object.keys(row).forEach(key => { if (!cols.includes(key)) cols.push(key); }); return cols; }, ["Source PDF"]);
  const result = job?.results?.[selected] || null;
  const publicOutput = result?.output || result?.data || {};
  const progress = job ? `${job.current_page || 0} of ${job.total_pages || 0} pages` : "No extraction run yet";
  return <div className="pdf-extraction">
    <div className="pdf-heading"><div><h1>PDF Data Extraction</h1><p>Extract structured PDF data deterministically with native text streams and local rules.</p></div><button className="btn" onClick={onBack}>← Back to OCR scans</button></div>
    {error && <div className="alert error">{error}</div>}
    <div className="pdf-layout"><div className="pdf-main">
      <section className="pdf-card"><div className="pdf-card-title"><h2><b>1</b> What do you need to extract?</h2><button className="btn small" onClick={() => setFieldsText(TEMPLATE)}>▱ Settlement Template</button></div><label>Target fields (one per line or comma-separated):</label><textarea value={fieldsText} onChange={e => setFieldsText(e.target.value)} />
        <h3>▦ FIELD MAPPING &amp; LABEL HEURISTICS <small>{mapping.filter(item => item.status === "known").length} of {mapping.length} mapped</small></h3><div className="mapping-table"><div className="mapping-head"><span>Field</span><span>Type</span><span>Matched Labels</span><span>Status</span></div>{mapping.map(item => <div className="mapping-row" key={item.field}><code>{item.field}</code><span>{item.type}</span><span className="labels">{item.matched_labels?.map(label => <em key={label}>{label}</em>)}<AddLabel pickType={item.status !== "known"} type={item.type} onSave={(label, type) => saveLabel(item, label, type)} /></span><strong className={`map-status ${item.status}`}>● {pretty(item.status)}</strong></div>)}</div>
      </section>
      <section className={`pdf-card ${!fields.length ? "locked" : ""}`}><div className="pdf-card-title"><h2><b>2</b> Upload PDFs</h2><span>Strict PDF mode · .pdf only</span></div><div className={`pdf-drop ${dragging ? "dragging" : ""}`} role="button" tabIndex={fields.length ? 0 : -1} aria-disabled={!fields.length || busy} onKeyDown={event => { if ((event.key === "Enter" || event.key === " ") && fields.length && !busy) input.current?.click(); }} onClick={() => fields.length && !busy && input.current?.click()} onDragEnter={event => { event.preventDefault(); if (fields.length) setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { event.preventDefault(); setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); if (fields.length && !busy) void stageFiles(event.dataTransfer.files); }}><div>⇧</div><strong>Drag &amp; drop PDF files here</strong><small>{fields.length ? "Files are classified server-side as Text layer, Scanned, or Mixed" : "Add at least one target field to unlock PDF upload"}</small><button type="button" className="btn primary pdf-browse" disabled={!fields.length || busy} onClick={event => { event.stopPropagation(); input.current?.click(); }}>{busy ? "Uploading…" : "Browse PDFs"}</button></div><input ref={input} hidden type="file" accept="application/pdf,.pdf" multiple onChange={event => { const chosen = Array.from(event.currentTarget.files || []); event.currentTarget.value = ""; void stageFiles(chosen); }} />{job?.files?.map((file, index) => <div className="pdf-file" key={`${file.name}-${index}`}><span>▧</span><strong>{file.name}<small>{file.pages} pages · {formatSize(file.size)}</small></strong><em>{file.layer === "text" ? "Text layer" : file.layer === "mixed" ? "Mixed" : "Scanned"}</em></div>)}</section>
      <section className="pdf-card"><div className="pdf-card-title"><h2><b>3</b> Run extraction &amp; live pipeline table</h2><span><button className="btn primary" disabled={!job?.files?.length || busy || running} onClick={() => void run()}>▶ Run Extraction</button> <button className="btn" disabled={!job?.results?.length || running} onClick={() => void downloadCsv()}>⇩ Download CSV</button></span></div><div className="pdf-progress">{progress}</div><div className="pdf-results"><div className="mapping-head run-grid"><span>File</span><span>Pages</span><span>Method</span><span>Fields found</span><span>Status</span><span>Action</span></div>{(job?.results?.length ? job.results : job?.files || []).map((item, index) => { const resultItem = job.results?.[index]; return <div className="mapping-row run-grid" key={`${item.file || item.name}-${index}`}><code title={item.file || item.name}>{item.file || item.name}</code><span>{resultItem?.pages || item.pages}</span><span>{resultItem ? resultItem.extraction_method?.join(" + ") : "—"}</span><strong>{resultItem ? `${resultItem.fields_found}/${resultItem.fields_total}` : "—"}</strong><span className={`run-status ${resultItem?.status || job.status}`}>{pretty(resultItem?.status || job.status)}{running && index === 0 ? ` · ${progress}` : ""}</span><button className="btn small" onClick={() => setSelected(index)}>View JSON</button></div>; })}</div>{tableRows.length > 0 && <div className="table-scroll csv-preview"><table><thead><tr>{tableColumns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{tableRows.map((row, index) => <tr key={index}>{tableColumns.map(column => <td key={column}>{row[column] ?? ""}</td>)}</tr>)}</tbody></table></div>}</section>
    </div><aside className="pdf-side"><div className="pdf-side-head"><strong>{result?.file || job?.files?.[0]?.name || "No PDF selected"}</strong><div><button className="btn small" onClick={() => result && navigator.clipboard?.writeText(JSON.stringify(publicOutput, null, 2))}>▣ Copy JSON</button><button className="btn small" onClick={() => result && downloadFile(`${result.file}.json`, JSON.stringify(publicOutput, null, 2), "application/json")}>⇩</button></div></div><div className="pdf-tabs"><button className={tab === "data" ? "active" : ""} onClick={() => setTab("data")}>▣ Data</button><button className={tab === "evidence" ? "active" : ""} onClick={() => setTab("evidence")}>▧ Evidence</button><button className={tab === "warnings" ? "active" : ""} onClick={() => setTab("warnings")}>Warnings ({result?.warnings?.length || 0})</button></div>{tab === "data" && <pre className="json-view">{JSON.stringify(result ? publicOutput : {}, null, 2)}</pre>}{tab === "warnings" && <div className="warning-list">{result?.warnings?.length ? result.warnings.map(warning => <p key={warning}>⚠ {warning}</p>) : <p>No warnings.</p>}</div>}{tab === "evidence" && <div className="evidence-list">{Object.entries(result?.evidence || {}).map(([field, evidence]) => <article key={field}><div><code>{field}</code><span>{evidence.page ? `Page ${evidence.page}` : "Whole document"} · {pretty(evidence.strategy)}{evidence.label ? ` · “${evidence.label}”` : ""}</span><strong>{Math.round((result.confidence?.[field] || 0) * 100)}%</strong></div><progress value={result.confidence?.[field] || 0} max="1" /><p>{evidence.snippet}</p></article>)}{!result && <p>No evidence yet.</p>}</div>}<button className="zip-link" disabled={!job?.results?.length} onClick={() => void downloadCsv()}>Download CSV</button><button className="zip-link" onClick={() => job?.job_id && fetch(`/api/extract/jobs/${job.job_id}/export`).then(response => response.blob()).then(blob => downloadFile(`extraction-${job.job_id}.zip`, blob, "application/zip"))}>Download all (.zip)</button></aside></div>
  </div>;
}

function AddLabel({ onSave, pickType, type }) { const [value, setValue] = useState(""); const [kind, setKind] = useState(type || "text"); return <span className="add-label"><input value={value} onChange={e => setValue(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { onSave(value, kind); setValue(""); } }} placeholder="+ Add label" />{pickType && <select value={kind} onChange={e => setKind(e.target.value)} aria-label="Field type">{["text", "id", "date", "amount", "phone", "email"].map(option => <option key={option}>{option}</option>)}</select>}<button onClick={() => { onSave(value, kind); setValue(""); }}>Save</button></span>; }
