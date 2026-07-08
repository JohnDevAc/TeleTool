// Shared UI helpers for all pages.

function $(sel, root=document){ return root.querySelector(sel); }
function $all(sel, root=document){ return Array.from(root.querySelectorAll(sel)); }

async function apiErrorFromResponse(r, text) {
  let msg = text || `${r.status} ${r.statusText}`;
  try {
    const data = JSON.parse(text);
    if (data && data.detail) msg = Array.isArray(data.detail) ? data.detail.map(x => x.msg || JSON.stringify(x)).join("; ") : String(data.detail);
    else if (data && data.error) msg = String(data.error);
  } catch (_) {}
  return new Error(msg);
}

async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) throw await apiErrorFromResponse(r, await r.text());
  return r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body || {})
  });
  if (!r.ok) throw await apiErrorFromResponse(r, await r.text());
  return r.json();
}
async function jdelete(url) {
  const r = await fetch(url, { method:"DELETE" });
  if (!r.ok) throw await apiErrorFromResponse(r, await r.text());
  return r.json();
}

function fmtUptime(startedAtSec){
  if (!startedAtSec) return "—";
  const minutes = Math.max(0, Math.floor((Date.now()/1000 - startedAtSec) / 60));
  return `${minutes} min`;
}

function setBadge(el, kind, text){
  if (!el) return;
  el.classList.remove("good","bad","warn");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

function rfSignalKind(rf){
  const kind = rf && rf.kind ? String(rf.kind) : "";
  if (kind === "good" || kind === "warn" || kind === "bad") return kind;
  const dbm = Number(rf && rf.dbm);
  if (Number.isFinite(dbm)){
    if (dbm >= -65) return "good";
    if (dbm >= -80) return "warn";
    return "bad";
  }
  const percent = Number(rf && rf.percent);
  if (Number.isFinite(percent)){
    if (percent >= 65) return "good";
    if (percent >= 35) return "warn";
  }
  return "bad";
}

function rfSignalLabel(rf){
  if (!rf || !rf.available) return "N/A";
  if (rf.dbm_label && rf.dbm_label !== "N/A") return String(rf.dbm_label);
  if (Number.isFinite(Number(rf.dbm))) return `${Math.round(Number(rf.dbm))} dBm`;
  if (Number.isFinite(Number(rf.percent))) return `${Math.round(Number(rf.percent))}%`;
  return String(rf.label || "N/A");
}

function rfSignalTitle(rf){
  if (!rf || !rf.available) return "RF signal unavailable";
  const parts = [`RF signal ${rfSignalLabel(rf)}`];
  if (rf.dbm_estimated) parts.push("estimated from TV signal strength");
  if (Number.isFinite(Number(rf.percent))) parts.push(`${Math.round(Number(rf.percent))}%`);
  if (rf.snr) parts.push(`SNR ${rf.snr}`);
  if (rf.mux) parts.push(String(rf.mux));
  return parts.join(" | ");
}

let _toastTimer = null;
function toast(msg, kind="info", ms=2400){
  const el = document.querySelector("#toast");
  if (!el) { console.log("[toast]", msg); return; }
  el.textContent = msg;
  el.classList.remove("good","bad","warn");
  if (kind==="good"||kind==="bad"||kind==="warn") el.classList.add(kind);
  el.classList.add("show");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>el.classList.remove("show"), ms);
}

async function copyText(text){
  try{
    await navigator.clipboard.writeText(String(text));
    toast("Copied", "good");
  }catch(e){
    // fallback
    const ta=document.createElement("textarea");
    ta.value=String(text);
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("Copied", "good");
  }
}

// Prevent periodic refresh from overwriting form edits.
let _netFormLockedUntil = 0;
function lockNetworkFormFor(ms){
  _netFormLockedUntil = Date.now() + (ms||1200);
}
function isNetworkFormLocked(){
  return Date.now() < _netFormLockedUntil;
}
function fallbackTvUrl(){
  const host = window.location.hostname;
  return host ? `http://${host}:9981/` : "#";
}

function normalizedTvUrl(raw){
  const fallback = fallbackTvUrl();
  const text = String(raw || "").trim();
  if (!text) return fallback;
  try{
    const url = new URL(text, window.location.href);
    if (["127.0.0.1", "localhost", "0.0.0.0"].includes(url.hostname) && window.location.hostname){
      url.hostname = window.location.hostname;
    }
    return url.toString();
  }catch(e){
    return fallback;
  }
}

async function setTvLinks(){
  try{
    let url = fallbackTvUrl();
    try{
      const cfg = await jget("/api/config/ui");
      url = normalizedTvUrl(cfg && cfg.tvh_base_url);
    }catch(e){
      // Keep the header usable while the API is unavailable.
    }
    document.querySelectorAll('[data-tvh-link]').forEach(a => {
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.title = "Open TV";
    });
  }catch(e){
    // ignore
  }
}

async function setDevelopmentReleaseBanner(){
  try{
    const info = await jget("/api/release");
    if (!info || !info.development) return;
    if (document.querySelector(".devReleaseBanner")) return;
    const banner = document.createElement("div");
    banner.className = "devReleaseBanner";
    const version = String(info.version || "").trim();
    banner.textContent = version ? `Development Release ${version}` : "Development Release";
    document.body.insertBefore(banner, document.body.firstChild);
  }catch(e){
    // Release metadata is non-critical UI decoration.
  }
}

async function setTeleToolPageTitle(){
  let title = "TeleTool";
  try{
    const info = await jget("/api/system/hostname");
    const hostname = String(info && info.hostname || "").trim();
    if (hostname) title = `TeleTool - ${hostname}`;
  }catch(e){
    // Hostname metadata is non-critical UI decoration.
  }
  document.title = title;
}

document.addEventListener('DOMContentLoaded', () => {
  setTeleToolPageTitle();
  setDevelopmentReleaseBanner();
  setTvLinks();
});
