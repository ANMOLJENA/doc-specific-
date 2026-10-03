export async function api(path, options) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(body?.detail || `Request failed (${response.status})`);
  return body;
}

export function pretty(value) {
  return String(value ?? "unknown").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
}

export function originalUrl(page) {
  return page?.source_url?.startsWith("/cases/") ? page.source_url : null;
}

export function seconds(start, end) {
  const startMs = Date.parse(start || "");
  const endMs = end ? Date.parse(end) : Date.now();
  return Number.isFinite(startMs) && Number.isFinite(endMs) ? `${Math.max(0, Math.floor((endMs - startMs) / 1000))}s` : "—";
}

export function caseHref(path, caseId) {
  return path + (caseId ? `?case=${encodeURIComponent(caseId)}` : "");
}

export function downloadFile(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export function csvCell(value) {
  let text = String(value ?? "");
  if (/^\s*[=+\-@]/.test(text)) text = "'" + text;
  return `"${text.replaceAll('"', '""')}"`;
}

export function csv(rows) {
  return rows.map(row => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

const historyKey = "doc-specific-ocr-recent-cases";

export function recentCases() {
  try {
    const ids = JSON.parse(localStorage.getItem(historyKey) || "[]");
    return Array.isArray(ids) ? ids.filter(id => typeof id === "string").slice(0, 8) : [];
  } catch {
    return [];
  }
}

export function rememberCase(id) {
  try {
    localStorage.setItem(historyKey, JSON.stringify([id, ...recentCases().filter(item => item !== id)].slice(0, 8)));
  } catch {
    // Browser storage is optional; OCR still works without it.
  }
}
