import { useState } from "react";
import { api, csv, downloadFile } from "./lib.js";

export default function ExportsPage({ caseData, caseId }) {
  const [message, setMessage] = useState("");
  if (!caseData) return <div className="empty-message">Load a bundle ID above, or run OCR on the Document Ingestion page first.</div>;
  async function downloadFields() {
    try {
      const result = await api(`/cases/${encodeURIComponent(caseId)}/fields`);
      downloadFile(`${caseId}-fields.json`, JSON.stringify(result, null, 2), "application/json");
      setMessage(`Downloaded ${result.fields.length} field proposal(s).`);
    } catch (error) { setMessage(error.message); }
  }
  return <div className="subpage">
    {message && <div className="alert" role="status">{message}</div>}
    <div className="alert warn"><strong>Local exports only.</strong> No EMR, FHIR, claims gateway, or other external connector is configured. Downloaded data stays in your browser unless you send it elsewhere.</div>
    <section className="panel"><div className="panel-title"><div><span className="panel-icon">⇩</span><h2>Download This Bundle</h2></div></div><p className="section-intro">Original image links remain in the case JSON. Use the ingestion or validation page to open the originals.</p>
      <div className="export-grid"><article><h3>Case JSON</h3><p>Bundle ID, ordered pages, OCR text, review flags, and proposed groups.</p><button className="btn primary" onClick={() => downloadFile(`${caseId}-case.json`, JSON.stringify(caseData, null, 2), "application/json")}>Download JSON</button></article>
        <article><h3>OCR Pages CSV</h3><p>One row per original image, including extracted text. Treat this as sensitive if real data is used.</p><button className="btn" onClick={() => downloadFile(`${caseId}-ocr-pages.csv`, csv([["bundle_id", "image_id", "page", "filename", "status", "ocr_text", "review_required"], ...caseData.pages.map(page => [caseId, page.image_id, page.page, page.name, page.status, page.ocr_text, page.review_required])]), "text/csv;charset=utf-8")}>Download CSV</button></article>
        <article><h3>Field Proposals</h3><p>Available after AI grouping. Evidence status is preserved; export does not mean clinical approval.</p><button className="btn" onClick={() => void downloadFields()}>Download fields JSON</button></article></div>
    </section>
  </div>;
}
