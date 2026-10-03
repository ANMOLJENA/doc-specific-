import { useState } from "react";
import { api, originalUrl, pretty } from "./lib.js";

export default function ValidationPage({ caseData, caseId, refreshCase }) {
  const [message, setMessage] = useState("");
  if (!caseData) return <div className="empty-message">Load a bundle ID above, or run OCR on the Document Ingestion page first.</div>;
  const docs = caseData.analysis?.documents || [];
  const flagged = caseData.pages.filter(page => page.review_required || page.status === "failed").length;

  async function changeCategory(doc) {
    const category = window.prompt("Name this document category (for example: lab_report):");
    if (!category?.trim()) return;
    try {
      await api(`/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(doc.id)}/category`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category: category.trim() }) });
      await refreshCase(); setMessage("Category proposed. Review it before approval.");
    } catch (error) { setMessage(error.message); }
  }
  async function approve(doc) {
    try {
      await api(`/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(doc.id)}/approve`, { method: "POST" });
      await refreshCase(); setMessage("Category approved. Any unresolved OCR flags remain visible.");
    } catch (error) { setMessage(error.message); }
  }

  return <div className="subpage">
    {message && <div className="alert" role="status">{message}</div>}
    <div className="summary-grid"><div><span>BUNDLE STATUS</span><strong>{pretty(caseData.status)}</strong></div><div><span>ORIGINAL IMAGES</span><strong>{caseData.pages.length}</strong></div><div><span>PROPOSED GROUPS</span><strong>{docs.length}</strong></div><div><span>NEEDS REVIEW</span><strong>{flagged}</strong></div></div>
    <section className="panel"><div className="panel-title"><div><span className="panel-icon">▣</span><h2>OCR Text &amp; Source Images</h2></div></div><p className="section-intro">Read each original and its text before accepting any proposed field.</p><div className="source-list">{caseData.pages.map(page => <article className="source-item" key={page.image_id}><div><strong>Page {page.page} · {page.name}</strong><small>Image ID: {page.image_id} · {pretty(page.status)}{page.review_required ? " · Review needed" : ""}</small></div>{originalUrl(page) && <a href={originalUrl(page)} target="_blank" rel="noreferrer">Open original ↗</a>}<pre>{page.ocr_text || page.error || "OCR text is not available yet."}</pre></article>)}</div></section>
    <section className="panel"><div className="panel-title"><div><span className="panel-icon">✓</span><h2>Proposed Document Groups</h2></div></div>
      {!docs.length ? <div className="empty-message">No groups have been proposed. OCR text above remains available; configure the analysis model to generate categories and fields.</div> : docs.map(doc => <article className="proposal" key={doc.id}><div className="proposal-head"><div><h3>{doc.title}</h3><p>{pretty(doc.category)} · Pages {doc.pages.join(", ")} · Proposed score {Math.round((doc.confidence || 0) * 100)}%</p></div><span className="status-chip">{doc.category_status === "approved" ? "Approved" : "Review proposed"}</span></div>
        {doc.issues?.length > 0 && <p className="review-note">Issues: {doc.issues.map(pretty).join(", ")}</p>}
        {doc.fields?.length ? <div className="field-table">{doc.fields.map((field, index) => { const source = caseData.pages.find(page => page.page === field.source_page); return <div key={`${field.name}-${index}`}><span>{pretty(field.name)}</span><div><strong>{field.value}</strong><small>Page {field.source_page} · Evidence: {field.evidence || "none"} · {pretty(field.status)}</small></div>{originalUrl(source) ? <a href={originalUrl(source)} target="_blank" rel="noreferrer">Original ↗</a> : null}</div>; })}</div> : <p>No fields proposed for this group.</p>}
        <div className="proposal-actions">{doc.category === "unknown" ? <button className="btn" onClick={() => void changeCategory(doc)}>Set category</button> : doc.category_status !== "approved" ? <button className="btn primary" onClick={() => void approve(doc)}>Approve category</button> : null}</div>
      </article>)}
    </section>
  </div>;
}
