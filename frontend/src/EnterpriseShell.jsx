import { pretty } from "./lib.js";

const nav = [
  ["ingestion", "/", "Ingestion"],
  ["queue", "/queue", "Ingestion Queue"],
  ["database", "/database", "Document Database"],
  ["validation", "/validation", "Validation"],
];

export default function EnterpriseShell({ active, system, children }) {
  const ready = system?.api === "reachable" && system?.database === "reachable";
  return <div className="enterprise-shell">
    <header className="enterprise-header">
      <a className="enterprise-brand" href="/" aria-label="DocEngine Enterprise OCR home">
        <span className="enterprise-logo">⌁</span>
        <span>DocEngine <strong>Enterprise OCR</strong></span>
        <small>v0.2</small>
      </a>
      <nav className="enterprise-nav" aria-label="Enterprise workspace">
        {nav.map(([id, href, label]) => <a key={id} className={active === id ? "active" : ""} href={href}>{label}</a>)}
      </nav>
      <div className="enterprise-header-actions">
        <span className={`system-pill ${ready ? "online" : "offline"}`}><i />{ready ? "Pipeline online" : "Status unavailable"}</span>
        <a className="square-action" href="/docs" title="API reference">?</a>
        <span className="enterprise-avatar" aria-label="Local prototype user">LP</span>
      </div>
    </header>
    <div className="enterprise-engine-strip">
      <span><i className={system?.paddle_worker === "available" ? "ok" : ""} /> Primary: <b>{system?.primary_ocr || "PaddleOCR PP-OCRv6"}</b></span>
      <span>Fallback: <b>{system?.fallback_ocr || "Surya 2"}</b></span>
      <span>Redis: <b>{pretty(system?.redis || "checking")}</b></span>
    </div>
    {children}
    <footer className="enterprise-footer">
      <span><i /> Local OCR workspace</span><span>Original images retained with source-linked OCR results</span><a href="/docs">API documentation</a>
    </footer>
  </div>;
}
