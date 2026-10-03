import { useEffect, useRef, useState } from "react";
import "./ocr-picker.css";
import { api, caseHref, originalUrl, pretty, recentCases, rememberCase, seconds } from "./lib.js";
import PdfExtractionPage from "./PdfExtractionPage.jsx";

const accepted = "image/jpeg,image/png,image/tiff,image/webp,application/pdf";
const supportedName = /\.(jpe?g|png|tiff?|webp|pdf)$/i;

function Panel({ children, className = "", id }) {
  return <section id={id} className={`panel ${className}`}>{children}</section>;
}

function Stage({ number, title, detail, state }) {
  return <div className={`stage ${state}`}><div className="stage-title"><span>{state === "done" ? "✓" : state === "active" ? "◌" : "○"}</span> {number}. {title}</div><small>{detail}</small></div>;
}

function formatBytes(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(2)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function ModeSwitch({ pdfExtraction, setPdfExtraction }) {
  return <div className="ingestion-mode-bar" role="group" aria-label="Choose ingestion mode">
    <div className="ingestion-segmented"><button className={!pdfExtraction ? "active" : ""} aria-pressed={!pdfExtraction} onClick={() => setPdfExtraction(false)}>▣ OCR Scans</button><button className={pdfExtraction ? "active" : ""} aria-pressed={pdfExtraction} onClick={() => setPdfExtraction(true)}>↔ PDF Data Extraction</button></div>
    <span className="local-mode-badge">◇ Runs locally · no AI calls</span>
  </div>;
}

export default function IngestionPage({ caseData, caseId, system, refreshCase }) {
  const [files, setFiles] = useState([]);
  const [mode, setMode] = useState("");
  const [ocrEngine, setOcrEngine] = useState("paddle");
  const [uploading, setUploading] = useState(false);
  const [uploadStarted, setUploadStarted] = useState(null);
  const [uploadError, setUploadError] = useState("");
  const [compareError, setCompareError] = useState("");
  const [compareBusy, setCompareBusy] = useState(false);
  const [runActionBusy, setRunActionBusy] = useState(false);
  const [previewUrl, setPreviewUrl] = useState("");
  const [now, setNow] = useState(Date.now());
  const [selectedPage, setSelectedPage] = useState(1);
  const [modal, setModal] = useState("");
  const [pdfExtraction, setPdfExtraction] = useState(false);
  const [knownIds, setKnownIds] = useState([]);
  const [loadId, setLoadId] = useState(caseId);
  const singleInput = useRef(null);
  const bundleInput = useRef(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (files.length === 1 && files[0].type.startsWith("image/")) {
      const url = URL.createObjectURL(files[0]);
      setPreviewUrl(url);
      return () => URL.revokeObjectURL(url);
    }
    setPreviewUrl("");
  }, [files]);
  useEffect(() => { if (caseId) rememberCase(caseId); }, [caseId]);

  const comparisonRunning = !!caseData?.pages?.some(item => ["queued", "processing"].includes(item.ocr_comparison?.paddle?.status));
  useEffect(() => {
    if (!comparisonRunning || !refreshCase) return;
    const timer = window.setInterval(() => { void refreshCase().catch(() => {}); }, 2200);
    return () => window.clearInterval(timer);
  }, [comparisonRunning, refreshCase]);

  const caseRunning = ["queued", "processing"].includes(caseData?.status);
  useEffect(() => {
    if (!caseRunning || !refreshCase) return;
    const timer = window.setInterval(() => { void refreshCase().catch(() => {}); }, 2200);
    return () => window.clearInterval(timer);
  }, [caseRunning, refreshCase]);

  const pages = caseData?.pages || [];
  const donePages = pages.filter(page => ["processed", "failed", "quality_review"].includes(page.status)).length;
  const qualityChecked = pages.filter(page => !["queued", "quality_check"].includes(page.status)).length;
  const ocrFinished = pages.filter(page => ["processed", "failed"].includes(page.status)).length;
  const enhancedCount = pages.filter(page => page.quality_gate?.enhancements?.length).length;
  const isFinished = !!caseData && !["queued", "processing"].includes(caseData.status);
  const start = caseData?.analysis?.ocr_started_at || caseData?.created_at;
  const finish = caseData?.analysis?.ocr_finished_at;
  const elapsed = caseData ? finish ? seconds(start, finish) : isFinished ? "—" : seconds(start) : uploadStarted ? `${Math.floor((now - uploadStarted) / 1000)}s` : "—";
  const page = pages.find(item => item.page === selectedPage) || pages[0];
  const analysisStage = caseData?.analysis?.analysis_stage;
  const analysisError = caseData?.analysis?.error;
  const groupingRunning = caseRunning && (analysisStage === "classifying" || analysisStage === "extracting_fields" || (pages.length > 0 && ocrFinished === pages.length));
  const liveLabel = caseData ? groupingRunning ? analysisStage === "extracting_fields" ? "AI extracting document fields" : "AI classifying documents" : caseData.status === "processing" ? "OCR engine processing" : caseData.status === "queued" ? "Waiting for OCR worker" : `Run ${pretty(caseData.status)}` : uploading ? "Uploading original images" : "Ready for document ingestion";

  async function submit(chosen = files) {
    if (!chosen.length || uploading) return;
    setUploading(true); setUploadStarted(Date.now()); setUploadError("");
    const form = new FormData(); chosen.forEach(file => form.append("files", file));
    form.append("ocr_engine", ocrEngine);
    try {
      const result = await api("/cases", { method: "POST", body: form });
      rememberCase(result.case_id);
      window.location.assign(caseHref("/", result.case_id));
    } catch (error) {
      setUploadError(error.message);
      setUploading(false);
    }
  }

  async function cancelRun() {
    if (!caseId || !caseRunning || runActionBusy) return;
    setRunActionBusy(true); setUploadError("");
    try {
      await api(`/cases/${encodeURIComponent(caseId)}/cancel`, { method: "POST" });
      await refreshCase();
    } catch (error) { setUploadError(error.message); }
    finally { setRunActionBusy(false); }
  }

  async function retryRun() {
    if (!caseId || caseData?.status !== "cancelled" || runActionBusy) return;
    setRunActionBusy(true); setUploadError("");
    try {
      await api(`/cases/${encodeURIComponent(caseId)}/retry`, { method: "POST" });
      await refreshCase();
    } catch (error) { setUploadError(error.message); }
    finally { setRunActionBusy(false); }
  }

  function choose(fileList, selectedMode) {
    if (uploading) return;
    const next = Array.from(fileList || []);
    if (!next.length) return;
    if (next.length > 10 || next.some(file => !supportedName.test(file.name))) {
      setUploadError("Choose 1–10 JPG, PNG, TIFF, WebP images, or a PDF bundle (maximum 10 expanded pages).");
      return;
    }
    setUploadError(""); setUploadStarted(null); setFiles(next); setMode(selectedMode);
  }

  function openHistory() { setKnownIds(recentCases()); setModal("history"); }
  async function runPaddle(number) {
    if (!caseId || compareBusy) return;
    setCompareBusy(true); setCompareError("");
    try {
      await api(`/cases/${encodeURIComponent(caseId)}/pages/${number}/compare/paddle`, { method: "POST" });
      await refreshCase();
    } catch (error) { setCompareError(error.message); }
    finally { setCompareBusy(false); }
  }
  async function recordVerdict(number, verdict) {
    if (!caseId || compareBusy) return;
    setCompareBusy(true); setCompareError("");
    try {
      await api(`/cases/${encodeURIComponent(caseId)}/pages/${number}/compare/verdict`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ verdict }) });
      await refreshCase();
    } catch (error) { setCompareError(error.message); }
    finally { setCompareBusy(false); }
  }
  function showPage(number) {
    setSelectedPage(number);
    document.getElementById("inspect-original")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const ocrFace = <>
    <div className="page-heading">
      <div><h1>Document Ingestion &amp; Live OCR Processing</h1><p>Select images, choose PaddleOCR or Surya 2, then trigger extraction. Selecting files does not start OCR. Every image must pass the quality gate first.</p></div>
      <div className="heading-actions"><button className="btn" onClick={openHistory}>◴ &nbsp; Ingestion History</button><button className="btn" onClick={() => setModal("settings")}>☷ &nbsp; OCR Settings</button></div>
    </div>

    <div className="live-bar" role="status"><span className={`live-dot ${caseData?.status === "processing" ? "pulsing" : ""} ${system?.paddle_worker !== "available" && system?.surya !== "reachable" ? "warning" : ""}`} />
      <div className="live-title"><strong>LIVE: {liveLabel}</strong><span className={`status-chip ${caseData?.status === "processing" ? "active" : ""}`}>{caseData ? pretty(caseData.status) : "IDLE"}</span></div>
      <div className="live-meta">Paddle: {system?.paddle_worker || "checking"} <span>·</span> Surya: {system?.surya || "checking"} <span>·</span> Redis: {system?.redis || "checking"}</div>
      <div className="live-controls"><button className="btn small" disabled title="Pausing a run is not supported">Ⅱ &nbsp; Pause Stream</button>{caseData?.status === "cancelled" ? <button className="btn small primary" disabled={runActionBusy} onClick={() => void retryRun()}>↻ &nbsp; {runActionBusy ? "Restarting…" : "Retry Run"}</button> : <button className="btn small danger" disabled={!caseRunning || runActionBusy} onClick={() => void cancelRun()}>⊗ &nbsp; {runActionBusy ? "Cancelling…" : "Cancel Active Run"}</button>}</div>
    </div>

    <Panel id="upload-scans">
      <div className="panel-title"><div><span className="panel-icon">♧</span><h2>Upload Document Scans</h2></div><span>Images or digital/scanned PDF</span></div>
      <div className="dropzone" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); const dropped = event.dataTransfer.files; choose(dropped, dropped.length === 1 ? "single" : "bundle"); }}>
        <div className="drop-icon">⇧</div><h3>Drag &amp; drop document scans here, or browse files</h3>
        <p>Mixed bills, reports, prescriptions, and other photos are welcome. Categories are not assumed at upload time.</p>
        <div className="file-tags"><span>PNG / JPEG</span><span>TIFF / WebP</span><span>PDF</span><span>UP TO 10 PAGES</span></div>
        <div className="upload-buttons"><button className="btn primary" onClick={() => singleInput.current?.click()} disabled={uploading}>⊕ &nbsp; Select Image</button><button className="btn" onClick={() => bundleInput.current?.click()} disabled={uploading}>▥ &nbsp; Select Bundle</button></div>
        <input ref={singleInput} type="file" accept={accepted} hidden onChange={event => { choose(event.target.files, "single"); event.target.value = ""; }} />
        <input ref={bundleInput} type="file" accept={accepted} multiple hidden onChange={event => { choose(event.target.files, "bundle"); event.target.value = ""; }} />
      </div>
      <fieldset className="ocr-engine-picker" disabled={uploading}><legend>Choose OCR engine before extraction</legend><label><input type="radio" name="ocr-engine" value="paddle" checked={ocrEngine === "paddle"} onChange={() => setOcrEngine("paddle")} /><span><strong>PaddleOCR — default</strong><small>Reads first; Surya 2 is used only on error, timeout, or no text.</small></span></label><label><input type="radio" name="ocr-engine" value="surya" checked={ocrEngine === "surya"} onChange={() => setOcrEngine("surya")} /><span><strong>Surya 2</strong><small>Runs Surya directly for this image or entire bundle, without trying Paddle.</small></span></label></fieldset>
      <div className="staging"><div className="overline">CURRENTLY STAGED FILES FOR OCR INGESTION</div>
        {files.length ? <div className="staged-row"><span className="file-symbol">▤</span><div className="staged-name"><strong>{files.length === 1 ? files[0].name : `${files.length} files in upload order`}</strong><small>{formatBytes(files.reduce((sum, file) => sum + file.size, 0))} · {files.length} file{files.length === 1 ? "" : "s"} · PDF text pages use PyMuPDF; scanned pages use {ocrEngine === "paddle" ? "PaddleOCR" : "Surya 2"}</small></div>
          <button className="btn small" onClick={() => (mode === "bundle" ? bundleInput : singleInput).current?.click()} disabled={uploading}>↻ Re-upload</button>
          <button className="btn small danger" onClick={() => { setFiles([]); setMode(""); setUploadError(""); }} disabled={uploading}>▤ Remove</button>
          <button className="btn small primary" onClick={() => void submit()} disabled={uploading}>▶ {uploading ? "Uploading…" : "Trigger Extraction"}</button></div> : <div className="staged-empty">Select an image, bundle, or PDF. Digital PDF pages use PyMuPDF text extraction; scanned pages continue through OCR. Nothing is sent before Trigger Extraction.</div>}
      </div>
      {uploadError && <div className="alert error" role="alert">{uploadError}</div>}
      {previewUrl && <div className="local-preview"><div><strong>{uploading ? "OCR submission in progress" : "Selected original"}</strong><span>{uploadStarted ? `${Math.floor((now - uploadStarted) / 1000)}s since upload started` : "Ready"}</span></div><img src={previewUrl} alt={`Selected original: ${files[0]?.name}`} /></div>}
    </Panel>

    <Panel id="telemetry">
      <div className="panel-title telemetry-title"><div><span className="timer-icon">◴</span><div><h2>Processing Time Calculator &amp; Live Telemetry</h2><small>Measured status for the selected upload; no estimated completion time is available.</small></div></div><span className="pipeline-chip">● {caseData?.status === "processing" ? "Live Pipeline Active" : caseData ? pretty(caseData.status) : "No active run"}</span></div>
      <div className="stats-grid"><div className="stat-box"><span>OCR ELAPSED TIME</span><strong>{elapsed}</strong><small>{caseData ? finish ? "Recorded OCR duration" : groupingRunning ? "OCR finished; AI classification is running" : isFinished ? "Duration not recorded for this result" : caseData.analysis?.ocr_started_at ? "OCR running now" : "Waiting since upload" : uploadStarted ? "Uploading to the API" : "Starts when you click Trigger Extraction"}</small></div>
        <div className="stat-box"><span>ESTIMATED TIME TO COMPLETE</span><strong>—</strong><small>Not available; OCR runtime varies by image.</small></div>
        <div className="stat-box"><span>INFERENCE THROUGHPUT</span><strong>—</strong><small>Not measured by this prototype.</small></div></div>
      <div className="progress-heading"><strong>Extraction Stage Progress</strong><span>{pages.length ? `${donePages} of ${pages.length} images finished` : "Waiting for images"}</span></div>
      <div className="progress-track"><div style={{ width: pages.length ? `${Math.round(donePages / pages.length * 100)}%` : "0%" }} /></div>
      <div className="stages"><Stage number="1" title="Originals saved" detail={caseData ? `${pages.length} image IDs assigned` : "Waiting for upload"} state={caseData ? "done" : uploading ? "active" : "pending"} />
        <Stage number="2" title="Quality check" detail={caseData ? `${qualityChecked}/${pages.length} images checked` : "Not started"} state={pages.length && qualityChecked === pages.length ? "done" : caseData?.status === "processing" ? "active" : "pending"} />
        <Stage number="3" title="Automatic enhancement" detail={caseData ? `${enhancedCount} image${enhancedCount === 1 ? "" : "s"} enhanced; clean images skip this step` : "Runs only for failed metrics"} state={qualityChecked ? "done" : caseData?.status === "processing" ? "active" : "pending"} />
        <Stage number="4" title="OCR text extraction" detail={caseData ? `${ocrFinished}/${pages.length} images extracted or failed` : "Starts automatically after quality processing"} state={pages.length && ocrFinished === pages.length ? "done" : pages.some(item => item.status === "processing") ? "active" : "pending"} />
        <Stage number="5" title="Document classification & grouping" detail={analysisError ? "AI request failed — see error below" : groupingRunning ? analysisStage === "extracting_fields" ? "Categories saved; extracting fields" : "AI classification in progress" : caseData?.analysis?.documents?.length ? "Categories assigned" : system?.analysis_configured ? "Waiting for OCR to finish" : "AI model not configured"} state={analysisError ? "failed" : groupingRunning ? "active" : caseData?.analysis?.documents?.length ? "done" : "pending"} />
        <Stage number="6" title="Results review & export" detail="Quality warnings appear above OCR text" state={isFinished && ocrFinished > 0 ? "active" : "pending"} /></div>
    </Panel>

    <Panel id="ingestion-results" className="table-panel">
      <div className="table-title"><div><h2>Real-Time Ingestion Queue &amp; Recent Ingests</h2><span className="count-chip">{caseData ? `${pages.filter(item => item.status === "processing").length} Processing · ${donePages} Finished` : "No active upload"}</span></div><small>↻ Auto-refresh: {caseData && ["queued", "processing"].includes(caseData.status) ? "2.2s" : "when a run is active"}</small></div>
      <div className="table-scroll"><table><thead><tr><th>DOCUMENT SCAN</th><th>FILE SPECS</th><th>ELAPSED / EXECUTION</th><th>INFERENCE STATUS</th><th title="Model score, not verified accuracy">OCR MODEL SCORE</th><th>EXTRACTED OUTPUT</th></tr></thead><tbody>
        {pages.length ? pages.map(item => <tr key={item.image_id || item.page}><td><strong>{item.name}</strong><small>Page {item.page} · {item.image_id}</small></td><td>Original image</td><td>{item.ocr_elapsed_seconds != null ? `${Math.round(item.ocr_elapsed_seconds)}s` : ["processing", "queued"].includes(item.status) ? "Running" : "—"}</td><td><span className={`row-status ${item.status}`}>{pretty(item.status)}</span>{item.ocr_method && <small>Engine: {item.ocr_method === "paddle" ? "PaddleOCR" : item.ocr_method === "surya_fallback" ? "Surya fallback" : item.ocr_method === "surya" ? "Surya 2" : "Historical OCR"}</small>}</td><td>{item.ocr_confidence == null ? "—" : `${Math.round(item.ocr_confidence * 100)}%`}</td><td><button className="btn small" onClick={() => showPage(item.page)}>{item.ocr_text ? "◉ View OCR" : "Inspect"}</button></td></tr>) : <tr><td colSpan="6" className="empty-row">Upload an image or bundle to see real OCR results here. Recent case IDs are available from Ingestion History.</td></tr>}
      </tbody></table></div>
    </Panel>

    {caseData && <Panel id="inspect-original" className="inspect-panel"><div className="panel-title"><div><span className="panel-icon">▣</span><h2>Inspect Original &amp; OCR Text</h2></div><span>Each image keeps its own ID and upload order</span></div>
      <div className="inspection"><div className="page-list">{pages.map(item => <button key={item.page} className={item.page === page?.page ? "active" : ""} onClick={() => setSelectedPage(item.page)}><strong>Page {item.page} · {item.name}</strong><small>{pretty(item.status)}{item.review_required ? " · Review needed" : ""}</small></button>)}</div>
        {page && <div className="inspection-detail"><div className="inspection-header"><strong>Page {page.page} · {page.name}</strong><small>Image ID: {page.image_id} · {pretty(page.status)}{page.ocr_rechecked ? " · Second Surya pass" : ""}</small></div><div className="inspection-status"><strong>{page.status === "processed" ? "OCR complete" : page.status === "processing" ? "OCR processing" : page.status === "quality_review" ? "Quality gate held image — no extraction" : page.status === "quality_check" ? "Checking image quality" : page.status === "failed" ? "OCR failed" : "Waiting for OCR worker"}</strong><span>{pages.length === 1 && isFinished && finish ? `Run took ${elapsed} (quality gate + OCR if accepted)` : page.status === "processing" ? `Elapsed ${elapsed}` : "Per-image duration not recorded"}</span></div>
          <div className="inspection-grid"><div><h3>ORIGINAL IMAGE</h3>{originalUrl(page) ? <><div className="image-frame"><img src={originalUrl(page)} alt={`Original image: ${page.name}`} /></div><a href={originalUrl(page)} target="_blank" rel="noreferrer">Open original at full size ↗</a></> : <div className="image-frame">Original unavailable</div>}</div><div><h3>{page.ocr_method === "paddle" ? "PADDLEOCR TEXT" : page.ocr_method === "surya_fallback" ? "SURYA FALLBACK TEXT" : page.ocr_method === "surya" ? "SURYA 2 OCR TEXT" : "OCR TEXT"}</h3>{page.quality_gate?.forwarded_with_quality_warning && <div className="alert quality-warning" role="alert"><strong>Image quality warning</strong><span>{page.quality_gate.failure_reasons?.map(pretty).join(", ") || "Quality re-check did not pass"}. OCR was still completed automatically; verify this text against the original.</span></div>}<pre className="ocr-text">{page.ocr_text || page.error || "OCR text is not available yet."}</pre>{page.review_required && <p className="review-note">Review this result against the original before using it, especially medication names and dosing.</p>}</div></div>
          {page.quality_gate && <div className="quality-report"><h3>Pre-OCR image quality: {pretty(page.quality_gate.decision)}</h3><p>{page.quality_gate.decision === "passed_direct" ? "No enhancement was required; this image went directly to OCR." : page.quality_gate.decision === "passed_after_enhancement" ? "The relevant fixes passed re-check; the derived image was sent to OCR automatically." : "Automatic enhancement did not pass every check, so OCR continued automatically with a visible quality warning."} {page.quality_gate.elapsed_seconds}s quality-check time.</p><p>Applied automatically: {page.quality_gate.enhancements?.join(" → ") || "No safe fix was available for the failed metric"}</p>{page.quality_gate.warnings?.includes("source_resolution_was_below_threshold") && <p className="review-note">The OCR input was upscaled, but interpolation cannot recreate missing source detail. Verify the extracted text.</p>}{page.quality_gate.failure_reasons?.length > 0 && <p className="review-note">Quality failures after enhancement: {page.quality_gate.failure_reasons.join(", ")}. OCR continued automatically.</p>}{page.quality_gate.original && <div className="table-scroll"><table><thead><tr><th>METRIC</th><th>ORIGINAL</th><th>FINAL</th><th>FINAL CHECK</th></tr></thead><tbody>{["blur", "contrast", "skew", "noise", "resolution"].map(metric => <tr key={metric}><td>{metric === "resolution" ? "Text height (pixels)" : pretty(metric)}</td><td>{page.quality_gate.original.scores[metric === "resolution" ? "text_height" : metric] ?? "Unmeasurable"}</td><td>{page.quality_gate.post_enhancement?.scores[metric === "resolution" ? "text_height" : metric] ?? "Unmeasurable"}</td><td>{page.quality_gate.post_enhancement?.passes[metric] ? "Pass" : "Fail"}</td></tr>)}</tbody></table></div>}{page.quality_image_url && <a href={page.quality_image_url} target="_blank" rel="noreferrer">View derived image actually sent to OCR ↗</a>}<details><summary>Thresholds and full telemetry</summary><pre>{JSON.stringify(page.quality_gate, null, 2)}</pre></details></div>}
          {page.ocr_method === "paddle" ? <p className="compare-status">PaddleOCR is the primary result for this page. Surya is only used when Paddle cannot return text.</p> : <div className="ocr-compare"><div className="ocr-compare-head"><div><h3>Compare OCR models</h3><p>A comparison requires completed OCR and a fresh quality-gate pass. Compare both readings against the original.</p></div><button className="btn small" disabled={compareBusy || page.status !== "processed" || system?.paddle_worker !== "available" || ["queued", "processing", "completed"].includes(page.ocr_comparison?.paddle?.status)} onClick={() => void runPaddle(page.page)}>{compareBusy ? "Please wait…" : page.ocr_comparison?.paddle?.status === "failed" ? "Retry Paddle" : "Run PaddleOCR"}</button></div>
            {system?.paddle_worker !== "available" && <small className="compare-help">Paddle worker is off. In VS Code terminal, run <code>docker compose up -d paddle-worker</code> from the E: project folder.</small>}
            {compareError && <div className="alert error" role="alert">{compareError}</div>}
            {page.ocr_comparison?.paddle?.status && <p className="compare-status">Paddle: {pretty(page.ocr_comparison.paddle.status)}{page.ocr_comparison.paddle.elapsed_seconds != null ? ` · ${page.ocr_comparison.paddle.elapsed_seconds}s` : ""}{page.ocr_comparison.paddle.error ? ` · ${page.ocr_comparison.paddle.error}` : ""}</p>}
            {page.ocr_comparison?.paddle?.status === "completed" && <><div className="compare-columns"><div><h4>Surya 2 — saved result</h4><pre>{page.ocr_text || "No text returned"}</pre></div><div><h4>PaddleOCR {page.ocr_comparison.paddle.model} — candidate</h4><pre>{page.ocr_comparison.paddle.text || "No text returned"}</pre></div></div><p className="compare-help">Model scores are not comparable accuracy percentages. Check the original image, especially names and medical instructions.</p><div className="compare-verdict"><span>Which reading is closer to the original?</span>{[["surya", "Surya"], ["paddle", "Paddle"], ["tie", "Tie"], ["neither", "Neither"]].map(([value, label]) => <button key={value} className={`btn small ${page.ocr_comparison.verdict === value ? "primary" : ""}`} disabled={compareBusy} onClick={() => void recordVerdict(page.page, value)}>{label}</button>)}</div></>}
          </div>}</div>}</div>
    </Panel>}

    {caseData && <Panel id="document-groups"><div className="panel-title"><div><span className="panel-icon">▥</span><h2>Document Classification &amp; Groups</h2></div><span>Proposed only after OCR and AI analysis</span></div>
      {analysisError && <div className="alert error" role="alert">AI classification failed: {analysisError}</div>}
      {caseData.analysis?.field_errors?.length > 0 && <div className="alert error" role="alert">Categories were saved, but some field extraction requests failed. Review the available results.</div>}
      {caseData.analysis?.documents?.length ? <div className="group-list">{caseData.analysis.documents.map(doc => <article className="group-row" key={doc.id}><div><strong>{doc.title}</strong><small>{pretty(doc.category)} · Images {doc.pages.join(", ")}{doc.review_required ? " · Review needed" : ""}</small></div><a className="btn small" href={caseHref("/validation", caseId)}>Review fields &amp; evidence →</a></article>)}</div> : <p className="empty-message">{system?.analysis_configured ? "Document grouping is not available yet for this run." : "OCR text is available above. AI document grouping is not configured, so no categories or fields have been invented."}</p>}</Panel>}

    <div className="safety-callout"><span>♧</span><p><strong>Prototype storage &amp; safety:</strong> Original scans and extracted OCR text are stored locally. Authentication and clinical privacy controls are not implemented. Use non-sensitive test files.</p></div>

    {modal && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setModal(""); }}><div className="modal" role="dialog" aria-modal="true" aria-label={modal === "history" ? "Ingestion history" : "OCR settings"}><div className="modal-head"><h2>{modal === "history" ? "Ingestion History" : "OCR Settings"}</h2><button className="icon-button" onClick={() => setModal("")} aria-label="Close">×</button></div>
      {modal === "history" ? <><p>Recent bundle IDs saved in this browser. This is not a server-wide history.</p><form className="case-loader" onSubmit={event => { event.preventDefault(); if (loadId.trim()) window.location.assign(caseHref("/", loadId.trim())); }}><input value={loadId} onChange={event => setLoadId(event.target.value)} placeholder="Paste a bundle ID" /><button className="btn primary" type="submit">Open</button></form><div className="history-list">{knownIds.length ? knownIds.map(id => <a key={id} href={caseHref("/", id)}>{id} ↗</a>) : <span>No local history yet.</span>}</div></> : <><p>Current backend settings. Quality-gate thresholds are provisional and must be calibrated on representative labels.</p><dl className="settings-list"><div><dt>Default OCR engine</dt><dd>PaddleOCR PP-OCRv6 (choose per upload)</dd></div><div><dt>Fallback for Paddle choice</dt><dd>Surya 2 (error, timeout, or no text)</dd></div><div><dt>Pre-OCR quality gate</dt><dd>Enabled — one conditional enhancement pass</dd></div><div><dt>Images per internal batch</dt><dd>{system?.ocr_batch_size ?? "Unavailable"}</dd></div><div><dt>Weak-page review threshold</dt><dd>{system?.ocr_recheck_confidence ?? "Unavailable"}</dd></div><div><dt>Minimum recognized characters</dt><dd>{system?.ocr_min_chars ?? "Unavailable"}</dd></div><div><dt>AI grouping</dt><dd>{system?.analysis_configured ? "Configured" : "Not configured"}</dd></div></dl></>}
    </div></div>}
  </>;

  return <>
    <ModeSwitch pdfExtraction={pdfExtraction} setPdfExtraction={setPdfExtraction} />
    <div className={`flip-container ${pdfExtraction ? "is-flipped" : ""}`}>
      <div className="flip-inner">
        <section className="flip-face flip-front" aria-hidden={pdfExtraction} inert={pdfExtraction ? "" : undefined}>{ocrFace}</section>
        <section className="flip-face flip-back" aria-hidden={!pdfExtraction} inert={!pdfExtraction ? "" : undefined}><PdfExtractionPage onBack={() => setPdfExtraction(false)} /></section>
      </div>
    </div>
  </>;
}
