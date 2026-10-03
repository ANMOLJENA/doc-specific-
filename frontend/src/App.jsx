import { useCallback, useEffect, useState } from "react";
import IngestionPage from "./IngestionPage.jsx";
import ValidationPage from "./ValidationPage.jsx";
import ExportsPage from "./ExportsPage.jsx";
import EnterpriseShell from "./EnterpriseShell.jsx";
import QueuePage from "./QueuePage.jsx";
import RepositoryPage from "./RepositoryPage.jsx";
import { api, caseHref, pretty, rememberCase, recentCases } from "./lib.js";

const initialCaseId = new URLSearchParams(window.location.search).get("case") || recentCases()[0] || "";

function NavLink({ to, children, caseId, active }) {
  return <a className={active ? "nav-item active" : "nav-item"} href={caseHref(to, caseId)}>{children}</a>;
}

export default function App() {
  const [caseId, setCaseId] = useState(initialCaseId);
  const [path, setPath] = useState(window.location.pathname);
  const [caseData, setCaseData] = useState(null);
  const [caseError, setCaseError] = useState("");
  const [system, setSystem] = useState(null);
  const [caseInput, setCaseInput] = useState(initialCaseId);

  useEffect(() => {
    const updateRoute = () => {
      setPath(window.location.pathname);
      const nextId = new URLSearchParams(window.location.search).get("case");
      if (nextId) { setCaseId(nextId); setCaseInput(nextId); }
    };
    const navigate = event => {
      const anchor = event.target.closest?.("a[href]");
      if (!anchor || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || anchor.target || anchor.hasAttribute("download")) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin || !["/", "/queue", "/database", "/validation", "/exports"].includes(url.pathname) || url.hash) return;
      event.preventDefault();
      window.history.pushState(null, "", url.pathname + url.search);
      updateRoute();
      window.scrollTo(0, 0);
    };
    document.addEventListener("click", navigate);
    window.addEventListener("popstate", updateRoute);
    return () => { document.removeEventListener("click", navigate); window.removeEventListener("popstate", updateRoute); };
  }, []);

  const refreshCase = useCallback(async () => {
    if (!caseId) return null;
    const result = await api(`/cases/${encodeURIComponent(caseId)}`);
    setCaseData(result);
    setCaseError("");
    return result;
  }, [caseId]);

  useEffect(() => {
    let stopped = false;
    let timer;
    async function poll() {
      try {
        const result = await api(`/cases/${encodeURIComponent(caseId)}`);
        if (stopped) return;
        setCaseData(result);
        setCaseError("");
        rememberCase(caseId);
        if (["queued", "processing"].includes(result.status)) timer = window.setTimeout(poll, 2200);
      } catch (error) {
        if (!stopped) setCaseError(error.message);
      }
    }
    if (caseId) void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [caseId]);

  useEffect(() => {
    let stopped = false;
    async function check() {
      try { const result = await api("/pipeline/status"); if (!stopped) setSystem(result); }
      catch { if (!stopped) setSystem(null); }
    }
    void check();
    const timer = window.setInterval(check, 15000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, []);

  const route = path === "/validation" ? "validation" : path === "/exports" ? "exports" : path === "/queue" ? "queue" : path === "/database" ? "database" : "ingestion";
  const title = route === "validation" ? "Validation Workflows" : route === "exports" ? "Export & Connectors" : route === "queue" ? "Ingestion Queue" : route === "database" ? "Document Database" : "Document Ingestion";
  const loadCase = event => {
    event.preventDefault();
    if (caseInput.trim()) window.location.assign(caseHref(path, caseInput.trim()));
  };

  return <>
    {(route === "queue" || route === "database") && <EnterpriseShell active={route} system={system}>
      {route === "queue" ? <QueuePage system={system} /> : <RepositoryPage />}
    </EnterpriseShell>}
    <div className="app-shell" style={{ display: route === "queue" || route === "database" ? "none" : undefined }}>
    <header className="topbar">
      <a className="brand" href="/"><span className="brand-icon">▣</span><span>DocEngine<br />Enterprise</span></a>
      <nav className="top-nav" aria-label="Primary"><a className="active" href={caseHref("/", caseId)}>OCR Pipeline</a><a href="/docs">API Reference</a><span className="nav-disabled" title="Not available in this prototype">Support</span></nav>
      <div className="top-actions"><input aria-label="Search docs or APIs (not yet available)" placeholder="⌕  Search docs or APIs..." disabled /><button disabled title="Help is not available">ⓘ</button><button disabled title="Account settings are not available">⚙</button><button className="text-action" disabled title="Feedback is not configured">Feedback</button><button className="sign-in" disabled title="Authentication is not configured">Sign In</button><span className="avatar" aria-label="Local prototype">LP</span></div>
    </header>
    <div className="body-shell">
      <aside className="sidebar">
        <div className="manual-mark"><span>▣</span><div><strong>OCR Platform</strong><small>Local prototype</small></div></div>
        <div className="side-caption">PIPELINE MODULES</div>
        <nav className="side-nav" aria-label="Pipeline pages">
          <NavLink to="/" caseId={caseId} active={route === "ingestion"}><span>⇧</span> Document Ingestion</NavLink>
          <NavLink to="/queue" active={false}><span>≋</span> Ingestion Queue</NavLink>
          <NavLink to="/database" active={false}><span>▤</span> Document Database</NavLink>
          <NavLink to="/validation" caseId={caseId} active={route === "validation"}><span>✓</span> Validation Workflows</NavLink>
          <NavLink to="/exports" caseId={caseId} active={route === "exports"}><span>⇄</span> Export & Connectors</NavLink>
        </nav>
        <button className="offline-button" disabled title="Offline PDF is not available">⇩ &nbsp; Download Offline PDF</button>
        <div className="side-spacer" />
        <div className="side-bottom"><button disabled title="API keys are not configured">⚿ &nbsp; API Keys</button><button disabled title="Help desk is not connected">ⓘ &nbsp; Help Desk</button></div>
      </aside>
      <main className="workspace">
        <div className="breadcrumbs"><a href={caseHref("/", caseId)}>Home</a><span>›</span><span>OCR Pipeline</span><span>›</span><strong>{title}</strong></div>
        {caseError && <div className="alert error" role="alert">Could not load bundle: {caseError}</div>}
        <div style={{ display: route === "ingestion" ? undefined : "none" }}><IngestionPage caseData={caseData} caseId={caseId} system={system} refreshCase={refreshCase} /></div>
        {(route === "validation" || route === "exports") && <>
          <div className="page-heading compact"><div><h1>{title}</h1><p>{route === "validation" ? "Compare every proposed value with its source image and OCR text." : "Download local case data. External clinical connectors are not configured."}</p></div></div>
          <form className="case-loader" onSubmit={loadCase}><label htmlFor="case-id">Bundle ID</label><input id="case-id" value={caseInput} onChange={event => setCaseInput(event.target.value)} placeholder="Paste a bundle ID" spellCheck="false" /><button className="btn" type="submit">Load bundle</button><span>{caseData ? `${caseData.pages.length} images · ${pretty(caseData.status)}` : "No bundle selected"}</span></form>
          {route === "validation" ? <ValidationPage caseData={caseData} caseId={caseId} refreshCase={refreshCase} /> : <ExportsPage caseData={caseData} caseId={caseId} />}
        </>}
      </main>
    </div>
  </div></>;
}
