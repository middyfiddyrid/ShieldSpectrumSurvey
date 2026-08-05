/* SHIELD TSCM — Spectrum Survey ops console */
"use strict";

const $ = (id) => document.getElementById(id);
const fmtF = (mhz) => mhz == null ? "" :
  mhz < 1 ? (mhz * 1000).toFixed(1) + " kHz" :
  mhz >= 1000 ? (mhz / 1000).toFixed(4) + " GHz" : mhz.toFixed(4) + " MHz";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts)
  } : undefined);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

/* popup blockers silently eat window.open after fetch — render a persistent
   link the operator can click instead. The embedded preview browser also
   cannot render PDFs at all (black page), so the PRIMARY action opens the
   file in the desktop PDF viewer via the server. */
window.openReportLocal = async (filename) => {
  try { await api("/api/reports/open_local", { filename }); }
  catch (e) { alert("Could not open in desktop viewer: " + e.message); }
};
function showReportLink(elId, r, label) {
  const href = "/reports/" + encodeURIComponent(r.filename);
  $(elId).innerHTML =
    `✔ ${esc(label)} ready — ` +
    `<a class="report-ready" href="#" onclick="openReportLocal('${esc(r.filename)}');return false">OPEN IN PDF VIEWER</a> ` +
    `<a class="report-ready" href="${href}" download>DOWNLOAD</a> ` +
    `<span class="note">${esc(r.filename)} — if the in-app browser shows a black page, use OPEN IN PDF VIEWER</span>`;
}

const App = {
  state: null, baseline: null, anomalies: [], dirQueue: [], dirIndex: 0,
  seq: null,          // active multi-step sweep sequence
  overlayHidden: false,
  hunt: [], huntIdx: 0, autoTimer: null,   // signal-hunt list + ◂/▸/AUTO stepping
  demodMhz: null, bandLo: null, bandHi: null,   // movable demod tuner within the band
  triage: null, triageRemaining: 0,
  _wasSweepRunning: false, _activeSweep: null, _triagePromptedSweep: null,
  _specSmooth: null, _meterPeakHold: null, _sigHistory: [], _sigHistoryT0: 0,
  liveSweepSignals: [],
  expandedGroups: {},
  labelPrefs: null,
  _lastLabelKey: "",
  focusAnomalies: false,   // F3: hide expected/known, dampen+rescale chart to anomalies
  mutedKeys: {},           // F3: individually muted signals (key -> {lo,hi})
  wantWake: false, _wakeLock: null,   // F1: screen wake lock
  // F6: sustained-segment info panes
  infoTab: "signals",      // which pane sits in the table slot while sustained
  liveWindow: null,        // {lo,hi} MHz zoom window for the LIVE pane (null = whole segment)
  _sweepSt: null, _lastTrace: null, _catKey: null,
  // F8: ledger detail — historic segment review + row-hover highlight + lightbox
  review: null,            // {segId, sid, trace, seg} while reviewing a past segment
  hoverHL: null,           // {lo,hi} MHz — vertical highlight of the hovered ledger row
  _segCache: {},           // segment_id -> /api/segments payload (immutable once written)
  _ledgerKey: "",          // change-detection key so the ledger DOM isn't rebuilt every poll
  postSweep: null, _postStartedSweep: null, _postPromptedKey: null,
  dirAudioOn: true, _dirAudio: null, _dirLastBeep: 0, _dirReadings: [],
};

/* ---------- F1: keep the laptop screen awake while the app is open ----------
   The ambient sweep runs OUTSIDE on a laptop that may sleep mid-scan. The
   Screen Wake Lock API holds the display on; browsers auto-release it when the
   tab loses focus, so we re-acquire on visibilitychange. Needs a user gesture
   (any click satisfies it) and HTTPS/localhost (the app is localhost). No-ops
   gracefully where unsupported. */
async function acquireWakeLock() {
  if (!App.wantWake || !("wakeLock" in navigator)) { updateWakeIndicator(); return; }
  if (App._wakeLock) { updateWakeIndicator(); return; }   // already held — no orphan
  try {
    App._wakeLock = await navigator.wakeLock.request("screen");
    App._wakeLock.addEventListener("release", () => { App._wakeLock = null; updateWakeIndicator(); });
  } catch (e) { App._wakeLock = null; }   // denied / not visible — try again on next gesture/visibility
  updateWakeIndicator();
}

async function releaseWakeLock() {
  App.wantWake = false;
  try { if (App._wakeLock) await App._wakeLock.release(); } catch (e) {}
  App._wakeLock = null;
  updateWakeIndicator();
}

function updateWakeIndicator() {
  const el = document.getElementById("ft-wake");
  if (!el) return;
  if (!("wakeLock" in navigator)) { el.textContent = ""; return; }
  const on = App.wantWake && !!App._wakeLock;
  el.textContent = on ? "🔆 screen awake" : (App.wantWake ? "🔆 awake (pending)" : "🌙 screen sleep on");
  el.style.color = on ? "var(--green)" : "var(--dim)";
  el.style.cursor = "pointer";
  el.title = "Click to toggle keeping the screen awake during sweeps";
}

function initWakeLock() {
  if (!("wakeLock" in navigator)) { updateWakeIndicator(); return; }
  // engage on the operator's first interaction (gesture requirement satisfied)
  const arm = () => { if (!App.wantWake) { App.wantWake = true; acquireWakeLock(); } };
  window.addEventListener("pointerdown", arm, { once: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && App.wantWake && !App._wakeLock) acquireWakeLock();
  });
  const ind = document.getElementById("ft-wake");
  if (ind) ind.onclick = () => { if (App.wantWake) releaseWakeLock(); else { App.wantWake = true; acquireWakeLock(); } };
  updateWakeIndicator();
}

/* ---------- navigation ---------- */
document.querySelectorAll("nav .step").forEach(el => {
  el.onclick = () => show(el.dataset.view);
});
function show(view) {
  if (!$("view-" + view)) view = "session";
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll("nav .step").forEach(s => s.classList.remove("active"));
  $("view-" + view).classList.add("active");
  document.querySelector(`nav .step[data-view=${view}]`).classList.add("active");
  if (("#" + view) !== location.hash) history.replaceState(null, "", "#" + view);
  if (view === "baseline") loadBaseline();
  if (view === "rooms") loadRooms();
  if (view === "results") loadResults();
  if (view === "directional") loadDirectional();
  if (view === "inventory") loadInventory();
  if (view === "network") loadNetwork();
  if (view === "droid") loadDroid();
  if (view === "wiki") loadWiki();
  if (view === "devicelab") loadDeviceLab();
  if (view === "recent") {
    // returning to the landing re-reads whichever session is being viewed
    if ($("pane-sessions") && $("pane-sessions").style.display !== "none") loadSessionsPane();
    else loadRecentSweeps(App.viewingSessionId);
  }
  if (view === "session") { loadCaseFiles(); loadEngagement(); loadTechTasks(); loadReportOptions(); loadSweepHistory(); }
  if (view === "poi") loadPoi();
  if (view === "cases") loadCases();
  if (view === "triage") loadTriage();
  if (view === "sweep") renderDeviceRoles();
}

/* ---------- engagement / pre-sweep ---------- */
async function loadEngagement() {
  try {
    const e = await api("/api/engagement");
    $("eng-type").value = e.engagement_type || "onsite";
    $("eng-client").value = e.client_name || "";
    $("eng-contact").value = e.client_contact || "";
    $("eng-concerns").value = e.client_concerns || "";
    $("eng-offsite").value = e.offsite_location || "";
    $("eng-scheduling").value = e.scheduling_note || "";
  } catch (err) {}
}
function engagementBody() {
  return {
    engagement_type: $("eng-type").value, client_name: $("eng-client").value,
    client_contact: $("eng-contact").value, client_concerns: $("eng-concerns").value,
    offsite_location: $("eng-offsite").value, scheduling_note: $("eng-scheduling").value,
  };
}
$("btn-eng-save").onclick = async () => {
  try {
    await api("/api/engagement", engagementBody());
    $("eng-msg").textContent = "saved — technician tasks seeded below";
    loadTechTasks();
  } catch (e) { $("eng-msg").textContent = "FAILED: " + e.message; }
};
$("btn-presweep").onclick = async () => {
  $("eng-msg").textContent = "rendering…";
  try {
    await api("/api/engagement", engagementBody());
    const r = await api("/api/report/presweep", {});
    showReportLink("eng-msg", r, "pre-sweep briefing");
    loadTechTasks();
  } catch (e) { $("eng-msg").textContent = "FAILED: " + e.message; }
};
async function loadTechTasks() {
  try {
    const rows = await api("/api/tech_tasks");
    $("tbl-tech-tasks").innerHTML = rows.map(t =>
      `<tr><td style="width:24px"><input type="checkbox" ${t.done ? "checked" : ""} ` +
      `onchange="techToggle(${t.id})"></td>` +
      `<td style="${t.done ? "text-decoration:line-through; color:var(--dim)" : ""}">${esc(t.text)}</td></tr>`
    ).join("") || "<tr><td>save the engagement to seed the technician checklist</td></tr>";
  } catch (e) {}
}
window.techToggle = (id) => api(`/api/tech_tasks/${id}/toggle`, {});
$("btn-tech-task-add").onclick = async () => {
  const text = $("tech-task-input").value.trim();
  if (!text) return;
  await api("/api/tech_tasks", { text });
  $("tech-task-input").value = "";
  loadTechTasks();
};

/* ---------- persons of interest / deconfliction ---------- */
const POI_FIELDS = ["name", "aliases", "relationship", "source", "motive",
  "access_note", "access_tier", "capability_tier", "capability_basis",
  "resources_note", "conduct_note", "analyst_notes", "opportunity_vectors", "status"];
let poiSelected = null;
const poiEl = {
  name: "poi-name", aliases: "poi-aliases", relationship: "poi-relationship",
  source: "poi-source", motive: "poi-motive", access_note: "poi-access-note",
  access_tier: "poi-access-tier", capability_tier: "poi-capability-tier",
  capability_basis: "poi-capability-basis", resources_note: "poi-resources",
  conduct_note: "poi-conduct", analyst_notes: "poi-notes",
  opportunity_vectors: "poi-opportunity", status: "poi-status",
};
function poiClear() {
  poiSelected = null;
  POI_FIELDS.forEach(f => { $(poiEl[f]).value = f === "status" ? "open"
    : (f.endsWith("_tier") ? "unknown" : ""); });
  $("poi-named-client").checked = false;
  $("poi-msg").textContent = "";
  $("poi-docs").style.display = "none";
}
$("btn-internal-memo").onclick = async () => {
  $("memo-msg").textContent = "rendering…";
  try {
    const r = await api("/api/report/internal_memo", {});
    showReportLink("memo-msg", r, "confidential internal memo");
  } catch (e) { $("memo-msg").textContent = "FAILED: " + e.message; }
};
$("btn-analyze").onclick = async () => {
  $("btn-analyze").disabled = true;
  $("memo-msg").textContent = "analyzing — fanning out to configured AI engines (this can take a minute)…";
  try {
    const r = await api("/api/analyze/threat_vectors", {});
    showReportLink("memo-msg", r, `threat-vector memorandum (${r.engines.join(", ")})`);
    loadReports();
  } catch (e) { $("memo-msg").textContent = "FAILED: " + e.message; }
  $("btn-analyze").disabled = false;
};
$("btn-poi-new").onclick = poiClear;
$("btn-poi-save").onclick = async () => {
  const body = { id: poiSelected, named_by_client: $("poi-named-client").checked };
  POI_FIELDS.forEach(f => body[f] = $(poiEl[f]).value);
  try {
    const r = await api("/api/poi", body);
    poiSelected = r.id;
    $("poi-msg").textContent = r.task_added
      ? `saved — technician task added: gather client-authorized background for ${body.name}; attach it below`
      : "saved";
    loadPoi();
    loadPoiDocs(r.id);
  } catch (e) { $("poi-msg").textContent = "FAILED: " + e.message; }
};
async function loadPoiDocs(pid) {
  $("poi-docs").style.display = "";
  try {
    const rows = await api(`/api/case_files?poi_id=${pid}`);
    $("tbl-poi-docs").innerHTML = rows.length ?
      "<tr><th>File</th><th>Kind</th><th>Uploaded</th></tr>" +
      rows.map(f => `<tr><td><a style="color:var(--cyan)" href="/case_files/${f.id}">${esc(f.filename)}</a></td>` +
        `<td>${esc(f.kind || "")}</td><td>${new Date(f.uploaded_utc * 1000).toLocaleString()}</td></tr>`).join("") :
      "<tr><td>no documents yet — gather the client-authorized background for this person and attach it here</td></tr>";
  } catch (e) {}
}
$("btn-poi-doc-attach").onclick = () => {
  if (!poiSelected) return alert("Select or save the person first.");
  uploadFiles("poi-doc-input", "poi-doc-msg",
              { kind: $("poi-doc-kind").value || "background", poi_id: poiSelected },
              () => loadPoiDocs(poiSelected));
};
$("btn-poi-delete").onclick = async () => {
  if (!poiSelected) return poiClear();
  if (!confirm("Delete this person of interest record?")) return;
  await api(`/api/poi/${poiSelected}/delete`, {});
  poiClear(); loadPoi();
};
async function loadPoi() {
  try {
    const rows = await api("/api/poi");
    const cls = s => s === "cleared" ? "ok" : s === "concern" ? "failed" : "stale";
    $("tbl-poi").innerHTML =
      "<tr><th>Name</th><th>Relationship</th><th>Capability</th><th>Status</th></tr>" +
      rows.map(p => `<tr onclick='poiOpen(${JSON.stringify(p).replace(/'/g, "&#39;")})' style="cursor:pointer">` +
        `<td>${esc(p.name)}</td><td>${esc(p.relationship)}</td>` +
        `<td>${esc(p.capability_tier)}</td>` +
        `<td><span class="pill ${cls(p.status)}">${esc(p.status).toUpperCase()}</span></td></tr>`).join("")
      || "<tr><td>no persons of interest — add client-named suspects to clear</td></tr>";
  } catch (e) { $("tbl-poi").innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; }
}
window.poiOpen = (p) => {
  poiSelected = p.id;
  POI_FIELDS.forEach(f => $(poiEl[f]).value = p[f] || (f.endsWith("_tier") ? "unknown" : ""));
  $("poi-status").value = p.status || "open";
  $("poi-named-client").checked = !!p.named_by_client;
  $("poi-msg").textContent = `editing #${p.id}`;
  loadPoiDocs(p.id);
};

/* ---------- case-study / device library ---------- */
const CS_EL = { title: "cs-title", device_class: "cs-device", rf_signature: "cs-rf",
  discovery: "cs-discovery", case_history: "cs-history", lessons: "cs-lessons" };
let csSelected = null;
function csClear() {
  csSelected = null;
  Object.values(CS_EL).forEach(id => { $(id).value = ""; });
  $("cs-deid").checked = false;
  $("cs-msg").textContent = "";
  $("cs-photo-block").style.display = "none";
}
function csShowPhoto() {
  $("cs-photo-block").style.display = "";
  const img = $("cs-photo");
  img.onload = () => { img.style.display = ""; };
  img.onerror = () => { img.style.display = "none"; };
  img.src = `/case_study_photo/${csSelected}?t=${Date.now()}`;
}
$("btn-cs-new").onclick = csClear;
$("btn-cs-save").onclick = async () => {
  const body = { id: csSelected, deidentified: $("cs-deid").checked };
  Object.entries(CS_EL).forEach(([k, id]) => body[k] = $(id).value);
  try {
    const r = await api("/api/case_studies", body);
    csSelected = r.id;
    $("cs-msg").textContent = "saved" + ($("cs-deid").checked ? ""
      : " — will NOT appear in reports until attested de-identified");
    loadCases(); csShowPhoto();
  } catch (e) { $("cs-msg").textContent = "FAILED: " + e.message; }
};
$("btn-cs-delete").onclick = async () => {
  if (!csSelected) return csClear();
  if (!confirm("Delete this case study?")) return;
  await api(`/api/case_studies/${csSelected}/delete`, {});
  csClear(); loadCases();
};
$("btn-cs-photo").onclick = async () => {
  if (!csSelected) return alert("Save the case study first.");
  const f = $("cs-photo-input").files[0];
  if (!f) return alert("Choose an image first.");
  const fd = new FormData();
  fd.append("file", f);
  $("cs-photo-msg").textContent = "uploading…";
  try {
    const r = await fetch(`/api/case_studies/${csSelected}/photo`, { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "upload failed");
    $("cs-photo-msg").textContent = "photo saved";
    $("cs-photo-input").value = "";
    csShowPhoto();
  } catch (e) { $("cs-photo-msg").textContent = "FAILED: " + e.message; }
};
async function loadCases() {
  try {
    const rows = await api("/api/case_studies");
    $("tbl-cases").innerHTML =
      "<tr><th>Title</th><th>Device class</th><th>Report-eligible</th></tr>" +
      rows.map(c => `<tr onclick='csOpen(${JSON.stringify(c).replace(/'/g, "&#39;")})' style="cursor:pointer">` +
        `<td>${esc(c.title)}</td><td>${esc(c.device_class)}</td>` +
        `<td><span class="pill ${c.deidentified ? "ok" : "stale"}">${c.deidentified ? "ATTESTED" : "NOT ATTESTED"}</span></td></tr>`).join("")
      || "<tr><td>no case studies yet — add de-identified composite examples of device finds</td></tr>";
  } catch (e) { $("tbl-cases").innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; }
}
window.csOpen = (c) => {
  csSelected = c.id;
  Object.entries(CS_EL).forEach(([k, id]) => { $(id).value = c[k] || ""; });
  $("cs-deid").checked = !!c.deidentified;
  $("cs-msg").textContent = `editing #${c.id}`;
  csShowPhoto();
};

/* ---------- polling ---------- */
async function poll() {
  try {
    App.state = await api("/api/state");
    if (App._pollFails >= 3) hideServerLostBanner();
    App._pollFails = 0;
    render(App.state);
  } catch (e) {
    App._pollFails = (App._pollFails || 0) + 1;
    $("ft-msg").textContent = "server unreachable: " + e.message;
    if (App._pollFails === 3) showServerLostBanner();
  }
  setTimeout(poll, 1000);
}

// The radio drivers run in-process: a USB fault can kill the whole backend, and
// then every click silently does nothing — the split screen "doesn't work", the
// buttons "don't work". Make that state impossible to miss and self-explaining.
function showServerLostBanner() {
  let b = $("server-lost-banner");
  if (!b) {
    b = document.createElement("div");
    b.id = "server-lost-banner";
    b.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:9999;" +
      "background:#7a1010;color:#fff;font:bold 13px monospace;" +
      "padding:10px 16px;text-align:center;border-bottom:2px solid #ff5252";
    document.body.appendChild(b);
  }
  b.textContent = "⛔ RADIO SERVER LINK LOST — the app process crashed or was closed " +
    "(clicks won't do anything). The launcher window restarts it in ~5 s; if that " +
    "window is closed, run START Shield Sweep again. Reconnecting…";
  b.style.display = "";
}
function hideServerLostBanner() {
  const b = $("server-lost-banner");
  if (b) b.style.display = "none";
  $("ft-msg").textContent = "reconnected";
}

function dot(id, cls) {
  const d = $("dot-" + id);
  if (d) d.className = "dot" + (cls ? " " + cls : "");
}

function render(st) {
  App.postSweep = (st.post_sweep && st.session &&
                   st.post_sweep.session_id === st.session.id) ? st.post_sweep : null;
  renderPostSweep(App.postSweep);
  if ($("view-results") && $("view-results").classList.contains("active") &&
      (!App.postSweep || App.postSweep.stage === "idle") && st.sweeps) {
    const finished = st.sweeps.filter(s => s.phase === "interior" && s.status === "done");
    if (finished.length) startPostSweep(finished[finished.length - 1].id);
  }
  // public demo: flag the body once so CSS hides PII-bearing features
  if (st.demo && !document.body.classList.contains("demo")) {
    document.body.classList.add("demo");
    App.demo = true;
  }
  // header / footer
  if (st.session) {
    $("hdr-addr").textContent = `#${st.session.id} · ${st.session.address}`;
    $("btn-report").disabled = !(st.sweeps && st.sweeps.length);
    $("btn-csv").disabled = !(st.sweeps && st.sweeps.length);
    $("btn-baseline").disabled = st.baseline.running;
  }
  const dev = st.device;
  $("ft-port").textContent = dev ? dev.port : "—";
  $("ft-model").textContent = dev ? dev.model : "—";
  $("ft-fw").textContent = dev ? dev.firmware : "—";
  $("ft-module").textContent = st.sweep.module || "—";
  $("ft-range").textContent = dev && dev.modules.length ?
    dev.modules.map(m => `${m.side}: ${fmtF(m.min_mhz)}–${fmtF(m.max_mhz)}`).join("  ·  ") : "—";
  $("ft-bat").textContent = dev && dev.battery ? dev.battery : "—";
  $("ft-sim").innerHTML = dev && dev.simulated ? '<span class="sim">⚠ SIMULATED DEVICE</span>' : "";
  $("ft-msg").textContent = st.sweep.message || "idle";
  // LIVE indicator — driven by the REAL radio listen state (st.live.listening),
  // not UI intent, so a silently-dropped stream correctly reads IDLE.
  const lb = $("insp-live-badge");
  if (lb) {
    const lv = st.live || {};
    if (lv.listening) {
      lb.textContent = "● LIVE" + (lv.center_mhz != null ? " @ " + fmtF(lv.center_mhz) : "") +
        (lv.mode ? " " + String(lv.mode).toUpperCase() : "") + (App.autoTimer ? " · AUTO" : "");
      lb.style.color = "#ff5252";
      // The stream holding the radio can be a LEFTOVER from an earlier tune (a
      // preempted or fallback stream that survived) — then this badge contradicts
      // the panel title and nothing says which is real (2026-08-02: panel on
      // 797.8092, radio still demodulating 343.9386). Flag it. The 5 s grace
      // covers normal takeover lag on a re-tune; AUTO stepping skips the check.
      if (!App.autoTimer && App.inspCenter != null && lv.center_mhz != null &&
          Math.abs(lv.center_mhz - App.inspCenter) > 0.002 &&
          Date.now() - (App.inspTunedAt || 0) > 5000) {
        lb.textContent = "● LIVE @ " + fmtF(lv.center_mhz) +
          " ⚠ LEFTOVER STREAM — this panel is " + fmtF(App.inspCenter) +
          " — press LISTEN to retake";
        lb.style.color = "#ffd24a";
      }
    } else {
      lb.textContent = "○ IDLE"; lb.style.color = "#6b7b90";
    }
    // Say WHY the audio stopped. The stream can die on the radio while the
    // endpoint has already returned 200, so without this the operator watches a
    // live-looking waterfall and hears silence with nothing to explain it —
    // which is exactly how a dead radio stayed invisible for weeks. The backend
    // clears listen_error on every fresh tune, so this self-clears on retry.
    const nt = $("insp-note");
    if (nt) {
      if (!lv.listening && lv.error) {
        const d = String(lv.error.detail || lv.error.kind || "");
        const rig = lv.error.source === "b220" ? "B220 Mini"
                  : (lv.error.source === "pluto" ? "Pluto" : (lv.error.source || "radio"));
        let hint = "";
        if (/worker is not running|reconnect to respawn/i.test(d))
          hint = "  ▸ press CONNECT to respawn the radio worker, then tune again.";
        else if (/RFVCO|uhd_error 44|set_rx_rate/i.test(d))
          hint = "  ▸ the radio refused the listen sample rate. Unplug the " +
                 "B220 USB for 5 s, replug, then tune again without sweeping first.";
        nt.textContent = "AUDIO STREAM DIED on the " + rig + " — " + d + hint;
        nt.style.cssText = "background:rgba(255,193,7,.12);border:1px solid #ffc107;" +
                           "color:#ffc107;padding:5px 10px;border-radius:3px";
        App._liveErrShown = true;
      } else if (App._liveErrShown && !lv.error) {
        nt.textContent = ""; nt.style.cssText = ""; App._liveErrShown = false;
      }
    }
  }
  renderMonitorBanner(st);
  // sync the device-source picker to the active/last-used source on first load
  // (don't fight the operator if they've changed it this session)
  if ($("device-source") && !App._srcSynced && st.source) {
    $("device-source").value = st.source;
    App._srcSynced = true;
  }
  // F4: single-RX devices (Pluto) show ONE "Main sweep" step; RF Explorer keeps LEFT/RIGHT
  document.body.classList.toggle("single-rx", isSingleRx());

  // nav dots
  dot("session", st.session ? "done" : "");
  const blStatus = st.baseline.running ? "live" : (st.baseline.result || (App.baseline && App.baseline.count) ? "done" : "");
  dot("baseline", blStatus);
  const phases = { ambient: "ambient", sweepL: "left", sweepR: "right" };
  const done = (p, m) => (st.sweeps || []).some(s => s.phase === p && (!m || s.module === m) && s.status === "done");
  dot("ambient", st.sweep.running && st.sweep.phase === "ambient" ? "live" : done("ambient") ? "done" : "");
  dot("sweepL", st.sweep.running && st.sweep.phase === "interior" && st.sweep.module === "left" ? "live" : done("interior", "left") ? "done" : "");
  dot("sweepR", st.sweep.running && st.sweep.phase === "interior" && st.sweep.module === "right" ? "live" : done("interior", "right") ? "done" : "");
  dot("sweep", st.sweep.running && st.sweep.phase === "interior" ? "live" : done("interior") ? "done" : "");
  const rooms = new Set((st.sweeps || []).filter(s => s.phase === "interior" && s.status === "done").map(s => s.room));
  dot("rooms", rooms.size > 1 ? "done" : "");
  dot("directional", st.sweep.running && st.sweep.phase === "directional" ? "live" : "");
  dot("results", (st.sweeps || []).some(s => s.status === "done") ? "done" : "");
  if (App.triageRemaining > 0) dot("triage", "live");
  else if (App.triage && App.triage.signals && App.triage.signals.length) dot("triage", "done");
  else dot("triage", "");
  dot("calibration", st.calibration.running ? "live" : st.calibration.result ? "done" : "");

  // auto-open triage after an interior sweep finishes (once per sweep)
  if (st.sweep.running) {
    App._activeSweep = { phase: st.sweep.phase, sweep_id: st.sweep.sweep_id };
    // remember so the ledger stays populated after the overlay closes
    if (st.sweep.sweep_id) App._lastFinishedSweepId = st.sweep.sweep_id;
    // gates the post-sweep ledger rehydrate below: only a sweep observed in
    // THIS page session may repopulate the ledger, so a cold open stays clean
    App._sweepSeenThisSession = true;
  } else if (App._wasSweepRunning && !App.seq) {
    const info = App._activeSweep;
    if (info && info.sweep_id) App._lastFinishedSweepId = info.sweep_id;
    if (info && info.phase === "interior") startPostSweep(info.sweep_id);
    // don't leave the operator staring at a finished sweep — suggest the NEXT step
    if (info) suggestNextStep(info.phase);
    // final ledger pull with behavior / inventory enrichment while the id is hot
    if (info && info.sweep_id) {
      ledgerLast = 0;
      refreshLiveSweepSignals(info.sweep_id);
    }
    App._activeSweep = null;
  }
  App._wasSweepRunning = st.sweep.running;

  // baseline build progress
  if (st.baseline.running || st.baseline.result || st.baseline.error) {
    const parts = (st.baseline.progress || []).map(p =>
      `<span class="pill ${p.status}">${esc(p.source)}: ${p.status} (${p.count})</span>`);
    $("baseline-sources").innerHTML = parts.join(" ");
    if (st.baseline.error) $("baseline-msg").textContent = "FAILED: " + st.baseline.error;
    else if (st.baseline.result) $("baseline-msg").textContent =
      `${st.baseline.result.total} entries — ${st.baseline.result.label}`;
    else $("baseline-msg").textContent = "building…";
  }

  // sweep overlay
  // F5: if a manually-paused sweep is waiting and the overlay was backgrounded,
  // bring it back so the operator sees the NEXT SEGMENT button.
  // reveal once on the rising edge only, so a later RUN IN BACKGROUND tap sticks
  if (st.sweep.awaiting_advance && !App._wasAwaitingAdvance && App.overlayHidden)
    App.overlayHidden = false;
  App._wasAwaitingAdvance = st.sweep.awaiting_advance;
  const so = $("sweep-overlay");
  if (st.sweep.running && st.sweep.phase !== "directional" && !App.overlayHidden) {
    so.classList.add("show");
    const capt = st.sweep.phase === "capture" ? st.sweep.capture : null;
    $("sweep-overlay-title").textContent = capt ?
      `SIGNAL CAPTURE — ${fmtF(capt.center_mhz)}` :
      `${(st.sweep.phase || "").toUpperCase()} SWEEP — ${(st.sweep.module || "").toUpperCase()} MODULE` +
      (st.sweep.room ? ` — ${st.sweep.room.toUpperCase()}` : "");
    const pct = capt ? Math.round(100 * ((capt.index || 1) - 1) / Math.max(capt.total, 1))
                     : (st.sweep.percent || 0);
    $("sweep-bar").style.width = pct + "%";
    const cs = st.sweep.current_segment;
    $("sweep-overlay-detail").textContent = capt ?
      `documenting signal ${capt.index}/${capt.total} — high-iteration zoom (avg + max-hold)` :
      `segment ${st.sweep.segment_index}/${st.sweep.segment_total}` +
      (cs ? ` · ${cs.low_mhz.toFixed(2)}–${cs.high_mhz.toFixed(2)} MHz · RBW ${cs.rbw_khz} kHz [${cs.band_tag}]` : "") +
      ` · signals so far: ${st.sweep.signal_count}`;
    const eta = st.sweep.eta_seconds || 0;
    // F5: ETA is meaningless while paused for manual advance
    $("sweep-overlay-eta").textContent = st.sweep.awaiting_advance
      ? "▶ LIVE — still sweeping this segment; tap NEXT SEGMENT to continue"
      : `${st.sweep.percent || 0}%  ·  ETA ${Math.floor(eta / 60)}m ${eta % 60}s`;
    const nextBtn = $("btn-next-segment");
    if (nextBtn) nextBtn.style.display = st.sweep.awaiting_advance ? "" : "none";
    // LIVE RBW readout — the delivered RBW of the current segment (don't stomp
    // the box while the operator is typing in it)
    const rbwNow = $("sweep-rbw-now");
    if (rbwNow && cs && !App._rbwFocused)
      rbwNow.textContent = cs.rbw_khz != null ? `now ${cs.rbw_khz} kHz` : "";
    // F8: while reviewing a historic segment the chart stays frozen on it —
    // the sweep (and its data) continues server-side. Review is STICKY: only
    // BACK TO LIVE, clicking another row, or the sweep ending leaves it —
    // an auto-exit on segment advance made review flash away on fast segments.
    if (App.review) {
      drawSegmentReview();
    } else if (st.sweep.live_trace) {
      drawTrace($("livechart"), st.sweep.live_trace, muteRangesFor(), App.hoverHL);
      buildSweepLabelOverlays($("livechart"), st.sweep.live_trace,
        sweepChartSignals(st.sweep.live_trace));
      pushWaterfall($("waterfall"), st.sweep.live_trace);
    }
    refreshLiveSweepSignals(st.sweep.sweep_id);
    renderSegPanel(st);
    // F6: sustain button + swappable info panes
    App._sweepSt = st.sweep;
    App._lastTrace = st.sweep.live_trace || App._lastTrace;
    const sBtn = $("btn-sustain");
    if (sBtn) {
      const sus = !!st.sweep.sustaining, req = !!st.sweep.sustain_requested;
      sBtn.style.display = st.sweep.phase === "capture" ? "none" : "";
      sBtn.textContent = sus ? "▶ RESUME SWEEP"
        : req ? "⏳ SUSTAIN — finishing passes…" : "⏸ SUSTAIN SEGMENT";
      sBtn.classList.toggle("on", sus || req);
    }
    const tabs = $("sweep-info-tabs");
    if (tabs) {
      tabs.style.display = st.sweep.sustaining ? "flex" : "none";
      // leaving sustain closes the extra panes — back to the signal table
      if (!st.sweep.sustaining && App.infoTab !== "signals") setInfoTab("signals");
    }
    if (st.sweep.sustaining) {
      if (App.infoTab === "live") renderLiveZoom(st.sweep.live_trace);
      if (App.infoTab === "expected") renderExpandedExpected(st);
      if (App.infoTab === "catalog") maybeLoadCatalog(st.sweep.current_segment);
    }
  } else if (!st.sweep.running) {
    so.classList.remove("show");
    MonitorDock.overlayClosed();   // hand the monitor to the page dock (keeps playing)
    App.overlayHidden = false;
    // KEEP liveSweepSignals after the sweep ends so the hit ledger stays
    // clickable (behavior class + inventory match + lightbox). Previously this
    // wiped the list every idle poll → empty ledger even with hundreds of hits.
    // New sweeps replace the list via refreshLiveSweepSignals.
    //
    // cold open / page reload: rehydrate from last finished sweep
    // COLD OPEN STARTS CLEAN (operator ask 2026-07-26). This used to rehydrate
    // the ledger from the last finished sweep on every page load — so opening
    // the app, or refreshing during a NEW sweep, put the PREVIOUS sweep's hits
    // on screen alongside the current job. That is contamination of live client
    // work, not convenience. We now only re-populate for a sweep that actually
    // ran in THIS page session; to revisit an older sweep, open it deliberately
    // from sweep history. (A reload MID-sweep is unaffected: the running-sweep
    // branch above pulls the live ledger by its own sweep_id.)
    if ((!App.liveSweepSignals || !App.liveSweepSignals.length)
        && App._sweepSeenThisSession && App._lastFinishedSweepId) {
      refreshLiveSweepSignals(App._lastFinishedSweepId);
    }
  }

  // directional live meter
  if (st.sweep.directional) {
    const d = st.sweep.directional;
    $("dir-live").style.display = "";
    $("dir-freq").textContent = fmtF(d.center_mhz) + (d.room ? "  ·  " + d.room : "");
    $("dir-meter").textContent = `${d.current_dbm} dBm   (peak ${d.peak_dbm})`;
    const frac = Math.max(0, Math.min(1, (d.current_dbm + 110) / 80));
    $("dir-meter").style.color =
      frac > 0.7 ? "var(--red)" : frac > 0.45 ? "var(--amber)" : "var(--green)";
    updateDirectionalGuidance(d, frac);
    if (d.trace) drawTrace($("dirchart"), { freqs: d.trace.freqs, amps: d.trace.amps });
  }

  // phase-DF live compass
  if (st.sweep.df) renderDFLive(st.sweep.df);
  else if (App._dfWasLive) { App._dfWasLive = false; const dl = $("df-live"); if (dl) dl.style.display = "none"; loadDFLog(); }

  // sequence advancement
  if (App.seq && !st.busy && !st.sweep.running) advanceSeq();

  // calibration
  if (st.calibration.rows) renderCalRows(st.calibration);
}

/* ---------- identity tiers + sweep label overlays ---------- */
const TIER_COLORS = { IDENTIFIED: "#2ecc71", KNOWN_BAND: "#f39c12", ANOMALY: "#e74c3c", SCANNING: "#ffd24a" };
const CLASS_COLORS = TIER_COLORS;

// Plain, client-facing names for the internal classification/tier codes. The
// stored values stay VERIFIED/PLAUSIBLE/UNVERIFIED (logic + DB + CSS classes);
// only the text the operator and client read changes here.
//   Identified    — matched to a known transmitter
//   Likely normal — sits in a known/normal band, probably a legit device
//   Anomaly       — doesn't match anything; needs a look
//   Detecting…    — a live peak still being identified mid-sweep
const STATUS_LABELS = {
  VERIFIED: "Identified", IDENTIFIED: "Identified",
  PLAUSIBLE: "Likely normal", KNOWN_BAND: "Likely normal",
  UNVERIFIED: "Anomaly", ANOMALY: "Anomaly",
  SCANNING: "Detecting…",
};
function statusLabel(v) { return STATUS_LABELS[v] || v || ""; }

function defaultLabelPrefs() {
  return { show: true, identified: true, known: true, anomaly: true, caution: true, scanning: true };
}

function loadLabelPrefs() {
  try {
    const raw = localStorage.getItem("shield.labelPrefs");
    App.labelPrefs = raw ? { ...defaultLabelPrefs(), ...JSON.parse(raw) } : defaultLabelPrefs();
  } catch (e) {
    App.labelPrefs = defaultLabelPrefs();
  }
  syncLegendCheckboxes();
}

function saveLabelPrefs() {
  try { localStorage.setItem("shield.labelPrefs", JSON.stringify(App.labelPrefs)); } catch (e) {}
}

function syncLegendCheckboxes() {
  const p = App.labelPrefs || defaultLabelPrefs();
  const map = [["leg-show", "show"], ["leg-identified", "identified"], ["leg-known", "known"],
    ["leg-anomaly", "anomaly"], ["leg-caution", "caution"], ["leg-scanning", "scanning"]];
  map.forEach(([id, k]) => { const el = $(id); if (el) el.checked = !!p[k]; });
}

function signalTier(s) {
  if (s.live) return "SCANNING";
  if (s.identity_tier) return s.identity_tier;
  if (s.classification === "VERIFIED") return "IDENTIFIED";
  if (s.classification === "PLAUSIBLE") return "KNOWN_BAND";
  return "ANOMALY";
}

function labelVisible(s, ignoreFocusMute) {
  const p = App.labelPrefs || defaultLabelPrefs();
  if (!p.show) return false;
  const t = signalTier(s);
  // F3: focus-anomalies hides the expected/known/detecting clutter; muted ones drop
  // too. These are sweep-overlay-only — callers outside it pass ignoreFocusMute.
  if (!ignoreFocusMute) {
    if (App.focusAnomalies && (t === "IDENTIFIED" || t === "KNOWN_BAND" || t === "SCANNING"))
      return false;
    if (isMutedSignal(s)) return false;
  }
  if (t === "SCANNING" && !p.scanning) return false;
  if (t === "IDENTIFIED" && !p.identified) return false;
  if (t === "KNOWN_BAND" && !p.known) return false;
  if (t === "ANOMALY" && !p.anomaly) return false;
  if (s.caution && !p.caution) return false;
  return true;
}

function sweepFreqBounds(t) {
  const f0 = t.low != null ? t.low : (t.freqs && t.freqs[0]);
  const f1 = t.high != null ? t.high : (t.freqs && t.freqs[t.freqs.length - 1]);
  return (f0 != null && f1 != null) ? [Math.min(f0, f1), Math.max(f0, f1)] : null;
}

function mergeSweepLabels(rows, tolMhz) {
  tolMhz = tolMhz || 0.04;
  const out = [];
  for (const s of rows) {
    const hit = out.find(o => Math.abs(o.center_mhz - s.center_mhz) <= tolMhz);
    if (hit) {
      if ((s.peak_dbm || -999) > (hit.peak_dbm || -999)) {
        // a live "Detecting…" peak must not clobber an already-identified DB
        // signal's identity — adopt only its stronger numeric values
        if (s.live && hit.live === false) {
          hit.peak_dbm = s.peak_dbm;
          if (s.bw_khz != null) hit.bw_khz = s.bw_khz;
        } else {
          Object.assign(hit, s);
        }
      }
    } else out.push({ ...s });
  }
  return out.sort((a, b) => a.center_mhz - b.center_mhz);
}

function livePeaksFromTrace(t) {
  const amps = t.maxpeak || t.avg || t.amps;
  const freqs = t.freqs;
  if (!amps || !freqs || amps.length < 5) return [];
  const sorted = [...amps].sort((a, b) => a - b);
  const floor = sorted[Math.floor(sorted.length * 0.12)];
  const thresh = floor + 10;
  const raw = [];
  for (let i = 2; i < amps.length - 2; i++) {
    if (amps[i] < thresh) continue;
    if (amps[i] < amps[i - 1] || amps[i] < amps[i + 1]) continue;
    if (amps[i] < amps[i - 2] && amps[i] < amps[i + 2]) continue;
    const pk = amps[i];
    let lo = i, hi = i;
    while (lo > 0 && amps[lo] >= pk - 3) lo--;
    while (hi < amps.length - 1 && amps[hi] >= pk - 3) hi++;
    const bwKhz = Math.abs(freqs[hi] - freqs[lo]) * 1000;
    raw.push({
      center_mhz: freqs[i], peak_dbm: pk,
      bw_khz: Math.max(bwKhz, (freqs[1] - freqs[0]) * 1000 * 2),
      classification: "UNVERIFIED", identity_tier: "SCANNING", live: true,
      display_label: "Detecting…",
    });
  }
  return mergeSweepLabels(raw, 0.05);
}

function sweepChartSignals(t) {
  const bounds = sweepFreqBounds(t);
  if (!bounds) return livePeaksFromTrace(t);
  const [lo, hi] = bounds;
  const inRange = s => s.center_mhz >= lo && s.center_mhz <= hi;
  const db = (App.liveSweepSignals || []).filter(inRange).map(s => ({
    center_mhz: s.center_mhz,
    peak_dbm: s.peak_dbm,
    bw_khz: s.bw_khz,
    classification: s.classification,
    identity_tier: s.identity_tier,
    display_label: s.display_label,
    group_key: s.group_key,
    group_label: s.group_label,
    matched_label: s.matched_label,
    match_reason: s.match_reason,
    caution: s.caution,
    device_hint: s.device_hint,
    id_device_class: s.id_device_class,
    live: false,
  }));
  return mergeSweepLabels([...db, ...livePeaksFromTrace(t)]);
}

// How many live "Detecting…" peaks to label on the chart at once. The rest are
// still counted in the detail line + live ledger; we just don't carpet the scope.
const MAX_SCANNING_LABELS = 5;

function clusterSweepLabels(signals) {
  const out = [];
  const buckets = {};
  const scanning = [];
  for (const s of signals) {
    if (!labelVisible(s)) continue;
    const tier = signalTier(s);
    if (tier === "SCANNING") { scanning.push(s); continue; }
    if (tier !== "IDENTIFIED" || !s.group_key || s.group_key.startsWith("named:")) {
      out.push({ kind: "single", signal: s });
      continue;
    }
    if (!buckets[s.group_key]) {
      buckets[s.group_key] = { group_key: s.group_key, group_label: s.group_label || s.display_label,
        members: [] };
    }
    buckets[s.group_key].members.push(s);
  }
  for (const g of Object.values(buckets)) {
    if (g.members.length === 1 || App.expandedGroups[g.group_key]) {
      g.members.forEach(sig => out.push({ kind: "single", signal: sig }));
    } else {
      const peak = g.members.reduce((a, b) =>
        (b.peak_dbm || -999) > (a.peak_dbm || -999) ? b : a, g.members[0]);
      out.push({ kind: "group", group_key: g.group_key,
        group_label: g.group_label, count: g.members.length,
        signal: peak, members: g.members });
    }
  }
  // Live peaks carry no identity yet, so a label per peak just buries the chart.
  // Show only the strongest few and roll the rest into one count chip.
  if (scanning.length) {
    scanning.sort((a, b) => (b.peak_dbm || -999) - (a.peak_dbm || -999));
    scanning.slice(0, MAX_SCANNING_LABELS).forEach(sig => out.push({ kind: "single", signal: sig }));
    const extra = scanning.length - MAX_SCANNING_LABELS;
    if (extra > 0) out.push({ kind: "scanmore", count: extra, signal: scanning[0] });
  }
  return out;
}

function sweepLabelText(item) {
  const s = item.signal;
  const tier = signalTier(s);
  if (item.kind === "group") {
    return `${item.group_label} (${item.count})`;
  }
  if (item.kind === "scanmore") return `+${item.count} detecting…`;
  if (tier === "SCANNING") return "Detecting…";
  // device-type tag: signature ID > behavioral class > band guess
  const hint = s.id_device_class || s.device_hint
    || (s.behavior && s.behavior.label) || "";
  if (tier === "ANOMALY")
    return hint ? `${hint} · ${fmtF(s.center_mhz)}` : `ANOMALY · ${fmtF(s.center_mhz)}`;
  return s.display_label || s.matched_label || hint || fmtF(s.center_mhz);
}

function buildSweepLabelOverlays(canvas, t, signals) {
  const layer = $("sweep-label-layer");
  if (!layer || !canvas || !t || !t.freqs || t.freqs.length < 2) {
    if (layer) layer.innerHTML = "";
    return;
  }
  const W = canvas.clientWidth || canvas.width;
  const H = canvas.clientHeight || canvas.height;
  const f0 = t.freqs[0], f1 = t.freqs[t.freqs.length - 1];
  const span = f1 - f0 || 1;
  // use the SAME vertical scale drawTrace just published (focus mode rescales it),
  // so labels stay anchored to their peaks
  const sc = App._traceScale || { aMin: -120, aMax: -10 };
  const aMin = sc.aMin, aMax = sc.aMax;
  const yOf = dbm => H - (Math.max(aMin, Math.min(aMax, dbm || -80)) - aMin) / (aMax - aMin) * H;
  const xOf = mhz => ((mhz - f0) / span) * W;
  const items = clusterSweepLabels(signals || []);
  const key = items.map(it => {
    const s = it.signal;
    return `${it.kind}:${it.group_key || s.center_mhz}:${it.count || 0}:${signalTier(s)}:${s.peak_dbm}:${s.display_label || ""}`;
  }).join("|") + ":" + W + ":" + aMin + ":" + aMax;
  if (key === App._lastLabelKey) return;
  App._lastLabelKey = key;
  layer.innerHTML = "";
  const placed = [];
  const collides = (x, y) => placed.some(p => Math.abs(p.x - x) < 88 && Math.abs(p.y - y) < 18);
  // Identified/anomaly labels claim space first; live scanning peaks fill what's left.
  const rank = { IDENTIFIED: 0, ANOMALY: 1, KNOWN_BAND: 2, SCANNING: 3 };
  items.sort((a, b) =>
    (a.kind === "scanmore" ? 4 : (rank[signalTier(a.signal)] ?? 9)) -
    (b.kind === "scanmore" ? 4 : (rank[signalTier(b.signal)] ?? 9)) ||
    (b.signal.peak_dbm || -999) - (a.signal.peak_dbm || -999));
  items.forEach(item => {
    const s = item.signal;
    const x = xOf(s.center_mhz);
    if (x < 8 || x > W - 8) return;
    // labels anchor at their bottom (translate -100%), so keep y past the
    // label height from the top edge or the box clips above the chart.
    const base = Math.max(36, Math.min(H - 4, yOf(s.peak_dbm) - 12));
    let y = base, found = !collides(x, base);
    // try alternating below/above the peak, staying inside the canvas
    for (let n = 1; !found && n <= 8; n++) {
      for (const cand of [base + n * 18, base - n * 18]) {
        if (cand < 36 || cand > H - 4) continue;
        if (!collides(x, cand)) { y = cand; found = true; break; }
      }
    }
    if (!found) return;   // no room — drop rather than carpet the scope
    placed.push({ x, y });
    const tier = signalTier(s);
    const el = document.createElement("div");
    el.className = `sweep-label tier-${tier}` +
      (item.kind === "scanmore" ? " scanmore" : "") + (s.caution ? " caution" : "");
    el.style.left = x + "px";
    el.style.top = y + "px";
    el.title = s.match_reason || "";
    const main = sweepLabelText(item);
    let body = esc(main);
    if (item.kind !== "scanmore") {
      const sub = `${s.peak_dbm != null ? s.peak_dbm + " dBm" : "—"} · ` +
        `${s.bw_khz != null ? Math.round(s.bw_khz) + " kHz" : "—"}`;
      body += `<span class="sub">${esc(sub)}</span>`;
    }
    el.innerHTML = body;
    if (item.kind === "group") {
      el.dataset.groupKey = item.group_key;
      el.onclick = (e) => {
        e.stopPropagation();
        App.expandedGroups[item.group_key] = true;
        App._lastLabelKey = "";
        buildSweepLabelOverlays(canvas, t, signals);
      };
    } else if (s.group_key && App.expandedGroups[s.group_key]) {
      el.onclick = (e) => {
        e.stopPropagation();
        delete App.expandedGroups[s.group_key];
        App._lastLabelKey = "";
        buildSweepLabelOverlays(canvas, t, signals);
      };
    }
    layer.appendChild(el);
  });
}

/* ---------- canvas traces (RF Explorer for Windows style) ---------- */
function drawTrace(canvas, t, muteRanges, highlight) {
  /* three traces drawn together: realtime (green, filled), average (cyan),
     max-hold (yellow) on a black scope grid — RF Explorer for Windows look.
     F3: muteRanges (MHz {lo,hi}[]) pulls those bins to the floor ("dampen the
     expected") and the chart auto-rescales to what's left, so weak anomalies
     fill the view instead of hugging the noise floor.
     F8: t.amps may be absent (historic segment review draws only the stored
     max-hold + average); highlight = {lo,hi} MHz draws a vertical band so a
     hovered ledger row's signal stands out from the rest of the segment. */
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 800;
  const H = canvas.height = canvas.clientHeight || 230;
  ctx.fillStyle = "#000208"; ctx.fillRect(0, 0, W, H);
  const n = (t.amps || t.maxpeak || t.avg || []).length;
  if (!n) return;
  const freqs = t.freqs || [];
  const ranges = (muteRanges && muteRanges.length && freqs.length === n) ? muteRanges : null;
  const muted = i => ranges && ranges.some(r => freqs[i] >= r.lo && freqs[i] <= r.hi);
  // vertical scale — fixed by default; when dampening, fit to the un-muted energy
  let aMin = -120, aMax = -10;
  if (ranges) {
    let vmax = -200, vmin = 200;
    const scan = arr => { if (!arr) return;
      for (let i = 0; i < arr.length; i++) { if (muted(i)) continue;
        if (arr[i] > vmax) vmax = arr[i]; if (arr[i] < vmin) vmin = arr[i]; } };
    scan(t.amps); scan(t.maxpeak);
    if (vmax > -200) {
      aMax = Math.min(-10, Math.ceil((vmax + 8) / 5) * 5);
      aMin = Math.max(-120, Math.floor((vmin - 6) / 5) * 5);
      if (aMax - aMin < 30) aMin = aMax - 30;
    }
  }
  // publish the scale so the label overlay anchors to the SAME axis (else labels
  // float off their peaks when focus mode rescales the trace)
  App._traceScale = { aMin, aMax };
  const Y = v => H - (Math.max(aMin, Math.min(aMax, v)) - aMin) / (aMax - aMin) * H;
  const v = (arr, i) => muted(i) ? aMin : arr[i];   // muted bins flatline at the floor
  const step = (aMax - aMin) <= 45 ? 5 : ((aMax - aMin) <= 90 ? 10 : 20);
  // dotted scope grid
  ctx.setLineDash([2, 4]); ctx.strokeStyle = "#27313f"; ctx.lineWidth = 1;
  for (let g = Math.ceil(aMin / step) * step; g <= aMax; g += step) {
    ctx.beginPath(); ctx.moveTo(0, Y(g)); ctx.lineTo(W, Y(g)); ctx.stroke();
  }
  for (let i = 1; i < 10; i++) {
    const x = W * i / 10;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }
  ctx.setLineDash([]);
  for (let g = Math.ceil(aMin / step) * step; g <= aMax; g += (step <= 5 ? 10 : step * 2)) {
    ctx.fillStyle = "#5a6a80"; ctx.font = "9px monospace";
    ctx.fillText(g + " dBm", 4, Y(g) - 2);
  }
  // F8: hovered ledger row — vertical band behind the traces marking that
  // signal's slice of the segment (min 2 px so narrowband hits stay visible)
  if (highlight && freqs.length > 1) {
    const f0 = freqs[0], f1 = freqs[freqs.length - 1];
    if (f1 > f0 && highlight.hi > f0 && highlight.lo < f1) {
      const x0 = Math.max(0, (highlight.lo - f0) / (f1 - f0) * W);
      const x1 = Math.min(W, (highlight.hi - f0) / (f1 - f0) * W);
      const w = Math.max(x1 - x0, 2);
      ctx.fillStyle = "rgba(26,188,156,.16)";
      ctx.fillRect(x0, 0, w, H);
      ctx.strokeStyle = "rgba(26,188,156,.85)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x0 + .5, 0); ctx.lineTo(x0 + .5, H); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x0 + w - .5, 0); ctx.lineTo(x0 + w - .5, H); ctx.stroke();
    }
  }
  const plot = (arr, color, width) => {
    if (!arr || arr.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
    for (let i = 0; i < arr.length; i++) {
      const x = i / (arr.length - 1) * W;
      i ? ctx.lineTo(x, Y(v(arr, i))) : ctx.moveTo(x, Y(v(arr, i)));
    }
    ctx.stroke();
  };
  // realtime: filled area under the trace like the vendor app (absent in the
  // historic-segment review — only avg/max-hold were persisted, by design)
  if (t.amps) {
    ctx.beginPath();
    ctx.moveTo(0, H);
    for (let i = 0; i < n; i++) ctx.lineTo(i / (n - 1) * W, Y(v(t.amps, i)));
    ctx.lineTo(W, H); ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, "rgba(0,255,90,0.35)");
    grad.addColorStop(1, "rgba(0,255,90,0.04)");
    ctx.fillStyle = grad; ctx.fill();
  }
  plot(t.maxpeak, "#ffd000", 1.1);    // max-hold (yellow)
  plot(t.avg, "#00c8ff", 1.1);        // average (cyan)
  plot(t.amps, "#00ff5a", 1.5);       // realtime (green) on top
  ctx.font = "10px monospace";
  let lx = W - 250;
  [["REALTIME", "#00ff5a", !!t.amps], ["AVG", "#00c8ff", !!t.avg], ["MAX HOLD", "#ffd000", !!t.maxpeak]]
    .forEach(([label, color, on]) => {
      if (!on) return;
      ctx.fillStyle = color; ctx.fillRect(lx, 8, 10, 3);
      ctx.fillText(label, lx + 14, 13); lx += ctx.measureText(label).width + 34;
    });
  if (ranges) {
    ctx.fillStyle = "#ffd24a";
    ctx.fillText("⚠ DAMPENED — anomaly focus", 4, 13);
  }
  if (t.freqs && t.freqs.length) {
    ctx.fillStyle = "#8fa0b8";
    ctx.fillText(fmtF(t.freqs[0]), 4, H - 4);
    const mid = fmtF(t.freqs[Math.floor(t.freqs.length / 2)]);
    ctx.fillText(mid, W / 2 - ctx.measureText(mid).width / 2, H - 4);
    const lastLabel = fmtF(t.freqs[t.freqs.length - 1]);
    ctx.fillText(lastLabel, W - ctx.measureText(lastLabel).width - 4, H - 4);
  }
}

/* F3: which MHz ranges to dampen on the chart — individually muted signals
   plus (in focus mode) everything currently classified expected/known. */
function muteRangesFor() {
  const out = [];
  for (const k in App.mutedKeys) { if (App.mutedKeys[k]) out.push(App.mutedKeys[k]); }
  if (App.focusAnomalies) {
    for (const s of (App.liveSweepSignals || [])) {
      const tier = signalTier(s);
      if (tier === "IDENTIFIED" || tier === "KNOWN_BAND") {
        const half = ((s.bw_khz || 50) / 1000) / 2 + 0.02;
        out.push({ lo: s.center_mhz - half, hi: s.center_mhz + half });
      }
    }
  }
  return out;
}

function isMutedSignal(s) {
  for (const k in App.mutedKeys) {
    const r = App.mutedKeys[k];
    if (r && s.center_mhz >= r.lo && s.center_mhz <= r.hi) return true;
  }
  return false;
}

/* ---------- waterfall ---------- */
const Waterfall = { lastTs: 0, segKey: "" };
function wfColor(dbm) {
  const v = Math.max(0, Math.min(1, (dbm + 115) / 85));
  const stops = [
    [0.00, 2, 2, 30], [0.35, 0, 60, 255], [0.55, 0, 220, 80],
    [0.75, 255, 220, 0], [1.00, 255, 40, 40]];
  for (let i = 1; i < stops.length; i++) {
    if (v <= stops[i][0]) {
      const [p0, r0, g0, b0] = stops[i - 1], [p1, r1, g1, b1] = stops[i];
      const f = (v - p0) / (p1 - p0);
      return [r0 + (r1 - r0) * f, g0 + (g1 - g0) * f, b0 + (b1 - b0) * f];
    }
  }
  return [255, 40, 40];
}
function pushWaterfall(canvas, t) {
  if (!t || !t.amps || !t.amps.length) return;
  if (t.ts && t.ts === Waterfall.lastTs) return;     // no new sweep since last poll
  Waterfall.lastTs = t.ts || 0;
  const W = canvas.clientWidth || 800, H = canvas.clientHeight || 120;
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext("2d");
  const segKey = t.low + "-" + t.high;
  if (segKey !== Waterfall.segKey) {                 // new segment -> fresh waterfall
    Waterfall.segKey = segKey;
    ctx.fillStyle = "#000208"; ctx.fillRect(0, 0, W, H);
  }
  ctx.drawImage(canvas, 0, 0, W, H - 2, 0, 2, W, H - 2);   // scroll down 2px
  const row = ctx.createImageData(W, 2);
  const n = t.amps.length;
  for (let x = 0; x < W; x++) {
    // bin-CENTER sampling (round over x/(W-1)) to match drawTrace's i/(n-1)
    // x-mapping — the old floor(x/W*n) edge sampling skewed the waterfall a
    // half-bin right of the chart peaks
    const [r, g, b] = wfColor(t.amps[Math.min(n - 1, Math.round(x / Math.max(1, W - 1) * (n - 1)))]);
    for (let y = 0; y < 2; y++) {
      const o = (y * W + x) * 4;
      row.data[o] = r; row.data[o + 1] = g; row.data[o + 2] = b; row.data[o + 3] = 255;
    }
  }
  ctx.putImageData(row, 0, 0);
}

const BAND_COLORS = { broadcast: "#2e86c1", land_mobile: "#7d3c98", ism: "#e67e22",
  cellular: "#16a085", aviation: "#5d6d7e", gps: "#c0392b", ham: "#7f8c8d", other: "#566573" };
function drawStrip(rows, signals) {
  const canvas = $("strip");
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 900, H = canvas.height = 64;
  ctx.fillStyle = "#0d1319"; ctx.fillRect(0, 0, W, H);
  const dev = App.state && App.state.device;
  const hi = dev && dev.modules.length ? Math.max(...dev.modules.map(m => m.max_mhz)) : 6100;
  const X = f => Math.max(0, Math.min(1, f / hi)) * W;
  rows.forEach(r => {
    ctx.fillStyle = BAND_COLORS[r.category] || "#566573";
    ctx.fillRect(X(r.freq_low_mhz), 30, Math.max(X(r.freq_high_mhz) - X(r.freq_low_mhz), 1), 28);
  });
  (signals || []).forEach(s => {
    if (!labelVisible(s, true)) return;   // baseline strip ignores sweep focus/mute
    const tier = signalTier(s);
    ctx.strokeStyle = TIER_COLORS[tier] || "#e74c3c";
    ctx.beginPath(); ctx.moveTo(X(s.center_mhz), 4); ctx.lineTo(X(s.center_mhz), 28); ctx.stroke();
  });
  ctx.fillStyle = "#42506b"; ctx.font = "9px monospace";
  for (let g = 0; g <= hi; g += 1000) ctx.fillText((g / 1000) + "G", X(g) + 2, 12);
}

/* ---------- live ledger (under the spectral graph while sweeping) ---------- */
let ledgerLast = 0;
async function refreshLiveSweepSignals(sweepId) {
  if (!sweepId || Date.now() - ledgerLast < 900) return;
  ledgerLast = Date.now();
  try {
    const sigs = await api("/api/signals?sweep_id=" + sweepId);
    App.liveSweepSignals = sigs;
    renderLiveLedger();
  } catch (e) { /* ledger is best-effort during sweep */ }
}

/* Every detection on a client report has to carry a date. `detected_utc` is
   stamped at INSERT and backfilled for historic rows from the parent sweep's
   start, so old sweeps still date correctly. Cell shows clock time (the sweep
   is one sitting); the tooltip carries the full date for the report. */
function fmtDetected(s, full) {
  const t = s && (s.detected_utc || s.analysis_utc);
  if (!t) return full ? "no timestamp recorded" : "—";
  const d = new Date(t * 1000);
  if (isNaN(d.getTime())) return full ? "bad timestamp" : "—";
  return full ? d.toLocaleString() : d.toLocaleTimeString();
}

function renderLiveLedger() {
  const sigs = App.liveSweepSignals || [];
  const tierName = s => statusLabel(signalTier(s));
  // F3: in focus mode (or with individual mutes) the ledger shows only the
  // anomalies/cautions left after dampening the expected — full list is kept above.
  const shown = sigs.filter(s => {
    if (isMutedSignal(s)) return false;
    if (App.focusAnomalies) {
      const tt = signalTier(s);
      return tt === "ANOMALY" || s.caution;
    }
    return true;
  });
  // F8: rebuild the DOM only when a row actually changed — keeps hover state
  // alive and stops the thumbnails re-flowing on every poll
  const key = (App.focusAnomalies ? "F" : "") + Object.keys(App.mutedKeys).join(",") + "|"
    + shown.map(s => [s.id, s.triage_status, s.analysis_utc || 0, s.id_updated_utc || 0,
                      s.anomaly_score || 0, s.classification, s.identity_tier,
                      s.display_label, s.peak_dbm,
                      (s.behavior && s.behavior.class_id) || "",
                      s.inventory_summary || ""].join(":")).join("|");
  if (key === App._ledgerKey) return;
  App._ledgerKey = key;
  const header = "<tr><th>#</th><th>Signal</th><th>Detected</th><th>Frequency</th><th>Peak</th><th>SNR</th>" +
    "<th>Width</th><th>Persist</th><th>Status</th><th>Identity / verdict</th><th>Next step</th></tr>";
  const body = shown.slice().reverse().map((s, i) => {
    const k = tierName(s);
    const cls = s.identity_tier || (s.classification === "VERIFIED" ? "IDENTIFIED"
      : s.classification === "PLAUSIBLE" ? "KNOWN_BAND" : "ANOMALY");
    const id = s.display_label || s.matched_label || s.match_reason || "";
    // once ANALYZE ran, its verdict IS the identity answer — show it first
    let idText = s.analysis_summary ? "🎧 " + s.analysis_summary : id;
    if (!idText && s.behavior && s.behavior.label)
      idText = s.behavior.label;
    if (s.inventory_summary)
      idText = (idText ? idText + " · " : "") + "📡 " + s.inventory_summary;
    const ns = s.next_step;
    const nsCell = ns
      ? `<button class="ns-chip ${esc(ns.kind)}" data-sid="${s.id}" title="${esc(ns.detail)}">${esc(ns.action)}</button>`
      : "—";
    const behTitle = s.behavior && s.behavior.phrase
      ? esc(s.behavior.phrase) : "";
    return `<tr data-sid="${s.id}" data-seg="${s.segment_id ?? ""}" data-center="${s.center_mhz}" ` +
      `data-bw="${s.bw_khz ?? ""}" title="click row: review the segment it was found in (max hold + average)">` +
      `<td>${shown.length - i}</td>` +
      `<td class="thumb-cell"><img class="sig-thumb" loading="lazy" alt="" data-sid="${s.id}" ` +
      `src="/api/signals/${s.id}/image.svg?size=thumb" ` +
      `onerror="this.style.visibility='hidden'" title="click: expand signal picture + details"></td>` +
      `<td class="det-utc" title="${esc(fmtDetected(s, true))}">${esc(fmtDetected(s))}</td>` +
      `<td><a class="freqlink" href="#" data-center="${s.center_mhz}" data-label="${esc(id)}" ` +
      `data-sid="${s.id}" data-attr="${esc(s.attribution_status || "")}">${fmtF(s.center_mhz)}</a>` +
      // A frequency the OPERATOR marked by eye is not an instrument detection.
      // It has to be obvious at a glance in the ledger, not just in the database.
      (s.operator_marked ? `<span class="op-marked" title="The operator marked this ` +
        `frequency on the live display — it is NOT an automatic detection">MARKED BY EYE</span>` : "") +
      // A near-floor lead did not clear the detection threshold. It has to be
      // as visible here as it is in the report, or the operator works from a
      // ledger that quietly disagrees with the deliverable.
      (s.near_floor ? `<span class="near-floor" title="Close to the noise floor — ` +
        `it did not clear the detection threshold. A lead worth checking, not a ` +
        `measured finding: at this level noise and a weak transmitter look alike."` +
        `>NEAR FLOOR</span>` : "") +
      `</td>` +
      `<td>${s.peak_dbm} dBm</td>` +
      `<td>${s.snr_db != null ? s.snr_db + " dB" : "—"}</td>` +
      `<td>${s.bw_khz != null ? Math.round(s.bw_khz) + " kHz" : "—"}</td>` +
      `<td>${s.persistence != null ? Math.round(s.persistence * 100) + "%" : "—"}</td>` +
      `<td><span class="pill ${cls}">${k}</span>${s.caution ? " ⚠" : ""}` +
      `${s.is_novel_to_address ? " <span class='caution' title='new to this address'>★</span>" : ""}</td>` +
      `<td title="${behTitle}">${esc(idText).slice(0, 80)}</td>` +
      `<td>${nsCell}</td></tr>`;
  }).join("");
  // apply the empty-state to the BODY only (header is always non-empty, so the
  // old `header+body || fallback` could never show it); colspan spans all cols
  const empty = App.focusAnomalies
    ? "<tr><td colspan=10 class='note'>no anomalies — all detected signals are expected/known</td></tr>"
    : "<tr><td colspan=10 class='note'>no signals detected yet</td></tr>";
  $("tbl-live-ledger").innerHTML = header + (body || empty);
}

/* ---------- session / device / baseline ---------- */
$("btn-create").onclick = async () => {
  try {
    $("session-msg").textContent = "geocoding…";
    const kind = ($("in-kind") || {}).value || "field";
    const r = await api("/api/session", {
      address: $("in-address").value, radius_broadcast: $("in-rb").value,
      radius_lmr: $("in-rl").value, operator: $("in-op").value, kind });
    // a LAB session may now carry an address (geocoded like field) OR none
    // (bench work, no geo). Show coordinates whenever we actually got them, and
    // still flag LAB so a bench session is never mistaken for a client one.
    const geocoded = r.geocode && r.geocode.lat != null;
    const labTag = r.kind === "lab" ? " — LAB / BENCH (no client report, not pushed to portal)" : "";
    $("session-msg").textContent = geocoded
      ? `session #${r.session_id} @ ${r.geocode.lat.toFixed(5)}, ` +
        `${r.geocode.lon.toFixed(5)} (${r.geocode.geocoder})${labTag}`
      : `session #${r.session_id} created${labTag || " (no geo)"}`;
    $("btn-baseline").disabled = false;
  } catch (e) { $("session-msg").textContent = "ERROR: " + e.message; }
};


/* DEVICE card: plain-language "who is doing what" line so the operator can SEE
   dual-radio mode working (connections reset on every app restart). */
async function renderDeviceRoles() {
  const el = $("device-roles");     // DEVICE card (Session tab)
  const sw = $("sweep-radio-ready"); // readiness line on the Sweep screen
  if (!el && !sw) return;
  let r;
  try { r = await api("/api/device/roles"); }
  catch (_) { if (el) el.style.display = "none"; if (sw) sw.textContent = ""; return; }
  const name = s => s === "pluto" ? "Pluto" : s === "b220" ? "B220 Mini" : "RF Explorer";
  const roles = r.roles || [];
  if (el) {
    if (!roles.length) { el.style.display = "none"; }
    else {
      const job = x => x.role === "sweeper" ? "runs the sweep"
                     : x.role === "monitor" ? "listens/analyzes signals" : "connected (idle)";
      el.innerHTML = roles.map(x =>
        `<b>${name(x.source)}</b> — ${job(x)}${x.busy ? " (busy)" : ""}`).join(" &nbsp;·&nbsp; ");
      if (r.concurrent_enabled && roles.length >= 2) {
        el.innerHTML += ` &nbsp;·&nbsp; <b style="color:var(--green)">BOTH RADIOS ACTIVE</b> — you can inspect signals while a sweep runs`;
        // field resilience: let the operator swap which radio sweeps vs monitors
        el.innerHTML += ` &nbsp; <button class="btn" id="btn-swap-roles" style="padding:2px 8px;font-size:11px" title="switch which radio sweeps and which monitors">⇄ SWAP ROLES</button>`;
      } else if (r.concurrent_enabled && roles.length === 1)
        el.innerHTML += ` &nbsp;·&nbsp; connect the other radio too to inspect during sweeps`;
      el.style.display = "";
      const swapBtn = $("btn-swap-roles");
      if (swapBtn) swapBtn.onclick = async () => {
        swapBtn.disabled = true;
        try { await api("/api/device/swap", {}); }
        catch (e) { alert("Swap failed: " + (e.message || e)); }
        renderDeviceRoles();
      };
    }
  }
  if (sw) {
    const have = new Set(roles.map(x => x.source));
    if (!roles.length)
      sw.innerHTML = `<span style="color:var(--dim)">No radio connected — connect one on the Session tab (DEVICE) before sweeping.</span>`;
    else if (r.concurrent_enabled && have.has("rfexplorer") && have.has("pluto"))
      sw.innerHTML = `<span style="color:var(--green)">✓ Both radios ready</span> — the Pluto monitors while the RF Explorer sweeps, so you can inspect signals live mid-sweep.`;
    else if (r.concurrent_enabled)
      sw.innerHTML = `<span style="color:var(--amber)">⚠ Only ${name(roles[0].source)} connected.</span> Connect the Pluto too (Session tab › DEVICE) to inspect signals live during the sweep — the sweep still runs either way.`;
    else
      sw.innerHTML = `<b>${name(roles[0].source)}</b> connected — ready to sweep.`;
  }
}

$("btn-connect").onclick = async () => {
  const src = $("device-source") ? $("device-source").value : null;
  $("device-msg").textContent = `connecting to ${src === "pluto" ? "Pluto" : src === "b220" ? "B220 Mini (first connect flashes firmware, ~20 s)" : "RF Explorer"}…`;
  try {
    // dual-radio: if concurrent monitoring is on and ANOTHER radio is already
    // connected, keep it open (RFE sweeps while the Pluto monitors)
    let keepOther = false;
    try {
      const roles = await api("/api/device/roles");
      keepOther = !!(roles.concurrent_enabled &&
                     roles.roles.some(r => r.source !== src));
    } catch (_) { /* roles unavailable → legacy single-device connect */ }
    const d = await api("/api/device/connect", src ? { source: src, keep_other: keepOther } : {});
    $("device-msg").textContent = "connected";
    $("device-detail").innerHTML =
      `<div>Model</div><div>${esc(d.model)} (driver: ${esc(d.driver)})</div>` +
      `<div>Firmware</div><div>${esc(d.firmware)}</div>` +
      `<div>Modules</div><div>${d.modules.map(m =>
        `${m.side.toUpperCase()} SMA: ${esc(m.model)} — ${fmtF(m.min_mhz)} to ${fmtF(m.max_mhz)}`).join("<br>")}</div>` +
      (d.wideband_license ? "<div>License</div><div>RFELIC05 wideband EXTENSION ACTIVE</div>" : "") +
      (d.modules.length < 2 ? `<div>⚠ Coverage</div><div class="caution">single-module mode — coverage gap will be noted in the report</div>` : "") +
      (d.warnings.length ? `<div>⚠ Warnings</div><div class="caution">${d.warnings.map(esc).join("<br>")}</div>` : "");
    renderDeviceRoles();
  } catch (e) {
    $("device-msg").textContent = "FAILED: " + e.message;
    // a driver/native-load failure is the classic first-run wall — nudge the
    // operator straight to the in-app driver installer instead of a dead end
    const drv = $("btn-install-driver");
    if (drv && /driver|libiio|LoadLibrary|not loadable|no device|find the file/i.test(e.message))
      drv.classList.add("primary");
  }
};
// INSTALL RADIO DRIVER — runs the bundled Pluto/USB driver installer from inside
// the app so the client never has to find a .exe on the stick.
if ($("btn-install-driver")) $("btn-install-driver").onclick = async () => {
  const b = $("btn-install-driver");
  b.disabled = true;
  $("device-msg").textContent = "launching the radio driver installer…";
  try {
    const r = await api("/api/device/install_driver", {});
    $("device-msg").textContent = r.message || "driver installer launched — approve its prompts, then CONNECT";
  } catch (e) {
    $("device-msg").textContent = "driver install failed: " + e.message;
  }
  b.disabled = false;
};
renderDeviceRoles();   // show current radio roles on page load too

$("btn-baseline").onclick = async () => {
  try { await api("/api/baseline/build", {}); } catch (e) { $("baseline-msg").textContent = e.message; }
};

/* ---------- case files ---------- */
async function uploadFiles(inputId, msgId, extra, after) {
  const files = [...$(inputId).files];
  if (!files.length) return alert("Choose one or more files first.");
  let done = 0;
  for (const f of files) {
    $(msgId).textContent = `uploading ${done + 1}/${files.length} — ${f.name}…`;
    const fd = new FormData();
    fd.append("file", f);
    Object.entries(extra).forEach(([k, v]) => fd.append(k, v));
    try {
      const r = await fetch("/api/case_files", { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "upload failed");
      done++;
    } catch (e) {
      $(msgId).textContent = `FAILED on ${f.name}: ${e.message} (${done} stored)`;
      after();
      return;
    }
  }
  $(msgId).textContent = `${done} file${done !== 1 ? "s" : ""} stored`;
  $(inputId).value = "";
  after();
}
$("btn-case-upload").onclick = () =>
  uploadFiles("case-file-input", "case-msg",
              { kind: $("case-file-kind").value }, loadCaseFiles);
async function loadCaseFiles() {
  try {
    const rows = await api("/api/case_files");
    $("tbl-case-files").innerHTML = rows.length ?
      "<tr><th>File</th><th>Kind</th><th>POI</th><th>Size</th><th>Uploaded</th></tr>" +
      rows.map(f => `<tr><td><a style="color:var(--cyan)" href="/case_files/${f.id}">${esc(f.filename)}</a></td>` +
        `<td>${esc(f.kind || "")}</td><td>${esc(f.poi_name || "")}</td>` +
        `<td>${(f.size_bytes / 1024).toFixed(0)} KB</td>` +
        `<td>${new Date(f.uploaded_utc * 1000).toLocaleString()}</td></tr>`).join("") : "";
  } catch (e) {}
}

async function loadSessions() {
  try {
    const rows = await api("/api/sessions");
    $("tbl-sessions").innerHTML = "<tr><th>#</th><th>created</th><th>address</th><th></th></tr>" +
      rows.map(s => `<tr><td>${s.id}</td><td>${new Date(s.created_utc * 1000).toLocaleString()}</td>` +
        `<td>${esc(s.address)}</td><td><button class="btn" onclick="openSession(${s.id})">OPEN</button></td></tr>`).join("");
  } catch (e) { /* no sessions yet */ }
}

function historyPhaseOptions(value) {
  return `<option value="ambient"${value === "ambient" ? " selected" : ""}>OUTSIDE / AMBIENT</option>` +
         `<option value="interior"${value === "interior" ? " selected" : ""}>INSIDE / INTERIOR</option>`;
}

/* ---------- RECENT SWEEPS (landing view) ----------------------------------
   The app boots here instead of restoring the last view from the URL hash,
   which used to drop the operator straight back into a finished sweep — its
   hits reading as part of the NEW job. Top: the last few sweeps as cards.
   Below: every sweep in the session, scrollable. Click either to open it. */
const RECENT_CARD_COUNT = 4;

function sweepWhen(w, full) {
  if (!w || !w.started_utc) return "—";
  const d = new Date(w.started_utc * 1000);
  if (isNaN(d.getTime())) return "—";
  return full ? d.toLocaleString() : d.toLocaleDateString() + " " + d.toLocaleTimeString();
}

function sweepDuration(w) {
  if (!w || !w.started_utc || !w.finished_utc) return "incomplete";
  return Math.max(0, (w.finished_utc - w.started_utc) / 60).toFixed(1) + " min";
}

function sweepRange(w) {
  if (!w || w.range_low_mhz == null || w.range_high_mhz == null) return "—";
  return `${fmtF(w.range_low_mhz)} – ${fmtF(w.range_high_mhz)}`;
}

// what was actually done: phase + profile, in the operator's words
function sweepWhat(w) {
  const phase = (w.phase || "—").toUpperCase();
  return w.profile ? `${phase} · ${esc(w.profile)}` : phase;
}

function openSweep(id) {
  if (!id) return;
  App._lastFinishedSweepId = id;
  // a DELIBERATE open may populate the ledger; the cold-open guard only blocks
  // the automatic rehydrate that used to happen on every page load
  App._sweepSeenThisSession = true;
  ledgerLast = 0;
  refreshLiveSweepSignals(id);
  show("sweep");
}

/* Two-level landing: ALL SESSIONS  <->  the sweeps inside one session.
   Drilling into a session is REVIEW ONLY — it does not change which session new
   sweeps record into. Making it active is a separate, explicit button, so
   browsing last month's job can never silently redirect today's work. */
function showSessionsPane() {
  const a = $("pane-sessions"), b = $("pane-sweeps");
  if (a) a.style.display = "";
  if (b) b.style.display = "none";
  loadSessionsPane();
}

function showSweepsPane(sessionId, message) {
  const a = $("pane-sessions"), b = $("pane-sweeps");
  if (a) a.style.display = "none";
  if (b) b.style.display = "";
  loadRecentSweeps(sessionId, message);
}

async function loadRecentSweeps(sessionId, message) {
  const cards = $("recent-cards"), table = $("tbl-recent-sweeps");
  if (!cards || !table) return;
  let data;
  try {
    data = await api("/api/sweeps/history"
      + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""));
  } catch (e) {
    cards.innerHTML = "";
    table.innerHTML = "";
    if ($("recent-msg")) {
      $("recent-msg").textContent =
        "no open session — press + NEW SESSION on the ALL SESSIONS screen";
    }
    if ($("recent-session-info")) $("recent-session-info").innerHTML = "";
    return;
  }
  const sweeps = (data.sweeps || []).slice();   // already newest-first from the API
  const session = data.session || {};
  App.recentSweeps = sweeps;
  App.viewingSessionId = session.id || null;
  App.viewingSessionKind = session.kind || "field";
  if (session.is_open) App.openSessionId = session.id;
  // WHICH engagement am I looking at, is it the ACTIVE one, and is it client
  // work or bench work? Getting this wrong is how bench data reaches a client.
  const isLab = App.viewingSessionKind === "lab";
  const badge = $("recent-kind-badge");
  if (badge) {
    badge.innerHTML = isLab
      ? `<span class="kind-lab">LAB / BENCH</span>`
      : `<span class="kind-field">FIELD</span>`;
  }
  const tk = $("btn-toggle-kind");
  if (tk) tk.textContent = isLab ? "MARK AS FIELD" : "MARK AS LAB";
  const title = $("recent-session-title");
  if (title) title.textContent = `SESSION #${session.id || "—"}`;
  const act = $("btn-activate-session");
  if (act) act.style.display = session.is_open ? "none" : "";
  const si = $("recent-session-info");
  if (si) {
    si.innerHTML = session.id
      ? `${esc(session.address || "no address recorded")}` +
        `<span class="note"> · opened ` +
        `${esc(sweepWhen({ started_utc: session.created_utc }))}` +
        (session.is_open ? " · <b>ACTIVE</b> — new sweeps record here" : "") + `</span>`
      : `<span class="note">no session open — press <b>+ NEW SESSION</b></span>`;
  }
  const hint = $("recent-session-hint");
  if (hint) {
    hint.innerHTML = isLab
      ? "Bench / test work. <b>No client report is generated for this session and " +
        "it is never pushed to the client portal.</b>"
      : (session.is_open
          ? "Client work. New sweeps record into this session."
          : "Client work. You are reviewing this session — new sweeps still record " +
            "into the active one until you press MAKE ACTIVE.");
  }
  if ($("recent-msg")) {
    $("recent-msg").textContent = `session #${session.id || "—"} · ` +
      `${sweeps.length} sweep${sweeps.length === 1 ? "" : "s"}`;
  }
  if (!sweeps.length) {
    cards.innerHTML = `<p class="note">No sweeps recorded in this session yet.</p>`;
    table.innerHTML = "";
    return;
  }
  // --- the last few, as cards
  cards.innerHTML = sweeps.slice(0, RECENT_CARD_COUNT).map(w => {
    const hi = w.high_count || 0;
    const tone = w.status === "running" ? "#ffd24a"
               : hi > 0 ? "#ff9b6b" : "#7bd88f";
    return `<div class="recent-card" data-sweep="${w.id}" role="button" tabindex="0"` +
      ` title="open this sweep's detections"` +
      ` style="border:1px solid #24405c;border-left:3px solid ${tone};border-radius:6px;` +
      `padding:10px 12px;cursor:pointer;background:#0d1319">` +
      `<div style="color:#7fd1ff;font-weight:700">${sweepWhat(w)}</div>` +
      `<div style="font-size:12px;color:#cdd6e0">${esc(w.room || "location not set")}</div>` +
      `<div class="note" style="margin:4px 0">${esc(sweepWhen(w))}</div>` +
      `<div class="note">${esc(sweepRange(w))} · ${esc(sweepDuration(w))}</div>` +
      `<div style="margin-top:6px;font-size:12px">` +
      `<b>${w.signal_count || 0}</b> signals` +
      `<span class="note"> · ${hi} high · ${w.medium_count || 0} med · ` +
      `${w.low_count || 0} low</span></div>` +
      (w.status && w.status !== "done"
        ? `<div class="note" style="color:#ffd24a">status: ${esc(w.status)}</div>` : "") +
      `<div style="margin-top:6px">${sweepAction(w)}</div>` +
      `</div>`;
  }).join("");
  // --- everything, as a scrollable list
  table.innerHTML =
    "<tr><th>Started</th><th>What was done</th><th>Room / location</th>" +
    "<th>Range</th><th>Duration</th><th>Signals</th><th>Status</th><th></th></tr>" +
    sweeps.map(w =>
      `<tr data-sweep="${w.id}" style="cursor:pointer" title="open this sweep's detections">` +
      `<td>${esc(sweepWhen(w))}</td>` +
      `<td>${sweepWhat(w)}</td>` +
      `<td>${esc(w.room || "—")}</td>` +
      `<td>${esc(sweepRange(w))}</td>` +
      `<td>${esc(sweepDuration(w))}</td>` +
      `<td>${w.signal_count || 0}` +
      `<span class="note"> (${w.high_count || 0}h/${w.medium_count || 0}m/${w.low_count || 0}l)</span></td>` +
      `<td>${esc(w.status || "—")}</td>` +
      `<td>${sweepAction(w)}</td></tr>`).join("");
  if (message && $("recent-msg")) $("recent-msg").textContent = message;
}

/* RESUME vs REVIEW — an honest distinction, not a hopeful one.
   Only a sweep that is STILL RUNNING can be resumed (that is the sustain/next-
   segment gate in the runner). A sweep that finished, aborted or errored is a
   closed record: there is no re-entering it, so the only truthful action is to
   review its detections. Labelling a finished sweep "RESUME" would promise
   something the backend cannot do. */
function sweepAction(w) {
  return w.status === "running"
    ? `<button class="btn primary" data-sweep-resume="${w.id}">▶ RESUME</button>`
    : `<button class="btn" data-sweep="${w.id}">REVIEW</button>`;
}

// one delegated handler for cards, rows AND the REVIEW / RESUME buttons —
// survives every re-render
document.addEventListener("click", (ev) => {
  if (!ev.target || !ev.target.closest) return;
  const res = ev.target.closest("[data-sweep-resume]");
  if (res) {                       // still running: go watch it, don't reload it
    show("sweep");
    return;
  }
  const hit = ev.target.closest(
    "#recent-cards [data-sweep], #tbl-recent-sweeps tr[data-sweep]");
  if (!hit) return;
  const id = parseInt(hit.getAttribute("data-sweep"), 10);
  if (id) openSweep(id);
});

/* ALL SESSIONS pane. 85+ engagements and growing, so it scrolls inside its own
   card and is filterable — otherwise the old ones are simply unreachable. */
async function loadSessionsPane() {
  const tbl = $("tbl-session-switch");
  if (!tbl) return;
  let list = [];
  try {
    list = await api("/api/sessions");
  } catch (e) {
    tbl.innerHTML = "<tr><td class='note'>could not load sessions</td></tr>";
    return;
  }
  App._sessionList = list || [];
  const fi = $("session-filter");
  if (fi && !fi._wired) { fi.oninput = () => renderSessionSwitchRows(fi.value); fi._wired = true; }
  renderSessionSwitchRows(fi ? fi.value : "");
}

function renderSessionSwitchRows(filter) {
  const tbl = $("tbl-session-switch");
  if (!tbl) return;
  const all = App._sessionList || [];
  const q = (filter || "").trim().toLowerCase();
  const list = q
    ? all.filter(s => String(s.id).includes(q)
        || (s.address || "").toLowerCase().includes(q)
        || (s.operator || "").toLowerCase().includes(q))
    : all;
  const cnt = $("session-count");
  if (cnt) {
    cnt.textContent = q ? `${list.length} of ${all.length} sessions`
                        : `${all.length} sessions`;
  }
  tbl.innerHTML = "<tr><th>#</th><th>Kind</th><th>Address</th><th>Created</th>" +
    "<th>Sweeps</th><th>Operator</th></tr>" +
    (list.length
      ? list.map(s => {
          const open = s.id === App.openSessionId;
          const lab = (s.kind || "field") === "lab";
          return `<tr data-session="${s.id}" title="see the sweeps in this session"` +
            ` style="cursor:pointer${open ? ";color:#7bd88f" : ""}">` +
            `<td>${s.id}${open ? " ●" : ""}</td>` +
            `<td><span class="${lab ? "kind-lab" : "kind-field"}">` +
            `${lab ? "LAB" : "FIELD"}</span></td>` +
            `<td>${esc(s.address || "—")}</td>` +
            `<td>${esc(sweepWhen({ started_utc: s.created_utc }))}</td>` +
            `<td>${s.sweep_count != null ? s.sweep_count : "—"}</td>` +
            `<td>${esc(s.operator || "—")}</td></tr>`;
        }).join("")
      : "<tr><td colspan=6 class='note'>no session matches that filter</td></tr>");
}

// clicking a session DRILLS IN to review its sweeps; it does NOT change which
// session new work records into (that is the explicit MAKE ACTIVE button)
document.addEventListener("click", (ev) => {
  const row = ev.target && ev.target.closest
    ? ev.target.closest("#tbl-session-switch tr[data-session]") : null;
  if (!row) return;
  const id = parseInt(row.getAttribute("data-session"), 10);
  if (id) showSweepsPane(id);
});

async function loadSweepHistory(message) {
  const table = $("tbl-sweep-history");
  if (!table) return;
  try {
    const data = await api("/api/sweeps/history");
    App.sweepHistory = data.sweeps || [];
    const session = data.session || {};
    App.sweepHistorySessionId = session.id || null;
    table.innerHTML =
      "<tr><th># / started</th><th>status</th><th>phase</th><th>room / location</th>" +
      "<th>signals</th><th>technician notes</th><th></th></tr>" +
      App.sweepHistory.map(w => {
        const editable = w.status !== "running" &&
                         (w.phase === "ambient" || w.phase === "interior");
        const mins = w.finished_utc && w.started_utc
          ? Math.max(0, (w.finished_utc - w.started_utc) / 60).toFixed(1) + " min"
          : "incomplete";
        const counts = `${w.signal_count || 0} total` +
          `<div class="note">${w.high_count || 0} high · ${w.medium_count || 0} medium · ${w.low_count || 0} low</div>`;
        const phase = editable
          ? `<select id="hist-phase-${w.id}">${historyPhaseOptions(w.phase)}</select>`
          : `<span class="pill ${esc(w.status)}">${esc(w.phase)}</span>`;
        return `<tr>` +
          `<td><b>#${w.id}</b><div class="note">${new Date(w.started_utc * 1000).toLocaleString()}</div>` +
          `<div class="note">${esc(w.module || "")} · ${esc(w.profile || "")} · ${mins}</div></td>` +
          `<td><span class="pill ${esc(w.status)}">${esc(w.status)}</span></td>` +
          `<td>${phase}</td>` +
          `<td><input id="hist-room-${w.id}" value="${esc(w.room || "")}" ` +
          `style="width:150px" ${editable ? "" : "disabled"}></td>` +
          `<td>${counts}</td>` +
          `<td><textarea id="hist-notes-${w.id}" style="width:240px;height:58px;` +
          `background:var(--input-bg);color:var(--txt);border:1px solid var(--line);font-family:var(--mono)" ` +
          `${editable ? "" : "disabled"}>${esc(w.operator_notes || "")}</textarea>` +
          (w.last_edited_utc ? `<div class="note">edited ${new Date(w.last_edited_utc * 1000).toLocaleString()}</div>` : "") +
          `</td><td>${editable ? `<button class="btn primary" onclick="saveHistoricalSweep(${w.id})">SAVE + RESCORE</button>` : ""}` +
          `<button class="btn" onclick="show('results')">SESSION RESULTS</button></td></tr>`;
      }).join("");
    $("history-msg").textContent = message ||
      `session #${session.id || "—"} · ${App.sweepHistory.length} saved sweep${App.sweepHistory.length === 1 ? "" : "s"}`;
  } catch (e) {
    App.sweepHistory = [];
    App.sweepHistorySessionId = null;
    table.innerHTML = "";
    $("history-msg").textContent = e.message === "no session" ? "open or create a session first" : e.message;
  }
}

window.saveHistoricalSweep = async (id) => {
  const row = (App.sweepHistory || []).find(w => w.id === id);
  if (!row) return;
  const phase = $("hist-phase-" + id).value;
  const room = $("hist-room-" + id).value.trim();
  const notes = $("hist-notes-" + id).value.trim();
  const changed = phase !== row.phase || room !== (row.room || "");
  if (changed && !confirm(
      `Correct sweep #${id} from ${row.phase}/${row.room || "unspecified"} to ` +
      `${phase}/${room || (phase === "ambient" ? "exterior" : "unspecified")}?\n\n` +
      "Raw measurements will be preserved. All session anomaly scores will be recomputed.")) return;
  $("history-msg").textContent = `saving sweep #${id} and rescoring the session…`;
  try {
    const r = await api("/api/sweep/history/update", {
      session_id: App.sweepHistorySessionId, sweep_id: id, phase, room, notes
    });
    await loadSweepHistory(r.message || "saved and rescored");
    loadResults();
    loadBaseline();
  } catch (e) {
    $("history-msg").textContent = "FAILED: " + e.message;
  }
};

$("btn-history-refresh").onclick = () => loadSweepHistory("history refreshed");

window.openSession = async (id) => {
  await api("/api/session/open", { id });
  await poll();
  loadBaseline();
  loadEngagement();
  loadCaseFiles();
  loadTechTasks();
  loadReportOptions();
  loadSweepHistory(`opened session #${id}`);
  App.triage = null;
  App.triageRemaining = 0;
};

async function loadBaseline() {
  try {
    App.baseline = await api("/api/baseline");
  } catch (e) { return; }
  const b = App.baseline;
  $("baseline-count").textContent = `${b.count} entries`;
  $("baseline-degraded").innerHTML = b.degraded ?
    `<div class="card" style="border-color:var(--red)"><span class="pill failed">DEGRADED BASELINE</span>
     static allocation table only — no licensed-record verification possible (offline?)</div>` : "";
  const srcSel = $("baseline-source-filter");
  const sources = [...new Set(b.rows.map(r => r.source))];
  srcSel.innerHTML = '<option value="">all</option>' +
    sources.map(s => `<option>${esc(s)}</option>`).join("");
  renderBaselineTable();
  let sigs = [];
  try { sigs = await api("/api/signals?phase=interior"); } catch (e) {}
  drawStrip(b.rows, sigs);
}

function renderBaselineTable() {
  const f = ($("baseline-filter").value || "").toLowerCase();
  const src = $("baseline-source-filter").value;
  const rows = (App.baseline ? App.baseline.rows : []).filter(r =>
    (!src || r.source === src) &&
    (!f || JSON.stringify(r).toLowerCase().includes(f)))
    .sort((a, b) => a.freq_low_mhz - b.freq_low_mhz)
    .slice(0, 1500);
  App.blView = rows;                 // ordered list the inspect panel steps through
  $("tbl-baseline").innerHTML =
    "<tr><th>Frequency</th><th>Service</th><th>Callsign/Label</th><th>Licensee</th>" +
    "<th>Dist mi</th><th>Brg</th><th>Source</th><th>Confidence</th><th></th></tr>" +
    rows.map((r, i) => {
      const f1 = r.freq_low_mhz === r.freq_high_mhz ? fmtF(r.freq_low_mhz) :
        fmtF(r.freq_low_mhz) + " – " + fmtF(r.freq_high_mhz);
      return `<tr><td><a href="#" class="freqlink" data-i="${i}">${f1}</a></td><td>${esc(r.service)}</td>` +
        `<td>${esc(r.callsign || r.label)}</td><td>${esc(r.licensee)}</td>` +
        `<td>${r.distance_mi != null ? r.distance_mi.toFixed(1) : ""}</td>` +
        `<td>${r.bearing_deg != null ? r.bearing_deg.toFixed(0) + "°" : ""}</td>` +
        `<td>${esc(r.source)}</td><td>${esc(r.confidence)}</td>` +
        `<td>${r.caution ? '<span class="caution">⚠</span>' : ""}</td></tr>`;
    }).join("");
}
$("baseline-filter").oninput = renderBaselineTable;
$("baseline-source-filter").onchange = renderBaselineTable;

/* ---------- Web Audio chunked playback (smooth live listen) ----------
   The <audio src="/api/listen"> path stutters: the werkzeug dev server delivers
   the WAV stream at ~0.8x real-time and the media element has no jitter buffer,
   so any underrun stalls or resets currentTime=0. Instead we fetch the raw-PCM
   stream (/api/listen_pcm), parse int16 frames as they arrive, and schedule small
   AudioBuffers back-to-back against the AudioContext clock. A small lead-in
   (PCM_PLAY_LEAD_S) absorbs delivery jitter so brief gaps don't become audible
   gaps; if delivery falls behind and the schedule cursor goes stale, we re-anchor
   to "now + lead" rather than playing a burst of backlog. */
const Listen = {
  ctx: null, gain: null, reader: null, abort: null,
  nextAt: 0,          // AudioContext time the next buffer should start
  rate: 0,            // PCM sample rate from the stream header
  leftover: null,     // trailing odd byte carried between chunks (int16 alignment)
  active: false,
};
const PCM_PLAY_LEAD_S = 0.25;    // schedule this far ahead of the clock (jitter buffer)
const PCM_MAGIC = "SHLDPCM1";

/* ---------- LISTEN progress ladder ----------------------------------------
   On a single-radio rig, LISTEN has to TAKE the radio off the sweep before it
   can demodulate. That hand-off is several seconds of real work (stop sweep →
   wait for release → tune → open stream → demod) and it used to be completely
   invisible: the operator clicked LISTEN and, if anything went wrong, got
   silence. This renders the sequence live, so the radio being taken is
   something you WATCH rather than infer.

   Deliberately fixed-position and appended to <body>: the sweep overlay covers
   the page, and #ft-msg is rewritten once a second by render(), so neither is
   a safe place to report this. Builds its own DOM — no index.html change, so
   this cannot drift out of sync with the markup. */
const ListenSteps = {
  el: null, steps: [], hideT: null,

  _mount() {
    if (this.el && this.el.isConnected) return this.el;
    let el = document.getElementById("listen-steps");
    if (!el) {
      el = document.createElement("div");
      el.id = "listen-steps";
      el.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:99999;min-width:290px;" +
        "max-width:380px;background:#0b0f14ee;border:1px solid #24405c;" +
        "border-radius:6px;padding:10px 12px;font:11px/1.7 Consolas,monospace;" +
        "color:#cdd6e0;box-shadow:0 6px 22px #000a;pointer-events:none";
      document.body.appendChild(el);
    }
    this.el = el;
    return el;
  },

  // list: array of step labels. Renders them all as pending.
  begin(list) {
    if (this.hideT) { clearTimeout(this.hideT); this.hideT = null; }
    this.steps = (list || []).map((t) => ({ t: t, s: "pending" }));
    this._render();
  },

  active(i, label) { this._set(i, "active", label); },
  done(i, label) { this._set(i, "done", label); },
  fail(i, why) { this._set(i, "fail", null, why); },

  _set(i, state, label, why) {
    const st = this.steps[i];
    if (!st) return;
    st.s = state;
    if (label) st.t = label;
    if (why) st.why = why;
    // finishing a step implies every earlier one is done
    if (state === "done" || state === "active") {
      for (let k = 0; k < i; k++) {
        if (this.steps[k].s === "pending" || this.steps[k].s === "active") {
          this.steps[k].s = "done";
        }
      }
    }
    this._render();
  },

  // auto-dismiss once audio is flowing; failures stay up until the next click
  finish(ok) {
    this._render();
    if (!ok) return;
    if (this.hideT) clearTimeout(this.hideT);
    this.hideT = setTimeout(() => this.clear(), 4500);
  },

  clear() {
    this.steps = [];
    if (this.el) { this.el.innerHTML = ""; this.el.style.display = "none"; }
  },

  _render() {
    let el;
    try { el = this._mount(); } catch (e) { return; }
    if (!el) return;
    if (!this.steps.length) { el.innerHTML = ""; el.style.display = "none"; return; }
    el.style.display = "";
    const ico = { pending: "○", active: "◐", done: "●", fail: "✕" };
    const col = { pending: "#6b7b90", active: "#ffd24a", done: "#7bd88f", fail: "#ff6b6b" };
    el.innerHTML =
      "<div style='color:#7fd1ff;margin-bottom:4px'>LISTEN — radio hand-off</div>" +
      this.steps.map((st, i) =>
        `<div style="color:${col[st.s] || "#cdd6e0"}">${ico[st.s] || "○"} ` +
        `${i + 1}. ${esc(st.t)}` +
        (st.why ? `<div style="padding-left:14px;color:#ff9b6b">${esc(st.why)}</div>` : "") +
        `</div>`).join("");
  },

  /* Bridge from listenStart's onState(msg, live, mode) callback to the ladder.
     `live === true` means the PCM header arrived — stream AND demod are up. */
  onStream(msg, live, mode) {
    if (!this.steps.length) return;
    const demod = this.steps.length - 1, stream = this.steps.length - 2;
    if (live === true) {
      if (stream >= 0) this.done(stream);
      this.done(demod, "demodulating" + (mode ? " — " + mode.toUpperCase() : "") + " · LIVE");
      this.finish(true);
    } else if (live === false && msg && !/^tuning/i.test(msg)) {
      // a real failure/termination, not the initial "tuning & streaming…" note
      const at = this.steps.findIndex((s) => s.s === "active");
      this.fail(at >= 0 ? at : demod, msg);
      this.finish(false);
    }
  },
};

function listenStop() {
  Listen.active = false;
  if (Listen.abort) { try { Listen.abort.abort(); } catch (e) {} Listen.abort = null; }
  Listen.reader = null;
  if (Listen.gain && Listen.ctx) {
    try { Listen.gain.gain.cancelScheduledValues(Listen.ctx.currentTime); } catch (e) {}
  }
  Listen.nextAt = 0; Listen.leftover = null; Listen.rate = 0;
}

// must be called from a user gesture (the click that opens inspect) so the
// browser autoplay policy lets the AudioContext start/resume.
// Resolves true (PCM streaming), "fallback" (this CLIENT can't do Web Audio /
// fetch-streaming — the <audio> path is worth trying), or false (failed or the
// server refused — the <audio> path must NOT be tried; see caller).
async function listenStart(center, modeOverride, onState) {
  listenStop();
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) { onState("Web Audio not supported — using <audio> fallback", false); return "fallback"; }
  if (!Listen.ctx) {
    Listen.ctx = new Ctor();
    Listen.gain = Listen.ctx.createGain();
    Listen.gain.connect(Listen.ctx.destination);
  }
  try { await Listen.ctx.resume(); } catch (e) {}
  Listen.active = true;
  Listen.abort = new AbortController();
  const ibw = ($("insp-ibw") || {}).value || "2.4";
  const blockS = ($("insp-block-s") || {}).value || "0.5";
  try {
    localStorage.setItem("shield.listenIbw", ibw);
    localStorage.setItem("shield.listenBlockS", blockS);
  } catch (e) {}
  const url = `/api/listen_pcm?center_mhz=${encodeURIComponent(center)}` +
              (modeOverride ? `&mode=${encodeURIComponent(modeOverride)}` : "") +
              `&ibw_mhz=${encodeURIComponent(ibw)}&block_s=${encodeURIComponent(blockS)}`;
  let r;
  try {
    r = await fetch(url, { signal: Listen.abort.signal, cache: "no-store" });
  } catch (e) {
    if (Listen.active) onState("stream failed: " + e.message, false);
    return false;
  }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    onState(msg, false);
    return false;
  }
  if (!r.body || !r.body.getReader) {
    onState("streaming unsupported — using <audio> fallback", false);
    return "fallback";
  }
  Listen.reader = r.body.getReader();
  pumpPcm(onState).catch(() => {});
  return true;
}

async function pumpPcm(onState) {
  let headerDone = false, headerBuf = "";
  const ctx = Listen.ctx;
  while (Listen.active && Listen.reader) {
    let chunk;
    try { chunk = await Listen.reader.read(); }
    catch (e) { break; }                       // aborted / disconnected
    if (chunk.done) break;
    let bytes = chunk.value;                    // Uint8Array
    if (!headerDone) {
      // header is PCM_MAGIC + one JSON line terminated by '\n'
      let nl = -1;
      for (let i = 0; i < bytes.length; i++) { if (bytes[i] === 10) { nl = i; break; } }
      headerBuf += new TextDecoder().decode(nl >= 0 ? bytes.subarray(0, nl) : bytes);
      if (nl < 0) continue;                     // header spans chunks (rare)
      const meta = parseListenHeader(headerBuf);
      if (!meta) { onState("bad stream header", false); break; }
      Listen.rate = meta.rate;
      if (meta.mode) onState("● live", true, meta.mode);
      else onState("● live", true);
      headerDone = true;
      bytes = bytes.subarray(nl + 1);           // remainder is PCM
      if (!bytes.length) continue;
    }
    schedulePcmBytes(bytes);
  }
  if (Listen.active) onState("stream ended", false);
}

function parseListenHeader(s) {
  if (s.slice(0, PCM_MAGIC.length) !== PCM_MAGIC) return null;
  try { return JSON.parse(s.slice(PCM_MAGIC.length)); } catch (e) { return null; }
}

function schedulePcmBytes(bytes) {
  const ctx = Listen.ctx;
  if (!ctx || !Listen.rate) return;
  // re-attach any trailing odd byte from the previous chunk (int16 alignment)
  if (Listen.leftover) {
    const merged = new Uint8Array(Listen.leftover.length + bytes.length);
    merged.set(Listen.leftover, 0); merged.set(bytes, Listen.leftover.length);
    bytes = merged; Listen.leftover = null;
  }
  const usable = bytes.length - (bytes.length % 2);
  if (usable < bytes.length) Listen.leftover = bytes.subarray(usable);
  if (usable < 2) return;
  // copy into an aligned buffer so Int16Array can view it (subarray may be odd-offset)
  const aligned = bytes.buffer.byteLength === bytes.length && (bytes.byteOffset % 2 === 0)
    ? bytes.subarray(0, usable)
    : bytes.slice(0, usable);
  const i16 = new Int16Array(aligned.buffer, aligned.byteOffset, usable / 2);
  const n = i16.length;
  if (!n) return;
  const f32 = new Float32Array(n);
  for (let i = 0; i < n; i++) f32[i] = i16[i] / 32768;
  const buf = ctx.createBuffer(1, n, Listen.rate);
  buf.getChannelData(0).set(f32);
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(Listen.gain);
  // schedule back-to-back; if we've fallen behind the clock, re-anchor with lead
  const now = ctx.currentTime;
  if (Listen.nextAt < now + 0.005) Listen.nextAt = now + PCM_PLAY_LEAD_S;
  src.start(Listen.nextAt);
  Listen.nextAt += buf.duration;
}

/* ---------- tune & inspect (analyzer + waterfall + audio) ---------- */
function blCenter(r) {
  return r.freq_low_mhz === r.freq_high_mhz ? r.freq_low_mhz
    : (r.freq_low_mhz + r.freq_high_mhz) / 2;
}

$("tbl-baseline").addEventListener("click", (e) => {
  const a = e.target.closest(".freqlink");
  if (!a) return;
  e.preventDefault();
  openInspect(parseInt(a.dataset.i, 10));
});

// .freqlink affordances on the non-baseline lists carry the center freq + label
// in data-* and tune via the freq-based entry point (no baseline stepping).
// One shared delegated handler covers Results (anomalies), detected Signals, and
// the Signal Wiki. The wiki row has a bubble-phase inline onclick that opens the
// detail editor; that <tr> sits BELOW this table in the tree, so it would fire
// first on bubble and stopPropagation here would be too late. We register in the
// CAPTURE phase instead — a freqlink click is caught at the table on the way down
// and stopPropagation pre-empts the row onclick (listen WITHOUT opening it). A
// non-freqlink click returns without stopping, so normal row clicks still work.
function freqLinkDelegate(e) {
  const a = e.target.closest(".freqlink");
  if (!a || a.dataset.center == null) return;
  e.preventDefault();
  e.stopPropagation();
  if (a.dataset.huntIdx != null) { huntStep(parseInt(a.dataset.huntIdx, 10)); return; }
  const sid = a.dataset.sid ? parseInt(a.dataset.sid, 10) : null;
  listenAt(parseFloat(a.dataset.center), a.dataset.label || "", a.dataset.mode || undefined, sid, a.dataset.attr || null);
}
["tbl-anomalies", "tbl-signals", "tbl-wiki", "tbl-hunt", "tbl-triage", "tbl-live-ledger",
 "seg-anomalies"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("click", freqLinkDelegate, true);  // capture: beat the row onclick
});

/* ---------- F8: ledger row → segment review / thumbnail lightbox / next step ----------
   Bubble-phase, so the capture-phase freqLinkDelegate above still owns clicks
   that land on the frequency link (tune & listen, unchanged). */
(function wireLedger() {
  const ledger = $("tbl-live-ledger");
  if (!ledger) return;
  ledger.addEventListener("click", (e) => {
    const thumb = e.target.closest(".sig-thumb");
    if (thumb) { openSigLightbox(parseInt(thumb.dataset.sid, 10)); return; }
    const chip = e.target.closest(".ns-chip");
    if (chip) { nextStepAction(parseInt(chip.dataset.sid, 10)); return; }
    const tr = e.target.closest("tr[data-sid]");
    if (tr) openSegmentReview(parseInt(tr.dataset.sid, 10));
  });
  // hovering a row highlights that signal vertically on whichever segment the
  // chart is showing (live or under review); leaving the table clears it
  ledger.addEventListener("mouseover", (e) => {
    const tr = e.target.closest("tr[data-sid]");
    if (!tr || tr === App._hoverRow) return;
    App._hoverRow = tr;
    const c = parseFloat(tr.dataset.center);
    const half = Math.max((parseFloat(tr.dataset.bw) || 50) / 2000, 0.01);
    App.hoverHL = { lo: c - half, hi: c + half };
    redrawSweepChart();
  });
  ledger.addEventListener("mouseleave", () => {
    App._hoverRow = null;
    App.hoverHL = null;
    redrawSweepChart();
  });
})();

// redraw the sweep chart NOW (hover feedback can't wait for the 1 Hz poll)
function redrawSweepChart() {
  if (App.review) { drawSegmentReview(); return; }
  const t = App._lastTrace;
  if (t && $("sweep-overlay").classList.contains("show"))
    drawTrace($("livechart"), t, muteRangesFor(), App.hoverHL);
}

/* ---------- F9: read & mark ANY frequency straight off the display ----------
   The detector's threshold is not the operator's eye. A signal that is plainly
   VISIBLE on the chart or waterfall can sit under the detection threshold, or
   inside a dampened "expected" range, and never reach the ledger — so the tech
   has no way to say "that one, right there". These handlers give them one:
   hover reads the frequency under the cursor, click runs the SAME fused ANALYZE
   the ledger rows use, and ADD TO REVIEW puts it in the queue marked as
   operator-marked (never as an instrument detection). */
const XHair = { mhz: null, dbm: null, raf: 0 };

// x pixel -> {mhz,dbm}. Uses drawTrace's exact i/(n-1) bin-CENTER mapping (the
// same one pushWaterfall matches), so the readout agrees with the peak the
// operator is actually pointing at instead of sitting a half-bin off.
function traceAtX(cv, clientX) {
  const t = App._lastTrace;
  if (!t) return null;
  const arr = t.amps || t.maxpeak || t.avg || [];
  const n = arr.length;
  if (!n) return null;
  const r = cv.getBoundingClientRect();
  if (!r.width) return null;
  const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  const i = Math.round(frac * (n - 1));
  const freqs = t.freqs || [];
  const mhz = (freqs.length === n) ? freqs[i]
    : ((t.low != null && t.high != null) ? t.low + frac * (t.high - t.low) : null);
  if (mhz == null) return null;
  return { mhz, dbm: (arr[i] == null ? null : arr[i]) };
}

function xhairMove(ev, cv) {
  const p = traceAtX(cv, ev.clientX);
  if (!p) { xhairClear(); return; }
  XHair.mhz = p.mhz; XHair.dbm = p.dbm;
  const el = $("xhair-readout");
  el.innerHTML = `<b>${esc(fmtF(p.mhz))}</b>` +
    (p.dbm == null ? "" : ` <span class="xr-amp">${p.dbm.toFixed(1)} dBm</span>`) +
    `<br><span class="xr-hint">click to inspect this frequency</span>`;
  el.classList.add("show");
  // keep the box beside the cursor but always fully on screen
  const pad = 14, w = el.offsetWidth, h = el.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > window.innerWidth - 4) x = ev.clientX - pad - w;
  if (y + h > window.innerHeight - 4) y = ev.clientY - pad - h;
  el.style.left = Math.max(4, x) + "px";
  el.style.top = Math.max(4, y) + "px";
  // thin vertical marker on the chart — reuses the ledger-hover highlight so
  // drawTrace needs no new parameter
  const t = App._lastTrace;
  const span = (t && t.high != null && t.low != null) ? (t.high - t.low) : 0;
  const half = Math.max(span * 0.002, 0.004);
  App.hoverHL = { lo: p.mhz - half, hi: p.mhz + half };
  // one redraw per animation frame — mousemove fires far faster than the chart
  // can usefully repaint, and drawTrace redraws the whole canvas
  if (!XHair.raf) XHair.raf = requestAnimationFrame(() => {
    XHair.raf = 0; redrawSweepChart();
  });
}

function xhairClear() {
  const el = $("xhair-readout");
  if (el) el.classList.remove("show");
  XHair.mhz = null;
  if (!App._hoverRow) { App.hoverHL = null; redrawSweepChart(); }
}

["livechart", "waterfall"].forEach(id => {
  const cv = $(id);
  if (!cv) return;
  cv.addEventListener("mousemove", (e) => xhairMove(e, cv));
  cv.addEventListener("mouseleave", xhairClear);
  cv.addEventListener("click", (e) => {
    const p = traceAtX(cv, e.clientX);
    if (p) openMarkModal(p.mhz, p.dbm);
  });
});

const Mark = { mhz: null, dbm: null, sid: null };

function openMarkModal(mhz, dbm) {
  Mark.mhz = mhz; Mark.dbm = dbm; Mark.sid = null;
  $("mm-title").textContent = "Inspect " + fmtF(mhz);
  $("mm-sub").textContent = (dbm == null ? "" : dbm.toFixed(1) + " dBm on the display");
  $("mm-verdict").innerHTML = "";
  $("mm-grid").innerHTML = "";
  $("mm-cands").innerHTML = "";
  $("mm-msg").textContent = "";
  const add = $("mm-add");
  add.disabled = false; add.textContent = "＋ ADD TO REVIEW";
  $("mark-modal").classList.add("show");
  xhairClear();
  runMarkAnalyze();
}
function closeMarkModal() { $("mark-modal").classList.remove("show"); }

/* ---------- WINDOW 4: the truth panel + the per-sweep scorecard ----------
   Rule (operator's): the panel REVEALS THE CONTENT of the signal, or the signal
   is a FAIL. No middle. Every analyze resolves to WIN/FAIL from j.verdict (see
   survey/verdict.py). The panel never decides whether a signal is "relevant" —
   that is the operator's call — it only renders what came out and scores whether
   content was revealed. Each FAIL carries its cause: code (on us) vs physics
   (encryption / out-of-band / too-wide — unfixable by any software) vs no-signal. */
function t4SweepKey() {
  return (App._activeSweep && App._activeSweep.sweep_id)
    || App._lastFinishedSweepId || "session";
}
function renderTruthPanel(j) {
  const el = $("mm-truth");
  if (!el) return;
  const v = j && j.verdict;
  if (!v) { el.className = ""; el.innerHTML = ""; return; }
  if (v.verdict === "NA") {          // no capture attempted — not scored
    el.className = "";
    el.innerHTML = `<div class="t4-head"><span class="t4-cause">NOT SCORED</span>`
      + `<span>${esc(v.reason || "no capture")}</span></div>`;
    return;
  }
  const win = v.verdict === "WIN";
  el.className = win ? "win" : "fail";
  const causeLabel = { code: "CODE · on us", physics: "PHYSICS · unfixable",
                       "no-signal": "NO SIGNAL" }[v.cause] || "";
  let body = "";
  if (win) {
    const kind = v.content_kind;
    if (kind === "image" && j.decode && j.decode.frame_png) {
      body = `<div class="t4-reason">Recovered still (one field, greyscale — confirms a video raster):</div>`
           + `<img alt="recovered video still" src="${j.decode.frame_png}">`;
    } else if (kind === "audio" && j.audio) {
      body = `<div class="t4-reason">Demodulated audio — listen to judge the content:</div>`
           + `<audio controls preload="none" src="${esc(j.audio)}"></audio>`;
    } else if (kind === "text" || kind === "data") {
      const msgs = (j.decode && j.decode.messages) || [];
      const txt = msgs.map(m => (m && (m.text != null ? m.text : JSON.stringify(m))))
                      .join("\n") || "(decoded)";
      body = `<div class="t4-reason">Decoded ${esc(kind)} — the payload itself:</div>`
           + `<pre class="t4-text">${esc(txt)}</pre>`;
    } else {
      body = `<div class="t4-reason">Content revealed.</div>`;
    }
  } else {
    body = `<div class="t4-reason">${esc(v.reason || "no content revealed")}</div>`;
  }
  el.innerHTML =
    `<div class="t4-head"><span class="t4-badge">${win ? "WIN" : "FAIL"}</span>`
    + `<span>${win ? "content revealed — " + esc(v.content_kind || "")
                    : "content NOT revealed"}</span>`
    + (causeLabel ? `<span class="t4-cause">${esc(causeLabel)}</span>` : "")
    + `</div><div class="t4-body">${body}</div>`;
}
function updateScorecard(verdict) {
  if (!verdict) return;
  if (verdict.verdict !== "WIN" && verdict.verdict !== "FAIL") return;  // skip NA
  const key = t4SweepKey();
  if (!App._scorecard || App._scorecard.key !== key)
    App._scorecard = { key, list: [] };
  App._scorecard.list.push(verdict);
  const L = App._scorecard.list, total = L.length;
  const wins = L.filter(v => v.verdict === "WIN").length;
  const fails = total - wins;
  const byCause = c => L.filter(v => v.verdict === "FAIL" && v.cause === c).length;
  const pct = total ? Math.round(1000 * wins / total) / 10 : 0;
  const el = $("mm-score");
  if (!el) return;
  const sweepLbl = (typeof key === "number") ? ("sweep #" + key) : "this session";
  el.innerHTML =
    `<span>Did the job (${esc(sweepLbl)}): <b>${pct}%</b></span>`
    + `<span class="sc-seg">wins ${wins} / ${total}</span>`
    + `<span class="sc-seg">fails — code ${byCause("code")} · `
    + `physics ${byCause("physics")} · empty ${byCause("no-signal")}</span>`;
}

async function runMarkAnalyze() {
  const stat = $("mm-status");
  stat.textContent = "analyzing " + fmtF(Mark.mhz) + " …";
  let j;
  try { j = await api("/api/analyze/signal", { center_mhz: Mark.mhz, label: "" }); }
  catch (e) { stat.textContent = "analyze failed: " + (e.message || e); return; }
  App.lastAnalyze = j;
  stat.textContent = j.note || "";
  renderTruthPanel(j);          // WINDOW 4: reveal the content, or a labelled FAIL
  updateScorecard(j.verdict);   // per-sweep did-the-job %
  const interp = j.interpretation || {};
  const verdict = interp.readable || interp.summary || "";
  $("mm-verdict").innerHTML = verdict
    ? esc(verdict)
    : "<span class='note'>no verdict yet — see what could occupy this band below</span>";
  const f = j.features || {};
  const rows = [
    ["Frequency", fmtF(Mark.mhz)],
    ["Anything there?", j.signal_present
      ? "yes — above the noise floor"
      : "no clear signal above the noise floor right now"],
    ["Signal strength over noise", f.snr_db == null ? "—" : f.snr_db + " dB"],
    ["Protocol decoded", (j.decode && j.decode.protocol) || "none"],
    ["Radio used", j.source || "—"],
  ];
  $("mm-grid").innerHTML = rows.map(([k, v]) =>
    `<div>${esc(k)}</div><div>${esc(String(v))}</div>`).join("");
  const cands = (j.candidates || []).slice(0, 6);
  $("mm-cands").innerHTML = cands.map(c =>
    `<div class="mm-cand"><span>${esc(c.name || c.label || "?")}</span>` +
    `<span class="note">${c.score == null ? "" : esc(String(c.score))}</span></div>`).join("");
}

if ($("mm-close")) $("mm-close").addEventListener("click", closeMarkModal);
if ($("mark-modal")) $("mark-modal").addEventListener("click", (e) => {
  if (e.target.id === "mark-modal") closeMarkModal();       // backdrop click
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("mark-modal") &&
      $("mark-modal").classList.contains("show")) closeMarkModal();
});
if ($("mm-reanalyze")) $("mm-reanalyze").addEventListener("click", runMarkAnalyze);
if ($("mm-listen")) $("mm-listen").addEventListener("click", () => {
  if (Mark.mhz != null) listenAt(Mark.mhz, fmtF(Mark.mhz), undefined, Mark.sid, null);
});
if ($("mm-add")) $("mm-add").addEventListener("click", async () => {
  const btn = $("mm-add");
  if (Mark.mhz == null) return;
  btn.disabled = true;
  try {
    const r = await api("/api/signals/manual", {
      center_mhz: Mark.mhz,
      peak_dbm: Mark.dbm,
      note: "operator marked this off the live display",
    });
    Mark.sid = r.id;
    btn.textContent = "✓ ADDED TO REVIEW";
    $("mm-msg").textContent = "Added at " + fmtF(r.center_mhz) +
      " — it is in the review list as OPERATOR-MARKED, not as an instrument " +
      "detection, so the report can always tell the two apart.";
    ledgerLast = 0;                       // let the next poll pick it up now
  } catch (e) {
    btn.disabled = false;
    $("mm-msg").textContent = "could not add: " + (e.message || e);
  }
});

/* -- historic segment review: click a ledger row, see the segment it was
      found in — the stored max hold + average (the realtime trace is never
      persisted). View-only; the radio and the sweep are untouched. -- */
async function openSegmentReview(sid) {
  const s = (App.liveSweepSignals || []).find(x => x.id === sid);
  if (!s || !s.segment_id) return;
  try {
    let seg = App._segCache[s.segment_id];
    if (!seg) {
      seg = await api("/api/segments/" + s.segment_id);
      App._segCache[s.segment_id] = seg;
    }
    if (!seg.data || !seg.data.freqs || seg.data.freqs.length < 2) return;
    // version-skew guard: if the page HTML predates this app.js, the review
    // banner doesn't exist — tell the operator instead of half-working
    if (!$("review-banner") || !$("review-banner-text")) {
      if (!App._staleWarned) {
        App._staleWarned = true;
        alert("The app was updated but this page didn't fully reload.\n" +
              "Press Ctrl+F5 to hard-refresh, then try again.");
      }
      return;
    }
    App.review = { segId: s.segment_id, sid, seg,
      trace: { freqs: seg.data.freqs, avg: seg.data.avg, maxpeak: seg.data.maxpeak,
               low: seg.low_mhz, high: seg.high_mhz } };
    App._lastLabelKey = "";        // labels must rebuild for the review bounds
    const nSig = (seg.signals || []).length;
    $("review-banner-text").textContent =
      `REVIEWING SEGMENT ${seg.low_mhz.toFixed(3)}–${seg.high_mhz.toFixed(3)} MHz — ` +
      `max hold + average as recorded (${nSig} signal${nSig === 1 ? "" : "s"}) — ` +
      "sweep continues in the background";
    $("review-banner").style.display = "flex";
    const wf = $("waterfall");
    if (wf) wf.style.opacity = ".25";   // the waterfall is live-only — dim it
    drawSegmentReview();
  } catch (e) { $("ft-msg").textContent = "segment review failed: " + e.message; }
}

function drawSegmentReview() {
  if (!App.review) return;
  const t = App.review.trace;
  const canvas = $("livechart");
  drawTrace(canvas, t, null, App.hoverHL);
  // unmistakable on-canvas mode tag, independent of the banner
  const ctx = canvas.getContext("2d");
  ctx.font = "bold 12px monospace";
  ctx.fillStyle = "#f39c12";
  ctx.fillText("◂ SEGMENT REVIEW — recorded max hold + average", 4, 26);
  // DB-identified labels only — livePeaksFromTrace's "Detecting…" chips are a
  // live-sweep affordance, noise on a finished segment
  buildSweepLabelOverlays(canvas, t,
    sweepChartSignals(t).filter(x => !x.live));
}

function exitSegmentReview() {
  if (!App.review) return;
  App.review = null;
  App._lastLabelKey = "";
  const b = $("review-banner");
  if (b) b.style.display = "none";
  const wf = $("waterfall");
  if (wf) wf.style.opacity = "";
  redrawSweepChart();
}
if ($("btn-back-live")) $("btn-back-live").onclick = exitSegmentReview;

/* -- next-step chip: the one obvious action for this row -- */
function nextStepAction(sid) {
  const s = (App.liveSweepSignals || []).find(x => x.id === sid);
  if (!s || !s.next_step) return;
  const ns = s.next_step;
  if (ns.kind === "listen") {
    listenAt(s.center_mhz, s.display_label || s.matched_label || "", ns.mode,
             s.id, s.attribution_status || null);
  } else if (ns.kind === "clear" && !document.body.classList.contains("demo")) {
    sigTriage(sid, "cleared");     // cross it off in one click
  } else {
    openSigLightbox(sid);          // test/inventory/urgent/review/done → details
  }
}

async function sigTriage(sid, status) {
  try {
    await api(`/api/signals/${sid}/triage`, { status });
    const s = (App.liveSweepSignals || []).find(x => x.id === sid);
    if (s) s.triage_status = status;
    App._ledgerKey = "";           // repaint the ledger with the new state
    renderLiveLedger();
    closeSigLightbox();
  } catch (e) {
    const m = $("sl-msg");
    if (m && $("sig-lightbox").classList.contains("show"))
      m.textContent = "triage failed: " + e.message;
    else $("ft-msg").textContent = "triage failed: " + e.message;
  }
}

/* -- lightbox nav: the ordered id list matches what the ledger shows, so the
   ◂ ▸ arrows step through signals in the same order the operator sees them -- */
function lightboxOrder() {
  return (App.liveSweepSignals || []).filter(x => {
    if (isMutedSignal(x)) return false;
    if (App.focusAnomalies) { const t = signalTier(x); return t === "ANOMALY" || x.caution; }
    return true;
  }).slice().reverse().map(x => x.id);
}
function updateLightboxNav() {
  const ids = App._lightboxIds || [];
  const i = App._lightboxIdx;
  const pos = $("sl-nav-pos");
  if (pos) pos.textContent = ids.length ? `${i + 1} / ${ids.length}` : "";
  const has = ids.length > 1;
  if ($("sl-nav-prev")) $("sl-nav-prev").style.visibility = has ? "" : "hidden";
  if ($("sl-nav-next")) $("sl-nav-next").style.visibility = has ? "" : "hidden";
}
function lightboxStep(delta) {
  const ids = App._lightboxIds || [];
  if (ids.length < 2) return;
  App._lightboxIdx = (App._lightboxIdx + delta + ids.length) % ids.length;
  openSigLightbox(ids[App._lightboxIdx]);   // keeps the same list; re-indexes below
}

/* -- lightbox: the expanded signal picture + everything known about it -- */
async function openSigLightbox(sid) {
  let s = (App.liveSweepSignals || []).find(x => x.id === sid);
  // Results / triage tables may open a signal that isn't in the live ledger —
  // pull the enriched row (behavior + inventory) on demand.
  if (!s) {
    try {
      const all = await api("/api/signals");
      s = (all || []).find(x => x.id === sid);
      if (s) {
        App.liveSweepSignals = App.liveSweepSignals || [];
        if (!App.liveSweepSignals.some(x => x.id === sid))
          App.liveSweepSignals.push(s);
      }
    } catch (_) { /* fall through */ }
  }
  if (!s) return;
  // (re)build the nav list; keep the operator's position on the clicked signal
  App._lightboxIds = lightboxOrder();
  App._lightboxIdx = Math.max(0, App._lightboxIds.indexOf(sid));
  updateLightboxNav();
  const cls = s.identity_tier || (s.classification === "VERIFIED" ? "IDENTIFIED"
    : s.classification === "PLAUSIBLE" ? "KNOWN_BAND" : "ANOMALY");
  $("sl-title").textContent = fmtF(s.center_mhz) +
    ((s.display_label || s.matched_label) ? " — " + (s.display_label || s.matched_label) : "");
  $("sl-pills").innerHTML =
    `<span class="pill ${cls}">${esc(statusLabel(signalTier(s)))}</span>` +
    (s.caution ? " <span class='caution'>⚠ caution band</span>" : "") +
    (s.is_novel_to_address ? " <span class='caution'>★ new to this address</span>" : "");
  $("sl-img").src = `/api/signals/${sid}/image.svg`;
  const kv = [
    ["Peak", s.peak_dbm + " dBm"],
    ["SNR", s.snr_db != null ? s.snr_db + " dB" : "—"],
    ["Width", s.bw_khz != null ? Math.round(s.bw_khz) + " kHz" : "—"],
    ["Persistence", s.persistence != null ? Math.round(s.persistence * 100) + "% of passes" : "—"],
    ["Room / module", `${s.room || "—"} / ${s.module || "—"}`],
    ["Classification", (s.classification || "—") + (s.matched_label ? " — " + s.matched_label : "")],
  ];
  if (s.match_reason) kv.push(["Match reason", s.match_reason]);
  if (s.anomaly_score) kv.push(["Anomaly score",
    Math.round(s.anomaly_score) + "/100" + (s.score_reason ? " — " + s.score_reason : "")]);
  if (s.interior_delta_db != null) kv.push(["Interior vs ambient",
    (s.interior_delta_db >= 0 ? "+" : "") + Math.round(s.interior_delta_db) + " dB" +
    (s.interior_delta_db >= 10 ? " — louder inside (originates in the space)" : "")]);
  if (s.localization_score != null || s.strongest_room)
    kv.push(["Localization", (s.strongest_room ? "strongest in " + s.strongest_room : "") +
      (s.localization_score != null ? ` (${Math.round(s.localization_score * 100)}%)` : "")]);
  if (s.attribution_status && s.attribution_status !== "unassessed")
    kv.push(["Attribution", s.attribution_status.replace("_", " ")]);
  if (s.cue) kv.push(["Client relevance", s.cue.summary]);
  if (s.novelty_reason) kv.push(["Novelty", s.novelty_reason]);
  if (s.behavior && s.behavior.label) {
    const conf = s.behavior.confidence != null
      ? ` (${Math.round(s.behavior.confidence * 100)}%)` : "";
    kv.push(["Behavior class", s.behavior.label + conf]);
    if (s.behavior.phrase) kv.push(["What that means", s.behavior.phrase]);
    if (s.behavior.evidence && s.behavior.evidence.length)
      kv.push(["Evidence", s.behavior.evidence.join("; ")]);
    if (s.behavior.could_also_be && s.behavior.could_also_be.length)
      kv.push(["Could also be", s.behavior.could_also_be.join("; ")]);
  }
  if (s.inventory_matches && s.inventory_matches.length) {
    kv.push(["Inventory match",
      s.inventory_matches.slice(0, 4).map(m => m.phrase || m.label).join(" | ")]);
  } else if (s.inventory_summary) {
    kv.push(["Inventory match", s.inventory_summary]);
  }
  if (s.local_summary) kv.push(["Your signature library", s.local_summary]);
  if (s.experiment_summary) kv.push(["Field test", s.experiment_summary]);
  if (s.id_top_name) kv.push(["Signature ID",
    s.id_top_name + (s.id_device_class ? ` (${s.id_device_class})` : "") +
    (s.id_mode === "band" ? " — frequency-only match" : "")]);
  if (s.analysis_summary) kv.push(["Analyze verdict",
    s.analysis_summary + (s.analysis_protocol ? ` [${s.analysis_protocol}]` : "")]);
  if (s.triage_status && s.triage_status !== "unreviewed")
    kv.push(["Triage", s.triage_status + (s.triage_notes ? " — " + s.triage_notes : "")]);
  $("sl-grid").innerHTML = kv.map(([k, v]) =>
    `<div>${esc(k)}</div><div>${esc(String(v))}</div>`).join("");
  $("sl-compare").style.display = "none";      // collapse any prior comparison
  renderRundown(s);                            // the "how we know" checklist
  const ns = s.next_step;
  $("sl-next").innerHTML = ns
    ? `<span class="ns-chip ${esc(ns.kind)}">${esc(ns.action)}</span><span>${esc(ns.detail)}</span>`
    : "";
  const demo = document.body.classList.contains("demo");
  const listenable = s.cue && s.cue.listenable;
  const admin = !!(App.state && App.state.admin);
  $("sl-actions").innerHTML =
    `<button class="btn primary" id="sl-listen">🎧 LISTEN</button>` +
    // admin/authorized: hear it to confirm the app's blinded call was right —
    // logged to the audit trail. Only shown in admin mode.
    (admin ? `<button class="btn" id="sl-verify" title="Admin: listen to confirm the blinded call — logged">▶ LISTEN TO VERIFY (admin)</button>` : "") +
    `<button class="btn" id="sl-directional">📡 DIRECTIONAL (RX-A)</button>` +
    `<button class="btn" id="sl-identify">IDENTIFY SIGNATURE</button>` +
    `<button class="btn" id="sl-scan-inv" title="Scan Wi-Fi + BLE and match this hit">📡 SCAN INV</button>` +
    `<button class="btn" id="sl-save-sig" title="Save de-identified shape to your library">💾 SAVE SIG</button>` +
    (demo ? "" :
      `<button class="btn" id="sl-clear">✓ CLEAR</button>` +
      `<button class="btn danger" id="sl-flag">⚑ FLAG</button>` +
      `<button class="btn" id="sl-ask">? ASK CLIENT</button>`);
  // content is ECPA-gated: listening is only clean when attributed to the
  // client's space — otherwise it needs the audited override (already in the app)
  $("sl-msg").textContent = (listenable && s.cue && !s.cue.listen_permitted)
    ? "Listening to content requires attributing this to the client's space (or an audited ECPA override) — the blinded cue above needs no such privilege."
    : "";
  $("sl-listen").onclick = () => {
    closeSigLightbox();
    listenAt(s.center_mhz, s.display_label || s.matched_label || "",
             (ns && ns.mode) || undefined, sid, s.attribution_status || null);
  };
  // admin verify-by-listening: audit the authorized listen, then hear it. This
  // is the operator confirming the blinded call against a KNOWN test signal —
  // logged as an ECPA override so every content-listen is accountable.
  if ($("sl-verify")) $("sl-verify").onclick = async () => {
    try {
      await api("/api/decode/override", {
        ecpa_ack: true, signal_id: sid, center_mhz: s.center_mhz,
        mode: (ns && ns.mode) || "nfm",
        reason: "admin verification of the blinded call (known test signal)" });
    } catch (e) { /* audit is best-effort; still allow the authorized listen */ }
    closeSigLightbox();
    listenAt(s.center_mhz, s.display_label || s.matched_label || "",
             (ns && ns.mode) || undefined, sid, s.attribution_status || null);
  };
  // switch to the directional strength meter on RX-A (receive-only) to walk to it
  $("sl-directional").onclick = async () => {
    try {
      await api("/api/directional/start", {
        center_mhz: s.center_mhz, bw_khz: s.bw_khz || 100,
        room: s.strongest_room || s.room || "" });
      closeSigLightbox();
      show("directional");
    } catch (e) { $("sl-msg").textContent = "directional failed: " + e.message; }
  };
  $("sl-identify").onclick = async () => {
    const b = $("sl-identify");
    b.disabled = true; $("sl-msg").textContent = "identifying — capturing signature…";
    try {
      const r = await api("/api/identify", { signal_id: sid });
      const top = (r.candidates || [])[0];
      let msg = top
        ? `best match: ${top.name}${top.device_class ? " (" + top.device_class + ")" : ""}` +
          (r.id_mode === "band" ? " — frequency-only (connect the SDR monitor for a live match)" : "")
        : "no signature match" + (r.note ? " — " + r.note : "");
      if (r.behavior && r.behavior.label)
        msg += ` · behavior: ${r.behavior.label}`;
      if (r.inventory_summary)
        msg += ` · ${r.inventory_summary}`;
      $("sl-msg").textContent = msg;
      if (top) {
        s.id_top_name = top.name; s.id_top_score = top.score;
        s.id_device_class = top.device_class || ""; s.id_mode = r.id_mode || s.id_mode;
      }
      if (r.behavior) s.behavior = r.behavior;
      if (r.inventory_matches) {
        s.inventory_matches = r.inventory_matches;
        s.inventory_summary = r.inventory_summary || "";
      }
      s.id_candidates_json = JSON.stringify(r.candidates || []);
      s._features = r.features || null;        // measured modulation, for the side-by-side
      App._ledgerKey = "";
      renderLiveLedger();
      // re-paint the detail grid so behavior / inventory rows appear
      const keep = msg;
      openSigLightbox(sid);
      $("sl-msg").textContent = keep;
    } catch (e) { $("sl-msg").textContent = "identify failed: " + e.message; }
    b.disabled = false;
  };
  if ($("sl-scan-inv")) $("sl-scan-inv").onclick = async () => {
    const b = $("sl-scan-inv");
    b.disabled = true;
    $("sl-msg").textContent = "scanning Wi-Fi + BLE for this hit…";
    try {
      const r = await api("/api/inventory/scan_for_signal", { signal_id: sid });
      if (r.signal) {
        Object.assign(s, r.signal);
        App._ledgerKey = "";
        renderLiveLedger();
        openSigLightbox(sid);
      }
      $("sl-msg").textContent =
        (r.messages || []).join(" · ") +
        (r.signal && r.signal.inventory_summary
          ? " → " + r.signal.inventory_summary
          : " → no channel/BLE match yet");
    } catch (e) { $("sl-msg").textContent = "inventory scan failed: " + e.message; }
    b.disabled = false;
  };
  if ($("sl-save-sig")) $("sl-save-sig").onclick = async () => {
    const label = prompt(
      "Label this signature for your local library (no client names):\n" +
      "e.g. “lobby badge reader” / “front door fob class”",
      (s.behavior && s.behavior.label) || s.matched_label || "");
    if (!label) return;
    try {
      const r = await api("/api/siglib/save", {
        signal_id: sid, label,
        notes: (s.behavior && s.behavior.phrase) || "",
        site_type: "" });
      $("sl-msg").textContent = r.saved
        ? `saved “${r.ref.label}” to your signature library`
        : "save failed";
    } catch (e) { $("sl-msg").textContent = "save failed: " + e.message; }
  };
  // quick field-test shortcuts on behavior class
  const bclass = (s.behavior && s.behavior.class_id) || "";
  const expKind =
    bclass === "cellular_uplink" ? "phone_test" :
    bclass === "video_like" || bclass === "wifi_data" || bclass === "hid_keepalive"
      ? "inventory_subtract" :
    bclass === "dect_cordless" ? "dect_unplug" :
    bclass === "keyfob_like" ? "keyfob_press" : "";
  if (expKind && !demo) {
    const expBtn = document.createElement("button");
    expBtn.className = "btn";
    expBtn.id = "sl-exp";
    expBtn.textContent = "🧪 FIELD TEST";
    expBtn.title = "Record a guided field experiment outcome for this hit";
    $("sl-actions").appendChild(expBtn);
    expBtn.onclick = async () => {
      try {
        const g = await api("/api/experiments/guide");
        const guide = (g.guides && g.guides[expKind]) || { title: expKind, steps: [] };
        const steps = (guide.steps || []).map((t, i) => `${i + 1}. ${t}`).join("\n");
        const outcome = prompt(
          `${guide.title}\n\n${steps}\n\nOutcome code:\n` +
          (expKind === "keyfob_press"
            ? "correlated | uncorrelated | inconclusive"
            : "energy_dropped | energy_remains | residue_gone | residue_remains | inconclusive"),
          expKind === "keyfob_press" ? "correlated" : "energy_dropped");
        if (!outcome) return;
        const notes = prompt("Notes (optional)", "") || "";
        const label = expKind === "keyfob_press"
          ? (prompt("Which remote? (e.g. front door)", "") || "") : "";
        const r = await api("/api/experiments/record", {
          signal_id: sid, kind: expKind, outcome, notes, label });
        s.experiment_summary = r.summary;
        $("sl-msg").textContent = r.summary;
        openSigLightbox(sid);
        $("sl-msg").textContent = r.summary;
      } catch (e) { $("sl-msg").textContent = "experiment failed: " + e.message; }
    };
  }
  if (!demo) {
    $("sl-clear").onclick = () => sigTriage(sid, "cleared");
    $("sl-flag").onclick = () => sigTriage(sid, "flagged");
    $("sl-ask").onclick = () => sigTriage(sid, "ask_client");
  }
  $("sig-lightbox").classList.add("show");
}

/* -- verification rundown: the ordered "how we know what this is" checklist,
   cheapest checks first, each showing its result or a one-click way to run it.
   Signature-ID candidates are shown as scored probabilities you can click for a
   measured-vs-expected side-by-side. Re-rendered as analysis refines. -- */
function buildRundown(s) {
  const pct = v => Math.round((v || 0) * 100);
  const items = [];
  // 1 — band / type (instant)
  const hint = s.id_device_class || s.device_hint
    || (s.behavior && s.behavior.label) || "";
  items.push({ state: "done", title: "Frequency band",
    detail: hint ? `In band — reads as ${hint}` : `At ${fmtF(s.center_mhz)}` });
  // 1b — CLIENT RELEVANCE (blinded cue) — content-free: is it in the client's
  // space + does it carry audio, without anyone listening to content
  if (s.cue) {
    const c = s.cue;
    const st2 = (c.space === "client" || c.space === "likely_client") ? "pending"
      : (c.space === "not_client" || c.space === "likely_not") ? "done" : "na";
    items.push({ state: st2, title: (c.listenable ? "🎧 " : "") + "Client relevance (blinded)",
      detail: c.summary });
  }
  // 2 — baseline correlation (instant)
  if (s.classification === "VERIFIED" || s.matched_label)
    items.push({ state: "done", title: "Baseline match",
      detail: `${s.matched_label || "matched"} (${s.classification})` +
        (s.match_reason ? ` — ${s.match_reason}` : "") });
  else
    items.push({ state: "na", title: "Baseline match",
      detail: "no licensed/known transmitter here — treated as an anomaly" });
  // 3 — signal shape (instant)
  items.push({ state: "done", title: "Signal shape",
    detail: `${s.bw_khz != null ? Math.round(s.bw_khz) + " kHz wide" : "width —"} · ` +
      `${s.persistence != null ? pct(s.persistence) + "% duty" : "duty —"} · ` +
      `${s.snr_db != null ? s.snr_db + " dB SNR" : "SNR —"}` });
  // 4 — anomaly score (post-sweep)
  if (s.anomaly_score != null && s.anomaly_score !== 0)
    items.push({ state: "done", title: `Anomaly score ${Math.round(s.anomaly_score)}/100`,
      detail: s.score_reason || "" });
  // 5 — novelty vs this address
  if (s.is_novel_to_address || s.novelty_reason)
    items.push({ state: "done", title: s.is_novel_to_address ? "New to this address" : "Seen before here",
      detail: s.novelty_reason || "" });
  // 6 — signature ID (on demand)
  let cands = [];
  try { cands = JSON.parse(s.id_candidates_json || "[]"); } catch (e) { cands = []; }
  if (s.id_top_name || cands.length)
    items.push({ state: "done", title: "Signature ID"
        + (s.id_mode === "band" ? " (frequency-only)" : s.id_mode === "iq" ? " (live IQ)" : ""),
      detail: "probable device class — click a match for the side-by-side:",
      cands });
  else
    items.push({ state: "pending", title: "Signature ID", run: "IDENTIFY",
      detail: "match the fingerprint to a device signature (runs a capture)" });
  // 7 — demod / analyze verdict (on demand)
  if (s.analysis_summary)
    items.push({ state: "done", title: "Demod / analyze",
      detail: s.analysis_summary + (s.analysis_protocol ? ` [${s.analysis_protocol}]` : "") });
  else
    items.push({ state: "pending", title: "Demod / analyze", run: "LISTEN",
      detail: "listen / decode to confirm what it actually is" });
  return items;
}

function renderRundown(s) {
  const el = $("sl-rundown");
  if (!el) return;
  const items = buildRundown(s);
  const ic = st => st === "done" ? "✓" : st === "pending" ? "○" : "—";
  el.innerHTML = items.map((it, i) => {
    let cands = "";
    if (it.cands && it.cands.length) {
      const tot = it.cands.reduce((a, c) => a + (c.score || 0), 0) || 1;
      cands = `<div class="rd-cands">` + it.cands.slice(0, 5).map((c, j) =>
        `<span class="rd-cand ${j === 0 ? "top" : ""}" data-ci="${j}">` +
        `${Math.round((c.score || 0) / tot * 100)}% ${esc(c.device_class || c.name || "?")}</span>`
      ).join("") + `</div>`;
    }
    const run = it.run
      ? `<button class="btn rd-run" data-run="${esc(it.run)}">${esc(it.run)}</button>` : "";
    return `<div class="rd-item ${it.state}" data-i="${i}">` +
      `<span class="rd-ic">${ic(it.state)}</span>` +
      `<span class="rd-body"><span class="rd-title">${esc(it.title)}</span>` +
      (it.detail ? ` <span class="rd-detail">— ${esc(it.detail)}</span>` : "") +
      cands + `</span>${run}</div>`;
  }).join("");
  // wire candidate clicks → side-by-side, and pending-run buttons
  el.querySelectorAll(".rd-cand").forEach(chip => {
    chip.onclick = () => {
      const item = items.find(it => it.cands);
      showCompare(s, item.cands[parseInt(chip.dataset.ci, 10)]);
    };
  });
  el.querySelectorAll(".rd-run").forEach(btn => {
    btn.onclick = () => {
      const which = btn.dataset.run;
      if (which === "IDENTIFY" && $("sl-identify")) $("sl-identify").click();
      else if (which === "LISTEN" && $("sl-listen")) $("sl-listen").click();
    };
  });
}

// side-by-side: the MEASURED signal vs the candidate's EXPECTED signature
function showCompare(s, cand) {
  if (!cand) return;
  const el = $("sl-compare");
  const f = s._features || {};
  const measBw = s.bw_khz != null ? Math.round(s.bw_khz) : (f.occupied_bw_khz || null);
  const rows = (pairs) => pairs.map(([k, a, b, ok]) =>
    `<div>${esc(k)}</div><div class="${ok === true ? "cmp-match" : ok === false ? "cmp-miss" : ""}">` +
    `${esc(a)}</div>`).join("");
  const measured = [
    ["Center", fmtF(s.center_mhz)],
    ["Width", measBw != null ? measBw + " kHz" : "—"],
    ["Modulation", f.modulation_class || (s.id_mode === "band" ? "(not measured — band-only)" : "—")],
    ["Duty", s.persistence != null ? Math.round(s.persistence * 100) + "%" : "—"],
    ["SNR", s.snr_db != null ? s.snr_db + " dB" : "—"],
  ];
  const expBw = cand.bandwidth_khz != null ? cand.bandwidth_khz + " kHz" : "—";
  const bwOk = (measBw != null && cand.bandwidth_khz != null)
    ? (measBw <= cand.bandwidth_khz * 3 && measBw >= cand.bandwidth_khz / 3) : null;
  const expected = [
    ["Name", cand.name || "—"],
    ["Device class", cand.device_class || "—"],
    ["Expected width", expBw, null, bwOk],
    ["Signature class", cand.class || cand.mode || "—"],
    ["TSCM concern", cand.tscm ? "yes — surveillance-capable" : "—"],
  ];
  const steps = (cand.confirm_steps || []).slice(0, 4);
  el.innerHTML =
    `<h4>SIDE-BY-SIDE — measured signal vs ${esc(cand.name || "candidate")}` +
    ` (${Math.round((cand.score || 0) * 100) > 0 ? "" : ""}match)</h4>` +
    `<div class="cmp-cols">` +
    `<div class="cmp-col"><h5>Measured (this signal)</h5><div class="kv">` +
    measured.map(([k, v]) => `<div>${esc(k)}</div><div>${esc(String(v))}</div>`).join("") +
    `</div></div>` +
    `<div class="cmp-col"><h5>Expected (${esc(cand.device_class || cand.name || "reference")})</h5><div class="kv">` +
    expected.map(([k, v, , ok]) => `<div>${esc(k)}</div><div class="${ok === true ? "cmp-match" : ok === false ? "cmp-miss" : ""}">${esc(String(v))}</div>`).join("") +
    `</div></div></div>` +
    (steps.length ? `<div class="note" style="margin-top:8px"><b>Confirm it:</b> ${steps.map(esc).join(" · ")}</div>` : "") +
    (cand.url ? `<div class="note" style="margin-top:4px"><a href="${esc(cand.url)}" target="_blank" rel="noopener">reference signature ↗</a></div>` : "");
  el.style.display = "";
  el.scrollIntoView({ block: "nearest" });
}

function closeSigLightbox() { $("sig-lightbox").classList.remove("show"); }
if ($("sl-close")) $("sl-close").onclick = closeSigLightbox;
if ($("sl-nav-prev")) $("sl-nav-prev").onclick = () => lightboxStep(-1);
if ($("sl-nav-next")) $("sl-nav-next").onclick = () => lightboxStep(1);
if ($("sig-lightbox")) {
  $("sig-lightbox").addEventListener("click", (e) => {
    if (e.target.id === "sig-lightbox") closeSigLightbox();   // backdrop click
  });
  document.addEventListener("keydown", (e) => {
    if (!$("sig-lightbox").classList.contains("show")) return;
    if (e.key === "Escape") closeSigLightbox();
    else if (e.key === "ArrowLeft") lightboxStep(-1);
    else if (e.key === "ArrowRight") lightboxStep(1);
  });
}

function inRangeClient(center) {
  // dual mode: if an SDR (Pluto or B220) is connected as the monitor, listening
  // works even while the RF Explorer is the active sweeper — the server tunes
  // the SDR and enforces its own range check.
  const roles = App.state && App.state.roles;
  const hasSdrRole = !!(roles && roles.roles
    && roles.roles.some(r => SDR_SOURCES.has(r.source)));
  const dev = App.state && App.state.device;
  const devIsSdr = !!(dev && dev.modules && dev.modules.length
    && SDR_SOURCES.has(dev.driver));
  // RANGE IS CHECKED EVEN WHEN AN SDR IS PRESENT. This used to return ok:true
  // the moment any SDR held a role, skipping the check below entirely — so
  // clicking a baseline entry the radio physically cannot reach (e.g. 49.86 MHz
  // on a B220, which starts at 70 MHz) fired a request that came back 416 and
  // read as "the panel opened but there's no sound and no trace". The radio's
  // own limits are knowable here; say so BEFORE asking the server.
  if (devIsSdr) {
    const lo = Math.min(...dev.modules.map(m => m.min_mhz));
    const hi = Math.max(...dev.modules.map(m => m.max_mhz));
    if (center < lo || center > hi) {
      return { ok: false, msg: `${fmtF(center)} is outside this radio's `
        + `${fmtF(lo)}–${fmtF(hi)} tuning range — it physically cannot receive `
        + `it. (Baseline lists licensed transmitters across all bands, including `
        + `ones below the radio's floor.)` };
    }
    return { ok: true };
  }
  // an SDR holds a role but is not the reported device (e.g. RFE sweeps while a
  // Pluto monitors): its range is not knowable client-side, so let the server
  // enforce — the failure now surfaces on the LISTEN ladder either way
  if (hasSdrRole) return { ok: true };
  if (!dev || !dev.modules || !dev.modules.length)
    return { ok: false, msg: "connect a device first (Session tab)." };
  if (!SDR_SOURCES.has(dev.driver))
    return { ok: false, msg: "tune / view / listen needs an SDR (Pluto or B220 Mini) — connect one in the DEVICE card (it can stay connected alongside the RF Explorer)." };
  return { ok: true };
}

// Size a canvas backing store to its displayed CSS pixels x devicePixelRatio so
// text + waterfall are pin-sharp (the store was a fixed 660px stretched by CSS).
// Resizes ONLY when dims change — assigning cv.width CLEARS the canvas, which
// would wipe the visible trace every frame. Returns {ctx, W, H} in CSS pixels.
function fitCanvas(cv) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const r = cv.getBoundingClientRect();
  const cssW = Math.max(1, Math.round(r.width)), cssH = Math.max(1, Math.round(r.height));
  const needW = Math.round(cssW * dpr), needH = Math.round(cssH * dpr);
  if (cv.width !== needW || cv.height !== needH) { cv.width = needW; cv.height = needH; }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);    // all draw math stays in CSS px
  return { ctx, W: cssW, H: cssH };
}

function resetMonitorMetrics() {
  App._specSmooth = null;
  App._meterPeakHold = null;
  App._sigHistory = [];
  App._sigHistoryT0 = 0;
  ["sm-peak", "sm-mean", "sm-snr", "sm-floor"].forEach(id => { if ($(id)) $(id).textContent = "—"; });
  const dm = $("sm-demod"); if (dm) dm.textContent = "";
  const ic = $("insp-class"); if (ic) ic.style.display = "none";   // last stream's verdict is not this one's
  const fill = $("sm-bar-fill"); if (fill) fill.style.width = "0%";
  const dot = $("sm-peak-dot"); if (dot) dot.style.display = "none";
  const hc = $("insp-history");
  if (hc) { const { ctx, W, H } = fitCanvas(hc); ctx.clearRect(0, 0, W, H); }
}

function clearInspectCanvases() {
  App.wf = null; App.wfSeq = -1;             // reset the client waterfall history
  resetMonitorMetrics();
  for (const id of ["insp-analyzer", "insp-waterfall"]) {
    const { ctx, W, H } = fitCanvas($(id));
    ctx.clearRect(0, 0, W, H);
  }
}

// Demod filter half-width (MHz) for S-meter / peak-in-passband — SDR Console measures
// within the current receiver filter, not the whole span.
function meterFilterHalfMhz(mode) {
  const m = String(mode || "").toLowerCase();
  if (m === "wfm") return 0.1;
  if (m === "nfm" || m === "am") return 0.012;
  if (m === "usb" || m === "lsb") return 0.003;
  if (m === "cw") return 0.0008;
  return 0.018;
}

function computeMeterStats(analyzer, centerMhz, demodMhz, mode) {
  const p = analyzer.power_dbm, fr = analyzer.freqs_mhz, n = p.length;
  if (!n) return null;
  const tune = demodMhz != null ? demodMhz : centerMhz;
  const hw = meterFilterHalfMhz(mode);
  const inBand = [];
  for (let i = 0; i < n; i++) {
    if (Math.abs(fr[i] - tune) <= hw) inBand.push(p[i]);
  }
  const use = inBand.length ? inBand : p;
  const peak = Math.max(...use);
  const mean = use.reduce((a, b) => a + b, 0) / use.length;
  const sorted = [...p].sort((a, b) => a - b);
  const k = Math.max(1, Math.floor(n * 0.1));
  let nf = 0;
  for (let i = 0; i < k; i++) nf += sorted[i];
  const floor = nf / k;
  const snr = peak - floor;
  // SDR Console: peak holds ~1 s (we poll ~10 Hz)
  const now = Date.now();
  if (!App._meterPeakHold || now - App._meterPeakHold.t0 > 1000) {
    App._meterPeakHold = { value: peak, t0: now };
  } else if (peak > App._meterPeakHold.value) {
    App._meterPeakHold.value = peak;
  }
  const peakHold = App._meterPeakHold.value;
  let peakIdx = 0, peakVal = -Infinity;
  for (let i = 0; i < n; i++) {
    if (Math.abs(fr[i] - tune) <= hw && p[i] > peakVal) { peakVal = p[i]; peakIdx = i; }
  }
  if (peakVal === -Infinity) {
    for (let i = 0; i < n; i++) if (p[i] > peakVal) { peakVal = p[i]; peakIdx = i; }
  }
  return { peak: peakHold, mean, floor, snr, peakIdx, peakFreq: fr[peakIdx], rawPeak: peak };
}

// Internal class enums -> what a client-facing operator is actually told. The
// raw names (analog_voice / ofdm_wideband / ...) are the sigidwiki class tags and
// must never be the thing on screen. Each label states the CLASS, not the device.
const CLASS_LABEL = {
  analog_voice: "Narrowband analog carrier (voice class)",
  analog_video: "Wideband analog FM (video / camera class)",
  fsk: "FSK data link",
  ook: "On-off keyed / bursty transmitter",
  ofdm_wideband: "Wideband digital (Wi-Fi / cellular class)",
  continuous_digital: "Continuous digital carrier",
  noise_floor: "Noise floor",
  unknown: "Unidentified modulation",
};
// ...and the only question the operator asked by pressing LISTEN: can this
// signal EVER come out as audio? Anything that can't must say so outright.
const AUDIO_VERDICT = {
  listen: "audio is possible — listen and judge by ear",
  decoded: "audio is possible — listen and judge by ear",
  maybe_decode: "LISTENING WILL NOT PRODUCE AUDIO — this is data; ANALYZE decodes it",
  encrypted: "LISTENING WILL NOT PRODUCE AUDIO — digital, content is not readable",
  identify_only: "LISTENING MAY PRODUCE ONLY HASH — modulation not identified",
  none: "nothing to hear — no emitter in this channel",
};

function updateListenClass(d) {
  // "WHAT AM I LISTENING TO", from the live class the server publishes with the
  // demod telemetry (identify.iq_features + decode_router.interpret — the same
  // pair ANALYZE uses, throttled to a couple of seconds). NFM is the default
  // mode outside broadcast, so without this the operator hears hash off a data
  // link with nothing on screen to explain it — a whole lab session was spent
  // that way. Stated in plain words, and never more certainly than measured.
  const el = $("insp-class");
  if (!el) return;
  const cls = d && d.modulation_class;
  if (!cls) { el.style.display = "none"; return; }
  const col = READABLE_COLOR[d.readable] || "#8fa0b8";
  const bits = [];
  if (d.occupied_bw_khz) bits.push("width ~" + d.occupied_bw_khz + " kHz");
  if (d.symbol_rate_kbd) bits.push("~" + d.symbol_rate_kbd + " kBd");
  el.style.display = "block";        // the stylesheet hides it until there IS a class
  el.style.borderLeftColor = col;
  const head = $("insp-class-head"), why = $("insp-class-why");
  if (head) {
    head.style.color = col;
    head.textContent = (CLASS_LABEL[cls] || cls) +
      (AUDIO_VERDICT[d.readable] ? " · " + AUDIO_VERDICT[d.readable] : "");
  }
  if (why) {
    // the summary is interpret()'s own hedged wording — kept verbatim, plus the
    // standing caveat that this is a short live sample, not the full ANALYZE
    why.textContent = (d.class_summary || "") +
      (bits.length ? "  (" + bits.join(" · ") + ")" : "") +
      "  Live sample — press ANALYZE for the full read.";
  }
}

function updateDemodDiag(d) {
  // live demod-chain readout (from the backend `demod` telemetry): the channel-
  // isolated SNR, how hard the AGC is working, and a plain "why is it weak"
  // verdict — front-end/antenna vs genuinely weak signal vs OK. The same object
  // carries the live signal class, which leads (it answers a bigger question).
  updateListenClass(d);
  const el = $("sm-demod");
  if (!el) return;
  if (!d || d.snr_db == null) { el.textContent = ""; return; }
  const agc = (d.agc_gain_db >= 0 ? "+" : "") + Math.round(d.agc_gain_db);
  let verdict;
  if (d.gated) verdict = "muted — squelch closed (nothing in the channel)";
  else if (d.snr_db < 6 && d.agc_gain_db > 18) verdict = "weak — AGC boosting hard; likely front-end/antenna/range, not DSP";
  else if (d.snr_db < 6) verdict = "weak signal in channel";
  else verdict = "OK";
  const sq = (d.squelch_db != null) ? " · squelch " + d.squelch_db + " dB" : "";
  el.textContent = "DEMOD  SNR " + d.snr_db.toFixed(1) + " dB · AGC " + agc + " dB" + sq + "  —  " + verdict;
}

function updateSmeterDisplay(m) {
  if (!m) return;
  if ($("sm-peak")) $("sm-peak").textContent = m.peak.toFixed(1);
  if ($("sm-mean")) $("sm-mean").textContent = m.mean.toFixed(1);
  if ($("sm-snr")) $("sm-snr").textContent = m.snr.toFixed(1);
  if ($("sm-floor")) $("sm-floor").textContent = m.floor.toFixed(1);
  const span = 50;
  const frac = Math.max(0, Math.min(1, (m.snr + 5) / span));
  const fill = $("sm-bar-fill");
  if (fill) fill.style.width = (frac * 100).toFixed(1) + "%";
  const dot = $("sm-peak-dot");
  if (dot) { dot.style.display = ""; dot.style.left = (frac * 100).toFixed(1) + "%"; }
}

const SIG_HISTORY_SEC = 60;
const SIG_HISTORY_MAX = 600;

function pushSignalHistory(m) {
  const t = Date.now() / 1000;
  if (!App._sigHistoryT0) App._sigHistoryT0 = t;
  App._sigHistory.push({ t, peak: m.rawPeak, snr: m.snr });
  const cutoff = t - SIG_HISTORY_SEC;
  while (App._sigHistory.length && App._sigHistory[0].t < cutoff) App._sigHistory.shift();
  if (App._sigHistory.length > SIG_HISTORY_MAX) App._sigHistory.shift();
}

function drawSignalHistory(cv) {
  const hist = App._sigHistory;
  if (!cv || !hist.length) return;
  const { ctx, W, H } = fitCanvas(cv);
  ctx.fillStyle = "#04070c";
  ctx.fillRect(0, 0, W, H);
  const t1 = hist[hist.length - 1].t;
  const span = Math.max(SIG_HISTORY_SEC, t1 - hist[0].t, 1);
  let snrLo = Infinity, snrHi = -Infinity, pkLo = Infinity, pkHi = -Infinity;
  for (const h of hist) {
    snrLo = Math.min(snrLo, h.snr); snrHi = Math.max(snrHi, h.snr);
    pkLo = Math.min(pkLo, h.peak); pkHi = Math.max(pkHi, h.peak);
  }
  snrHi = Math.max(snrHi, snrLo + 6);
  pkHi = Math.max(pkHi, pkLo + 6);
  const ySnr = v => H - 4 - ((v - snrLo) / (snrHi - snrLo)) * (H - 10);
  const yPk = v => H - 4 - ((v - pkLo) / (pkHi - pkLo)) * (H - 10);
  ctx.strokeStyle = "#1a2430"; ctx.lineWidth = 1;
  for (let g = 0; g < 4; g++) {
    const y = 4 + g * (H - 8) / 3;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
  ctx.strokeStyle = "#00c8ff"; ctx.lineWidth = 1.2; ctx.beginPath();
  hist.forEach((h, i) => {
    const x = W * (h.t - (t1 - span)) / span;
    const y = ySnr(h.snr);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.strokeStyle = "#ffd24a88"; ctx.lineWidth = 1; ctx.beginPath();
  hist.forEach((h, i) => {
    const x = W * (h.t - (t1 - span)) / span;
    const y = yPk(h.peak);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#6b7b90"; ctx.font = "9px monospace";
  ctx.fillText("SNR dB (cyan) · peak dBm (gold) · rolling " + SIG_HISTORY_SEC + "s", 4, H - 3);
}

// CORE entry point: tune the Pluto to `center`, stream audio, and LIVE-poll the
// analyzer + waterfall (both fed by the same stream, so they move). Not tied to
// App.blView — any list can call this with a center + display label. `mode` is
// an optional demod override (WFM/NFM/AM/USB/LSB/CW); omit to let the backend
// pick_mode / the <select> decide. The `tuneId` guards against a stale async
// fallback stomping a newer tune (multiple lists can drive the same panel).
function inspectByFreq(center, label, mode, sid, attr) {
  // EVERY frequency click splits the screen (operator ask): sweep overlay up →
  // split the overlay (sweep keeps drawing beside the monitor); anywhere else →
  // dock the monitor beside the current view.
  const _ov = $("sweep-overlay");
  if (_ov && _ov.classList.contains("show")
      && App.state && App.state.sweep && App.state.sweep.running) {
    MonitorDock.openOverlay();
  } else {
    MonitorDock.openPage();
  }
  const tuneId = (App.tuneId = (App.tuneId || 0) + 1);
  const prevCenter = App.inspCenter;
  App.inspCenter = center; App.inspLabel = label;   // for mode <select> re-tune
  App.inspTunedAt = Date.now();                     // grace anchor for the stale-stream badge
  App.demodMhz = center;                             // demod tuner starts at band center (offset 0)
  App.demodBwHz = null;                              // new signal -> per-mode default channel width
  if (arguments.length >= 4) {                       // opened from a detected signal (has an id)
    App.inspSignalId = (sid != null && !isNaN(sid)) ? sid : null;
    App.inspAttribution = attr || "unassessed";
  } else if (prevCenter == null || Math.abs(prevCenter - center) >= 0.0005) {
    // Re-tuned to a DIFFERENT frequency by a caller that carries no id
    // (openInspect, huntStep, DISSEMINATE ▶ LISTEN). Nothing else ever clears
    // these, so the previous signal's id would ride along and any analysis or
    // recording launched here would attach to the wrong signal. Same-frequency
    // re-tunes (mode <select>, DISSEMINATE on the signal already open) keep it.
    App.inspSignalId = null;
    App.inspAttribution = "unassessed";
  }
  renderInspAttribution();
  if ($("insp-bw")) $("insp-bw").value = "auto";
  App._lastSpecSeq = null;                           // new tune → first frame always draws
  $("inspect-panel").style.display = "";
  $("insp-title").textContent = `${fmtF(center)} · ${label || ""}`;   // fmtF already carries the unit
  $("inspect-panel").scrollIntoView({ block: "center" });   // instant — no smooth-scroll animation delay
  // keep the ANALYZE card (and its DISSEMINATE row) when re-tuning the SAME
  // signal (mode change, DISSEMINATE ▶ LISTEN) — hiding it on its own button
  // click was why "the disseminate button isn't there"; hide only on a new signal
  const ao = $("insp-analyze-out");
  const la = App.lastAnalyze;
  const sameSig = la && la.center_mhz != null && Math.abs(la.center_mhz - center) < 0.0005;
  if (ao && !sameSig) ao.style.display = "none";
  loadRecordings();
  stopSpectrumPoll();
  listenStop();
  // pause+removeAttribute alone does NOT abort the media fetch — the browser can
  // keep (and retry) the old /api/listen stream, which then preempts the new tune
  // and holds the radio at the OLD frequency. load() is what actually detaches it.
  const au = $("insp-audio"); au.pause(); au.removeAttribute("src"); au.load();
  clearInspectCanvases();
  // ANY entry point lands here — the signal lightbox, a baseline row, the wiki,
  // the hunt list. listenAt() starts the ladder for its own path; start one here
  // for every other caller so a refusal is visible instead of leaving the
  // operator with a panel that just sits there doing nothing.
  try {
    if (!ListenSteps.steps.length) {
      ListenSteps.begin([`tune to ${fmtF(center)}`, "open audio stream",
                         "demodulate — live audio"]);
      App.listenStepBase = 0;
    }
  } catch (e) { /* ladder is presentation only — never block the tune */ }
  const rc = inRangeClient(center);
  if (!rc.ok) {
    $("insp-note").textContent = rc.msg +
      (App.inspContext === "baseline" ? "  (Next steps through the rest.)" : "");
    $("insp-msg").textContent = "";
    const ac = $("insp-analyzer").getContext("2d");
    ac.fillStyle = "#e0a030"; ac.font = "13px monospace";
    ac.fillText(rc.msg, 12, 26);
    // the ladder must never be left hanging on a step that will never finish
    try { ListenSteps.fail(App.listenStepBase || 0, rc.msg); ListenSteps.finish(false); } catch (e) {}
    return;
  }
  $("insp-note").textContent = "";
  // POCSAG is a DATA decode, not live audio — the audio ladder doesn't apply
  if (mode === "pocsag") { try { ListenSteps.clear(); } catch (e) {} inspectPocsag(center, label, tuneId); return; }
  $("insp-decode").style.display = "none";
  $("insp-msg").textContent = "tuning & streaming…";
  // Prefer smooth Web Audio chunked playback (/api/listen_pcm); fall back to the
  // plain <audio> element (/api/listen) only if Web Audio / fetch-streaming is
  // unavailable or the stream fails to start.
  const onState = (msg, live, m) => {
    $("insp-msg").textContent = msg;
    if (m) $("insp-mode").value = m;
    try { ListenSteps.onStream(msg, live, m); } catch (e) {}
  };
  // tuned and about to ask the radio for audio — advance the hand-off ladder
  const _lb = App.listenStepBase || 0;
  try { ListenSteps.done(_lb, `tuned to ${fmtF(center)}`); ListenSteps.active(_lb + 1); } catch (e) {}
  listenStart(center, mode, onState).then((ok) => {
    // fall back to the <audio> element ONLY when the CLIENT can't do Web Audio /
    // fetch-streaming. Never on a server refusal: re-hitting /api/listen after a
    // 409 is how a zombie stream got born — it grabbed the radio once it freed
    // and sat on the OLD frequency while the operator had tuned elsewhere.
    if (ok === "fallback" && App.tuneId === tuneId) {   // guard: don't stomp a newer tune
      const ibw = ($("insp-ibw") || {}).value || "2.4";
      const blockS = ($("insp-block-s") || {}).value || "0.5";
      au.src = `/api/listen?center_mhz=${encodeURIComponent(center)}` +
               (mode ? `&mode=${encodeURIComponent(mode)}` : "") +
               `&ibw_mhz=${encodeURIComponent(ibw)}&block_s=${encodeURIComponent(blockS)}`;
      au.play().catch(() => {});
    }
  }).catch((e) => {
    // NEVER fail silently in the field. Without this, any throw inside
    // listenStart (AudioContext refused, fetch rejected, backend 409) left
    // "tuning & streaming…" on screen forever and looked like a dead button.
    if (App.tuneId !== tuneId) return;          // a newer tune owns the panel
    const why = (e && (e.message || e.error || e.msg)) || String(e || "unknown");
    if ($("insp-msg")) $("insp-msg").textContent = "LISTEN failed — " + why;
    try { ListenSteps.fail((App.listenStepBase || 0) + 1, why); ListenSteps.finish(false); } catch (_) {}
    console.error("listenStart failed:", e);
  });
  startSpectrumPoll();
}

// POCSAG is a paging DATA mode, not audio: one-shot capture (~2 s) + decode via
// /api/inspect, then render the analyzer/waterfall once and list decoded pages.
// No audio stream, no live poll. Decoded text is third-party content -> rendered
// with textContent (never innerHTML) and kept to this internal inspect view.
function inspectPocsag(center, label, tuneId) {
  $("insp-msg").textContent = "capturing ~2 s & decoding POCSAG…";
  $("insp-decode").style.display = "none";
  api("/api/inspect", { center_mhz: center, label, mode: "pocsag" }).then((j) => {
    if (App.tuneId !== tuneId) return;          // a newer tune superseded this one
    if (j.analyzer) {
      const meter = computeMeterStats(j.analyzer, center, center, "pocsag");
      if (meter) updateSmeterDisplay(meter);
      drawAnalyzer($("insp-analyzer"), j.analyzer, meter);
    }
    if (j.waterfall) drawWaterfall($("insp-waterfall"), j.waterfall);
    renderDecode(j);
  }).catch((e) => {
    if (App.tuneId === tuneId) $("insp-msg").textContent = "decode failed: " + (e && e.message || e);
  });
}

function renderDecode(j) {
  const el = $("insp-decode");
  const d = j && j.decode;
  if (!d) {
    el.style.display = "none";
    $("insp-msg").textContent = (j && j.note) || "decode unavailable";
    return;
  }
  const s = d.stats || {};
  const okc = (s.codewords_ok || 0) + (s.codewords_corrected || 0);
  const tot = okc + (s.codewords_bad || 0);
  const msgs = d.messages || [];
  const lines = [`POCSAG · ${d.baud ? d.baud + " baud" : "no sync"}` +
                 (d.inverted ? " · inverted polarity" : "") +
                 ` · ${d.sync_count || 0} sync · ${okc}/${tot} codewords ok`];
  if (!msgs.length) {
    lines.push(d.baud ? "(synced, but no addressed pages)" : "(no POCSAG pages decoded)");
  } else {
    for (const m of msgs) {
      const body = m.type === "tone" ? "(tone-only page)" : m.text;
      const flags = (m.bits_corrected ? `  [${m.bits_corrected} bit(s) corrected]` : "") +
                    (m.uncorrectable ? `  [${m.uncorrectable} bad codeword(s)]` : "");
      lines.push(`RIC ${m.ric}  fn${m.function}  ${(m.type || "").toUpperCase()}: ${body}${flags}`);
    }
  }
  el.textContent = lines.join("\n");
  el.style.display = "";
  $("insp-msg").textContent = msgs.length ? `● ${msgs.length} page(s) decoded` : "● decoded";
}

// Baseline-table entry point: steps App.blView[idx] and preserves Next/Prev.
// Computes center + label from the baseline row, then delegates to the core.
function openInspect(idx, modeOverride) {
  const list = App.blView || [];
  if (!list.length) return;
  stopAuto();                        // a baseline pick takes manual control
  idx = Math.max(0, Math.min(idx, list.length - 1));
  App.inspIdx = idx;
  App.inspContext = "baseline";          // Next/Prev step the baseline list
  const r = list[idx];
  const center = blCenter(r);
  const label = `${r.callsign || r.label || r.service || ""} (${idx + 1}/${list.length})`;
  setInspNav(true);
  inspectByFreq(center, label, modeOverride);
}

// Show or hide the Next/Prev controls. They only make sense while stepping the
// baseline list; entering from any other list disables them so they can't jump
// the operator into an unrelated baseline row.
function setInspNav(enabled) {
  for (const id of ["insp-next", "insp-prev"]) {
    const b = $(id);
    if (b) { b.disabled = !enabled; b.style.display = enabled ? "" : "none"; }
  }
}

// Is a SECOND radio available to monitor while the first keeps sweeping? The
// B220's two RX share one LO (they can't tune two different frequencies at
// once — that's why they're paired for direction-finding, not split-tuning), so
// a lone B220 CANNOT sweep and monitor a different frequency simultaneously.
// True concurrent monitoring needs a second, independent-LO radio (the Pluto).
function hasConcurrentMonitor() {
  const roles = App.state && App.state.roles;
  if (!roles || !roles.concurrent_enabled) return false;
  const sdrs = (roles.roles || []).filter(r => SDR_SOURCES.has(r.source));
  return sdrs.length >= 2 || sdrs.some(r => r.role === "monitor" && !r.busy);
}

/* Persistent "no monitor radio" warning while a sweep runs. Without it the only
   evidence the Pluto was offline was a log line — the backend silently retried
   auto-connect every ~75 s for an hour (2026-08-02) while LISTEN's hand-off
   behaviour looked random to the operator. Builds its own fixed DOM (bottom-LEFT;
   ListenSteps owns bottom-right), driven from the 1 Hz render() so it appears and
   clears with the actual role state, not UI intent. */
function renderMonitorBanner(st) {
  const need = !!(st && st.sweep && st.sweep.running &&
                  st.roles && st.roles.concurrent_enabled && !hasConcurrentMonitor());
  let el = document.getElementById("monitor-offline");
  if (!need) { if (el) el.style.display = "none"; return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "monitor-offline";
    el.style.cssText =
      "position:fixed;left:16px;bottom:16px;z-index:99998;max-width:360px;" +
      "background:#0b0f14ee;border:1px solid #ffc107;border-radius:6px;" +
      "padding:9px 12px;font:11px/1.6 Consolas,monospace;color:#ffc107;" +
      "box-shadow:0 6px 22px #000a;pointer-events:none";
    document.body.appendChild(el);
  }
  el.style.display = "";
  el.textContent = "⚠ MONITOR RADIO OFFLINE — no second SDR (Pluto) connected. " +
    "LISTEN will STOP this sweep to take the radio (data kept). Replug the " +
    "Pluto / press CONNECT to listen while the sweep keeps running.";
}

// Entry point for the NON-baseline lists (Results / detected signals / Wiki).
// Disables Next/Prev (no list to step) and tunes the given center+label.
// AUTO-HANDOFF: if there's no second radio to monitor with and a sweep is
// running, we don't error ("not plugged in") — we stop the sweep and repurpose
// the one radio to direction-find the clicked signal. One radio, no dead ends.
async function listenAt(center, label, mode, sid, attr) {
  if (center == null || isNaN(center)) return;
  stopAuto();                       // a manual freq pick takes manual control
  App.inspContext = "freq";
  App.inspIdx = null;
  setInspNav(false);
  const sweeping = App.state && App.state.sweep && App.state.sweep.running;
  // ONE radio: the sweeper IS the only receiver, so it cannot sweep and
  // demodulate at the same time — LISTEN has to TAKE the radio first.
  // This branch used to abort the sweep and start DIRECTION-FINDING instead;
  // when DF then failed (409) the only message went to #ft-msg — a different
  // view than the lightbox the click came from — so it read as "I clicked
  // LISTEN and nothing happened", with the sweep silently dead.
  const single = sweeping && !hasConcurrentMonitor();
  const tuneSteps = [`tune to ${fmtF(center)}`, "open audio stream",
                     "demodulate — live audio"];
  ListenSteps.begin(single
    ? ["stop the sweep (frees the radio — data kept)",
       "confirm the radio released"].concat(tuneSteps)
    : tuneSteps);
  App.listenStepBase = single ? 2 : 0;   // where the tune/stream/demod steps start
  if (single) {
    if ($("ft-msg")) {
      $("ft-msg").textContent =
        `one radio — sweep STOPPED (data kept) so you can listen on ${fmtF(center)}`;
    }
    ListenSteps.active(0);
    try {
      // explicit erase:false — pause-and-save: the hand-off must never delete
      // sweep data (the erase variant is reserved for the SWEEP card's dialog)
      await api("/api/sweep/abort", { erase: false });
    } catch (e) {
      ListenSteps.fail(0, "could not stop the sweep — stop it from the SWEEP card, "
                          + "then press LISTEN again");
      ListenSteps.finish(false);
      return;
    }
    ListenSteps.done(0);
    // The sweep thread needs a beat to release the radio: abort() only SETS an
    // event, it never joins the worker. Starting the stream too early races it
    // and the backend answers 409 "monitor radio is busy" — which is exactly
    // how the old direction-finding fallback failed, 31 ms after its abort.
    // A segment is avg_passes (20) PLUS a maxhold_dwell_s (12 s) dwell. abort()
    // only SETS a flag; collect()/stream() honour it, but the current pass still
    // has to unwind and the segment finalize. That routinely takes far longer
    // than a couple of seconds — an earlier 3 s budget here gave up before the
    // radio was ever free, which looked exactly like "LISTEN does nothing".
    // Budget for the real dwell and SHOW the countdown instead of bailing.
    ListenSteps.active(1);
    const WAIT_MS = 45000, TICK = 400;
    let released = false;
    for (let waited = 0; waited < WAIT_MS; waited += TICK) {
      await new Promise(r => setTimeout(r, TICK));
      let s = null;
      try { s = await api("/api/state"); } catch (e) { released = true; break; }
      if (!(s && s.sweep && s.sweep.running)) { released = true; break; }
      ListenSteps.active(1, "confirm the radio released — finishing the current "
        + `segment (up to ${Math.max(0, Math.round((WAIT_MS - waited) / 1000))}s)`);
    }
    if (released) {
      ListenSteps.done(1, "radio released");
    } else {
      // Do NOT refuse to try: attempt the stream anyway. If the radio really is
      // still held the backend says so and step 4 shows that reason — an honest
      // error beats a button that silently does nothing.
      ListenSteps.done(1, "radio slow to release — trying the stream anyway");
    }
    // We deliberately do NOT auto-restart the sweep afterwards: abort+start
    // opens a NEW sweep record rather than resuming the old one.
    App.sweepPausedForListen = true;
  }
  inspectByFreq(center, label, mode, sid, attr);
  // inspectByFreq clears #insp-note, so stamp the notice AFTER it. #insp-note is
  // never touched by the 1 Hz render(), unlike #ft-msg — so this stays on screen
  // instead of being wiped a second later. The operator must not be able to walk
  // away believing a sweep is still collecting when it is stopped.
  if (App.sweepPausedForListen && $("insp-note")) {
    $("insp-note").textContent =
      "SWEEP STOPPED to free the single radio for this listen — data collected so " +
      "far is KEPT. Restart the sweep from the SWEEP card when you are done listening.";
  }
}

/* ---------- signal hunt: step the not-in-baseline (unknown) emitters ---------- */
// Pull the de-duped UNVERIFIED/caution emitters for the open session and render
// the hunt list. Read-only — needs a prior interior sweep to be populated.
async function loadHunt() {
  let list = [];
  try { list = await api("/api/scan/unknowns"); } catch (e) {}
  App.hunt = Array.isArray(list) ? list : [];
  const tb = $("tbl-hunt");
  if (tb) {
    tb.innerHTML = App.hunt.length
      ? "<tr><th>Score</th><th>Frequency</th><th>BW kHz</th><th>Class</th><th>Why unexplained</th></tr>" +
        App.hunt.map((s, i) =>
          `<tr><td>${(s.anomaly_score || 0).toFixed(0)}</td>` +
          `<td><a href="#" class="freqlink" data-center="${s.center_mhz}" data-hunt-idx="${i}" ` +
          `data-label="${esc(s.matched_label || s.match_reason || s.classification || "unknown")}">${fmtF(s.center_mhz)}</a></td>` +
          `<td>${s.bw_khz}</td>` +
          `<td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span>${s.caution ? " ⚠" : ""}</td>` +
          `<td class="note" style="max-width:300px">${esc(s.match_reason || "")}</td></tr>`).join("")
      : "<tr><td class='note'>No unexplained emitters above the baseline — run an interior sweep first.</td></tr>";
  }
  return App.hunt;
}

// Tune the live listen view to hunt entry `idx` (wraps). Sets the 'hunt' stepping
// context so ◂/▸ walk the hunt list (not the baseline).
function huntStep(idx) {
  const list = App.hunt || [];
  if (!list.length) return;
  idx = ((idx % list.length) + list.length) % list.length;
  App.huntIdx = idx;
  App.inspContext = "hunt";
  const s = list[idx];
  setInspNav(true);
  inspectByFreq(s.center_mhz, `${s.matched_label || s.classification || "unknown"} (${idx + 1}/${list.length})`);
}

async function jumpFirstUnknown() {
  await loadHunt();
  if (!App.hunt.length) {
    alert("No unexplained emitters above the baseline yet — run an interior sweep first.");
    return;
  }
  huntStep(0);
}

function stopAuto() {
  if (App.autoTimer) { clearInterval(App.autoTimer); App.autoTimer = null; }
  const b = $("insp-auto"); if (b) b.classList.remove("on");
}

// AUTO: auto-advance through the hunt list, dwelling N seconds (min 3 — each step
// re-tunes the Pluto, so sub-3s thrashes the radio). Manual ◂/▸ still work alongside.
function toggleAuto() {
  if (App.autoTimer) { stopAuto(); return; }
  if (!App.hunt || !App.hunt.length) { alert("Jump to the hunt list first."); return; }
  const dwell = Math.max(3, parseFloat(($("insp-dwell") || {}).value) || 6);
  App.inspContext = "hunt";
  const b = $("insp-auto"); if (b) b.classList.add("on");
  if (App.huntIdx == null) App.huntIdx = 0;
  App.autoTimer = setInterval(() => huntStep((App.huntIdx ?? 0) + 1), dwell * 1000);
}

function specPollMs() {
  // 50 ms meant 20 spectrum requests a SECOND competing with the live PCM audio
  // on the dev server, which the operator heard as chop. 100 ms is still 10 fps
  // — smooth to the eye — for half the traffic.
  const blockS = parseFloat(($("insp-block-s") || {}).value) || 0.5;
  return blockS <= 0.2 ? 100 : blockS <= 0.25 ? 120 : 150;
}

function startSpectrumPoll() {
  stopSpectrumPoll();
  let busy = false;
  App.specPoll = setInterval(async () => {
    if (busy) return;                        // one in-flight request at a time
    busy = true;
    let s;
    try { s = await api("/api/listen_spectrum"); }
    catch (e) { busy = false; return; }
    busy = false;
    if (!s || s.center_mhz == null) return;
    // skip redundant redraws: the server stamps each frame with a monotonic seq;
    // at low frame rates most polls return the SAME frame, so only redraw on new ones
    // (also stops the 60 s signal-history from being oversampled with duplicate points).
    if (s.seq != null && s.seq === App._lastSpecSeq) { $("insp-msg").textContent = "● live"; return; }
    App._lastSpecSeq = s.seq;
    if (s.mode) $("insp-mode").value = s.mode;
    $("insp-msg").textContent = "● live";
    const mode = s.mode || ($("insp-mode") || {}).value;
    if (s.analyzer) {
      const meter = computeMeterStats(s.analyzer, s.center_mhz, App.demodMhz, mode);
      if (meter) { updateSmeterDisplay(meter); pushSignalHistory(meter); }
      drawAnalyzer($("insp-analyzer"), s.analyzer, meter);
      drawSignalHistory($("insp-history"));
    }
    updateDemodDiag(s.demod);              // live demod chain: SNR/AGC/squelch + "why weak" verdict
    if (s.waterfall) drawWaterfall($("insp-waterfall"), s.waterfall);
  }, specPollMs());
}

function stopSpectrumPoll() {
  if (App.specPoll) { clearInterval(App.specPoll); App.specPoll = null; }
}

function smoothSpectrumTrace(p) {
  const n = p.length;
  const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
  if (!App._specSmooth || App._specSmooth.length !== n) {
    App._specSmooth = p.slice(); App._specSmoothT = now;
    return App._specSmooth;
  }
  // instant attack, TIME-BASED fall (tau seconds): the trace tracks the radio at the
  // same wall-clock speed regardless of frame rate, so falling signals don't linger
  // for seconds when frames are slow (the old per-frame 0.18 decay did exactly that).
  const dt = Math.max(0, (now - (App._specSmoothT || now)) / 1000);
  App._specSmoothT = now;
  const tau = 0.12;
  const decay = dt > 0 ? (1 - Math.exp(-dt / tau)) : 0.5;
  for (let i = 0; i < n; i++) {
    const raw = p[i], prev = App._specSmooth[i];
    App._specSmooth[i] = raw > prev ? raw : prev - (prev - raw) * decay;
  }
  return App._specSmooth;
}

// Keep the "LISTENING TO" line honest on every analyzer frame — the demod tuner
// moves by click, by wheel and by re-tune, and the operator must never have to
// guess which frequency they are actually hearing.
function updateTunedReadout() {
  const f = $("insp-tuned-f");
  if (!f) return;
  const mhz = (App.demodMhz != null) ? App.demodMhz : App.inspCenter;
  f.textContent = (mhz == null) ? "—" : fmtF(mhz);
  const band = $("insp-tuned-band");
  if (band) {
    band.textContent = (App.bandLo == null || App.bandHi == null) ? ""
      : `in band ${fmtF(App.bandLo)} – ${fmtF(App.bandHi)}`;
  }
}

function drawAnalyzer(cv, a, meter) {
  const { ctx, W, H } = fitCanvas(cv);
  ctx.clearRect(0, 0, W, H);
  const p = a.power_dbm, fr = a.freqs_mhz, n = p.length;
  if (!n) return;
  App.bandLo = fr[0]; App.bandHi = fr[n - 1];
  updateTunedReadout();
  const smooth = smoothSpectrumTrace(p);
  let lo = Math.min(...smooth), hi = Math.max(...smooth);
  if (hi - lo < 10) hi = lo + 10;
  const yOf = v => Math.round(H - ((v - lo) / (hi - lo)) * (H - 14) - 7) + 0.5;
  ctx.fillStyle = "rgba(0,200,255,0.06)";
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = W * i / (n - 1);
    i ? ctx.lineTo(x, yOf(smooth[i])) : ctx.moveTo(x, yOf(smooth[i]));
  }
  ctx.lineTo(W, H); ctx.lineTo(0, H); ctx.closePath(); ctx.fill();
  ctx.strokeStyle = "#00c8ff"; ctx.lineWidth = 1.25; ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = W * i / (n - 1);
    const y = yOf(smooth[i]);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  if (meter && meter.peakIdx != null) {
    const px = W * meter.peakIdx / (n - 1);
    const py = yOf(smooth[meter.peakIdx]);
    ctx.fillStyle = "#ffd24a";
    ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "#fff8"; ctx.lineWidth = 1; ctx.stroke();
  }
  drawDemodMarker(ctx, W, H, fr[0], fr[n - 1]);
  ctx.fillStyle = "#6b7b90"; ctx.font = "10px monospace";
  ctx.fillText(fr[0].toFixed(3) + " MHz", 3, H - 3);
  ctx.fillText(fr[n - 1].toFixed(3), W - 56, H - 3);
  ctx.fillText(hi.toFixed(0) + " dBm", 3, 11);
}

const DEMOD_BW_STEPS = [3000, 6000, 12000, 16000, 25000, 50000, 100000, 150000, 250000];

// The demodulator channel width in Hz: an explicit operator choice (App.demodBwHz)
// or the per-mode default. This is what actually gets filtered/heard/decoded, and
// what the shaded band on the analyzer shows.
function currentDemodBwHz() {
  if (App.demodBwHz != null) return App.demodBwHz;
  const m = (($("insp-mode") || {}).value || "nfm");
  if (m === "wfm") return 150000;
  if (m === "am") return 10000;
  if (m === "usb" || m === "lsb") return 3000;
  if (m === "cw") return 1000;
  if (m === "video") return 250000;              // analog video is MHz-wide — never a 16 kHz channel
  return 16000;                                  // nfm + decode modes (pocsag/flex/ook/dtmf)
}

// Vertical cursor + shaded CHANNEL-WIDTH band at the demod tuner within [f0,f1].
function drawDemodMarker(ctx, W, H, f0, f1) {
  if (App.demodMhz == null || f1 === f0) return;
  const span = f1 - f0;
  const mx = W * (App.demodMhz - f0) / span;
  const halfMhz = (currentDemodBwHz() / 1e6) / 2;   // band half-width in MHz
  const xL = W * (App.demodMhz - halfMhz - f0) / span;
  const xR = W * (App.demodMhz + halfMhz - f0) / span;
  const cl = Math.max(0, xL), cr = Math.min(W, xR);
  if (cr > cl) {                                     // the passband the operator hears/decodes
    ctx.fillStyle = "rgba(255,210,74,0.14)";
    ctx.fillRect(cl, 0, cr - cl, H);
    ctx.strokeStyle = "rgba(255,210,74,0.55)"; ctx.lineWidth = 1;
    ctx.beginPath();
    if (xL >= 0 && xL <= W) { ctx.moveTo(Math.round(xL) + 0.5, 0); ctx.lineTo(Math.round(xL) + 0.5, H); }
    if (xR >= 0 && xR <= W) { ctx.moveTo(Math.round(xR) + 0.5, 0); ctx.lineTo(Math.round(xR) + 0.5, H); }
    ctx.stroke();
  }
  if (mx < 0 || mx > W) return;
  ctx.strokeStyle = "#ffd24a"; ctx.lineWidth = 1; ctx.beginPath();       // center hairline
  ctx.moveTo(Math.round(mx) + 0.5, 0); ctx.lineTo(Math.round(mx) + 0.5, H); ctx.stroke();
}

// Set the demod channel width (Hz, or null=auto) and push it to the live audio filter.
function setDemodBw(hz) {
  App.demodBwHz = hz;
  const sel = $("insp-bw");
  if (sel) sel.value = (hz == null) ? "auto" : String(hz);
  // trailing debounce: the wheel fires per tick (a trackpad flick = dozens), and
  // each used to POST — the server only needs the value the operator settles on
  clearTimeout(App._bwPostT);
  App._bwPostT = setTimeout(() => {
    const v = App.demodBwHz;
    api("/api/listen/bandwidth", { chan_bw_hz: (v == null) ? "auto" : v }).catch(() => {});
  }, 250);
}

// Scroll-wheel over the analyzer widens/narrows the channel through the preset steps.
function nudgeDemodBw(dir) {
  const cur = currentDemodBwHz();
  let idx = 0, best = Infinity;
  DEMOD_BW_STEPS.forEach((s, i) => { const d = Math.abs(s - cur); if (d < best) { best = d; idx = i; } });
  idx = Math.max(0, Math.min(DEMOD_BW_STEPS.length - 1, idx + (dir > 0 ? 1 : -1)));
  setDemodBw(DEMOD_BW_STEPS[idx]);
  $("insp-msg").textContent = "filter " + (DEMOD_BW_STEPS[idx] / 1000) + " kHz";
}

// Click the live spectrum/waterfall to tune the demodulator to THAT signal — a
// digital shift within the captured band, no re-tune (POST /api/listen/offset).
function tuneDemodFromClick(e, cv) {
  if (App.inspCenter == null || App.bandLo == null) return;
  const w = cv.clientWidth || cv.getBoundingClientRect().width;
  if (!w) return;
  const frac = Math.max(0, Math.min(1, e.offsetX / w));
  const freq = App.bandLo + frac * (App.bandHi - App.bandLo);
  App.demodMhz = freq;
  api("/api/listen/offset", { offset_hz: (freq - App.inspCenter) * 1e6 }).catch(() => {});
  $("insp-msg").textContent = "● tuned " + fmtF(freq);
  updateTunedReadout();          // say it immediately, not on the next frame
  App._meterPeakHold = null;
}

/* The monitor panel's own canvases carry the demod band, NOT the sweep trace, so
   they need their own x->frequency mapping (App.bandLo..bandHi, the same one
   tuneDemodFromClick uses). Without this the operator could click to tune here
   and never be told which frequency they landed on. */
function inspFreqAtX(cv, clientX) {
  if (App.bandLo == null || App.bandHi == null) return null;
  const r = cv.getBoundingClientRect();
  if (!r.width) return null;
  const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  return App.bandLo + frac * (App.bandHi - App.bandLo);
}

function inspHover(ev, cv) {
  const mhz = inspFreqAtX(cv, ev.clientX);
  const el = $("xhair-readout");
  if (mhz == null || !el) { if (el) el.classList.remove("show"); return; }
  const tuned = (App.demodMhz != null)
    ? `<br><span class="xr-hint">now hearing ${esc(fmtF(App.demodMhz))} — click to move here</span>`
    : `<br><span class="xr-hint">click to tune here</span>`;
  el.innerHTML = `<b>${esc(fmtF(mhz))}</b>${tuned}`;
  el.classList.add("show");
  const pad = 14, w = el.offsetWidth, h = el.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > window.innerWidth - 4) x = ev.clientX - pad - w;
  if (y + h > window.innerHeight - 4) y = ev.clientY - pad - h;
  el.style.left = Math.max(4, x) + "px";
  el.style.top = Math.max(4, y) + "px";
}

["insp-analyzer", "insp-waterfall"].forEach(id => {
  const cv = $(id);
  if (!cv) return;
  cv.style.cursor = "crosshair";
  cv.addEventListener("mousemove", (e) => inspHover(e, cv));
  cv.addEventListener("mouseleave", () => {
    const el = $("xhair-readout"); if (el) el.classList.remove("show");
  });
});
if ($("insp-analyzer")) $("insp-analyzer").addEventListener("click", (e) => tuneDemodFromClick(e, $("insp-analyzer")));
if ($("insp-waterfall")) $("insp-waterfall").addEventListener("click", (e) => tuneDemodFromClick(e, $("insp-waterfall")));
if ($("insp-bw")) $("insp-bw").addEventListener("change", () => {
  const v = $("insp-bw").value;
  setDemodBw(v === "auto" ? null : parseFloat(v));
});
if ($("insp-analyzer")) $("insp-analyzer").addEventListener("wheel", (e) => { e.preventDefault(); nudgeDemodBw(-e.deltaY); }, { passive: false });

// BUGFIX: the monitor/inspect panel was authored INSIDE the baseline <section>,
// which is display:none unless the baseline tab is active — so every other list
// (hunt, results, wiki, triage) called inspectByFreq but the panel never showed.
// Lift it to the end of the views' container so it's outside any .view and
// visible from anywhere it's invoked.
(function liftInspectPanel() {
  const p = $("inspect-panel");
  const sec = p && p.closest("section.view");
  if (p && sec && sec.parentElement) sec.parentElement.appendChild(p);
})();

/* ---------- monitor DOCK — split-screen monitor everywhere (2026-07-11) ---------- */
// Clicking a frequency ANYWHERE splits the screen. Two contexts, one module:
//   overlay — a sweep is running with the overlay up: the OVERLAY splits, the
//             sweep keeps drawing in one pane, the monitor in the other
//             (dual-radio: the Pluto plays live while the B220 sweeps);
//   page    — everywhere else: the CONTENT AREA splits, the view you clicked
//             from stays put and the monitor docks beside/below it.
// One persisted orientation preference drives both (⇄ side-by-side / ⇅ stacked).
// ✕ stops the listen; when the sweep overlay closes, a still-playing monitor is
// handed to the page dock so the audio + spectra survive.
const MonitorDock = {
  mode: null,                       // null | "overlay" | "page"
  _home: null, _homeNext: null,     // the panel's ORIGINAL spot (captured once)
  orient() { return localStorage.getItem("sweepSplitOrient") || "v"; },
  setOrient(o) {
    localStorage.setItem("sweepSplitOrient", o);
    this._applyOrient();
  },
  _applyOrient() {
    const o = this.orient(), ov = $("sweep-overlay");
    ov.classList.toggle("split-v", this.mode === "overlay" && o === "v");
    ov.classList.toggle("split-h", this.mode === "overlay" && o === "h");
    document.body.classList.toggle("pdock-v", this.mode === "page" && o === "v");
    document.body.classList.toggle("pdock-h", this.mode === "page" && o === "h");
    for (const id of ["btn-split-orient", "btn-pdock-orient"]) {
      const b = $(id);
      if (b) b.textContent = o === "v" ? "⇅" : "⇄";   // icon shows what it flips TO
    }
  },
  _stash() {
    const p = $("inspect-panel");
    if (p && this._home == null) { this._home = p.parentNode; this._homeNext = p.nextSibling; }
    return p;
  },
  openOverlay() {
    const p = this._stash();
    if (!p || this.mode === "overlay") return;
    this._clearClasses();
    $("sweep-monitor-slot").appendChild(p);
    $("sweep-overlay").classList.add("split-on");
    this.mode = "overlay";
    this._applyOrient();
    const roles = (App.state && App.state.roles && App.state.roles.roles) || [];
    // any SDR serving as monitor gives live audio during the sweep — the B220
    // can hold that role too (swap/assign), not just the Pluto
    const mon = roles.find(r => r.role === "monitor" && SDR_SOURCES.has(r.source));
    const note = $("sweep-mon-note");
    if (note) note.textContent = mon
      ? `on the ${mon.source === "b220" ? "B220 Mini" : "Pluto"} — the sweep keeps running`
      : "the sweep holds the radio — plug in the second radio to hear it live";
  },
  openPage() {
    const p = this._stash();
    if (!p || this.mode === "page") return;
    this._clearClasses();
    $("page-monitor-slot").appendChild(p);
    document.body.classList.add("pdock-on");
    this.mode = "page";
    this._applyOrient();
    const note = $("page-mon-note");
    if (note) note.textContent =
      (App.state && App.state.sweep && App.state.sweep.running)
        ? "sweep running in the background" : "";
    p.style.display = "";
  },
  // ✕: operator is done — stop the stream, put the panel home, hide it.
  // Mirrors insp-close: also stop AUTO hunt-stepping (or the dock reopens on
  // the next dwell tick) and silence the <audio> fallback element.
  close() {
    if (!this.mode) return;
    try { stopAuto(); listenStop(); stopSpectrumPoll(); } catch (e) {}
    const a = $("insp-audio");
    if (a) { a.pause(); a.removeAttribute("src"); a.load(); }
    const p = this._restore();
    if (p) p.style.display = "none";
  },
  // sweep overlay going away (sweep end / RUN IN BACKGROUND): hand the monitor
  // to the page dock, still playing
  overlayClosed() {
    if (this.mode !== "overlay") return;
    this.mode = null;
    this._clearClasses();
    this.openPage();
  },
  _restore() {
    const p = $("inspect-panel");
    if (p && this._home) this._home.insertBefore(p, this._homeNext);
    this._clearClasses();
    this.mode = null;
    return p;
  },
  _clearClasses() {
    $("sweep-overlay").classList.remove("split-on", "split-v", "split-h");
    document.body.classList.remove("pdock-on", "pdock-v", "pdock-h");
  },
};
[["btn-split-close", () => MonitorDock.close()],
 ["btn-pdock-close", () => MonitorDock.close()],
 ["btn-split-orient", () => MonitorDock.setOrient(MonitorDock.orient() === "v" ? "h" : "v")],
 ["btn-pdock-orient", () => MonitorDock.setOrient(MonitorDock.orient() === "v" ? "h" : "v")],
].forEach(([id, fn]) => { const b = $(id); if (b) b.onclick = fn; });

/* ---------- monitor: fused ANALYZE ("what is it?") + RECORD ---------- */

// Receive + identify the inspected signal in ONE capture: modulation features,
// sigidwiki candidates (with TSCM notes), POCSAG decode, demod audio, and whether
// it's NEW to this address. The "what IS it" brain behind the monitor — distinct
// from the live power-vs-freq analyzer trace.
async function analyzeSignal() {
  const center = App.inspCenter;
  if (center == null) { alert("Open a signal in the monitor first (click a frequency)."); return; }
  const out = $("insp-analyze-out");
  out.style.display = ""; out.innerHTML = "<span class='note'>analyzing…</span>";
  const mode = ($("insp-mode") || {}).value || undefined;
  let j;
  try { j = await api("/api/analyze/signal", { center_mhz: center, label: App.inspLabel || "", mode }); }
  catch (e) { out.innerHTML = `<span class='note'>analyze failed: ${esc(e.message || e)}</span>`; return; }
  App.lastAnalyze = j;                     // the DISSEMINATE actions read this
  renderAnalyze(j);
  if (j.audio) { const au = $("insp-audio"); au.src = j.audio; }
}

// One decoded message -> one human line, across every protocol the router runs.
function decodeMsgLine(proto, m) {
  const p = (proto || "").toUpperCase();
  if (p === "POCSAG" || p === "FLEX") {
    const id = m.ric != null ? `RIC ${m.ric}` : (m.capcode != null ? `capcode ${m.capcode}` : "");
    const body = m.text || m.alpha || m.numeric || (m.type === "tone" ? "(tone-only)" : "");
    const fl = m.bits_corrected ? `  [${m.bits_corrected} bit(s) corrected]` : "";
    return `${id}${m.function != null ? " fn" + m.function : ""}  ${(m.type || "").toUpperCase()}: ${body}${fl}`;
  }
  if (p && p.startsWith("OOK")) {
    return `${m.encoding || "?"} · ${m.n_bits || 0} bits · 0x${m.hex || ""} · ×${m.repeats || 1}` +
           (m.device_guess ? `  — ${m.device_guess}` : "");
  }
  if (p.startsWith("ANALOG-VIDEO")) {
    // A sync verdict, not content: lead with the standard + the timing that proves
    // it, then the evidence/caveat lines the decoder wrote (never re-word them here).
    const head = `${m.standard || "raster"} composite video sync · line ${(m.line_rate_hz || 0).toFixed(1)} Hz` +
      (m.field_rate_hz ? ` · field ${m.field_rate_hz} Hz` : " · field rate unconfirmed") +
      ` · sync ${m.sync_width_us} µs · confidence ${m.confidence}`;
    const ev = (m.evidence || []).map(s => "   + " + s);
    const cv = (m.caveats || []).map(s => "   ! " + s);
    return [head].concat(ev, cv).join("\n");
  }
  if (p === "DTMF") return `digits: ${m.digits || ""}`;
  if (p === "CW/MORSE" || p === "CW") return `${m.text || ""}${m.wpm ? `  (${m.wpm} wpm)` : ""}`;
  return JSON.stringify(m);
}

const READABLE_COLOR = {
  decoded: "#39d98a", listen: "#39d98a", maybe_decode: "#ffd24a",
  encrypted: "#e0a030", identify_only: "#e0a030", none: "#6b7b90", unknown: "#6b7b90",
};

function renderAnalyze(j) {
  const out = $("insp-analyze-out");
  const f = j.features, nv = j.novelty, cands = j.candidates || [], d = j.decode;
  const rows = [];
  rows.push(`<div class="row"><b>◎ ANALYZE</b> ${j.in_range ? "" : "<span class='pill'>freq-only</span>"} ` +
            `${j.pocsag_likely ? "<span class='pill'>looks like POCSAG</span>" : ""}</div>`);
  // plain-English field verdict — "what is it transmitting"
  if (j.interpretation) {
    const col = READABLE_COLOR[j.interpretation.readable] || "#8fa0b8";
    rows.push(`<div style="color:${col};font-weight:bold;margin:2px 0">▶ ${esc(j.interpretation.summary)}</div>`);
  }
  // decoded content (any protocol the router matched)
  if (d && d.messages && d.messages.length) {
    rows.push(`<div class="note" style="margin-top:2px">DECODED — ${esc(d.protocol || "")}` +
              `${d.note ? " · " + esc(d.note) : ""}:</div>`);
    rows.push("<pre style='white-space:pre-wrap;background:#04070c;color:#cfd8e3;border:1px solid #26303d;padding:6px;margin:2px 0'>" +
      d.messages.map(m => esc(decodeMsgLine(d.protocol, m))).join("\n") + "</pre>");
    // A recovered video still (survey/video.py Phase 2). The src is a data: URL
    // this app generated, so there is nothing to escape — but keep the honest
    // caption attached to it: a picture confirms a raster, not whose camera it is.
    const still = d.messages.find(m => m && m.frame_png);
    if (still) {
      rows.push(`<div class="note" style="margin-top:2px">RECOVERED STILL — one field ` +
        `(${still.frame_cols}×${still.frame_rows}), luminance only, contrast-stretched. ` +
        `Evidence of a video raster; it does not by itself identify the camera or its owner.</div>`);
      rows.push(`<img src="${still.frame_png}" alt="recovered video field" ` +
        `style="max-width:100%;image-rendering:pixelated;border:1px solid #26303d;margin:2px 0">`);
    }
  } else if (d && d.note) {
    rows.push(`<div class="note">${esc(d.note)}</div>`);
  }
  if (j.note) rows.push(`<div class="note">${esc(j.note)}</div>`);
  if (f) {
    rows.push(`<div class="note">${f.signal_present ? "● signal present" : "○ noise floor"} · ` +
      `<b>${esc(f.modulation_class)}</b> · BW ~${f.occupied_bw_khz} kHz · ${esc(f.continuity)} · ` +
      `duty ${(f.duty_cycle * 100).toFixed(0)}% · SNR ${f.snr_db} dB` +
      (f.fsk_states >= 2 ? ` · FSK ${f.fsk_states}-state, shift ${f.fsk_shift_khz} kHz` : "") +
      (f.symbol_rate_kbd ? ` · ~${f.symbol_rate_kbd} kBd` : "") + `</div>`);
  }
  if (nv) {
    rows.push(nv.is_novel_to_address
      ? `<div class="note" style="color:#ffd24a">▲ NEW TO THIS ADDRESS — ${esc(nv.novelty_reason || "not seen here before")}</div>`
      : `<div class="note">seen at this address before · ${nv.prior_sessions} prior sweep(s)` +
        (nv.differences && nv.differences.power_delta_db != null ? ` · Δpower ${nv.differences.power_delta_db} dB` : "") +
        `</div>`);
  }
  if (cands.length) {
    rows.push("<div class='note' style='margin-top:4px'>CANDIDATES (device catalog + sigidwiki — feature match, confirm by ear/eye):</div>");
    rows.push("<table><tr><th>Signal</th><th>Class</th><th>BW</th><th>TSCM note</th><th></th></tr>" +
      cands.map(c => `<tr><td>${esc(c.name)}` +
        (c.device_class ? `<br><span class="note">${esc(c.device_class)}</span>` : "") + `</td>` +
        `<td>${esc(c.class)}</td><td>${c.bandwidth_khz} kHz</td>` +
        `<td class="note" style="max-width:240px">${esc(c.tscm || "")}</td>` +
        `<td><a href="${esc(c.url)}" target="_blank" rel="noopener" class="note">ref ↗</a></td></tr>`).join("") +
      "</table>");
    const topCat = cands.find(c => c.device_class);
    if (topCat) rows.push(catalogDetail(topCat));
  }
  rows.push(dissemRow(j));
  out.innerHTML = rows.join("");
  out.style.display = "";
}

/* ---------- DISSEMINATE — the per-class next action after ANALYZE ---------- */
// (DEMOD_ANALYZE_DISSEMINATE_DESIGN.md §4c, minimal field version.) Turns the
// interpret() verdict into the concrete next move instead of leaving the card a
// dead end: analog -> auto-tuned LISTEN (mode + channel width set from the
// measured features), decodable -> re-analyze in the protocol mode, encrypted /
// unknown -> capture IQ evidence + locate. Receive-only throughout.

function nearestBwStep(hz) {
  for (const s of DEMOD_BW_STEPS) if (s >= hz) return s;
  return DEMOD_BW_STEPS[DEMOD_BW_STEPS.length - 1];
}

// features -> suggested analog demod mode (null = not an analog-listen signal)
function suggestedModeFor(j) {
  const f = j.features || {};
  const mc = f.modulation_class || "";
  const bwK = f.occupied_bw_khz || 0;
  if (mc === "analog_voice") return bwK > 80 ? "wfm" : "nfm";
  if (mc === "analog_video") return "wfm";     // audio subcarrier listen
  const c = (j.center_mhz || 0);
  if (mc === "unknown" && c >= 0.5 && c <= 1.71) return "am";
  return null;
}

function dissemRow(j) {
  if (!j.in_range || !j.features) {
    // freq-only analyze: nothing was received — say WHY there are no actions
    // instead of silently omitting the row (looks like a missing button)
    return "<div class='note' style='margin-top:6px;border-top:1px solid #26303d;padding-top:4px'>" +
           "DISSEMINATE: no actions — " +
           esc(j.note || "nothing was received (frequency-only match)") + "</div>";
  }
  const r = ((j.interpretation || {}).readable) || "unknown";
  const f = j.features || {};
  const d = j.decode;
  const btns = [], notes = [];
  const mode = suggestedModeFor(j);
  const bwHz = f.occupied_bw_khz ? nearestBwStep(f.occupied_bw_khz * 1000) : null;
  if (!f.signal_present) {
    return "<div class='note' style='margin-top:6px'>DISSEMINATE: nothing here — empty channel " +
           "(no audio played; fuzz would only mean amplified noise). Re-ANALYZE while the emitter is active.</div>";
  }
  // Only point at the attribution control when it EXISTS. renderInspAttribution()
  // hides itself when there is no signal id, and a baseline row has none — so
  // this used to tell the operator to set something they could not see anywhere
  // on the page. Attribution attaches to a DETECTION, not to a reference row.
  if (App.inspSignalId == null) {
    notes.push("this is a BASELINE reference frequency, not a detection from your "
      + "sweep — there is nothing to attribute yet. Attribution attaches to a "
      + "signal the sweep actually found; open one from the hit ledger or results "
      + "to set it.");
  } else if (App.inspAttribution === "unassessed" || !App.inspAttribution) {
    notes.push("set the ATTRIBUTION above first — it is the legal line for listening to / decoding content");
  }
  if (r === "listen" || mode) {
    btns.push(`<button class="btn primary" onclick="dissemListen()">▶ LISTEN` +
              `${mode ? " · " + mode.toUpperCase() : ""}${bwHz ? " · " + (bwHz / 1000) + " kHz" : ""}</button>`);
    btns.push(`<button class="btn" onclick="toggleRecord('audio')">● RECORD AUDIO EVIDENCE</button>`);
  }
  // analog_video is offered a DECODE even though its verdict is "listen": it IS
  // listenable (the buzz / an audio subcarrier), AND survey/video.py can confirm
  // or deny NTSC/PAL sync on it. Both actions are real, so offer both.
  if (r === "decoded" || r === "maybe_decode" || f.modulation_class === "analog_video") {
    const proto = (d && d.protocol) ||
      (f.modulation_class === "analog_video" ? "video"
        : f.modulation_class === "fsk" ? "pocsag" : "ook");
    btns.push(`<button class="btn" onclick="dissemDecode('${esc(proto.toLowerCase())}')">⟲ DECODE · ${esc(proto.toUpperCase())}</button>`);
    btns.push(`<button class="btn" onclick="toggleRecord('iq')">⦿ IQ EVIDENCE</button>`);
  }
  if (r === "encrypted" || r === "identify_only" || r === "unknown") {
    btns.push(`<button class="btn" onclick="toggleRecord('iq')">⦿ CAPTURE IQ EVIDENCE</button>`);
    notes.push("content is not readable — identify + LOCATE (directional / DF bearing) + capture, never decrypt");
  }
  return "<div style='margin-top:6px;border-top:1px solid #26303d;padding-top:4px'>" +
         "<div class='note'>DISSEMINATE — next action for this class:</div>" +
         "<div class='row' style='gap:6px;margin:3px 0'>" + btns.join(" ") + "</div>" +
         (notes.length ? "<div class='note'>" + notes.map(esc).join(" · ") + "</div>" : "") +
         "</div>";
}

// Start the live listen tuned the way ANALYZE measured it: suggested mode +
// channel width from occupied_bw. inspectByFreq resets the width, so set it after.
function dissemListen() {
  const j = App.lastAnalyze;
  if (!j) return;
  const mode = suggestedModeFor(j) || undefined;
  const f = j.features || {};
  if (mode && $("insp-mode")) $("insp-mode").value = mode;
  inspectByFreq(j.center_mhz, App.inspLabel || j.label || "", mode);
  if (f.occupied_bw_khz) setDemodBw(nearestBwStep(f.occupied_bw_khz * 1000));
}

// Re-run the fused analyze locked to a decode protocol (pocsag/flex/ook/video/
// dtmf/cw). The router reports PROTOCOL labels ("ANALOG-VIDEO", "OOK/FSK-DEVICE",
// "CW/MORSE") while the <select> carries MODE keys — map the ones that differ, or
// the assignment silently no-ops and the re-analyze runs in the old mode.
function dissemDecode(proto) {
  const k = String(proto || "").toLowerCase();
  const mode = k.startsWith("analog-video") ? "video"
    : k.startsWith("ook") ? "ook"
      : k.startsWith("cw") ? "cw" : k;
  if ($("insp-mode")) $("insp-mode").value = mode;
  analyzeSignal();
}

// RECORD the inspected signal to a per-session file (toggle). kind='audio' -> WAV
// you can play back; kind='iq' -> raw complex64 forensic evidence (encrypted/
// unknown intruder you can't read but must capture + hand to authorities).
async function toggleRecord(kind) {
  kind = kind || "audio";
  const aBtn = $("insp-record"), iBtn = $("insp-record-iq");
  if (App.recording) {
    App.recording = false;
    if (aBtn) { aBtn.textContent = "● REC"; aBtn.classList.remove("on"); }
    if (iBtn) { iBtn.textContent = "⦿ IQ"; iBtn.classList.remove("on"); }
    try {
      const r = await api("/api/record/stop", {});
      $("insp-msg").textContent = r.recording_id
        ? `■ saved (${r.duration_s || 0}s · ${r.kind || ""})` : (r.note || "stopped");
    } catch (e) { $("insp-msg").textContent = "stop failed: " + (e.message || e); }
    loadRecordings();
    return;
  }
  const center = App.inspCenter;
  if (center == null) { alert("Open a signal in the monitor first."); return; }
  const mode = ($("insp-mode") || {}).value || undefined;
  try {
    await api("/api/record/start", { center_mhz: center, mode, kind, label: App.inspLabel || "" });
    App.recording = true;
    const b = (kind === "iq") ? iBtn : aBtn;
    if (b) { b.textContent = "■ STOP"; b.classList.add("on"); }
    $("insp-msg").textContent = (kind === "iq") ? "● recording IQ evidence…" : "● recording…";
  } catch (e) { alert("Record failed: " + (e.message || e)); }
}

async function loadRecordings() {
  const el = $("insp-recordings");
  if (!el) return;
  let recs = [];
  try { recs = await api("/api/recordings"); } catch (e) { el.innerHTML = ""; return; }
  if (!recs.length) { el.innerHTML = ""; return; }
  el.innerHTML = "<div class='note'>RECORDINGS (this session · client-sensitive, gitignored):</div>" +
    recs.map(r => {
      const meta = `${fmtF(r.center_mhz)} · ${esc(r.kind || "audio")}${r.kind !== "iq" ? "/" + esc(r.mode || "") : ""}` +
        ` · ${r.duration_s || 0}s${r.notes === "archived" ? " · archived" : ""}`;
      const player = (r.kind === "iq")
        ? `<a class="note" href="/api/recordings/${r.id}/audio" download>download .cf32 evidence ↓</a>`
        : `<audio controls preload="none" src="/api/recordings/${r.id}/audio" style="height:30px"></audio>`;
      return `<div class="row" style="gap:6px"><span class="note">${meta}</span>${player}</div>`;
    }).join("");
}

// ✈ DISSEMINATE is ALWAYS in the panel (operator ask 2026-07-12): re-open the
// last ANALYZE card for this signal if we have one, else run ANALYZE first —
// either way it lands on the per-class action row (LISTEN / DECODE / CAPTURE).
async function dissemOpen() {
  const la = App.lastAnalyze;
  const sameSig = la && App.inspCenter != null && la.center_mhz != null &&
                  Math.abs(la.center_mhz - App.inspCenter) < 0.0005;
  if (!sameSig) await analyzeSignal();
  const out = $("insp-analyze-out");
  if (out && out.innerHTML) {
    out.style.display = "";
    out.scrollIntoView({ block: "nearest" });
  }
}
if ($("insp-analyze")) $("insp-analyze").onclick = analyzeSignal;
if ($("insp-dissem")) $("insp-dissem").onclick = dissemOpen;
// BENCH capture to a SigMF pair. Synchronous and short by design — this is the
// small sibling of tools/iq_record.py, which is the instrument that actually
// reports achieved sample rate, clipping and dropped buffers. Anything the
// server flags as a problem is shown here rather than swallowed: a clipped or
// spliced capture that looks fine is the expensive failure.
async function recordSigmf() {
  const center = App.inspCenter;
  if (center == null) { alert("Open a signal in the monitor first."); return; }
  const raw = prompt("Record how many seconds of raw IQ? (max 30)", "5");
  if (raw === null) return;
  const seconds = parseFloat(raw);
  if (!(seconds > 0)) { alert("Enter a positive number of seconds."); return; }
  const note = prompt("Operator note for the sidecar (what you're aiming at):", "") || "";
  const b = $("insp-record-sigmf");
  if (b) { b.disabled = true; b.textContent = "⦿ …"; }
  $("insp-msg").textContent = `● capturing ${seconds}s of IQ…`;
  try {
    const r = await api("/api/record/iq_sigmf",
                        { center_mhz: center, seconds, note, label: App.inspLabel || "" });
    const warn = (r.warnings || []).join(" · ");
    $("insp-msg").textContent =
      `■ SigMF saved: ${(r.samples || 0).toLocaleString()} samples, ` +
      `${((r.bytes || 0) / 1e6).toFixed(1)} MB, peak ${r.peak_amplitude}` +
      (warn ? ` — ${warn}` : " — clean");
    if (warn) alert("Capture written, but check this before you rely on it:\n\n" + warn);
    loadRecordings();
  } catch (e) { alert("SigMF capture failed: " + (e.message || e)); }
  finally { if (b) { b.disabled = false; b.textContent = "⦿ SigMF"; } }
}

if ($("insp-record")) $("insp-record").onclick = () => toggleRecord("audio");
if ($("insp-record-iq")) $("insp-record-iq").onclick = () => toggleRecord("iq");
if ($("insp-record-sigmf")) $("insp-record-sigmf").onclick = recordSigmf;

/* ---------- MANUAL gated triage (post-hoc, no radio) ---------- */
const TRIAGE_LABELS = { cleared: "Cleared", suspect: "Suspect", flagged: "Flag", ask_client: "Ask client" };
// Attribution: is this detection capturing THE CLIENT'S OWN space? (the legal line for decoding content)
const ATTRIBUTION_LABELS = { client_space: "Client's space ⚠", third_party: "Third-party",
                             infrastructure: "Infrastructure", inconclusive: "Inconclusive" };
const ATTRIBUTION_TITLES = {
  client_space: "Confirmed capturing the CLIENT's own environment — a bug targeting them. Escalate + capture IQ evidence.",
  third_party: "Someone else's signal (neighbor, passer-by). Do NOT decode its content — move along.",
  infrastructure: "External / expected infrastructure (tower, broadcast). Not about the client.",
  inconclusive: "Can't yet tell — needs more assessment (location, demod of the client's own space)." };

function triageFilters() {
  const minScore = parseFloat(($("triage-filter-score") || {}).value);
  return {
    novelOnly: !!($("triage-filter-novel") || {}).checked,
    minScore: isNaN(minScore) ? null : minScore,
    room: ($("triage-filter-room") || {}).value || "",
    status: ($("triage-filter-status") || {}).value ?? "unreviewed",
  };
}

function triageFilteredSignals(signals) {
  const f = triageFilters();
  return (signals || []).filter(s => {
    const st = s.triage_status || "unreviewed";
    // a confirmed threat is NEVER hidden by the review-state filter — flagging a
    // bug must not make it vanish from the list (only room/score still narrow it)
    const isThreat = st === "flagged" || s.attribution_status === "client_space";
    if (f.status && st !== f.status && !isThreat) return false;
    if (f.novelOnly && !s.is_novel_to_address) return false;
    if (f.minScore != null && (s.anomaly_score || 0) < f.minScore) return false;
    if (f.room && (s.room || "") !== f.room) return false;
    return true;
  });
}

function triagePopulateRooms(signals) {
  const sel = $("triage-filter-room");
  if (!sel) return;
  const cur = sel.value;
  const rooms = [...new Set((signals || []).map(s => s.room || "").filter(Boolean))].sort();
  sel.innerHTML = '<option value="">all rooms</option>' +
    rooms.map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
  if (rooms.includes(cur)) sel.value = cur;
}

function renderTriageTable() {
  const d = App.triage;
  const tb = $("tbl-triage");
  if (!d || !d.sweep_id || !d.signals.length) {
    tb.innerHTML = "<tr><td class='note'>Nothing to triage — run an interior sweep, or every detection is baseline-explained.</td></tr>";
    if ($("triage-filter-count")) $("triage-filter-count").textContent = "";
    return;
  }
  const visible = triageFilteredSignals(d.signals);
  if ($("triage-filter-count")) {
    $("triage-filter-count").textContent =
      `showing ${visible.length} of ${d.signals.length}`;
  }
  if (!visible.length) {
    tb.innerHTML = "<tr><td class='note'>No signals match the current filters.</td></tr>";
    return;
  }
  tb.innerHTML =
    "<tr><th>Score</th><th>Frequency</th><th>Room</th><th>Class</th><th>Novel?</th>" +
    "<th>Decision</th><th>Attribution (about the client?)</th><th>Notes</th><th>Monitor</th></tr>" +
    visible.map(triageRow).join("");
}

async function loadTriage() {
  try {
    const roles = await api("/api/device/roles");
    const pill = $("triage-mode");
    if (pill) pill.textContent = roles.concurrent_enabled
      ? "LIVE triage (dual-device: the Pluto monitors while the sweeper runs)"
      : "post-hoc (review the last sweep — radio free)";
  } catch (e) {}
  let d;
  try { d = await api("/api/triage/last"); }
  catch (e) { $("triage-summary").textContent = e.message || e; return; }
  App.triage = d;
  App.triageRemaining = d.remaining_unexplained || 0;
  triagePopulateRooms(d.signals);
  const tb = $("tbl-triage");
  if (!d.sweep_id || !d.signals.length) {
    tb.innerHTML = "<tr><td class='note'>Nothing to triage — run an interior sweep, or every detection is baseline-explained.</td></tr>";
    $("triage-summary").textContent = "";
    $("triage-gate").innerHTML = "";
    if ($("triage-filter-count")) $("triage-filter-count").textContent = "";
    return;
  }
  $("triage-summary").textContent =
    `sweep #${d.sweep_id} · ${d.signals.length} unexplained · ${d.remaining_unexplained} left to review`;
  $("triage-gate").innerHTML = d.remaining_unexplained > 0
    ? `<span style="color:#e0a030">⏳ ${d.remaining_unexplained} signal(s) still unreviewed — the review is INCOMPLETE.</span>`
    : `<span style="color:#39d98a">✓ every signal triaged — review COMPLETE.</span>`;
  renderTriageTable();
}

function triageRow(s) {
  const st = s.triage_status || "unreviewed";
  const btns = Object.keys(TRIAGE_LABELS).map(k =>
    `<button class="btn ${st === k ? "primary" : ""}" style="padding:2px 5px;font-size:11px" ` +
    `onclick="triageDecide(${s.id},'${k}')">${TRIAGE_LABELS[k]}</button>`).join(" ");
  // the real threat: flagged, or confirmed capturing the client's own space
  const isThreat = st === "flagged" || s.attribution_status === "client_space";
  const novel = s.is_novel_to_address
    ? `<span class="pill" style="color:#ffd24a" title="${esc(s.novelty_reason || "")}">▲ NEW</span>` : "";
  const lbl = (s.matched_label || s.classification || "signal").replace(/'/g, "");
  return `<tr data-sid="${s.id}" class="${isThreat ? "threat-row" : ""}">` +
    `<td title="${esc(s.score_reason || "")}">${(s.anomaly_score || 0).toFixed(0)}${isThreat ? '<br><span class="threat-tag">● THREAT</span>' : ''}</td>` +
    `<td><a href="#" class="freqlink" data-center="${s.center_mhz}" data-sid="${s.id}" data-attr="${s.attribution_status || "unassessed"}" data-label="${esc(lbl)}">${fmtF(s.center_mhz)}</a></td>` +
    `<td class="note">${esc(s.room || "—")}</td>` +
    `<td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span>${s.caution ? " ⚠" : ""}</td>` +
    `<td>${novel}</td>` +
    `<td>${btns}<div class="note" style="margin-top:2px">${st === "unreviewed" ? "— unreviewed" : esc(TRIAGE_LABELS[st] || st)}</div></td>` +
    `<td>${attributionCell(s)}</td>` +
    `<td><input class="triage-note" data-sid="${s.id}" value="${esc(s.triage_notes || "")}" placeholder="notes…" style="width:150px"></td>` +
    `<td><button class="btn" onclick="monitorFreq(${s.center_mhz},'${esc(lbl)}',${s.id},'${s.attribution_status || "unassessed"}')">▶ monitor</button></td>` +
    `</tr>`;
}

async function triageDecide(id, status) {
  const noteEl = document.querySelector(`.triage-note[data-sid="${id}"]`);
  const notes = noteEl ? noteEl.value : "";
  try { await api(`/api/signals/${id}/triage`, { status, notes }); }
  catch (e) { alert("Triage failed: " + (e.message || e)); return; }
  loadTriage();
}

// Attribution buttons + the ECPA-gated force-decode override, per signal.
function attributionCell(s) {
  const ast = s.attribution_status || "unassessed";
  const btns = Object.keys(ATTRIBUTION_LABELS).map(k =>
    `<button class="btn ${ast === k ? "primary" : ""}" style="padding:2px 5px;font-size:11px" ` +
    `title="${esc(ATTRIBUTION_TITLES[k])}" onclick="triageAttribute(${s.id},'${k}')">${ATTRIBUTION_LABELS[k]}</button>`).join(" ");
  const decodeBtn = `<button class="btn" style="padding:2px 5px;font-size:11px" ` +
    `title="Decode this signal's CONTENT. Lawful only for the client's own space/device — logs an ECPA acknowledgment." ` +
    `onclick="ecpaOverride(${s.id},${s.center_mhz})">⚖ decode…</button>`;
  const state = ast === "unassessed" ? "" :
    `<div class="note" style="margin-top:2px${ast === "client_space" ? ";color:#ff6b6b" : ""}">${esc(ATTRIBUTION_LABELS[ast])}</div>`;
  return btns + " " + decodeBtn + state;
}

async function triageAttribute(id, status) {
  const noteEl = document.querySelector(`.triage-note[data-sid="${id}"]`);
  const notes = noteEl ? noteEl.value : "";
  try { await api(`/api/signals/${id}/attribution`, { status, notes }); }
  catch (e) { alert("Attribution failed: " + (e.message || e)); return; }
  loadTriage();
}

async function ecpaOverride(id, center) {
  const ok = confirm(
`ECPA / Wiretap Act warning (18 U.S.C. §2511)

Decoding the CONTENT of a communication that is NOT your client's own may be unlawful interception. Proceed ONLY for a signal you have a lawful basis to decode — the client's own space or device.

This override will be LOGGED with your acknowledgment. Continue?`);
  if (!ok) return;
  const reason = prompt("Reason / lawful basis for decoding this signal (recorded in the audit log):", "");
  if (!reason) { alert("A reason is required — override NOT logged."); return; }
  try {
    const row = await api("/api/decode/override",
      { ecpa_ack: true, reason, signal_id: id, center_mhz: center });
    alert("Override logged (audit id " + row.id + "). You may now decode this signal in the monitor.");
  } catch (e) { alert("Override failed: " + (e.message || e)); }
}

async function triageBulk(status, notes, predicate) {
  const d = App.triage;
  if (!d || !d.signals.length) return alert("Load a sweep first.");
  const ids = triageFilteredSignals(d.signals)
    .filter(s => (s.triage_status || "unreviewed") === "unreviewed")
    .filter(s => !predicate || predicate(s))
    .map(s => s.id);
  if (!ids.length) return alert("No unreviewed signals match.");
  if (!confirm(`Mark ${ids.length} signal(s) as ${TRIAGE_LABELS[status] || status}?`)) return;
  $("triage-bulk-msg").textContent = "updating…";
  try {
    const r = await api("/api/triage/bulk", { signal_ids: ids, status, notes });
    $("triage-bulk-msg").textContent = `✓ updated ${r.updated}`;
    loadTriage();
  } catch (e) {
    $("triage-bulk-msg").textContent = "FAILED: " + (e.message || e);
  }
}

function triageIsmCandidate(s) {
  return s.classification === "PLAUSIBLE" && s.caution;
}

async function maybeOpenTriageAfterSweep(sweepId) {
  if (sweepId && App._triagePromptedSweep === sweepId) return;
  try {
    const url = sweepId ? `/api/triage/last?sweep_id=${sweepId}` : "/api/triage/last";
    const d = await api(url);
    if (!d.signals.length || !d.remaining_unexplained) return;
    App._triagePromptedSweep = d.sweep_id;
    const flash = $("triage-flash");
    if (flash) {
      flash.style.display = "";
      flash.textContent =
        `Interior sweep finished — ${d.remaining_unexplained} unexplained signal(s) need review.`;
    }
    show("triage");
    App.triage = d;
    App.triageRemaining = d.remaining_unexplained;
    triagePopulateRooms(d.signals);
    $("triage-summary").textContent =
      `sweep #${d.sweep_id} · ${d.signals.length} unexplained · ${d.remaining_unexplained} left to review`;
    $("triage-gate").innerHTML =
      `<span style="color:#e0a030">⏳ ${d.remaining_unexplained} signal(s) still unreviewed — the review is INCOMPLETE.</span>`;
    renderTriageTable();
  } catch (e) {}
}

// Open the monitor on a triaged signal and auto-run ANALYZE so the operator sees
// WHAT it is (receive + identify), not just a power trace.
function monitorFreq(center, label, sid, attr) {
  listenAt(center, label, undefined, sid != null ? sid : null, attr || null);
  setTimeout(analyzeSignal, 400);
}

// Attribution controls in the MONITOR panel — identify AND attribute a signal in
// one place (only shown when the panel was opened from a detected signal w/ an id).
function renderInspAttribution() {
  const el = $("insp-attribution");
  if (!el) return;
  if (App.inspSignalId == null) { el.style.display = "none"; return; }
  const cur = App.inspAttribution || "unassessed";
  const btns = Object.keys(ATTRIBUTION_LABELS).map(k =>
    `<button class="btn ${cur === k ? "primary" : ""}" style="padding:2px 6px;font-size:11px" ` +
    `title="${esc(ATTRIBUTION_TITLES[k])}" onclick="setInspAttribution('${k}')">${ATTRIBUTION_LABELS[k]}</button>`).join(" ");
  el.innerHTML = `<span class="note">About the client?</span> ${btns} ` +
    `<button class="btn" style="padding:2px 6px;font-size:11px" ` +
    `title="Decode this signal's CONTENT — lawful only for the client's own space/device; logs an ECPA acknowledgment" ` +
    `onclick="ecpaOverride(${App.inspSignalId}, ${App.inspCenter})">⚖ decode…</button>` +
    (cur !== "unassessed"
      ? `<span class="note"${cur === "client_space" ? ' style="color:var(--red)"' : ''}> · ${esc(ATTRIBUTION_LABELS[cur])}</span>`
      : "");
  el.style.display = "";
}

async function setInspAttribution(status) {
  if (App.inspSignalId == null) return;
  try { await api(`/api/signals/${App.inspSignalId}/attribution`, { status }); }
  catch (e) { alert("Attribution failed: " + (e.message || e)); return; }
  App.inspAttribution = status;
  renderInspAttribution();
  if (App.triage && App.triage.signals) loadTriage();   // keep the triage tab in sync
}
if ($("btn-triage-load")) $("btn-triage-load").onclick = () => {
  const flash = $("triage-flash");
  if (flash) { flash.style.display = "none"; flash.textContent = ""; }
  loadTriage();
};
["triage-filter-novel", "triage-filter-score", "triage-filter-room", "triage-filter-status"]
  .forEach(id => { const el = $(id); if (el) el.onchange = el.oninput = renderTriageTable; });
if ($("btn-triage-bulk-clear")) {
  $("btn-triage-bulk-clear").onclick = () =>
    triageBulk("cleared", "bulk: cleared (visible filter)", null);
}
if ($("btn-triage-bulk-ism")) {
  $("btn-triage-bulk-ism").onclick = () =>
    triageBulk("cleared", "bulk: plausible ISM/caution allocation — expected environmental",
               triageIsmCandidate);
}

// 256-entry RGB heat LUT (same curve as the old heat()), for fast per-texel paint.
const HEAT = (() => {
  const a = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    a[i * 3]     = 255 * Math.min(1, Math.max(0, 1.6 * t - 0.5));
    a[i * 3 + 1] = 255 * Math.min(1, Math.max(0, t < 0.5 ? 1.8 * t : 2.1 - 2.0 * t));
    a[i * 3 + 2] = 255 * Math.min(1, Math.max(0, 1.1 - 1.8 * t));
  }
  return a;
})();

const WF_ROWS = 240;   // client waterfall history depth (matches listen_wf_history_rows)

function wfEnsure(cols) {
  if (App.wf && App.wf.cols === cols) return App.wf;
  const off = document.createElement("canvas");
  off.width = cols; off.height = WF_ROWS;
  const octx = off.getContext("2d");
  octx.fillStyle = "#04070c"; octx.fillRect(0, 0, cols, WF_ROWS);
  App.wf = { cols, off, octx, line: octx.createImageData(cols, 1) };
  return App.wf;
}

// Scrolling waterfall: keep an offscreen texture (one texel per freq-bin x time
// row); each frame shift it up by the number of NEW rows, paint only the new rows
// from the heat LUT, then blit it scaled+smoothed onto the visible canvas. Polls
// faster than the server publishes, so a `seq` de-dupe skips re-scrolling a frame
// we already drew. A payload with no seq (POCSAG's one-shot /api/inspect) is a
// full paint that fills the panel. Re-tune resets the texture (clearInspectCanvases).
function drawWaterfall(cv, wf) {
  const { ctx, W, H } = fitCanvas(cv);
  const rows = wf.new_rows || wf.db;
  const nNew = rows ? rows.length : 0, cols = nNew ? rows[0].length : 0;
  if (!nNew || !cols) return;
  const vmin = wf.vmin, span = Math.max(wf.vmax - vmin, 1);
  const paintRow = (data, base, row) => {
    for (let c = 0; c < cols; c++) {
      let idx = ((row[c] - vmin) / span * 255) | 0;
      idx = idx < 0 ? 0 : idx > 255 ? 255 : idx;
      data[base + c * 4] = HEAT[idx * 3]; data[base + c * 4 + 1] = HEAT[idx * 3 + 1];
      data[base + c * 4 + 2] = HEAT[idx * 3 + 2]; data[base + c * 4 + 3] = 255;
    }
  };
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
  if (wf.seq == null) {                          // POCSAG one-shot: paint all rows
    const img = ctx.createImageData(cols, nNew);
    for (let r = 0; r < nNew; r++) paintRow(img.data, r * cols * 4, rows[r]);
    const tmp = document.createElement("canvas"); tmp.width = cols; tmp.height = nNew;
    tmp.getContext("2d").putImageData(img, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(tmp, 0, 0, cols, nNew, 0, 0, W, H);
    return;
  }
  const w = wfEnsure(cols);
  if (wf.seq !== App.wfSeq) {                     // fresh frame -> scroll in new rows
    const take = Math.min(nNew, WF_ROWS);
    w.octx.drawImage(w.off, 0, take, cols, WF_ROWS - take, 0, 0, cols, WF_ROWS - take);
    for (let r = 0; r < take; r++) {
      paintRow(w.line.data, 0, rows[nNew - take + r]);
      w.octx.putImageData(w.line, 0, WF_ROWS - take + r);
    }
    App.wfSeq = wf.seq;
  }
  ctx.clearRect(0, 0, W, H);
  ctx.drawImage(w.off, 0, 0, cols, WF_ROWS, 0, 0, W, H);
  if (wf.freqs_mhz && wf.freqs_mhz.length > 1)
    drawDemodMarker(ctx, W, H, wf.freqs_mhz[0], wf.freqs_mhz[wf.freqs_mhz.length - 1]);
}

// Next/Prev only step the baseline list (hidden in any other context).
$("insp-next").onclick = () => {
  if (App.inspContext === "baseline") openInspect((App.inspIdx ?? 0) + 1);
  else if (App.inspContext === "hunt") huntStep((App.huntIdx ?? 0) + 1);
};
$("insp-prev").onclick = () => {
  if (App.inspContext === "baseline") openInspect((App.inspIdx ?? 0) - 1);
  else if (App.inspContext === "hunt") huntStep((App.huntIdx ?? 0) - 1);
};
if ($("insp-auto")) $("insp-auto").onclick = toggleAuto;
if ($("btn-hunt")) $("btn-hunt").onclick = jumpFirstUnknown;
// Mode <select> re-tune works from any list: re-tune the SAME center with the
// chosen demod. In baseline context re-run openInspect (keeps the m/n title);
// otherwise re-tune the current center via the core entry point.
$("insp-mode").onchange = () => {
  const mode = $("insp-mode").value;
  if (App.inspContext === "baseline") openInspect(App.inspIdx ?? 0, mode);
  else if (App.inspCenter != null) inspectByFreq(App.inspCenter, App.inspLabel, mode);
};
$("insp-close").onclick = () => {
  stopAuto();
  stopSpectrumPoll();
  listenStop();
  $("inspect-panel").style.display = "none";
  const a = $("insp-audio"); a.pause(); a.removeAttribute("src"); a.load();
};

// On resize, drop the waterfall texture so the next poll rebuilds it at the new
// width (fitCanvas already re-sizes the visible canvas each frame).
window.addEventListener("resize", () => {
  clearTimeout(App._wfRz);
  App._wfRz = setTimeout(() => {
    const p = $("inspect-panel");
    if (p && p.style.display !== "none") { App.wf = null; App.wfSeq = -1; }
  }, 150);
});

/* ---------- ETA previews ---------- */
const CUSTOM_PANELS = [
  ["ambient-profile", "ambient-minutes", "custom-ambient"],
  ["bug-profile", "bug-minutes", "custom-bug"],
  ["profile-L", "minutes-L", "custom-L"],
  ["profile-R", "minutes-R", "custom-R"],
  ["profile-main", "minutes-main", "custom-main"],
];

function customTechFor(profileId) {
  // [seg, avg, maxhold, manual-advance checkbox, rbw]
  const map = {
    "ambient-profile": ["ambient-seg", "ambient-avg", "ambient-mh", "manual-ambient", "ambient-rbw"],
    "bug-profile": ["bug-seg", "bug-avg", "bug-mh", "manual-bug", "bug-rbw"],
    "profile-L": ["seg-L", "avg-L", "mh-L", "manual-L", "rbw-L"],
    "profile-R": ["seg-R", "avg-R", "mh-R", "manual-R", "rbw-R"],
    "profile-main": ["seg-main", "avg-main", "mh-main", "manual-main", "rbw-main"],
  };
  const ids = map[profileId] || [];
  const seg = $(ids[0]), avg = $(ids[1]), mh = $(ids[2]), man = $(ids[3]), rbw = $(ids[4]);
  const out = {};
  const sw = seg && parseFloat(seg.value);
  if (sw && sw >= 2) out.segment_width_mhz = sw;
  const ap = avg && parseFloat(avg.value);
  if (ap && ap >= 3) out.custom_avg_passes = Math.round(ap);
  const md = mh && parseFloat(mh.value);
  if (md && md >= 2) out.custom_maxhold_s = md;
  if (man && man.checked) out.manual_advance = true;   // F5 (server honors only for custom)
  const rk = rbw && parseFloat(rbw.value);
  if (rk && rk >= 1) out.rbw_khz = rk;                 // SDR-only; RFE ignores with a note
  return out;
}

function toggleCustomPanels(profileSel) {
  CUSTOM_PANELS.forEach(([pid, , panelId]) => {
    const panel = $(panelId);
    if (panel) panel.classList.toggle("show", $(pid) && $(pid).value === "custom");
  });
  if (profileSel) {
    const mins = { "ambient-profile": "ambient-minutes", "bug-profile": "bug-minutes",
      "profile-L": "minutes-L", "profile-R": "minutes-R", "profile-main": "minutes-main"
    }[profileSel.id];
    if (mins && $(mins)) $(mins).style.display = profileSel.value === "custom" ? "" : "none";
  }
}

function planQuery(profile, minutes, scanMode, profileId) {
  const tech = profileId ? customTechFor(profileId) : {};
  let q = `/api/plan?profile=${profile}&custom_minutes=${minutes || 45}` +
    `&scan_mode=${scanMode || "full"}`;
  if (tech.segment_width_mhz) q += `&segment_width_mhz=${tech.segment_width_mhz}`;
  if (tech.custom_avg_passes) q += `&custom_avg_passes=${tech.custom_avg_passes}`;
  if (tech.custom_maxhold_s) q += `&custom_maxhold_s=${tech.custom_maxhold_s}`;
  if (tech.rbw_khz) q += `&rbw_khz=${tech.rbw_khz}`;
  return q;
}

async function updateEta(profileSel, minutesInput, etaEl, scanMode) {
  const profile = profileSel.value;
  minutesInput.style.display = profile === "custom" ? "" : "none";
  toggleCustomPanels(profileSel);
  try {
    const plans = await api(planQuery(profile, minutesInput.value || 45, scanMode, profileSel.id));
    const totalMin = Object.values(plans).reduce((a, p) => a + p.estimated_minutes, 0);
    const segs = Object.values(plans).reduce((a, p) => a + p.segment_count, 0);
    // real per-segment RBW from the server (device-aware: SDR FFT+grid, or the
    // RFE's span/112) — replaces the old hardcoded span/112 which was wrong for SDRs
    const rbw = Object.values(plans).flatMap(p => p.segments.map(s => s.rbw_khz))
      .filter(v => v != null);
    const fixed = Object.values(plans).some(p => p.rbw_khz_requested && !p.rbw_adjustable);
    const lo = Math.min(...rbw), hi = Math.max(...rbw);
    etaEl.textContent = `est. ${totalMin.toFixed(0)} min total · ${segs} segments · ` +
      `RBW ${lo === hi ? lo.toFixed(1) : lo.toFixed(0) + "–" + hi.toFixed(0)} kHz/bin` +
      (fixed ? " (RBW fixed on the RF Explorer — use Seg MHz)" : "");
  } catch (e) { etaEl.textContent = e.message; }
}
const ETA_CARDS = [["ambient-profile", "ambient-minutes", "ambient-eta", "full"],
 ["profile-L", "minutes-L", "eta-L", "full"], ["profile-R", "minutes-R", "eta-R", "full"],
 ["profile-main", "minutes-main", "eta-main", "full"],
 ["bug-profile", "bug-minutes", "bug-eta", "bug"]];
ETA_CARDS.forEach(([p, m, e, mode]) => {
  if (!$(p)) return;
  $(p).onchange = () => updateEta($(p), $(m), $(e), mode);
  $(m).oninput = () => updateEta($(p), $(m), $(e), mode);
});
["ambient-seg", "ambient-avg", "ambient-mh", "ambient-rbw", "bug-seg", "bug-avg", "bug-mh", "bug-rbw",
 "seg-L", "avg-L", "mh-L", "rbw-L", "seg-R", "avg-R", "mh-R", "rbw-R",
 "seg-main", "avg-main", "mh-main", "rbw-main"].forEach(id => {
  const el = $(id);
  if (el) el.oninput = () => ETA_CARDS.forEach(([p, m, e, mode]) => {
    if ($(p)) updateEta($(p), $(m), $(e), mode);
  });
});

/* ---------- antenna prompt + sweep sequences ---------- */
function prompt2(msg, sub) {
  return new Promise((resolve) => {
    $("overlay-msg").textContent = msg;
    $("overlay-sub").textContent = sub || "";
    $("overlay").classList.add("show");
    $("overlay-confirm").onclick = () => { $("overlay").classList.remove("show"); resolve(true); };
    $("overlay-cancel").onclick = () => { $("overlay").classList.remove("show"); resolve(false); };
  });
}

async function startPhase(phase, module, room, profile, minutes, scanMode, profileId) {
  App.overlayHidden = false;
  App.expandedGroups = {};
  App._lastLabelKey = "";
  App.focusAnomalies = false; App.mutedKeys = {};   // F3: each sweep starts un-dampened
  syncFocusButton();
  App.wantWake = true; acquireWakeLock();   // F1: keep the screen on for the whole sweep
  const body = { phase, module, room, profile,
    custom_minutes: minutes, scan_mode: scanMode || "full" };
  Object.assign(body, customTechFor(profileId || ""));
  await api("/api/sweep/start", body);
}

/* sequence = remaining steps after the current sweep finishes */
async function advanceSeq() {
  const seq = App.seq;
  if (!seq || !seq.steps.length) { App.seq = null; return; }
  App.seq = null;                       // pause advancement while prompting
  const step = seq.steps.shift();
  const ok = await prompt2(step.msg, step.sub);
  if (!ok) return;
  try {
    await startPhase(step.phase, step.module, step.room, seq.profile, seq.minutes,
      seq.scanMode, seq.profileId);
    App.seq = seq.steps.length ? seq : null;
  } catch (e) {
    // never fail silently — the operator is standing there holding an antenna
    alert("SWEEP DID NOT START: " + e.message +
          "\n\nFix the issue (e.g. reconnect device on the Session tab), then " +
          "press the phase START button again.");
    $("ft-msg").textContent = "sweep start failed: " + e.message;
  }
}

function modulesAvailable() {
  const dev = App.state && App.state.device;
  return dev ? dev.modules.map(m => m.side) : [];
}

// SDR-family sources: radios that can capture raw IQ (tune/listen/analyze/
// identify). The RF Explorer never can.
const SDR_SOURCES = new Set(["pluto", "b220"]);

// true when the CONNECTED radio is the single-RX Pluto (drives antenna-prompt
// wording). Keyed off the device driver so it's correct regardless of how the
// source was picked; falls back to the selected source before connect.
function isPlutoDevice() {
  const dev = App.state && App.state.device;
  if (dev && dev.driver) return dev.driver === "pluto";
  return App.state && App.state.source === "pluto";
}

// true when the connected (or picked) radio is ANY IQ-capable SDR.
function isSdrDevice() {
  const dev = App.state && App.state.device;
  if (dev && dev.driver) return SDR_SOURCES.has(dev.driver);
  return !!(App.state && SDR_SOURCES.has(App.state.source));
}

// A wideband SDR sweeps the WHOLE range on ONE port — the RF Explorer is the
// only device whose two "modules" are actually different frequency bands. The
// B220's two ports (RX-A/RX-B) BOTH cover 70–6000 MHz (they're for direction-
// finding, not band coverage), so sweeping "left then right" just re-sweeps the
// same range. Detect that so the B220 does one sweep, no antenna-port dance.
function isSingleRangeSweep() {
  const dev = App.state && App.state.device;
  const mods = (dev && dev.modules) || [];
  if (mods.length <= 1) return true;
  const lo = Math.min(...mods.map(m => m.min_mhz));
  const hi = Math.max(...mods.map(m => m.max_mhz));
  const span = (hi - lo) || 1;
  // every port covers ~the whole span → one wideband sweep (B220);
  // ports covering distinct sub-bands → a real multi-module sweep (RF Explorer)
  return mods.every(m => (m.max_mhz - m.min_mhz) >= span * 0.8);
}
// The RX port to sweep on for a single-range device: RX-A = side "right" (chan 0
// per uhd_source SIDE_TO_CHAN), else whatever the one module is.
function primarySweepPort() {
  const mods = modulesAvailable();
  return mods.includes("right") ? "right" : (mods[0] || "right");
}

// F4: true when the device sweeps as one wideband range (Pluto, or B220's
// same-range ports) → one "Main sweep" step, no LEFT/RIGHT port dance.
function isSingleRx() {
  const dev = App.state && App.state.device;
  if (dev && dev.modules && dev.modules.length) return isSingleRangeSweep();
  const sel = $("device-source");
  const src = (sel && sel.value) || (App.state && App.state.source);
  return src === "pluto" || src === "b220";
}

// F2: paint the per-segment expected (looking…→found) + anomaly side panel.
// Idempotent — writes into fixed nodes every poll. Data comes from
// st.sweep.current_segment.expected and st.sweep.anomalies_found.
function renderSegPanel(st) {
  const expEl = $("seg-expected");
  if (!expEl) return;
  const cs = st.sweep.current_segment;
  const rangeEl = $("seg-range");
  if (rangeEl) rangeEl.textContent = cs && cs.low_mhz != null
    ? `${cs.low_mhz.toFixed(1)}–${cs.high_mhz.toFixed(1)} MHz` : "";
  const exp = (cs && cs.expected) || [];
  if (!exp.length) {
    const baselineBuilt = (st.baseline && st.baseline.result) || (App.baseline && App.baseline.count);
    expEl.innerHTML = `<div class="note" style="opacity:.6">` +
      (baselineBuilt ? "nothing specific expected in this band" : "build the baseline to see expected signals") +
      `</div>`;
  } else {
    expEl.innerHTML = exp.map(e => {
      const key = "m" + e.center_mhz.toFixed(3);
      const muted = !!App.mutedKeys[key];
      const found = e.status === "found";
      return `<div class="seg-item ${found ? "found" : "looking"}${muted ? " muted" : ""}">` +
        `<span class="ic">${found ? "✓" : "…"}</span>` +
        `<span class="f">${fmtF(e.center_mhz)}</span>` +
        `<span class="n">${esc(e.label || e.service || "")}</span>` +
        `<span class="mute" data-mk="${key}" data-lo="${e.freq_low_mhz}" data-hi="${e.freq_high_mhz}" ` +
        `title="dampen this signal on the chart">${muted ? "🔈" : "🔇"}</span></div>`;
    }).join("");
  }
  const anoms = st.sweep.anomalies_found || [];
  const cEl = $("seg-anom-count");
  if (cEl) cEl.textContent = anoms.length ? `(${anoms.length})` : "";
  const anEl = $("seg-anomalies");
  if (anEl) anEl.innerHTML = anoms.slice(-40).reverse().map(a =>
    `<div class="seg-item anom freqlink" data-center="${a.center_mhz}" ` +
    `data-label="${esc(a.label || a.reason || "anomaly")}" ` +
    `title="click to monitor this signal (splits the screen)">` +
    `<span class="f">${fmtF(a.center_mhz)}</span>` +
    `<span class="n">${esc(a.label || a.reason || "")}</span></div>`).join("");
}

/* ---------- F6: sustained-segment info panes ---------- */
function setInfoTab(name) {
  App.infoTab = name;
  document.querySelectorAll("#sweep-info-tabs button[data-tab]").forEach(b =>
    b.classList.toggle("on", b.dataset.tab === name));
  ["signals", "live", "expected", "catalog"].forEach(k => {
    const p = $("pane-" + k);
    if (p) p.style.display = k === name ? "" : "none";
  });
  if (name !== "live") hideLiveShade();
}

function hideLiveShade() {
  const el = $("live-shade");
  if (el) el.style.display = "none";
}

/* LIVE pane: shade the chosen window on the main chart and re-render just that
   span, live, in the table slot. Window defaults to the whole segment. */
function renderLiveZoom(t) {
  const c = $("livezoom");
  if (!c || !t) return;
  const lo = t.low, hi = t.high;
  let w = App.liveWindow;
  if (!w || w.hi <= lo || w.lo >= hi) w = { lo, hi };
  const chart = $("livechart"), shade = $("live-shade");
  if (chart && shade && hi > lo) {
    const W = chart.clientWidth;
    const x1 = Math.max(0, (w.lo - lo) / (hi - lo)) * W;
    const x2 = Math.min(1, (w.hi - lo) / (hi - lo)) * W;
    shade.style.display = "";
    shade.style.left = x1 + "px";
    shade.style.width = Math.max(2, x2 - x1) + "px";
  }
  const f = t.freqs || [], idx = [];
  for (let i = 0; i < f.length; i++) if (f[i] >= w.lo && f[i] <= w.hi) idx.push(i);
  if (idx.length < 2) return;
  const scale = App._traceScale;   // livechart's labels anchor to this — restore after
  const slice = arr => (arr ? idx.map(i => arr[i]) : null);
  drawTrace(c, { freqs: idx.map(i => f[i]), amps: slice(t.amps),
                 avg: slice(t.avg), maxpeak: slice(t.maxpeak) });
  App._traceScale = scale;
}

function renderExpandedExpected(st) {
  const el = $("tbl-seg-expected");
  if (!el) return;
  const cs = st.sweep.current_segment;
  const exp = (cs && cs.expected) || [];
  el.innerHTML = "<tr><th>Status</th><th>Frequency</th><th>Label / service</th><th>Band</th></tr>" +
    (exp.length ? exp.map(e =>
      `<tr><td>${e.status === "found" ? '<span class="pill ok">RECEIVED</span>'
                                       : '<span class="pill SCANNING">LOOKING…</span>'}</td>` +
      `<td>${fmtF(e.center_mhz)}</td><td>${esc(e.label || e.service || "")}</td>` +
      `<td>${e.freq_low_mhz != null ? fmtF(e.freq_low_mhz) + " – " + fmtF(e.freq_high_mhz) : ""}</td></tr>`
    ).join("") :
    `<tr><td colspan="4" class="note">nothing specific expected in this band</td></tr>`);
}

/* BUG CATALOG pane: which covert-device classes are known to transmit inside
   the sustained segment — pulled from the surveillance catalog, cached per segment. */
async function maybeLoadCatalog(cs) {
  if (!cs || cs.low_mhz == null) return;
  const key = cs.low_mhz + "-" + cs.high_mhz;
  if (App._catKey === key) return;
  App._catKey = key;
  const el = $("catalog-body");
  if (!el) return;
  el.innerHTML = `<p class="note" style="padding:6px 9px">checking surveillance catalog…</p>`;
  try {
    const r = await api(`/api/catalog/segment?low=${cs.low_mhz}&high=${cs.high_mhz}`);
    if (!r.classes || !r.classes.length) {
      el.innerHTML = `<p class="note" style="padding:6px 9px">no catalogued covert-device class uses ${cs.low_mhz.toFixed(1)}–${cs.high_mhz.toFixed(1)} MHz.</p>`;
      return;
    }
    // BAND intel, not a verdict on any one signal. Without this header the list
    // reads as "the thing you just clicked is a covert body transmitter" — which
    // is how it read to an operator who had ANALYZEd a licensed public-safety
    // record and scrolled to the bottom of the page. These classes are selected
    // by BAND OVERLAP alone; nothing here has looked at the selected signal.
    el.innerHTML =
      `<p class="note" style="padding:7px 9px;border-bottom:1px solid #16202b">` +
      `<b>Covert-device classes whose bands overlap ` +
      `${cs.low_mhz.toFixed(1)}–${cs.high_mhz.toFixed(1)} MHz.</b> This is about the ` +
      `BAND, not about any signal you have selected — these bands are shared with ` +
      `licensed land-mobile, wireless mics, ISM and cordless devices. Presence here ` +
      `is not evidence about a particular emitter.</p>` +
      r.classes.map(c =>
      `<div class="cat-item"><b>${esc(c.device_class)}</b>` +
      `<div class="note">${c.signatures.map(s =>
        `${fmtF(s.freq_min_mhz)}–${fmtF(s.freq_max_mhz)}` +
        (s.class ? ` · ${esc(s.class)}` : "") + (s.duty ? ` · ${esc(s.duty)}` : "")
      ).join(" &nbsp;·&nbsp; ")}</div>` +
      (c.confirm_steps && c.confirm_steps.length
        ? `<div class="note" style="color:var(--cyan)">confirm: ${esc(c.confirm_steps[0])}</div>` : "") +
      (c.false_positive_overlap && c.false_positive_overlap.length
        ? `<div class="note">might innocently be: ${esc(c.false_positive_overlap.join("; "))}</div>` : "") +
      `</div>`).join("");
  } catch (e) {
    App._catKey = null;
    el.innerHTML = `<p class="note" style="padding:6px 9px">catalog unavailable: ${esc(e.message || e)}</p>`;
  }
}

/* ---------- sigidwiki signal identification ---------- */
// A matched catalog signature carries its parent device class + how to confirm it
// + what it might innocently be. Render that as a compact profile card (empty for
// plain sigidwiki entries, which have no device_class).
function catalogDetail(c) {
  if (!c || !c.device_class) return "";
  const steps = (c.confirm_steps || []).length
    ? `<div style="margin-top:5px"><b>How to confirm:</b><ul style="margin:3px 0 0 16px;padding:0">`
      + c.confirm_steps.map(x => `<li>${esc(x)}</li>`).join("") + `</ul></div>` : "";
  const fps = (c.false_positive_overlap || []).length
    ? `<div style="margin-top:5px"><b>Might innocently be:</b> <span class="note">`
      + c.false_positive_overlap.map(esc).join("; ") + `</span></div>` : "";
  return `<div class="card" style="margin-top:8px;padding:10px;background:var(--panel2)">`
    + `<div><span class="pill ANOMALY">DEVICE</span> <b>${esc(c.device_class)}</b></div>`
    + steps + fps + `</div>`;
}

function renderIdentify(r) {
  let html = "";
  const f = r.features;
  if (f) {
    html += `<p class="note">Measured from IQ: SNR ${f.snr_db} dB, ~${f.occupied_bw_khz} kHz, `
      + `${f.continuity}, class <b>${esc(f.modulation_class)}</b>`
      + (f.fsk_states >= 2 ? `, ${f.fsk_states}-state FSK shift ${f.fsk_shift_khz} kHz` : "")
      + `, spectral flatness ${f.spectral_flatness}.</p>`;
  }
  if (r.pocsag_likely) {
    html += `<p class="note" style="color:var(--cyan)">Looks like a <b>POCSAG pager</b> `
      + `(2-FSK at a paging baud). Tune it in the inspect panel and choose `
      + `<b>POCSAG (decode)</b> mode to read the messages.</p>`;
  }
  if (!r.candidates || !r.candidates.length) {
    $("id-results").innerHTML = html + "<p class='note'>No candidate matches.</p>";
    return;
  }
  html += "<table><tr><th>Candidate</th><th>Score</th><th>Mode</th><th>Why it matched</th><th></th></tr>";
  for (const c of r.candidates) {
    html += `<tr><td>${esc(c.name)}`
      + (c.device_class ? `<br><span class="note">${esc(c.device_class)}</span>` : "")
      + `</td><td>${c.score}</td><td>${esc(c.mode)}</td>`
      + `<td class="note">${c.reasons.map(esc).join("; ")}`
      + (c.tscm ? `<br><b>TSCM:</b> ${esc(c.tscm)}` : "") + "</td>"
      + `<td><a href="${esc(c.url)}" target="_blank" rel="noopener">ref ↗</a></td></tr>`;
  }
  const topCat = r.candidates.find(c => c.device_class);
  $("id-results").innerHTML = html + "</table>" + (topCat ? catalogDetail(topCat) : "");
}

$("btn-identify").onclick = async () => {
  const c = parseFloat($("id-center").value);
  if (!c) return alert("Enter a center frequency in MHz (e.g. 433.92).");
  const bw = parseFloat($("id-bw").value);
  $("id-msg").textContent = isSdrDevice() ? "capturing IQ & analyzing…" : "matching…";
  $("id-results").innerHTML = "";
  try {
    const r = await api("/api/identify", { center_mhz: c, bw_khz: isNaN(bw) ? null : bw });
    $("id-msg").textContent = r.note || (r.features ? "identified from live IQ" : "");
    renderIdentify(r);
  } catch (e) { $("id-msg").textContent = "FAILED: " + e.message; }
};

// Type a frequency -> hear it. The box only fed IDENTIFY before, and the
// inspect panel's own tuner is display:none until something else tunes it, so
// there was no way to type a frequency and listen. Enter/blur commits too.
function tuneTypedFreq() {
  const c = parseFloat($("id-center").value);
  if (isNaN(c) || c <= 0) {
    $("id-msg").textContent = "enter a frequency in MHz, e.g. 433.92";
    return;
  }
  $("id-msg").textContent = "tuning " + c.toFixed(4) + " MHz…";
  listenAt(c, "manual tune");        // 2 args -> no signal id, correctly unlinked
}
if ($("btn-listen-freq")) $("btn-listen-freq").onclick = tuneTypedFreq;
if ($("id-center")) $("id-center").addEventListener("change", tuneTypedFreq);

function moduleRange(side) {
  const dev = App.state && App.state.device;
  const m = dev && dev.modules.find(x => x.side === side);
  return m ? `${fmtF(m.min_mhz)} – ${fmtF(m.max_mhz)}` : "";
}

/* a finished sweep should never just sit there — point the operator at the next
   step in the workflow (ambient → interior → review → directional → report). */
async function startPostSweep(sweepId, force = false) {
  if (!sweepId || (App._postStartedSweep === sweepId && !force)) return;
  App._postStartedSweep = sweepId;
  try {
    const ps = await api("/api/post_sweep/start", { sweep_id: sweepId, force });
    App.postSweep = ps; renderPostSweep(ps);
  } catch (e) {
    App._postStartedSweep = null;
    if ($("post-review-msg")) $("post-review-msg").textContent =
      "Could not start automatic review: " + e.message;
  }
}

function postSweepAction(ps) {
  if (!ps || !ps.prompt) return;
  if (ps.prompt.kind === "connect_second_radio") startPostSweep(ps.sweep_id, true);
  else if (ps.prompt.kind === "directional") show("directional");
  else show("results");
}

function renderPostSweep(ps) {
  const bar = $("post-review-bar"); if (!bar) return;
  if (!ps || ps.stage === "idle") { bar.className = "post-review-bar"; return; }
  const cls = ps.running ? "running" : ps.stage === "complete" ? "complete" :
    ps.stage === "needs_radio" ? "needs-radio" : ps.stage === "error" ? "error" : "";
  bar.className = "post-review-bar show " + cls;
  $("post-review-label").textContent = ps.running ? "AUTOMATIC SIGNAL REVIEW" :
    ps.stage === "needs_radio" ? "SECOND-RADIO CHECK NEEDED" :
      ps.stage === "error" ? "AUTOMATIC REVIEW FAILED" : "AUTOMATIC REVIEW COMPLETE";
  $("post-review-msg").textContent = ps.error || ps.message || "";
  const stagePct = { queued: 5, consolidating: 22, inventory: 38,
    characterizing: 55, needs_radio: 45, complete: 100, error: 100 };
  const pct = ps.total > 0 && ps.stage === "characterizing" ?
    45 + 50 * (ps.progress || 0) / ps.total : (stagePct[ps.stage] || 0);
  $("post-review-fill").style.width = Math.max(0, Math.min(100, pct)) + "%";
  $("post-review-count").textContent = ps.total ?
    `${ps.progress || 0}/${ps.total} live checks` : "content-free RF analysis";
  const action = $("post-review-action");
  if (ps.prompt) {
    action.style.display = ""; action.textContent = ps.prompt.action || "OPEN";
    action.onclick = () => postSweepAction(ps);
  } else action.style.display = "none";
  if (ps.overview) renderAmbientOverview(ps.overview, ps.prompt);

  const key = `${ps.session_id}:${ps.sweep_id}:${ps.prompt ? ps.prompt.kind : ps.stage}`;
  if (key !== App._postPromptedKey && ps.stage === "needs_radio") {
    App._postPromptedKey = key;
    showNextBanner(
      `<b>Stored sweep review is complete.</b> Plug in the second SDR so the app can ` +
      `confirm unexplained signals without opening audio or message content.`,
      "SECOND RADIO CONNECTED - CHECK", () => postSweepAction(ps));
  } else if (key !== App._postPromptedKey && ps.stage === "complete" &&
             ps.overview && ps.overview.priority_count > 0) {
    App._postPromptedKey = key; const n = ps.overview.priority_count;
    showNextBanner(
      `<b>${n} substantive transmission${n === 1 ? "" : "s"} need localization.</b> ` +
      `Attach the directional antenna; the app will tell you when to switch to near field.`,
      "OPEN DIRECTIONAL GUIDE", () => show("directional"));
  } else if (key !== App._postPromptedKey && ps.stage === "characterizing" &&
             ps.prompt && ps.prompt.kind === "directional") {
    App._postPromptedKey = key;
    showNextBanner(
      `<b>A substantive live transmission is confirmed.</b> Direction finding can start now; ` +
      `the other radio will keep reducing the remaining watch-list noise in the background.`,
      "OPEN DIRECTIONAL GUIDE", () => show("directional"));
  }
}

function drawAmbientPie(categories) {
  const cv = $("ambient-pie"); if (!cv) return;
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const total = categories.reduce((n, c) => n + (c.count || 0), 0);
  const cx = W / 2, cy = H / 2, r = Math.min(W, H) * .43;
  if (!total) {
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = "#344150"; ctx.lineWidth = 28; ctx.stroke();
  } else {
    let start = -Math.PI / 2;
    categories.forEach(c => {
      if (!c.count) return;
      const end = start + Math.PI * 2 * c.count / total;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, start, end);
      ctx.closePath(); ctx.fillStyle = c.color; ctx.fill();
      ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--panel") || "#121922";
      ctx.lineWidth = 2; ctx.stroke(); start = end;
    });
  }
  ctx.beginPath(); ctx.arc(cx, cy, r * .52, 0, Math.PI * 2);
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--panel") || "#121922";
  ctx.fill(); ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--bright") || "#fff";
  ctx.textAlign = "center"; ctx.font = "bold 25px Consolas, monospace";
  ctx.fillText(String(total), cx, cy + 1);
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--dim") || "#7b8794";
  ctx.font = "11px Consolas, monospace"; ctx.fillText("LIKELY EMITTERS", cx, cy + 19);
}

function renderAmbientOverview(o, prompt) {
  if (!o || !$("ambient-overview")) return;
  $("ambient-overview-wait").style.display = "none";
  $("ambient-overview").style.display = "";
  const kpi = (value, label, color) =>
    `<span><b${color ? ` style="color:${color}"` : ""}>${value}</b> ` +
    `<span class="note">${label}</span></span>`;
  $("ambient-kpis").innerHTML =
    kpi(o.raw_detection_count || 0, "raw detections") +
    kpi(o.unique_emitter_count || 0, "likely emitters") +
    kpi(o.duplicate_reduction_count || 0, "repeat hits consolidated", "var(--cyan)") +
    kpi(o.client_accounted_count || 0, "client-accounted", "var(--green)") +
    kpi(o.priority_count || 0, "substantive", o.priority_count ? "var(--red)" : "var(--green)");
  const cats = o.categories || []; drawAmbientPie(cats);
  $("ambient-legend").innerHTML = cats.map(c =>
    `<div class="ambient-legend-row"><span class="ambient-swatch" style="background:${esc(c.color)}"></span>` +
    `<span>${esc(c.label)}</span><b>${c.count || 0}</b></div>`).join("");
  const p = $("ambient-priority");
  if (o.priority_count) {
    p.style.display = "";
    p.innerHTML = `<b>${o.priority_count} substantive transmission${o.priority_count === 1 ? "" : "s"}</b>` +
      (o.priority_emitters || []).slice(0, 5).map(g => {
        const b = g.best || {};
        return `<div style="margin-top:5px"><a href="#" class="freqlink" data-center="${b.center_mhz}" ` +
          `data-sid="${b.id || ""}">${fmtF(b.center_mhz)}</a> - ${esc(g.substantive_reason)}</div>`;
      }).join("");
  } else { p.style.display = "none"; p.innerHTML = ""; }
  $("ambient-method").textContent = o.method_note || "";
  const pr = $("ambient-prompt-row");
  if (prompt) {
    pr.style.display = "flex";
    $("ambient-prompt-title").textContent = prompt.title || "Next step";
    $("ambient-prompt-detail").textContent = prompt.detail || "";
    $("ambient-prompt-action").textContent = prompt.action || "CONTINUE";
    $("ambient-prompt-action").onclick = () => postSweepAction(App.postSweep);
  } else pr.style.display = "none";
}

function showNextBanner(msgHtml, label, go) {
  const b = $("next-step-banner");
  if (!b) return;
  $("next-step-msg").innerHTML = msgHtml;
  const goBtn = $("next-step-go");
  goBtn.textContent = label;
  goBtn.onclick = () => { b.classList.remove("show"); go(); };
  b.classList.add("show");
}
function suggestNextStep(phase) {
  if (phase === "ambient")
    showNextBanner("<b>✓ Ambient reference done.</b> Next: sweep inside the premises.",
      "START INTERIOR SWEEP", () => show(isSingleRx() ? "sweep" : "sweepL"));
  else if (phase === "interior")
    showNextBanner("<b>Interior sweep done.</b> Automatic RF-shape and repeat-hit review is running in the background.",
      "VIEW AUTOMATIC REVIEW", () => show("results"));
  else if (phase === "directional")
    showNextBanner("<b>✓ Directional pass done.</b> Next: generate the report.",
      "GENERATE REPORT", () => show("results"));
}
if ($("next-step-x")) $("next-step-x").onclick = () => $("next-step-banner").classList.remove("show");

$("btn-ambient").onclick = async () => {
  const mods = modulesAvailable();
  if (!mods.length) return alert("Connect the device first (Session tab).");
  const profile = $("ambient-profile").value, minutes = $("ambient-minutes").value;
  const steps = [];
  if (isSingleRangeSweep()) {
    // one wideband RX (Pluto, or B220's same-range ports) — one step, no dance
    const module = primarySweepPort();
    steps.push({
      phase: "ambient", module, room: "exterior",
      msg: isPlutoDevice() ? "ATTACH THE OMNI ANTENNA TO THE PLUTO RX SMA"
                           : "ATTACH THE OMNI ANTENNA TO THE RX-A PORT",
      sub: `Sweeps ${moduleRange(module)} — ambient reference, run OUTSIDE / vehicle at the curb` });
  } else {
    if (mods.includes("left")) steps.push({
      phase: "ambient", module: "left", room: "exterior",
      msg: "ATTACH THE NON-DIRECTIONAL (OMNI) ANTENNA TO THE LEFT SMA PORT",
      sub: `This port sweeps ${moduleRange("left")} — ambient reference, run OUTSIDE / vehicle at the curb` });
    if (mods.includes("right")) steps.push({
      phase: "ambient", module: "right", room: "exterior",
      msg: "MOVE THE OMNI ANTENNA TO THE RIGHT SMA PORT",
      sub: `This port sweeps ${moduleRange("right")} — ambient reference continues` });
  }
  if (!steps.length) return alert("No modules reported by device.");
  App.seq = { steps, profile, minutes, profileId: "ambient-profile" };
  advanceSeq();
};

$("btn-bugscan").onclick = async () => {
  const mods = modulesAvailable();
  if (!mods.length) return alert("Connect the device first (Session tab).");
  const profile = $("bug-profile").value, minutes = $("bug-minutes").value;
  const room = $("bug-room").value || "bug-scan";
  const steps = [];
  if (isSingleRangeSweep()) {
    const module = primarySweepPort();
    steps.push({ phase: "interior", module, room,
      msg: isPlutoDevice() ? "ATTACH THE OMNI ANTENNA TO THE PLUTO RX SMA"
                           : "ATTACH THE OMNI ANTENNA TO THE RX-A PORT",
      sub: `Bug-frequency scan — known surveillance bands within ${moduleRange(module)}` });
  } else {
    if (mods.includes("left")) steps.push({
      phase: "interior", module: "left", room,
      msg: "ATTACH THE NON-DIRECTIONAL (OMNI) ANTENNA TO THE LEFT SMA PORT",
      sub: `Bug-frequency scan — known surveillance bands within ${moduleRange("left")}` });
    if (mods.includes("right")) steps.push({
      phase: "interior", module: "right", room,
      msg: "MOVE THE OMNI ANTENNA TO THE RIGHT SMA PORT",
      sub: `Bug-frequency scan — known surveillance bands within ${moduleRange("right")}` });
  }
  if (!steps.length) return alert("No modules reported by device.");
  App.seq = { steps, profile, minutes, scanMode: "bug", profileId: "bug-profile" };
  advanceSeq();
};

$("btn-sweep-L").onclick = async () => {
  if (!modulesAvailable().includes("left"))
    return alert("LEFT module not reported by device — single-module mode; use Phase B (right). " +
                 "The coverage gap will be noted in the report.");
  const room = $("in-room-L").value || "unspecified";
  $("in-room-R").value = room;
  App.seq = { steps: [{ phase: "interior", module: "left", room,
    msg: "ATTACH THE NON-DIRECTIONAL (OMNI) ANTENNA TO THE LEFT SMA PORT",
    sub: `This port sweeps ${moduleRange("left")} — Phase A, room: ${room}` }],
    profile: $("profile-L").value, minutes: $("minutes-L").value,
    profileId: "profile-L" };
  advanceSeq();
};

$("btn-sweep-R").onclick = async () => {
  if (!modulesAvailable().includes("right"))
    return alert("RIGHT module not reported by device.");
  const room = $("in-room-R").value || $("in-room-L").value || "unspecified";
  App.seq = { steps: [{ phase: "interior", module: "right", room,
    msg: "MOVE THE OMNI ANTENNA TO THE RIGHT SMA PORT",
    sub: `This port sweeps ${moduleRange("right")} — Phase B, room: ${room}` }],
    profile: $("profile-R").value, minutes: $("minutes-R").value,
    profileId: "profile-R" };
  advanceSeq();
};

// F4: single-RX "Main sweep" — one wideband interior step, no SMA-port dance
// (mirrors the ambient flow's isPlutoDevice branch).
if ($("btn-sweep-main")) $("btn-sweep-main").onclick = async () => {
  const mods = modulesAvailable();
  if (!mods.length) return alert("Connect the device first (Session tab).");
  const room = $("in-room-main").value || "unspecified";
  const module = mods.includes("right") ? "right" : mods[0];
  App.seq = { steps: [{ phase: "interior", module, room,
    msg: "ATTACH THE OMNI ANTENNA TO THE RX SMA",
    sub: `Main sweep ${moduleRange(module)} — inside the premises, room: ${room}` }],
    profile: $("profile-main").value, minutes: $("minutes-main").value,
    profileId: "profile-main" };
  advanceSeq();
};

// F5: release the manual-advance gate
if ($("btn-next-segment")) $("btn-next-segment").onclick = () =>
  api("/api/sweep/next_segment", {});

// F3: toggle anomaly-focus dampening
function syncFocusButton() {
  const b = $("btn-focus-anomalies");
  if (!b) return;
  b.classList.toggle("on", App.focusAnomalies);
  b.textContent = App.focusAnomalies ? "🔊 SHOW ALL" : "🔇 FOCUS ANOMALIES";
}
if ($("btn-focus-anomalies")) $("btn-focus-anomalies").onclick = () => {
  App.focusAnomalies = !App.focusAnomalies;
  App._lastLabelKey = "";          // force label-layer rebuild
  syncFocusButton();
};
// F3: per-signal mute/un-mute from the expected panel
if ($("seg-expected")) $("seg-expected").onclick = (e) => {
  const m = e.target.closest(".mute");
  if (!m) return;
  const k = m.dataset.mk;
  if (App.mutedKeys[k]) delete App.mutedKeys[k];
  else App.mutedKeys[k] = { lo: parseFloat(m.dataset.lo) - 0.02,
                            hi: parseFloat(m.dataset.hi) + 0.02 };
  App._lastLabelKey = "";
};

/* F7: aborting is a decision — pause & save (default-safe) or confirmed erase */
$("btn-abort").onclick = () => $("abort-dialog").classList.add("show");
$("abort-cancel").onclick = () => $("abort-dialog").classList.remove("show");
$("abort-save").onclick = async () => {
  $("abort-dialog").classList.remove("show");
  await api("/api/sweep/abort", {});
};
$("abort-erase").onclick = async () => {
  $("abort-dialog").classList.remove("show");
  await api("/api/sweep/abort", { erase: true });
};
/* SAVE & CLOSE — "I'm done with this sweep". Same mechanic as ABORT PHASE (keep
   data): abort(erase=false) stops the radio and KEEPS everything recorded. The
   difference is intent and destination — this is the normal way to finish, and
   it backs out to the sweep list instead of leaving the operator parked on a
   finished sweep's screen. No confirm dialog: nothing is discarded. */
$("btn-save-close").onclick = async () => {
  const b = $("btn-save-close");
  b.disabled = true;
  const was = b.textContent;
  b.textContent = "saving…";
  try {
    if (App.state && App.state.sweep && App.state.sweep.running) {
      await api("/api/sweep/abort", {});
      // let the segment unwind so the list shows a settled status, not "running"
      for (let i = 0; i < 25; i++) {
        await new Promise(r => setTimeout(r, 400));
        let s = null;
        try { s = await api("/api/state"); } catch (e) { break; }
        if (!(s && s.sweep && s.sweep.running)) break;
      }
    }
  } catch (e) { /* already stopping — still back out and show what was saved */ }
  App.overlayHidden = true;
  $("sweep-overlay").classList.remove("show");
  MonitorDock.overlayClosed();
  listenStop();                    // don't leave audio running behind the list
  b.disabled = false;
  b.textContent = was;
  show("recent");
  showSweepsPane(App.openSessionId, "sweep saved — everything recorded was kept");
};

$("btn-hide-overlay").onclick = () => {
  App.overlayHidden = true; $("sweep-overlay").classList.remove("show");
  MonitorDock.overlayClosed();   // monitor re-docks on the page and keeps playing
};

/* ---------- rooms ---------- */
$("btn-next-room").onclick = () => {
  const room = $("in-next-room").value.trim();
  if (!room) return alert("Enter the room label first.");
  // F4: route to the single Main sweep on single-RX devices, else the LEFT/RIGHT flow
  if (isSingleRx()) { if ($("in-room-main")) $("in-room-main").value = room; show("sweep"); }
  else { $("in-room-L").value = room; $("in-room-R").value = room; show("sweepL"); }
};

async function loadRooms() {
  try {
    const r = await api("/api/rooms");
    if (!r.rooms.length) { $("tbl-rooms").innerHTML = "<tr><td>no interior sweeps yet</td></tr>"; return; }
    const head = "<tr><th>Frequency</th>" + r.rooms.map(x => `<th>${esc(x)}</th>`).join("") +
      "<th>Strongest</th><th>Δ dB</th></tr>";
    $("tbl-rooms").innerHTML = head + r.groups.map(g => {
      const cells = r.rooms.map(room => {
        const v = g.per_room[room];
        const max = Math.max(...Object.values(g.per_room));
        const cls = v != null && v === max && g.delta_db >= 10 ? ' class="score-hi"' : "";
        return `<td${cls}>${v != null ? v.toFixed(0) : "—"}</td>`;
      }).join("");
      return `<tr><td>${fmtF(g.center_mhz)}</td>${cells}` +
        `<td>${esc(g.strongest_room)}</td><td>${g.delta_db >= 10 ?
          `<span class="score-hi">+${g.delta_db}</span>` : "+" + g.delta_db}</td></tr>`;
    }).join("");
  } catch (e) { $("tbl-rooms").innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; }
}

/* ---------- directional ---------- */
App.dirShowAll = false;
const DIR_TRIAGE = 5;   // directional time is expensive — hunt only what scores highest
async function loadDirectional() {
  try { App.anomalies = await api("/api/anomalies"); } catch (e) { App.anomalies = []; }
  const overview = App.postSweep && App.postSweep.overview;
  const priority = ((overview && overview.priority_emitters) || []).map(g => ({
    ...(g.best || {}), substantive_reason: g.substantive_reason,
    detection_count: g.detection_count, sweep_count: g.sweep_count,
  }));
  App.dirQueue = App.dirShowAll ? App.anomalies :
    priority.length ? priority : (overview ? [] : App.anomalies.slice(0, DIR_TRIAGE));
  if (App.dirQueue.length) {
    const top = App.dirQueue[0];
    const hidden = Math.max(0, App.anomalies.length - App.dirQueue.length);
    $("dir-instruction").innerHTML =
      `<b style="color:var(--bright)">ATTACH THE DIRECTIONAL ANTENNA</b> and return to ` +
      `<b style="color:var(--amber)">${esc(top.strongest_room || top.room || "the premises")}</b>. ` +
      `Hunting ${priority.length ? "the substantive" : "the top-scored"} ${App.dirQueue.length} signal${App.dirQueue.length === 1 ? "" : "s"}` +
      (hidden > 0 ? ` (${hidden} lower-scored hidden — clear them via baseline ` +
        `sources or the community instead) ` +
        `<button class="btn" onclick="App.dirShowAll=true;loadDirectional()">SHOW ALL ${App.anomalies.length}</button>`
        : App.dirShowAll && App.anomalies.length > DIR_TRIAGE ?
          ` <button class="btn" onclick="App.dirShowAll=false;loadDirectional()">SHOW TOP ${DIR_TRIAGE}</button>` : "") +
      ` — click LOCK to hunt each one.`;
  } else {
    $("dir-instruction").textContent =
      "No unverified/flagged anomalies pending. Complete sweeps first — or all clear.";
  }
  $("tbl-dir-queue").innerHTML =
    "<tr><th>#</th><th>Score</th><th>Frequency</th><th>Class</th><th>Strongest room</th><th></th></tr>" +
    App.dirQueue.map((a, i) =>
      `<tr><td>${i + 1}</td><td class="${a.anomaly_score >= 60 ? "score-hi" : "score-md"}">${(a.anomaly_score || 0).toFixed(0)}</td>` +
      `<td>${fmtF(a.center_mhz)}</td><td><span class="pill ${a.classification}">${esc(statusLabel(a.classification))}</span></td>` +
      `<td>${esc(a.strongest_room || a.room || "")}</td>` +
      `<td><button class="btn" onclick="dirLock(${i})">LOCK</button></td></tr>`).join("");
  try {
    const log = await api("/api/directional/log");
    $("tbl-dir-log").innerHTML = "<tr><th>Time</th><th>Frequency</th><th>Room</th><th>Peak dBm</th><th>Notes</th></tr>" +
      log.map(d => `<tr><td>${new Date(d.ts_utc * 1000).toLocaleTimeString()}</td>` +
        `<td>${fmtF(d.center_mhz)}</td><td>${esc(d.room)}</td>` +
        `<td>${d.peak_dbm != null ? d.peak_dbm.toFixed(0) : ""}</td><td>${esc(d.notes)}</td></tr>`).join("");
  } catch (e) {}
  loadDFLog();
}

function armDirectionalAudio() {
  if (!App.dirAudioOn) return;
  try {
    if (!App._dirAudio) App._dirAudio = new (window.AudioContext || window.webkitAudioContext)();
    if (App._dirAudio.state === "suspended") App._dirAudio.resume();
  } catch (e) { App.dirAudioOn = false; }
}

function directionalBeep(frac) {
  if (!App.dirAudioOn || !App._dirAudio) return;
  const now = performance.now(), interval = 1050 - Math.max(0, Math.min(1, frac)) * 900;
  if (now - App._dirLastBeep < interval) return;
  App._dirLastBeep = now;
  try {
    const ctx = App._dirAudio, osc = ctx.createOscillator(), gain = ctx.createGain();
    osc.frequency.value = 430 + frac * 1050;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.11, ctx.currentTime + .008);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + .065);
    osc.connect(gain); gain.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + .07);
  } catch (e) {}
}

function updateDirectionalGuidance(d, frac) {
  const now = performance.now();
  App._dirReadings.push({ t: now, v: Number(d.current_dbm) });
  App._dirReadings = App._dirReadings.filter(x => now - x.t < 5000).slice(-6);
  const oldest = App._dirReadings[0], delta = oldest ? Number(d.current_dbm) - oldest.v : 0;
  const trend = $("dir-trend"), stage = $("dir-stage"), guide = $("dir-guidance");
  if ($("dir-strength-fill")) $("dir-strength-fill").style.width = (frac * 100).toFixed(0) + "%";
  if (Number(d.current_dbm) >= -35) {
    if (stage) stage.textContent = "SWITCH TO NEAR-FIELD PROBE";
    if (guide) guide.textContent = "Signal is hot. Reduce gain if it pins, then inspect objects and seams at close range.";
    if (trend) { trend.textContent = "HOT"; trend.style.color = "var(--red)"; }
  } else if (delta >= 1.5) {
    if (stage) stage.textContent = "DIRECTIONAL ANTENNA - getting warmer";
    if (guide) guide.textContent = `Continue this direction (${delta.toFixed(1)} dB stronger).`;
    if (trend) { trend.textContent = "↑"; trend.style.color = "var(--green)"; }
  } else if (delta <= -1.5) {
    if (stage) stage.textContent = "DIRECTIONAL ANTENNA - getting colder";
    if (guide) guide.textContent = `Reverse or rotate; the signal fell ${Math.abs(delta).toFixed(1)} dB.`;
    if (trend) { trend.textContent = "↓"; trend.style.color = "var(--amber)"; }
  } else {
    if (stage) stage.textContent = "DIRECTIONAL ANTENNA - pause and compare";
    if (guide) guide.textContent = "Reading is steady. Rotate slowly, then move a few feet and pause again.";
    if (trend) { trend.textContent = "→"; trend.style.color = "var(--dim)"; }
  }
  directionalBeep(frac);
}

if ($("btn-dir-audio")) $("btn-dir-audio").onclick = () => {
  App.dirAudioOn = !App.dirAudioOn;
  if (App.dirAudioOn) armDirectionalAudio();
  $("btn-dir-audio").textContent = App.dirAudioOn ? "GUIDANCE BEEP ON" : "GUIDANCE BEEP OFF";
};

window.dirLock = async (i) => {
  App.dirIndex = i;
  const a = App.dirQueue[i];
  if (!a) return;
  App._dirReadings = []; App._dirLastBeep = 0; armDirectionalAudio();
  try {
    await api("/api/directional/start", {
      center_mhz: a.center_mhz, bw_khz: a.bw_khz || 100,
      room: a.strongest_room || a.room || "" });
    $("dir-live").style.display = "";
    $("dir-notes").value = "";
  } catch (e) { alert(e.message); }
};
$("btn-dir-stop").onclick = async () => {
  await api("/api/directional/stop", {});
  setTimeout(loadDirectional, 800);
};
$("btn-dir-save").onclick = async () => {
  await api("/api/directional/stop", {});
  setTimeout(async () => {
    await api("/api/directional/note", { notes: $("dir-notes").value });
    loadDirectional();
  }, 900);
};
$("btn-dir-next").onclick = async () => {
  await api("/api/directional/stop", {});
  setTimeout(() => {
    const next = App.dirIndex + 1;
    if (next < App.dirQueue.length) window.dirLock(next);
    else loadDirectional();
  }, 900);
};

/* ---------- phase DF (B220 dual-RX) ---------- */
const DF_VERDICT_TEXT = {
  BEARING_OK: ["var(--green)", "bearing locked"],
  AMBIGUOUS_SPACING: ["var(--amber)", "spacing too wide — rotate for null"],
  LOW_COHERENCE: ["var(--amber)", "multipath — move & retry"],
  NO_SIGNAL: ["var(--red)", "nothing above the floor"],
};

function renderDFLive(d) {
  App._dfWasLive = true;
  const live = $("df-live"); if (live) live.style.display = "";
  if (d.error) { $("df-verdict").innerHTML = `<span style="color:var(--red)">capture error: ${esc(d.error)}</span>`; return; }
  const [col, txt] = DF_VERDICT_TEXT[d.verdict] || ["var(--dim)", d.verdict || ""];
  const bText = d.bearing_deg == null ? "—"
    : `${d.bearing_deg > 0 ? "+" : ""}${d.bearing_deg.toFixed(0)}°`;
  $("df-bearing").textContent = bText;
  $("df-bearing").style.color = col;
  $("df-verdict").innerHTML = `<b style="color:${col}">${esc(txt)}</b>` +
    (d.calibrated ? "" : ` <span style="color:var(--amber)">· UNCALIBRATED</span>`);
  $("df-stats").innerHTML =
    `<div>Frequency</div><div>${fmtF(d.center_mhz)}</div>` +
    `<div>Mirror bearing</div><div>${d.mirror_deg == null ? "—" : (d.mirror_deg > 0 ? "+" : "") + d.mirror_deg.toFixed(0) + "°"}</div>` +
    `<div>Coherence</div><div>${(d.coherence != null ? d.coherence.toFixed(2) : "—")} ${cohBar(d.coherence)}</div>` +
    `<div>SNR A / B</div><div>${d.snr_a_db != null ? d.snr_a_db.toFixed(0) : "—"} / ${d.snr_b_db != null ? d.snr_b_db.toFixed(0) : "—"} dB</div>` +
    `<div>Unambiguous ≤</div><div>${d.max_unambiguous_mhz != null ? d.max_unambiguous_mhz.toFixed(0) + " MHz" : "—"}</div>`;
  $("df-warn").innerHTML = (d.warnings || []).map(esc).join("<br>");
  drawCompass($("df-compass"), d);
}
function cohBar(c) {
  if (c == null) return "";
  const n = Math.round((c || 0) * 10);
  return `<span style="font-family:monospace;color:${c > 0.7 ? "var(--green)" : c > 0.5 ? "var(--amber)" : "var(--red)"}">${"█".repeat(n)}${"░".repeat(10 - n)}</span>`;
}
function drawCompass(cv, d) {
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, cx = W / 2, cy = H / 2, r = Math.min(cx, cy) - 14;
  ctx.clearRect(0, 0, W, H);
  // dial
  ctx.strokeStyle = "#26303d"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, 2 * Math.PI); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r);
  ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy); ctx.stroke();
  ctx.fillStyle = "#52708a"; ctx.font = "10px monospace"; ctx.textAlign = "center";
  ctx.fillText("BROADSIDE 0°", cx, cy - r - 4);
  ctx.fillText("bar axis", cx + r - 18, cy - 4);
  if (d.bearing_deg == null) return;
  // bearing needle (0° = straight up/broadside; + to the right)
  const needle = (deg, color, dash) => {
    const rad = (deg - 90) * Math.PI / 180;
    ctx.strokeStyle = color; ctx.lineWidth = 2.5;
    ctx.setLineDash(dash || []);
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(cx + r * Math.cos(rad), cy + r * Math.sin(rad)); ctx.stroke();
    ctx.setLineDash([]);
  };
  needle(d.bearing_deg, "#2ecc71");
  if (d.mirror_deg != null) needle(d.mirror_deg, "#f39c12", [5, 4]);   // mirror twin
  ctx.fillStyle = "#2ecc71"; ctx.beginPath(); ctx.arc(cx, cy, 4, 0, 2 * Math.PI); ctx.fill();
}
async function loadDFLog() {
  try {
    const log = await api("/api/df/log");
    const t = $("tbl-df-log"); if (!t) return;
    t.innerHTML = "<tr><th>Time</th><th>Frequency</th><th>Room</th><th>Bearing</th><th>Mirror</th><th>Coherence</th><th>Cal</th></tr>" +
      log.map(d => `<tr><td>${new Date(d.ts_utc * 1000).toLocaleTimeString()}</td>` +
        `<td>${fmtF(d.center_mhz)}</td><td>${esc(d.room || "")}</td>` +
        `<td>${d.bearing_deg != null ? (d.bearing_deg > 0 ? "+" : "") + d.bearing_deg.toFixed(0) + "°" : "—"}</td>` +
        `<td>${d.mirror_deg != null ? (d.mirror_deg > 0 ? "+" : "") + d.mirror_deg.toFixed(0) + "°" : "—"}</td>` +
        `<td>${d.coherence != null ? d.coherence.toFixed(2) : "—"}</td>` +
        `<td>${d.calibrated ? "✓" : "—"}</td></tr>`).join("");
  } catch (e) {}
}
if ($("btn-df-start")) $("btn-df-start").onclick = async () => {
  const c = parseFloat($("df-center").value);
  if (isNaN(c)) { $("df-msg").textContent = "enter a frequency in MHz"; return; }
  const spacing = parseFloat($("df-spacing").value) || 0.35;
  $("df-msg").textContent = "";
  try {
    await api("/api/df/start", { center_mhz: c, spacing_m: spacing,
      room: $("df-room").value || "" });
    $("df-live").style.display = "";
  } catch (e) { $("df-msg").textContent = e.message; }
};
if ($("btn-df-cal")) $("btn-df-cal").onclick = async () => {
  try { await api("/api/df/calibrate", {}); $("df-msg").textContent = "hold broadside — zeroing on the next good read"; }
  catch (e) { $("df-msg").textContent = e.message; }
};
if ($("btn-df-stop")) $("btn-df-stop").onclick = async () => {
  await api("/api/df/stop", {});
  setTimeout(loadDFLog, 600);
};

/* ---------- Signal ID chips + per-row identify ---------- */
function idChip(s) {
  if (!s.id_updated_utc && !s.id_top_name) {
    return `<button class="btn" style="padding:3px 8px;font-size:11px" onclick="identifyRow(${s.id},${s.center_mhz},${s.bw_khz || "null"})">ID</button>`;
  }
  if (!s.id_top_name) {   // ran, no candidate
    return `<span class="pill not_configured" title="matcher ran, no candidate">no match</span> ` +
      `<a href="#" class="idredo" onclick="identifyRow(${s.id},${s.center_mhz},${s.bw_khz || "null"});return false" style="font-size:10px">↻</a>`;
  }
  const band = s.id_mode === "band";
  const cls = s.id_device_class ? ` · ${esc(s.id_device_class)}` : "";
  return `<span class="pill ${band ? "KNOWN_BAND" : "IDENTIFIED"}" ` +
    `title="${band ? "frequency/bandwidth guess only — no SDR IQ" : "modulation-matched from live IQ"}">` +
    `${esc(s.id_top_name)}${cls}${s.id_top_score != null ? " (" + s.id_top_score.toFixed(0) + ")" : ""}${band ? " ~band" : ""}</span> ` +
    `<a href="#" onclick="identifyRow(${s.id},${s.center_mhz},${s.bw_khz || "null"});return false" style="font-size:10px" title="re-identify">↻</a>`;
}
window.identifyRow = async (sid, center, bw) => {
  const cell = $("idcell-" + sid);
  if (cell) cell.innerHTML = `<span class="note">identifying…</span>`;
  try {
    const r = await api("/api/identify", { signal_id: sid, center_mhz: center, bw_khz: bw });
    const top = (r.candidates || [])[0];
    if (cell) cell.innerHTML = idChip({
      id: sid, center_mhz: center, bw_khz: bw, id_updated_utc: Date.now() / 1000,
      id_top_name: top ? top.name : "", id_top_score: top ? top.score : null,
      id_device_class: top ? top.device_class : "", id_mode: r.id_mode });
    // surface the honesty note (band-only / empty channel) briefly
    if (r.note && cell) cell.title = r.note;
  } catch (e) {
    if (cell) cell.innerHTML = `<span class="pill error" title="${esc(e.message)}">ID failed</span>`;
  }
};

/* ---------- results ---------- */
async function loadResults() {
  if (App.postSweep && App.postSweep.overview) {
    renderAmbientOverview(App.postSweep.overview, App.postSweep.prompt);
  } else if (App.postSweep && App.postSweep.running && $("ambient-overview-wait")) {
    $("ambient-overview-wait").textContent = App.postSweep.message || "Automatic review is running...";
  } else if (App.state && App.state.sweeps) {
    const doneInterior = App.state.sweeps.filter(s => s.phase === "interior" && s.status === "done");
    if (doneInterior.length) startPostSweep(doneInterior[doneInterior.length - 1].id);
  }
  try {
    const anomalies = await api("/api/anomalies");
    $("tbl-anomalies").innerHTML =
      "<tr><th>Score</th><th>Frequency</th><th>Peak dBm</th><th>BW kHz</th><th>Persist</th>" +
      "<th>In/Out Δ</th><th>Strongest room</th><th>Class</th><th>Signal ID</th><th>Why not verified</th><th></th></tr>" +
      anomalies.map(s =>
        `<tr><td class="${s.anomaly_score >= 60 ? "score-hi" : "score-md"}" title="${esc(s.score_reason || "")}">${(s.anomaly_score || 0).toFixed(0)}</td>` +
        `<td><a href="#" class="freqlink" data-center="${s.center_mhz}" data-sid="${s.id}" ` +
        `data-attr="${s.attribution_status || "unassessed"}" ` +
        `data-label="${esc(s.matched_label || s.match_reason || "anomaly")}">${fmtF(s.center_mhz)}</a></td>` +
        `<td>${s.peak_dbm}</td><td>${s.bw_khz}</td>` +
        `<td>${((s.persistence || 0) * 100).toFixed(0)}%</td>` +
        `<td>${s.interior_delta_db != null ? "+" + s.interior_delta_db + " dB" : "—"}</td>` +
        `<td>${esc(s.strongest_room || "")}${s.localization_score ? ` (+${s.localization_score} dB)` : ""}</td>` +
        `<td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span>${s.caution ? " ⚠" : ""}</td>` +
        `<td id="idcell-${s.id}">${idChip(s)}</td>` +
        `<td style="max-width:300px">${esc(s.match_reason)}` +
        (s.score_reason ? `<div class="note" style="margin-top:2px;color:#9fb2c9">${esc(s.score_reason)}</div>` : "") +
        `</td>` +
        `<td><button class="btn" onclick="captureOne(${s.id})">CAPTURE</button></td></tr>`).join("");
    renderFindingsSummary(anomalies);
  } catch (e) {}
  loadHunt();
  loadSignals();
  loadReports();
  loadExpected();
  loadReportOptions();
}

// One-glance verdict for the operator + client: unexplained anomalies, how many
// were flagged as threats (decision=flagged or attribution=client_space), cleared,
// and how many still need review. Threat count turns red when > 0.
async function renderFindingsSummary(anomalies) {
  const el = $("results-summary");
  if (!el) return;
  let sigs = [];
  let remaining = null;
  try {
    const tri = await api("/api/triage/last");
    sigs = tri.signals || [];
    remaining = tri.remaining_unexplained;
  } catch (e) { /* summary still shows anomaly count without triage detail */ }
  const threats = sigs.filter(s => s.triage_status === "flagged" ||
                                   s.attribution_status === "client_space").length;
  const cleared = sigs.filter(s => s.triage_status === "cleared").length;
  const chip = (val, label, style) =>
    `<span style="display:inline-block;margin-right:22px">` +
    `<b style="font-size:19px;${style || ""}">${val}</b> <span class="note">${label}</span></span>`;
  el.innerHTML =
    `<div style="display:flex;align-items:center;flex-wrap:wrap">` +
    chip(anomalies.length, "unexplained anomalies", anomalies.length ? "color:var(--amber)" : "color:var(--green)") +
    chip(threats, threats ? "flagged as THREATS ●" : "flagged as threats",
         threats ? "color:var(--red)" : "color:var(--dim)") +
    chip(cleared, "cleared", "color:var(--green)") +
    (remaining != null ? chip(remaining, "still to review",
         remaining ? "color:var(--amber)" : "color:var(--green)") : "") +
    `</div>`;
  el.style.display = "";
}

async function loadReportOptions() {
  try {
    const o = await api("/api/report_options");
    document.querySelectorAll(".report-opt").forEach(cb => {
      cb.checked = !!o[cb.dataset.opt];
    });
  } catch (e) {}
}
async function saveReportOptions() {
  const body = {};
  document.querySelectorAll(".report-opt").forEach(cb => body[cb.dataset.opt] = cb.checked);
  try {
    await api("/api/report_options", body);
    $("report-opt-msg").textContent = "saved — included on next GENERATE REPORT";
  } catch (e) { $("report-opt-msg").textContent = e.message; }
}
document.querySelectorAll(".report-opt").forEach(cb => cb.onchange = () => {
  // the same option can appear on more than one tab — mirror before saving
  document.querySelectorAll(`.report-opt[data-opt="${cb.dataset.opt}"]`)
    .forEach(o => { o.checked = cb.checked; });
  saveReportOptions();
});

async function loadExpected() {
  try {
    const r = await api("/api/expected");
    const c = r.counts;
    $("expected-summary").innerHTML =
      `${c.found}/${c.total_records} predicted signals received · ` +
      (c.missing_strong ? `<span class="score-hi">${c.missing_strong} strongly-expected MISSING</span> · ` : "") +
      (c.missing_medium ? `${c.missing_medium} medium missing · ` : "") +
      `${c.missing_weak} intermittent silent · ${c.not_swept} not swept`;
    const showFound = $("expected-show-found").checked;
    const rows = r.rows.filter(x => showFound || !x.found);
    $("tbl-expected").innerHTML =
      "<tr><th>Frequency</th><th>Station / record</th><th>Dist mi</th><th>Expectation</th>" +
      "<th>Status</th><th>Why</th></tr>" +
      rows.map(x => {
        const f = x.freq_low_mhz === x.freq_high_mhz ? fmtF(x.freq_low_mhz)
          : fmtF(x.freq_low_mhz) + "–" + fmtF(x.freq_high_mhz);
        const statusPill = x.found ? '<span class="pill ok">RECEIVED</span>'
          : !x.swept ? '<span class="pill not_configured">NOT SWEPT</span>'
          : x.expectation === "strong" ? '<span class="pill failed">MISSING</span>'
          : '<span class="pill stale">SILENT</span>';
        return `<tr${!x.found && x.expectation === "strong" ? ' style="background:var(--err-bg)"' : ""}>` +
          `<td>${f}</td><td>${esc(x.callsign || x.label)}</td>` +
          `<td>${x.distance_mi != null ? x.distance_mi.toFixed(0) : ""}</td>` +
          `<td>${esc(x.expectation)} — ${esc(x.basis)}</td>` +
          `<td>${statusPill}</td><td style="max-width:340px">${esc(x.reason)}</td></tr>`;
      }).join("") || "<tr><td>nothing to verify — build the baseline and sweep first</td></tr>";
  } catch (e) { $("expected-summary").textContent = e.message; }
}
$("expected-show-found").onchange = loadExpected;
async function loadSignals() {
  const phase = $("signals-phase").value;
  try {
    const sigs = await api("/api/signals" + (phase ? "?phase=" + phase : ""));
    // keep lightbox / next-step working from this table too
    if (sigs && sigs.length) App.liveSweepSignals = sigs;
    $("tbl-signals").innerHTML =
      "<tr><th></th><th>Frequency</th><th>Peak</th><th>Persist</th><th>Behavior</th>" +
      "<th>Inventory</th><th>Class</th><th>Match</th></tr>" +
      sigs.map(s => {
        const beh = (s.behavior && s.behavior.label) || s.device_hint || "—";
        const inv = s.inventory_summary || "—";
        return `<tr data-sid="${s.id}" style="cursor:pointer" title="click: open signal detail (behavior + inventory)">` +
          `<td class="thumb-cell"><img class="sig-thumb" loading="lazy" alt="" data-sid="${s.id}" ` +
          `src="/api/signals/${s.id}/image.svg?size=thumb" onerror="this.style.visibility='hidden'"></td>` +
          `<td><a href="#" class="freqlink" data-center="${s.center_mhz}" data-sid="${s.id}" ` +
          `data-label="${esc(s.matched_label || s.behavior && s.behavior.label || s.classification || "signal")}">${fmtF(s.center_mhz)}</a></td>` +
          `<td>${s.peak_dbm}</td>` +
          `<td>${((s.persistence || 0) * 100).toFixed(0)}%</td>` +
          `<td title="${esc((s.behavior && s.behavior.phrase) || "")}">${esc(beh)}</td>` +
          `<td style="max-width:220px;font-size:11px">${esc(String(inv).slice(0, 80))}</td>` +
          `<td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span></td>` +
          `<td>${esc(s.matched_label || "")}</td></tr>`;
      }).join("");
  } catch (e) {}
}
// Results signal table: thumbnail / row → lightbox (same as live ledger)
(function wireResultsSignals() {
  const tbl = $("tbl-signals");
  if (!tbl || tbl._behWired) return;
  tbl._behWired = true;
  tbl.addEventListener("click", (e) => {
    const thumb = e.target.closest(".sig-thumb");
    if (thumb) { openSigLightbox(parseInt(thumb.dataset.sid, 10)); return; }
    if (e.target.closest(".freqlink")) return;   // freqlink does listen
    const tr = e.target.closest("tr[data-sid]");
    if (tr) openSigLightbox(parseInt(tr.dataset.sid, 10));
  });
})();
$("signals-phase").onchange = loadSignals;

async function loadReports() {
  try {
    const reps = await api("/api/reports");
    $("tbl-reports").innerHTML = "<tr><th>Generated</th><th>Kind</th><th>File</th><th></th></tr>" +
      reps.map(r => `<tr><td>${new Date(r.generated_utc * 1000).toLocaleString()}</td>` +
        `<td><span class="pill ${r.kind === "FINAL" ? "ok" : "stale"}">${r.kind}</span></td>` +
        `<td><a style="color:var(--cyan)" href="/reports/${encodeURIComponent(r.filename)}" download>${esc(r.filename)}</a></td>` +
        `<td><button class="btn" onclick="openReportLocal('${esc(r.filename)}')">OPEN IN PDF VIEWER</button></td></tr>`).join("");
  } catch (e) {}
}

$("btn-report").onclick = async () => {
  $("btn-report").disabled = true;
  $("btn-report").textContent = "RENDERING…";
  try {
    const r = await api("/api/report/generate", {});
    showReportLink("report-msg", r, "report");
    loadReports();
  } catch (e) { $("report-msg").textContent = "FAILED: " + e.message; }
  $("btn-report").textContent = "GENERATE REPORT";
  $("btn-report").disabled = false;
};
$("btn-csv").onclick = () => { window.location = "/api/export/csv"; };

/* ---------- community (Discord) ---------- */
$("btn-discord").onclick = async () => {
  try {
    const p = await api("/api/discord/preview");
    if (!p.lines.length) return alert("No anomalies to post.");
    if (!p.configured)
      return alert("No Discord webhook configured.\nSet integrations.discord_webhook " +
                   "in config.yaml (channel settings → Integrations → Webhooks).");
    const preview = p.lines.join("\n");
    if (!confirm("EXACTLY this will be posted to the community channel — " +
                 "no address, client, operator, or room names:\n\n" + preview +
                 "\n\nPost it?")) return;
    const note = prompt("Optional note to the community (e.g. 'urban residential, " +
                        "US midwest'). Leave empty to skip:", "") || "";
    const r = await api("/api/discord/post", { note });
    $("discord-msg").textContent = `posted ${r.anomalies} anomalies to the community channel`;
  } catch (e) { $("discord-msg").textContent = "FAILED: " + e.message; }
};

/* ---------- signal wiki ---------- */
let wikiSelected = null;

async function captureSignals(body, labelEl) {
  try {
    const r = await api("/api/captures/start", body);
    if (labelEl) labelEl.textContent = `capturing ${r.targets} signals — watch the live view`;
    App.overlayHidden = false;
  } catch (e) { alert("Capture failed to start: " + e.message); }
}
$("btn-capture-all").onclick = () =>
  captureSignals({ all_anomalies: true }, $("discord-msg"));
window.captureOne = (id) => captureSignals({ signal_ids: [id] });

async function loadWiki() {
  try {
    const cat = $("wiki-cat").value, q = $("wiki-q").value;
    const r = await api("/api/captures?category=" + encodeURIComponent(cat) +
                        "&q=" + encodeURIComponent(q));
    const cur = $("wiki-cat").value;
    $("wiki-cat").innerHTML = '<option value="">all</option>' + r.categories.map(c =>
      `<option ${c.category === cur ? "selected" : ""}>${esc(c.category)}</option>`).join("");
    $("wiki-count").textContent =
      `${r.rows.length} ${r.grouped ? "distinct signals" : "captures"}`;
    $("tbl-wiki").innerHTML =
      "<tr><th>Frequency</th><th>Label</th><th>Category</th><th>Sightings</th><th>Status</th><th>Last seen</th></tr>" +
      r.rows.map(c => {
        const cls = c.status === "identified" ? "ok" : c.status === "threat" ? "failed" : "stale";
        const sight = c.sightings ?
          `${c.sightings}× / ${c.sessions_seen} session${c.sessions_seen > 1 ? "s" : ""}` : "—";
        return `<tr onclick="wikiOpen(${c.id})" style="cursor:pointer">` +
          `<td><a href="#" class="freqlink" title="tune &amp; listen" ` +
          `data-center="${c.center_mhz}" data-label="${esc(c.label || c.category || "signal")}">▶</a> ` +
          `${fmtF(c.center_mhz)}</td><td>${esc(c.label)}</td><td>${esc(c.category)}</td>` +
          `<td>${sight}</td>` +
          `<td><span class="pill ${cls}">${esc(c.status).toUpperCase()}</span></td>` +
          `<td>${new Date(c.captured_utc * 1000).toLocaleDateString()}</td></tr>`;
      }).join("") || "<tr><td>no captures yet — sweeps auto-archive every detected signal here</td></tr>";
  } catch (e) { $("tbl-wiki").innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; }
}
$("wiki-cat").onchange = loadWiki;
$("wiki-q").oninput = () => { clearTimeout(window._wt); window._wt = setTimeout(loadWiki, 400); };

window.wikiOpen = async (id) => {
  try {
    const c = await api("/api/captures/" + id);
    wikiSelected = c;
    $("wiki-detail").style.display = "";
    $("wiki-label").value = c.label || "";
    $("wiki-status").value = c.status || "unconfirmed";
    $("wiki-cat-edit").value = c.category || "";
    $("wiki-notes").value = c.notes || "";
    $("wiki-desc").textContent = c.description || "(no description yet)";
    $("wiki-img").innerHTML =
      `<a href="/capture_images/${encodeURIComponent(c.image_path)}" target="_blank">` +
      `<img src="/capture_images/${encodeURIComponent(c.image_path)}" style="width:100%; border:1px solid var(--line)"></a>`;
    $("wiki-meta").innerHTML =
      `<div>Captured</div><div>${new Date(c.captured_utc * 1000).toLocaleString()}</div>` +
      `<div>Session</div><div>#${c.session_id} — ${esc(c.address)}</div>` +
      `<div>Room / module</div><div>${esc(c.room || "—")} / ${esc(c.module)}</div>` +
      `<div>Center / BW</div><div>${fmtF(c.center_mhz)} / ~${(c.bw_khz || 0).toFixed(0)} kHz</div>` +
      `<div>Peak / floor</div><div>${c.peak_dbm} / ${c.floor_dbm} dBm</div>` +
      `<div>Classification</div><div>${esc(statusLabel(c.classification))} ${esc(c.matched_label || "")}</div>`;
    // observation timeline across all sessions + variance flag
    try {
      const tl = await api("/api/signal_timeline?center_mhz=" + c.center_mhz);
      const v = tl.variance;
      let html = "";
      if (v && v.observations > 1) {
        html = `<div class="card" style="margin:8px 0; ${v.flag ? "border-color:var(--amber)" : ""}">` +
          `<b>SIGHTING HISTORY:</b> ${v.observations}× across ${v.sessions} session(s) · ` +
          `peak ${v.peak_min_dbm} to ${v.peak_max_dbm} dBm (range ${v.peak_range_db} dB)` +
          (v.bw_min_khz != null ? ` · BW ${v.bw_min_khz}–${v.bw_max_khz} kHz` : "") +
          (v.flag ? `<br><span class="caution">⚠ ${esc(v.flag)}</span>` : "") +
          `<div class="scroll" style="max-height:120px; margin-top:8px"><table>` +
          "<tr><th>When</th><th>Where</th><th>Peak</th><th>BW</th><th>Kind</th><th></th></tr>" +
          tl.rows.slice().reverse().map(o =>
            `<tr><td>${new Date(o.captured_utc * 1000).toLocaleString()}</td>` +
            `<td>${esc((o.address || "").slice(0, 20))}${o.room ? " · " + esc(o.room) : ""}</td>` +
            `<td>${o.peak_dbm}</td><td>${(o.bw_khz || 0).toFixed(0)}</td><td>${o.kind}</td>` +
            `<td><a style="color:var(--cyan); cursor:pointer" onclick="wikiOpen(${o.id})">view</a></td></tr>`).join("") +
          "</table></div></div>";
      }
      $("wiki-timeline").innerHTML = html;
    } catch (e) { $("wiki-timeline").innerHTML = ""; }
    $("wiki-match").innerHTML = c.matches.length ?
      `<div class="card" style="border-color:var(--amber); margin:8px 0">` +
      `<b style="color:var(--amber)">⚠ MATCHES IN LIBRARY:</b> ` +
      c.matches.map(m => `seen ${new Date(m.captured_utc * 1000).toLocaleDateString()} ` +
        `at ${esc(m.address)}${m.room ? " (" + esc(m.room) + ")" : ""} — ` +
        `<a style="color:var(--cyan); cursor:pointer" onclick="wikiOpen(${m.capture_id})">${esc(m.label)}</a>`)
        .join("; ") + `</div>` : "";
  } catch (e) { alert(e.message); }
};

$("btn-wiki-save").onclick = async () => {
  if (!wikiSelected) return;
  await api("/api/captures/" + wikiSelected.id, {
    label: $("wiki-label").value, status: $("wiki-status").value,
    category: $("wiki-cat-edit").value, notes: $("wiki-notes").value });
  loadWiki();
};
$("btn-wiki-ai").onclick = async () => {
  if (!wikiSelected) return;
  $("wiki-desc").textContent = "asking the analyst model…";
  try {
    const r = await api(`/api/captures/${wikiSelected.id}/describe`, {});
    $("wiki-desc").textContent = r.description;
  } catch (e) { $("wiki-desc").textContent = "AI describe failed: " + e.message; }
};
$("btn-wiki-recapture").onclick = () => {
  if (!wikiSelected) return;
  if (!confirm(`Recapture ${fmtF(wikiSelected.center_mhz)} with the live analyzer now?`)) return;
  captureSignals({ signal_ids: [] , recapture: wikiSelected.id });
};

/* ---------- inventory ---------- */
async function invScan(kind) {
  $("inv-msg").textContent = kind === "ble" ?
    "scanning BLE (~8s)…" : "scanning WiFi…";
  try {
    const r = await api("/api/inventory/scan", { kind });
    $("inv-msg").textContent = r.message;
  } catch (e) { $("inv-msg").textContent = "FAILED: " + e.message; }
  loadInventory();
}
$("btn-scan-wifi").onclick = () => invScan("wifi");
$("btn-scan-ble").onclick = () => invScan("ble");

async function bleDetailScan() {
  const card = $("ble-detail-card");
  const btn = $("btn-scan-ble-detail");
  if (btn) btn.disabled = true;
  // status on the button row so it's obvious something is happening (the
  // detail card is BELOW the inventory table — easy to miss)
  if ($("inv-msg")) $("inv-msg").textContent = "Bluetooth detail sweep (~8s)… scroll down for full dossiers";
  if (card) {
    card.style.display = "";
    try { card.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (_) {}
  }
  if ($("ble-detail-msg")) $("ble-detail-msg").textContent = "scanning BLE (~8s)…";
  if ($("ble-detail-list")) $("ble-detail-list").innerHTML = "";
  try {
    const r = await api("/api/ble/scan", { seconds: 8 });
    const n = (r.devices || []).length;
    const tr = r.trackers || 0;
    if ($("inv-msg"))
      $("inv-msg").textContent = `${r.message || (n + " devices")}` +
        (tr ? ` — ⚑ ${tr} possible tracker(s) (see detail card below)` : " (detail card below)");
    if ($("ble-detail-msg"))
      $("ble-detail-msg").innerHTML = esc(r.message || "") +
        (tr ? ` — <span class="score-hi">⚑ ${tr} possible tracker(s)</span>` : "");
    if ($("ble-detail-list")) {
      try {
        $("ble-detail-list").innerHTML = (r.devices || []).map(bleCard).join("") ||
          "<p class='note'>no advertisers heard — is the Bluetooth adapter on? " +
          "Windows: Settings → Bluetooth & devices → On. Close other BLE apps.</p>";
      } catch (renderErr) {
        $("ble-detail-list").innerHTML =
          `<p class='note'>scan OK (${n} devices) but display failed: ${esc(renderErr.message)}</p>` +
          `<pre class="note" style="white-space:pre-wrap">${esc(JSON.stringify(r.devices, null, 2).slice(0, 4000))}</pre>`;
      }
    }
    if (card) try { card.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (_) {}
  } catch (e) {
    const msg = "FAILED: " + (e.message || e);
    if ($("ble-detail-msg")) $("ble-detail-msg").textContent = msg;
    if ($("inv-msg")) $("inv-msg").textContent = msg;
  }
  if (btn) btn.disabled = false;
}
if ($("btn-scan-ble-detail")) $("btn-scan-ble-detail").onclick = bleDetailScan;

async function stimulateProbe() {
  const center = parseFloat($("stim-center").value);
  const lo = parseFloat($("stim-lo").value);
  const hi = parseFloat($("stim-hi").value);
  if (!(center > 0) || !(lo > 0) || !(hi > lo)) {
    $("stim-msg").textContent = "enter a watch frequency and a valid ping band (lo < hi)";
    return;
  }
  const override = $("stim-override").checked;
  if (override && !confirm("Band override: you are choosing to transmit outside the " +
      "authorized ISM bands. Confirm you have lawful authority to transmit here.")) return;
  $("stim-msg").textContent = "probing (transmitting)…";
  $("stim-result").innerHTML = "";
  try {
    const r = await api("/api/stimulate/probe",
      { center_mhz: center, band_lo_mhz: lo, band_hi_mhz: hi, override_band: override });
    $("stim-msg").textContent = r.mode ? `mode: ${r.mode}` : "";
    const cls = r.verdict === "RESPONSE_DETECTED" ? "score-hi" : "";
    let html = `<p class="${cls}"><b>${esc(r.verdict.replace(/_/g, " "))}</b></p>` +
      `<p class="note">${esc(r.plain)}</p>`;
    if (r.responders && r.responders.length) {
      html += "<table><tr><th>Frequency</th><th>Δ vs quiet</th><th>Stim SNR</th></tr>" +
        r.responders.map(x =>
          `<tr><td>${fmtF(x.freq_mhz)}</td><td class="score-hi">+${x.delta_db} dB</td>` +
          `<td>${x.stim_snr_db} dB</td></tr>`).join("") + "</table>";
    }
    (r.warnings || []).forEach(w => {
      html += `<p class="note" style="color:#ffd24a">⚠ ${esc(w)}</p>`;
    });
    $("stim-result").innerHTML = html;
  } catch (e) { $("stim-msg").textContent = "FAILED: " + e.message; }
}
$("btn-stimulate").onclick = stimulateProbe;

async function emanationScan() {
  const lo = parseFloat($("ema-lo").value);
  const hi = parseFloat($("ema-hi").value);
  if (!(lo > 0) || !(hi > lo)) {
    $("ema-msg").textContent = "enter a scan band (lo < hi), e.g. 80 to 1000";
    return;
  }
  $("ema-msg").textContent = "scanning (averaging many captures — this can take a while)…";
  $("ema-result").innerHTML = "";
  try {
    const r = await api("/api/emanations/scan", { band_lo_mhz: lo, band_hi_mhz: hi });
    $("ema-msg").textContent = `${r.averaged_sweeps} averages/hop`;
    const cls = r.verdict === "SUSPECTED_EMITTER" ? "score-hi" : "";
    let html = `<p class="${cls}"><b>${esc(r.verdict.replace(/_/g, " "))}</b></p>` +
      `<p class="note">${esc(r.plain)}</p>`;
    (r.combs || []).forEach(c => {
      html += `<div class="card" style="margin:6px 0;background:#1f1212">` +
        `<div class="row" style="justify-content:space-between">` +
        `<b>Clock ~${c.clock_mhz} MHz — ${c.n_harmonics} harmonics</b>` +
        `<span class="threat-tag">⚑ SUSPECTED DEVICE</span></div>` +
        `<p class="note" style="color:#ffd24a;margin:2px 0">${esc(c.device_hint)}</p>` +
        `<p class="note" style="margin:2px 0">tones: ${c.harmonics_mhz.map(f => fmtF(f)).join(", ")}</p>` +
        `</div>`;
    });
    if (r.spikes && r.spikes.length) {
      html += `<p class="note">${r.spikes.length} narrowband tone(s) seen total.</p>`;
    }
    (r.warnings || []).forEach(w => {
      html += `<p class="note" style="color:#ffd24a">⚠ ${esc(w)}</p>`;
    });
    $("ema-result").innerHTML = html;
  } catch (e) { $("ema-msg").textContent = "FAILED: " + e.message; }
}
$("btn-emanation").onclick = emanationScan;

function bleCard(d) {
  d = d || {};
  const mfrs = Array.isArray(d.manufacturers) ? d.manufacturers : [];
  const svcs = Array.isArray(d.services) ? d.services : [];
  const mfr = mfrs.map(m => esc(m.company || "?") +
    ((m.hints && m.hints.length) ? " (" + m.hints.map(esc).join(", ") + ")" : "")).join("; ");
  const svc = svcs.filter(s => s && s.name).map(s =>
    `<span class="pill${s.tracker ? " failed" : ""}">${esc(s.name)}</span>`).join(" ");
  const rows = [
    ["MAC", esc(d.mac || "") + (d.address_inferred ? ' <span class="note">(type inferred from address)</span>' : "")],
    ["Address type", esc(d.address_kind_label || "—")],
    ["Vendor", esc(d.vendor || "—")],
    ["Name", esc(d.name) || "<i>(none advertised)</i>"],
    ["Signal", d.rssi_dbm != null ? `${d.rssi_dbm} dBm — ${esc(d.proximity || "")}` : "—"],
    ["Mfr data", mfr || "—"],
    ["Services", svc || "—"],
    ["Appearance", esc(d.appearance || "—")],
    ["TX power", d.tx_power != null ? d.tx_power + " dBm" : "—"],
  ];
  if (d.is_hid) rows.push(["Class", "⌨ HID (mouse/keyboard class)"]);
  if (d.is_av_remote) rows.push(["Class", "📺 AV remote class"]);
  const body = rows.map(([k, v]) =>
    `<tr><td class="note" style="width:110px;vertical-align:top">${k}</td><td>${v}</td></tr>`).join("");
  const flag = d.is_tracker
    ? '<span class="threat-tag">⚑ POSSIBLE TRACKER</span>'
    : (d.rotating ? '<span class="pill">rotating identity</span>' : "");
  return `<div class="card" style="margin:6px 0;${d.is_tracker ? "background:#1f1212" : ""}">` +
    `<div class="row" style="justify-content:space-between"><b>${esc(d.vendor || d.name || d.mac || "?")}</b>${flag}</div>` +
    `<table style="margin-top:4px">${body}</table>` +
    (d.tscm_note ? `<p class="note" style="color:#ffd24a;margin:4px 0 0">${esc(d.tscm_note)}</p>` : "") +
    `<p class="note" style="margin:2px 0 0">${esc(d.address_note || "")}</p></div>`;
}

async function loadInventory() {
  try {
    const rows = await api("/api/inventory");
    $("tbl-inventory").innerHTML =
      "<tr><th>Type</th><th>Name</th><th>ID (MAC/BSSID)</th><th>RSSI dBm</th>" +
      "<th>Ch / Freq</th><th>Info</th><th>Owned?</th><th></th></tr>" +
      rows.map(d => {
        const loud = d.loud_unowned;
        const rssi = d.rssi_dbm != null ?
          `<span class="${loud ? "score-hi" : ""}">${d.rssi_dbm}</span>` : "—";
        return `<tr${loud ? ' style="background:var(--err-bg)"' : ""}>` +
          `<td>${d.kind.toUpperCase()}</td><td>${esc(d.name) || "<i>(no name)</i>"}</td>` +
          `<td>${esc(d.identifier)}</td><td>${rssi}${loud ? " ⚠ LOUD+UNOWNED" : ""}</td>` +
          `<td>${esc(d.channel)}${d.freq_mhz ? " / " + fmtF(d.freq_mhz) : ""}</td>` +
          `<td>${esc(d.extra)}</td>` +
          `<td>${d.owned ? '<span class="pill ok">CLIENT-OWNED</span>'
                         : '<span class="pill failed">UNACCOUNTED</span>'}</td>` +
          `<td><button class="btn" onclick="invMark(${d.id},${d.owned ? 0 : 1})">` +
          `${d.owned ? "UNMARK" : "MARK OWNED"}</button></td></tr>`;
      }).join("") || "<tr><td>nothing scanned yet</td></tr>";
  } catch (e) { $("tbl-inventory").innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; }
}
window.invMark = async (id, owned) => {
  await api("/api/inventory/mark", { id, owned });
  loadInventory();
};

/* ---------- network check (LAN device checker) ---------- */
let _netDevices = [];
function renderNetwork() {
  const unrecOnly = $("net-unrec-only") && $("net-unrec-only").checked;
  const rows = _netDevices.filter(d => !unrecOnly || d.flagged);
  const header = "<tr><th>Status</th><th>IP</th><th>MAC</th><th>Vendor</th>" +
    "<th>Hostname</th><th>Also on RF</th><th>Actions</th></tr>";
  const body = rows.map(d => {
    let pill, rowStyle = "";
    if (d.blocked) { pill = '<span class="pill failed">BLOCKED</span>'; rowStyle = ' class="threat-row"'; }
    else if (d.is_gateway) pill = '<span class="pill running">ROUTER</span>';
    else if (d.owned) pill = '<span class="pill ok">KNOWN</span>';
    else { pill = '<span class="pill UNVERIFIED">UNRECOGNIZED</span>'; rowStyle = ' style="background:var(--err-bg)"'; }
    const rnd = d.randomized_mac ? ' <span class="note" title="randomized/private MAC — a phone or privacy device">rnd</span>' : "";
    // "gather more information": age + RF cross-reference
    const rel = (d.related || []);
    let relCell = "—";
    if (rel.length) {
      const top = rel[0];
      const bandTxt = top.kind === "ble" ? "BLE ~2.44 GHz"
        : (top.freq_mhz ? fmtF(top.freq_mhz) : "WiFi");
      relCell = `<span title="matched by ${esc(top.match)} to a ${esc(top.kind).toUpperCase()} device — DF it to locate">` +
        `${esc(top.kind).toUpperCase()} · ${esc(bandTxt)}${rel.length > 1 ? ` +${rel.length - 1}` : ""}</span>`;
    }
    let actions;
    if (d.blocked)
      actions = `<button class="btn" onclick="netUnblock(${d.id})">UNBLOCK</button> ` +
        `<button class="btn" onclick="netRouterGuide(${d.id})">ROUTER STEPS</button>`;
    else {
      actions = `<button class="btn" onclick="netOwn(${d.id},${d.owned ? 0 : 1})">` +
        `${d.owned ? "UNMARK" : "✓ MARK OWNED"}</button> ` +
        `<button class="btn danger" onclick="netBlock(${d.id})" title="Block this device at the router — the lawful way (no transmitting)">⛔ BLOCK (router)</button>`;
      if (rel.length)
        actions += ` <button class="btn" onclick="netLocate(${d.id})" title="Direction-find the matched RF emission to physically locate this device">📍 LOCATE</button>`;
    }
    return `<tr${rowStyle}><td>${pill}</td><td>${esc(d.ip) || "—"}</td>` +
      `<td>${esc(d.identifier)}${rnd}</td><td>${esc(d.vendor) || "—"}</td>` +
      `<td>${esc(d.name) || "<i>—</i>"}</td><td>${relCell}</td><td>${actions}</td></tr>`;
  }).join("");
  $("tbl-network").innerHTML = header + (body ||
    "<tr><td colspan=7 class='note'>no devices yet — press SCAN NETWORK</td></tr>");
}

// render the guided router-block steps into the note area (the LEGAL enforcement)
function showRouterGuide(g) {
  if (!g) return;
  const el = $("net-enforce-note");
  el.style.display = "";
  const link = g.admin_url
    ? `<a href="${esc(g.admin_url)}" target="_blank" rel="noopener">${esc(g.admin_url)}</a>`
    : "your router's admin page";
  el.innerHTML =
    `<b>Block at the router — the lawful way (the app never transmits):</b><br>` +
    `1. Open ${link}${g.router_brand && g.router_brand !== "unknown" ? " (" + esc(g.router_brand) + ")" : ""} and sign in.<br>` +
    `2. ${esc(g.steps)}<br>` +
    `3. Block this MAC: <code>${esc(g.mac)}</code>` +
    (g.device_label ? ` (${esc(g.device_label)})` : "") + `<br>` +
    `<span class="note">${esc(g.note || "")}</span>`;
}
async function loadNetwork() {
  try {
    const r = await api("/api/netscan/devices");
    _netDevices = r.devices || [];
    renderNetwork();
  } catch (e) { $("tbl-network").innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; }
}
if ($("btn-netscan")) $("btn-netscan").onclick = async () => {
  const b = $("btn-netscan");
  b.disabled = true; $("net-msg").textContent = "scanning the local network…";
  try {
    const ping = $("net-ping").checked;
    const r = await api("/api/netscan/run", { ping });
    _netDevices = r.devices || [];
    renderNetwork();
    const m = r.meta || {};
    const unrec = _netDevices.filter(d => d.flagged).length;
    $("net-msg").textContent =
      `${m.count} device${m.count === 1 ? "" : "s"} on ${m.network || "this network"}` +
      (m.arp_only ? " (ARP cache only)" : "") +
      ` · ${unrec} unrecognized`;
  } catch (e) { $("net-msg").textContent = "scan failed: " + e.message; }
  b.disabled = false;
};
if ($("net-unrec-only")) $("net-unrec-only").onchange = renderNetwork;
window.netOwn = async (id, owned) => {
  try { await api(`/api/netscan/devices/${id}/own`, { owned }); await loadNetwork(); }
  catch (e) { $("net-msg").textContent = e.message; }
};
window.netBlock = async (id) => {
  const d = _netDevices.find(x => x.id === id);
  const who = d ? (d.vendor || d.name || d.ip || d.identifier) : "this device";
  const reason = window.prompt(
    `Block ${who} (${d ? d.identifier : ""}) at the router?\n\n` +
    "The app does NOT transmit or jam. This records a confirmed block + audit " +
    "entry and shows you exactly how to drop it in the router you're on.\n\n" +
    "Reason for the block (required, logged):", "");
  if (reason == null) return;                 // cancelled
  if (!reason.trim()) { $("net-msg").textContent = "block needs a reason — nothing changed"; return; }
  try {
    const r = await api(`/api/netscan/devices/${id}/block`, { confirm: true, reason: reason.trim() });
    await loadNetwork();
    showRouterGuide(r.router_guide);
  } catch (e) { $("net-msg").textContent = "block failed: " + e.message; }
};
window.netRouterGuide = async (id) => {
  try { showRouterGuide(await api(`/api/netscan/devices/${id}/router_guide`)); }
  catch (e) { $("net-msg").textContent = e.message; }
};
window.netUnblock = async (id) => {
  try { await api(`/api/netscan/devices/${id}/unblock`, {}); await loadNetwork(); }
  catch (e) { $("net-msg").textContent = e.message; }
};

/* ---------- exposure check (Shodan, internal-only) ----------
   Outside-in complement to the LAN scan. Cameras/DVRs lead — they're the finding.
   Every status renders a plain-language line; 'not_configured' tells the operator
   exactly what's missing (a paid Shodan key) rather than looking broken. */
const _EXP_CLASS = {
  camera: "var(--red, #ff6b6b)", dvr: "var(--red, #ff6b6b)",
  nas: "var(--amber, #ffb454)", router: "var(--amber, #ffb454)",
  printer: "var(--cyan, #6cc)", iot: "var(--cyan, #6cc)",
  unknown: "var(--dim, #8a97a6)",
};
function renderExposure(hosts) {
  const t = $("tbl-exposure");
  if (!t) return;
  if (!hosts || !hosts.length) { t.innerHTML = ""; return; }
  t.innerHTML =
    `<tr><th>type</th><th>host</th><th>product / banner</th><th>org / ISP</th><th>seen</th></tr>` +
    hosts.map(h => {
      const dot = _EXP_CLASS[h.device_class] || _EXP_CLASS.unknown;
      const hostport = `${esc(h.ip)}${h.port ? ":" + esc(h.port) : ""}`;
      const prod = esc(h.product || h.banner || "—");
      const place = [h.city].filter(Boolean).map(esc).join("");
      return `<tr>
        <td><span class="tag" style="color:${dot}">${esc((h.device_class || "?").toUpperCase())}</span></td>
        <td style="font-family:monospace">${hostport}${place ? `<br><span class="note">${place}</span>` : ""}</td>
        <td>${prod}</td>
        <td class="note">${esc(h.org || "—")}</td>
        <td class="note">${esc((h.timestamp || "").slice(0, 10))}</td>
      </tr>`;
    }).join("");
}
if ($("btn-exposure")) $("btn-exposure").onclick = async () => {
  const b = $("btn-exposure");
  b.disabled = true;
  $("exp-msg").textContent = "querying Shodan's index…";
  $("exp-attr").style.display = "none";
  try {
    const r = await api("/api/osint/shodan/run", {});
    renderExposure(r.hosts);
    $("exp-msg").textContent = r.message || r.status;
    if (r.status === "not_configured") {
      $("exp-msg").innerHTML =
        `<b style="color:var(--amber,#ffb454)">Not set up —</b> ${esc(r.message)}. ` +
        `Add a paid Shodan key to <code>api_keys.shodan</code> in config.yaml.`;
    } else if (r.status === "denied") {
      $("exp-msg").innerHTML = `<b style="color:var(--red,#ff6b6b)">Denied —</b> ${esc(r.message)}`;
    }
    if (r.attribution && (r.hosts || []).length) {
      $("exp-attr").textContent = r.attribution;
      $("exp-attr").style.display = "";
    }
  } catch (e) {
    renderExposure([]);
    $("exp-msg").textContent = "exposure check failed: " + e.message;
  }
  b.disabled = false;
};

/* ---------- droid check (Android handset over adb, read-only) ----------
   Shows INDICATORS, never a clean/infected verdict. Severity orders the
   operator's attention; the evidence is always shown next to it. */
const _DROID_SEV = {
  high:  { dot: "var(--red, #ff6b6b)",  label: "LOOK NOW" },
  watch: { dot: "var(--amber, #ffb454)", label: "VERIFY" },
  clear: { dot: "var(--green, #7bd88f)", label: "nothing seen" },
};

function droidPre(stat) {
  const el = $("droid-pre");
  if (!el) return;
  if (!stat || stat.ready) { el.style.display = "none"; return; }
  el.style.display = "";
  el.innerHTML = `<b style="color:var(--amber,#ffb454)">NOT READY —</b> ${esc(stat.hint || "")}`;
}

function renderDroidFindings(findings) {
  const box = $("droid-findings");
  if (!box) return;
  const order = { high: 0, watch: 1, clear: 2 };
  const rows = (findings || []).slice().sort(
    (a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3));
  box.innerHTML = rows.map(f => {
    const s = _DROID_SEV[f.severity] || _DROID_SEV.clear;
    const items = (f.items || []).length
      ? `<div class="scroll" style="max-height:150px;margin-top:5px">` +
        (f.items || []).map(i => `<div class="note" style="font-family:monospace">${esc(i)}</div>`).join("") +
        `</div>` : "";
    return `<div style="border-left:3px solid ${s.dot};padding:6px 0 6px 9px;margin:8px 0">
      <div><b>${esc(f.title)}</b>
        <span class="tag" style="color:${s.dot}">${s.label}</span></div>
      <div class="note">${esc(f.detail || "")}</div>
      ${items}
      <div class="note" style="opacity:.85;margin-top:4px"><i>${esc(f.why || "")}</i></div>
    </div>`;
  }).join("");
}

function renderDroidApps(apps) {
  const t = $("tbl-droid-apps");
  if (!t) return;
  const rows = (apps || []).filter(a => a.score > 0);
  if (!rows.length) {
    t.innerHTML = `<tr><td class="note">No third-party app holds any of the
      mechanisms checked. That is not proof of a clean phone — a dormant app
      shows nothing until it runs.</td></tr>`;
    return;
  }
  t.innerHTML = `<tr><th>app</th><th>why it's here</th><th>permissions</th><th>rank</th></tr>` +
    rows.map(a => {
      const sev = a.score >= 6 ? "high" : (a.score >= 3 ? "watch" : "clear");
      const c = _DROID_SEV[sev].dot;
      return `<tr>
        <td style="font-family:monospace">${esc(a.pkg)}
          ${a.sideloaded ? `<span class="tag" style="color:${_DROID_SEV.watch.dot}">SIDELOADED</span>` : ""}</td>
        <td class="note">${(a.stack || []).map(esc).join("<br>") || "—"}</td>
        <td class="note">${(a.perms || []).map(esc).join(", ") || "—"}</td>
        <td style="color:${c};font-weight:700">${a.score}</td>
      </tr>`;
    }).join("");
}

function renderDroidConns(conns) {
  const t = $("tbl-droid-conns");
  if (!t) return;
  if (!(conns || []).length) {
    t.innerHTML = `<tr><td class="note">No live endpoints readable — Android
      restricts this to root on recent versions. Expected, and NOT a finding.</td></tr>`;
    return;
  }
  t.innerHTML = `<tr><th>remote endpoint</th></tr>` +
    conns.map(c => `<tr><td style="font-family:monospace">${esc(c)}</td></tr>`).join("");
}

async function loadDroid() {
  try {
    const stat = await api("/api/droid/status");
    droidPre(stat);
    $("droid-msg").textContent = stat.ready
      ? `phone ready (${stat.serial || "attached"})`
      : "";
  } catch (e) { $("droid-msg").textContent = e.message; }
}

if ($("btn-droid-recheck")) $("btn-droid-recheck").onclick = loadDroid;

if ($("btn-droid-scan")) $("btn-droid-scan").onclick = async () => {
  const b = $("btn-droid-scan");
  b.disabled = true;
  $("droid-msg").textContent = "reading the phone (read-only)…";
  try {
    const r = await api("/api/droid/scan", {});
    if (!r.ready) {
      droidPre(r.status);
      $("droid-msg").textContent = "not ready — see above";
      $("droid-results").style.display = "none";
    } else {
      droidPre(r.status);
      const p = r.phone || {};
      $("droid-phone").textContent =
        [p.brand, p.model].filter(Boolean).join(" ") || "phone";
      const c = r.counts || {};
      $("droid-counts").textContent =
        `Android ${p.android || "?"} · patch ${p.patch || "?"} · ` +
        `${c.third_party || 0} third-party apps, ${c.sideloaded || 0} sideloaded`;
      renderDroidFindings(r.findings);
      renderDroidApps(r.apps);
      renderDroidConns(r.connections);
      $("droid-limits").innerHTML = (r.limits || []).length
        ? "<b>Limits of this read:</b> " + (r.limits || []).map(esc).join(" ")
        : "";
      $("droid-results").style.display = "";
      const leads = (r.leads || []).length;
      $("droid-msg").textContent = leads
        ? `${leads} app(s) stack multiple mechanisms — start there`
        : "no app stacked multiple mechanisms";
    }
  } catch (e) { $("droid-msg").textContent = "scan failed: " + e.message; }
  b.disabled = false;
};
// LOCATE — direction-find the RF emission this network device also produces
// (receive-only). Tunes the monitor to the correlated WiFi/BLE frequency so the
// operator can walk signal strength to the physical device.
window.netLocate = async (id) => {
  const d = _netDevices.find(x => x.id === id);
  const rel = d && d.related && d.related[0];
  if (!rel) { $("net-msg").textContent = "no correlated RF emission to locate — run a WiFi/BLE inventory first"; return; }
  const freq = rel.freq_mhz || (rel.kind === "ble" ? 2440 : 2437);
  const label = `${d.vendor || d.name || d.ip} (${rel.kind.toUpperCase()})`;
  $("net-msg").textContent = `tuning the monitor to ${fmtF(freq)} — walk the signal strength to the device`;
  listenAt(freq, label);            // receive-only monitor dock; existing tune+listen path
};

/* ---------- calibration ---------- */
$("btn-cal").onclick = async () => {
  try {
    await api("/api/calibration/start", { test_freq_mhz: parseFloat($("cal-freq").value) });
    $("cal-msg").textContent = "running — keep the test source keyed/active…";
  } catch (e) { $("cal-msg").textContent = e.message; }
};
$("btn-cal-abort").onclick = () => api("/api/sweep/abort", {});
function renderCalRows(cal) {
  const rows = cal.rows || [];
  if (!rows.length && !cal.result) return;
  $("tbl-cal").innerHTML =
    "<tr><th>Strategy</th><th>Span MHz</th><th>Detection rate</th><th>Mean SNR dB</th><th>Time cost s</th></tr>" +
    rows.map(r => `<tr><td>${esc(r.strategy)}</td><td>${r.span_mhz}</td>` +
      `<td>${(r.detection_rate * 100).toFixed(0)}%</td><td>${r.mean_snr_db ?? "—"}</td>` +
      `<td>${r.time_cost_s}</td></tr>`).join("");
  if (cal.result && cal.result.winner) {
    $("cal-msg").innerHTML = `winner: <b>${esc(cal.result.winner.strategy)}</b> @ ` +
      `${cal.result.winner.span_mhz} MHz span — saved to config.yaml ` +
      `(seconds_per_sweep now ${cal.result.seconds_per_sweep})`;
  } else if (cal.error) $("cal-msg").textContent = "FAILED: " + cal.error;
}

/* ---------- boot ---------- */
loadSessions();
loadSweepHistory();
loadLabelPrefs();
[["leg-show", "show"], ["leg-identified", "identified"], ["leg-known", "known"],
 ["leg-anomaly", "anomaly"], ["leg-caution", "caution"], ["leg-scanning", "scanning"]]
  .forEach(([id, k]) => {
    const el = $(id);
    if (!el) return;
    el.onchange = () => {
      if (!App.labelPrefs) App.labelPrefs = defaultLabelPrefs();
      App.labelPrefs[k] = el.checked;
      saveLabelPrefs();
      App._lastLabelKey = "";
    };
  });
try {
  const ibw = localStorage.getItem("shield.listenIbw");
  const bs = localStorage.getItem("shield.listenBlockS");
  if (ibw && $("insp-ibw")) $("insp-ibw").value = ibw;
  if (bs && $("insp-block-s")) $("insp-block-s").value = bs;
} catch (e) {}
if ($("insp-ibw")) $("insp-ibw").onchange = () => {
  if (App.inspCenter != null) inspectByFreq(App.inspCenter, App.inspLabel, $("insp-mode").value);
};
if ($("insp-block-s")) $("insp-block-s").onchange = () => {
  if (App.inspCenter != null) inspectByFreq(App.inspCenter, App.inspLabel, $("insp-mode").value);
};

/* ---------- F6: sustain + info-pane wiring ---------- */
if ($("btn-sustain")) $("btn-sustain").onclick = async () => {
  const s = App._sweepSt || {};
  const on = !(s.sustaining || s.sustain_requested);
  try { await api("/api/sweep/sustain", { on }); }
  catch (e) { $("sweep-overlay-eta").textContent = "sustain failed: " + e.message; }
};
/* LIVE RBW knob: change resolution bandwidth mid-sweep — applies on the next
   segment. Blank restores the config/auto value. */
(() => {
  const inp = $("sweep-rbw"), now = $("sweep-rbw-now");
  if (!inp) return;
  inp.addEventListener("focus", () => { App._rbwFocused = true; });
  inp.addEventListener("blur", () => { App._rbwFocused = false; });
  const apply = async () => {
    const v = inp.value.trim();
    try {
      const r = await api("/api/sweep/rbw", { rbw_khz: v === "" ? "auto" : parseFloat(v) });
      if (now) now.textContent = !r.adjustable ? "fixed (RFE)"
        : r.achieved_khz != null ? `→ ${r.achieved_khz} kHz next segment` : "";
    } catch (e) { if (now) now.textContent = "RBW: " + (e.message || e); }
  };
  inp.addEventListener("change", apply);   // commit on Enter / blur
})();
document.querySelectorAll("#sweep-info-tabs button[data-tab]").forEach(b =>
  b.onclick = () => setInfoTab(b.dataset.tab));
/* LIVE window: drag a span on the main chart; double-click resets to the segment */
(() => {
  const c = $("livechart");
  if (!c) return;
  let drag = null;
  const canPick = () => App.infoTab === "live" && App._sweepSt && App._sweepSt.sustaining;
  c.addEventListener("mousedown", e => { if (canPick()) drag = { x0: e.offsetX, x1: null }; });
  c.addEventListener("mousemove", e => { if (drag) drag.x1 = e.offsetX; });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    const t = App._lastTrace;
    if (t && drag.x1 != null && Math.abs(drag.x1 - drag.x0) > 8) {
      const W = c.clientWidth, lo = t.low, hi = t.high;
      const a = Math.min(drag.x0, drag.x1) / W, b = Math.max(drag.x0, drag.x1) / W;
      App.liveWindow = { lo: lo + a * (hi - lo), hi: lo + b * (hi - lo) };
    }
    drag = null;
  });
  c.addEventListener("dblclick", () => { if (canPick()) App.liveWindow = null; });
})();

/* ========== DEVICE CAPTURE LAB (guided DUT characterization) ========== */
const DevLab = {
  pkg: null,
  meta: null,
  stream: null,
  photoDataUrl: null,
};

function dlNfSvg(kind) {
  // Simple inline diagrams (no external assets)
  const common = 'xmlns="http://www.w3.org/2000/svg" width="72" height="56" viewBox="0 0 72 56"';
  if (kind === "loop")
    return `<svg ${common}><circle cx="36" cy="28" r="18" fill="none" stroke="#1abc9c" stroke-width="3"/><line x1="36" y1="10" x2="36" y2="4" stroke="#1abc9c" stroke-width="2"/><text x="36" y="52" text-anchor="middle" fill="#7b8794" font-size="9">H-loop</text></svg>`;
  if (kind === "stub")
    return `<svg ${common}><line x1="36" y1="48" x2="36" y2="12" stroke="#f39c12" stroke-width="3"/><circle cx="36" cy="10" r="3" fill="#f39c12"/><text x="36" y="54" text-anchor="middle" fill="#7b8794" font-size="9">E-stub</text></svg>`;
  if (kind === "clamp")
    return `<svg ${common}><path d="M20 30 a16 16 0 1 1 32 0" fill="none" stroke="#3498db" stroke-width="3"/><rect x="28" y="28" width="16" height="12" rx="2" fill="none" stroke="#3498db" stroke-width="2"/><text x="36" y="52" text-anchor="middle" fill="#7b8794" font-size="9">clamp</text></svg>`;
  if (kind === "patch")
    return `<svg ${common}><rect x="18" y="14" width="36" height="28" rx="3" fill="none" stroke="#9b59b6" stroke-width="2"/><rect x="26" y="20" width="20" height="16" fill="#9b59b633" stroke="#9b59b6"/><text x="36" y="52" text-anchor="middle" fill="#7b8794" font-size="9">patch</text></svg>`;
  if (kind === "whip")
    return `<svg ${common}><line x1="36" y1="48" x2="36" y2="8" stroke="#2ecc71" stroke-width="2"/><path d="M36 8 q8 10 0 20" fill="none" stroke="#2ecc71" stroke-width="2"/><text x="36" y="54" text-anchor="middle" fill="#7b8794" font-size="9">whip</text></svg>`;
  return `<svg ${common}><rect x="20" y="16" width="32" height="24" rx="4" fill="none" stroke="#7b8794" stroke-width="2" stroke-dasharray="4 3"/><text x="36" y="52" text-anchor="middle" fill="#7b8794" font-size="9">other</text></svg>`;
}

function dlDrawSpectrum(spec, cid) {
  const c = $(cid || "dl-spec");
  if (!c) return;
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  ctx.fillStyle = "#0a0e14";
  ctx.fillRect(0, 0, W, H);
  const freqs = (spec && spec.freqs_mhz) || [];
  const pwr = (spec && spec.power_db) || [];
  if (freqs.length < 2 || pwr.length < 2) {
    ctx.fillStyle = "#7b8794";
    ctx.font = "12px monospace";
    ctx.fillText("No spectrum yet — run CAPTURE NOW", 12, H / 2);
    return;
  }
  const pmin = Math.min(...pwr), pmax = Math.max(...pwr);
  const span = Math.max(1, pmax - pmin);
  ctx.strokeStyle = "#1abc9c";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < pwr.length; i++) {
    const x = (i / (pwr.length - 1)) * (W - 8) + 4;
    const y = H - 8 - ((pwr[i] - pmin) / span) * (H - 16);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  // peak marker
  let imax = 0;
  for (let i = 1; i < pwr.length; i++) if (pwr[i] > pwr[imax]) imax = i;
  const px = (imax / (pwr.length - 1)) * (W - 8) + 4;
  ctx.strokeStyle = "#e74c3c";
  ctx.beginPath(); ctx.moveTo(px, 4); ctx.lineTo(px, H - 4); ctx.stroke();
  ctx.fillStyle = "#cfd8e3";
  ctx.font = "11px monospace";
  ctx.fillText(`peak ${pwr[imax].toFixed(1)} dB  floor ${(spec.floor_db ?? pmin).toFixed(1)}  SNR ${(spec.snr_db ?? 0).toFixed(1)} dB`, 8, 14);
}

function dlShowCaptureVisual(cap) {
  const ok = cap && cap.capture_ok;
  const snr = (cap && cap.features && cap.features.snr_db != null)
    ? cap.features.snr_db
    : (cap && cap.spectrum && cap.spectrum.snr_db);
  if ($("dl-cap-status")) {
    $("dl-cap-status").textContent = (cap && cap.capture_status) || "—";
    $("dl-cap-status").style.color = ok ? "var(--green)" : "var(--amber)";
  }
  if ($("dl-cap-snr"))
    $("dl-cap-snr").textContent = snr != null ? `SNR ${snr} dB` : "SNR —";
  const bar = $("dl-snr-bar");
  if (bar) {
    const pct = Math.max(0, Math.min(100, ((snr || 0) / 40) * 100));
    bar.style.width = pct + "%";
    bar.style.background = ok ? "var(--green)" : (snr > 3 ? "var(--amber)" : "var(--red)");
  }
  dlDrawSpectrum(cap && cap.spectrum);
}

async function loadDeviceLab() {
  try {
    DevLab.meta = await api("/api/device_lab/meta");
    const r = DevLab.meta.radio || {};
    if ($("dl-radio"))
      $("dl-radio").textContent = "Radio: " +
        (r.model || r.driver || r.active_source || "not connected — CONNECT before RF steps");
    // Libre RXA omni / RXB directional / NF probes
    const fillSel = (el, list, valKey) => {
      if (!el || !list) return;
      el.innerHTML = list.map(a =>
        `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    };
    fillSel($("dl-rxa-ant"), DevLab.meta.omni_antennas);
    fillSel($("dl-rxb-ant"), DevLab.meta.dir_antennas);
    fillSel($("dl-cap-nf"), DevLab.meta.near_field_antennas);
    fillSel($("dl-tri-nf"), DevLab.meta.near_field_antennas);
    // NF gallery with diagrams
    const gal = $("dl-nf-gallery");
    if (gal && DevLab.meta.near_field_antennas) {
      const cur = ($("dl-nf-ant") && $("dl-nf-ant").value) || "hfield_loop";
      gal.innerHTML = DevLab.meta.near_field_antennas.map(a =>
        `<button type="button" class="btn dl-nf-card" data-nf="${esc(a.id)}"
          style="display:flex;flex-direction:column;align-items:center;width:110px;padding:8px;
          ${a.id === cur ? "border-color:var(--cyan)" : ""}"
          title="${esc(a.use || "")}">
          ${dlNfSvg(a.svg || "other")}
          <span style="font-size:10px;margin-top:4px;text-align:center">${esc(a.name)}</span>
        </button>`).join("");
      gal.querySelectorAll(".dl-nf-card").forEach(btn => {
        btn.onclick = () => {
          if ($("dl-nf-ant")) $("dl-nf-ant").value = btn.dataset.nf;
          if ($("dl-cap-nf")) $("dl-cap-nf").value = btn.dataset.nf;
          gal.querySelectorAll(".dl-nf-card").forEach(b => {
            b.style.borderColor = b.dataset.nf === btn.dataset.nf ? "var(--cyan)" : "";
          });
        };
      });
    }
    const cat = $("dl-catalog");
    if (cat && DevLab.meta.catalog) {
      cat.innerHTML = DevLab.meta.catalog.map(c =>
        `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    }
    const packs = await api("/api/device_lab/packages");
    if ($("dl-packages")) {
      $("dl-packages").innerHTML =
        "<tr><th>ID</th><th>Name</th><th>Status</th><th>Freq</th><th></th></tr>" +
        (packs.packages || []).map(p =>
          `<tr><td>${esc(p.id)}</td><td>${esc(p.name)}</td>` +
          `<td><span class="pill">${esc(p.status || "")}</span></td>` +
          `<td>${p.center_mhz != null ? fmtF(p.center_mhz) : "—"}</td>` +
          `<td><button class="btn" onclick="devLabOpen('${esc(p.id)}')">OPEN</button></td></tr>`
        ).join("") || "<tr><td colspan=5 class='note'>no packages yet</td></tr>";
    }
  } catch (e) {
    if ($("dl-msg")) $("dl-msg").textContent = "lab meta failed: " + e.message;
  }
}

function devLabShowGuide(g) {
  if (!g) return;
  if ($("dl-guide-title")) $("dl-guide-title").textContent = g.title || "";
  if ($("dl-guide-do")) {
    $("dl-guide-do").innerHTML = (g.do || []).map(x => `<li>${esc(x)}</li>`).join("");
  }
  if ($("dl-guide-why"))
    $("dl-guide-why").textContent = g.why ? ("Why: " + g.why) : "";
}

function devLabShowStep(step) {
  document.querySelectorAll(".dl-step").forEach(el => { el.style.display = "none"; });
  step = step || "photo";
  let id;
  if (step === "capture_near_field") id = "dl-step-triage";
  else if (step === "capture_ten_ft" || step === "capture_next_room" ||
           step.startsWith("capture_stop:")) id = "dl-step-capture";
  else id = ({
    photo: "dl-step-photo",
    identify: "dl-step-identify",
    antenna: "dl-step-antenna",
    freq_confirm: "dl-step-freq",
    states: "dl-step-states",
    stimulate: "dl-step-stim",
    demod: "dl-step-demod",
    ready_to_commit: "dl-step-commit",
    committed: "dl-step-commit",
  })[step] || "dl-step-photo";
  if ($(id)) $(id).style.display = "";
  if (id === "dl-step-capture") {
    const did = step.startsWith("capture_stop:")
      ? step.slice("capture_stop:".length) : step.replace("capture_", "");
    const dist = (DevLab.meta && DevLab.meta.distances || []).find(d => d.id === did);
    const stop = (DevLab.pkg && DevLab.pkg.stops || []).find(s => s.id === did);
    if ($("dl-cap-title"))
      $("dl-cap-title").textContent =
        "Capture @ " + (dist ? dist.label : (stop ? stop.label : did));
    if ($("dl-cap-instr"))
      $("dl-cap-instr").textContent = dist ? dist.instruction :
        (stop ? `Move the DUT to: ${stop.label}. Hold position until captured.`
              : "Position the DUT, then capture.");
    if ($("dl-cap-go")) $("dl-cap-go").dataset.distance = did;
    const defPort = (dist && dist.default_port) || "RXA";
    if ($("dl-cap-port")) $("dl-cap-port").value = defPort;
    if ($("dl-cap-nf-wrap"))
      $("dl-cap-nf-wrap").style.display = did === "near_field" ? "" : "none";
    // show last spectrum for this distance if any
    const prev = DevLab.pkg && DevLab.pkg.captures && DevLab.pkg.captures[did];
    if (prev) dlShowCaptureVisual(prev);
    else if (!DLLive.timer) {
      dlDrawSpectrum(null);
      if ($("dl-cap-status")) $("dl-cap-status").textContent = "Waiting for capture…";
      if ($("dl-snr-bar")) $("dl-snr-bar").style.width = "0%";
    }
    if (DLLive.timer) DLLive.mode = "ladder";
  }
  if (id === "dl-step-triage") {
    if ($("dl-tri-nf") && DevLab.meta && DevLab.meta.near_field_antennas &&
        !$("dl-tri-nf").options.length)
      $("dl-tri-nf").innerHTML = DevLab.meta.near_field_antennas.map(a =>
        `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    if ($("dl-tri-nf") && DevLab.pkg && DevLab.pkg.nf_antenna)
      $("dl-tri-nf").value = DevLab.pkg.nf_antenna;
    if ($("dl-tri-center") && DevLab.pkg && DevLab.pkg.center_mhz != null)
      $("dl-tri-center").value = DevLab.pkg.center_mhz;
    if (DevLab.stream && $("dl-tri-cam")) $("dl-tri-cam").srcObject = DevLab.stream;
    if (DLLive.timer) DLLive.mode = "triage";
  }
  if (id === "dl-step-states") {
    if (DevLab.pkg) dlRenderStates(DevLab.pkg);
    if (DLLive.timer) DLLive.mode = "states";
  }
  // TX probe / demod / commit need the radio — stop the live loop
  if ((id === "dl-step-stim" || id === "dl-step-demod" || id === "dl-step-commit")
      && DLLive.timer) dlLiveStop();
}

function devLabRenderPkg(pkg, guide) {
  DevLab.pkg = pkg;
  if ($("dl-panel")) $("dl-panel").style.display = "";
  if ($("dl-pkg-id")) $("dl-pkg-id").textContent = pkg.id || "";
  if ($("dl-status")) $("dl-status").textContent = pkg.status || pkg.step || "";
  devLabShowGuide(guide);
  devLabShowStep(pkg.step);
  if ($("dl-center") && pkg.center_mhz != null) $("dl-center").value = pkg.center_mhz;
  if ($("dl-bw") && pkg.bw_khz != null) $("dl-bw").value = pkg.bw_khz;
  if ($("dl-center2") && pkg.center_mhz != null) $("dl-center2").value = pkg.center_mhz;
  if ($("dl-bw2") && pkg.bw_khz != null) $("dl-bw2").value = pkg.bw_khz;
  if ($("dl-catalog") && pkg.device_type_id) $("dl-catalog").value = pkg.device_type_id;
  if ($("dl-name") && pkg.device_name) $("dl-name").value = pkg.device_name;
  if ($("dl-desc") && pkg.device_description != null) $("dl-desc").value = pkg.device_description || "";
  if ($("dl-desc2") && pkg.device_description != null) $("dl-desc2").value = pkg.device_description || "";
  if ($("dl-guesses") && pkg.freq_guesses_mhz)
    $("dl-guesses").textContent = "Suggested frequencies (MHz): " +
      (pkg.freq_guesses_mhz || []).join(", ") +
      (pkg.lookup && pkg.lookup.notes ? " — " + pkg.lookup.notes : "");
  if ($("dl-lookup-note") && pkg.lookup && pkg.lookup.note)
    $("dl-lookup-note").textContent = pkg.lookup.note;
  if ($("dl-cap-results")) {
    const caps = pkg.captures || {};
    $("dl-cap-results").innerHTML = Object.keys(caps).map(k => {
      const c = caps[k];
      const f = c.features || {};
      return `<div class="note"><b>${esc(k)}</b>: SNR ${f.snr_db} dB · ` +
        `${esc(f.modulation_class || "")} · duty ${f.duty_cycle}` +
        (c.behavior && c.behavior.label ? ` · ${esc(c.behavior.label)}` : "") +
        `</div>`;
    }).join("") || "";
  }
  if ($("dl-summary")) {
    $("dl-summary").textContent = JSON.stringify({
      name: pkg.device_name,
      description: pkg.device_description,
      type: pkg.device_type_name,
      center_mhz: pkg.center_mhz, bw_khz: pkg.bw_khz,
      antenna: pkg.antenna,
      rx_port: pkg.rx_port_label || "RX-B (left)",
      tx_port: pkg.tx_port_label || "TX/RX-A",
      radio: (pkg.radio || {}).model || (pkg.radio || {}).driver,
      captures: Object.keys(pkg.captures || {}),
      stimulate: pkg.stimulate && (pkg.stimulate.verdict || pkg.stimulate.skipped),
      demod: pkg.demod && ((pkg.demod.interpretation && pkg.demod.interpretation.summary)
        || (pkg.demod.features && pkg.demod.features.modulation_class) || pkg.demod.skipped),
    }, null, 2);
  }
  if ($("dl-log")) {
    $("dl-log").innerHTML = (pkg.tech_log || []).slice().reverse().map(e =>
      `<div>${new Date((e.utc || 0) * 1000).toLocaleTimeString()} — ${esc(e.msg)}</div>`
    ).join("");
  }
  dlRenderLadder(pkg);
  dlRenderIdent(pkg);
  if ((pkg.step || "") === "states") dlRenderStates(pkg);
  if (pkg.walk_test && $("dl-walk-verdict") && !$("dl-walk-verdict").innerHTML) {
    const w = pkg.walk_test;
    const good = w.verdict === "tracks_motion";
    $("dl-walk-verdict").innerHTML =
      `<b style="color:${good ? "var(--green)" : "var(--amber)"}">` +
      `${esc((w.verdict || "").replace(/_/g, " ").toUpperCase())}</b> ` +
      `r=${w.r ?? "—"} · swing ${w.swing_db ?? "—"} dB — ${esc(w.plain || "")}`;
  }
  if (pkg.status === "committed" && $("dl-commit-msg"))
    $("dl-commit-msg").textContent = "Ready to commit — package committed.";
}

window.devLabOpen = async (id) => {
  try {
    const r = await api("/api/device_lab/" + id);
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

if ($("dl-start")) $("dl-start").onclick = async () => {
  try {
    const r = await api("/api/device_lab/start", {});
    DevLab.photoDataUrl = null;
    if ($("dl-preview")) $("dl-preview").style.display = "none";
    devLabRenderPkg(r.package, r.guide);
    if ($("dl-msg")) $("dl-msg").textContent = "Package started — follow the blue instructions.";
    loadDeviceLab();
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};
if ($("dl-refresh")) $("dl-refresh").onclick = () => loadDeviceLab();

if ($("dl-cam-start")) $("dl-cam-start").onclick = async () => {
  try {
    if (DevLab.stream) DevLab.stream.getTracks().forEach(t => t.stop());
    DevLab.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment" }, audio: false });
    $("dl-video").srcObject = DevLab.stream;
    if ($("dl-msg")) $("dl-msg").textContent = "Camera live — frame the DUT, then SNAP PHOTO.";
  } catch (e) {
    if ($("dl-msg")) $("dl-msg").textContent =
      "Camera failed: " + e.message + " — use UPLOAD instead, or allow camera permission.";
  }
};
if ($("dl-snap")) $("dl-snap").onclick = () => {
  const v = $("dl-video"), c = $("dl-canvas");
  if (!v || !c) return;
  c.style.display = "";
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
  DevLab.photoDataUrl = c.toDataURL("image/jpeg", 0.85);
  const img = $("dl-preview");
  if (img) { img.src = DevLab.photoDataUrl; img.style.display = ""; }
  if ($("dl-msg")) $("dl-msg").textContent = "Photo snapped — SAVE PHOTO → CONTINUE.";
};
if ($("dl-upload")) $("dl-upload").onchange = (ev) => {
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    DevLab.photoDataUrl = rd.result;
    const img = $("dl-preview");
    if (img) { img.src = DevLab.photoDataUrl; img.style.display = ""; }
    if ($("dl-msg")) $("dl-msg").textContent = "Photo loaded — SAVE PHOTO → CONTINUE.";
  };
  rd.readAsDataURL(f);
};

async function devLabPhotoSubmit(skip) {
  if (!DevLab.pkg) return;
  try {
    const body = {
      name: ($("dl-name") && $("dl-name").value) || "",
      description: ($("dl-desc") && $("dl-desc").value) || "",
      notes: ($("dl-photo-notes") && $("dl-photo-notes").value) || "",
    };
    if (skip) body.skip_photo = true;
    else {
      if (!DevLab.photoDataUrl) {
        if ($("dl-msg")) $("dl-msg").textContent = "Snap or upload a photo first (or SKIP PHOTO).";
        return;
      }
      body.photo = DevLab.photoDataUrl;
    }
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/photo", body);
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
}
if ($("dl-photo-next")) $("dl-photo-next").onclick = () => devLabPhotoSubmit(false);
if ($("dl-photo-skip")) $("dl-photo-skip").onclick = () => devLabPhotoSubmit(true);

if ($("dl-id-next")) $("dl-id-next").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/identify", {
      catalog_id: $("dl-catalog") && $("dl-catalog").value,
      name: ($("dl-name") && $("dl-name").value) || "",
      description: ($("dl-desc2") && $("dl-desc2").value)
        || ($("dl-desc") && $("dl-desc").value) || "",
      center_mhz: parseFloat($("dl-center") && $("dl-center").value),
      bw_khz: parseFloat($("dl-bw") && $("dl-bw").value),
    });
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

if ($("dl-ant-next")) $("dl-ant-next").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/antenna", {
      rxa_antenna: $("dl-rxa-ant") && $("dl-rxa-ant").value,
      rxb_antenna: $("dl-rxb-ant") && $("dl-rxb-ant").value,
      nf_antenna: ($("dl-nf-ant") && $("dl-nf-ant").value) || "hfield_loop",
      notes: ($("dl-ant-notes") && $("dl-ant-notes").value) || "",
    });
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

if ($("dl-freq-next")) $("dl-freq-next").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/freq", {
      center_mhz: parseFloat($("dl-center2") && $("dl-center2").value),
      bw_khz: parseFloat($("dl-bw2") && $("dl-bw2").value),
    });
    devLabRenderPkg(r.package, r.guide);
    if ($("dl-msg")) $("dl-msg").textContent =
      "Freq locked. Position DUT at 10 feet, then CAPTURE NOW.";
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

if ($("dl-cap-go")) $("dl-cap-go").onclick = async () => {
  if (!DevLab.pkg) return;
  const distance_id = $("dl-cap-go").dataset.distance || "ten_ft";
  if ($("dl-msg")) $("dl-msg").textContent = "Capturing IQ… keep DUT in position.";
  if ($("dl-cap-status")) $("dl-cap-status").textContent = "Capturing…";
  if ($("dl-snr-bar")) { $("dl-snr-bar").style.width = "30%"; $("dl-snr-bar").style.background = "var(--blue)"; }
  try {
    const port = ($("dl-cap-port") && $("dl-cap-port").value) || "RXA";
    const body = {
      distance_id,
      rx_port: port,
      notes: ($("dl-cap-notes") && $("dl-cap-notes").value) || "",
      center_mhz: DevLab.pkg.center_mhz,
      bw_khz: DevLab.pkg.bw_khz,
    };
    if (distance_id === "near_field")
      body.antenna_id = ($("dl-cap-nf") && $("dl-cap-nf").value)
        || ($("dl-nf-ant") && $("dl-nf-ant").value) || DevLab.pkg.nf_antenna;
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/capture", body);
    devLabRenderPkg(r.package, r.guide);
    dlShowCaptureVisual(r.capture);
    const f = (r.capture && r.capture.features) || {};
    if ($("dl-msg")) $("dl-msg").textContent =
      `${r.capture && r.capture.capture_status} · ${distance_id} on ${port} · SNR ${f.snr_db} dB · ${f.modulation_class || "—"}`;
  } catch (e) {
    if ($("dl-msg")) $("dl-msg").textContent = "Capture failed: " + e.message;
    if ($("dl-cap-status")) {
      $("dl-cap-status").textContent = "FAILED — " + e.message;
      $("dl-cap-status").style.color = "var(--red)";
    }
  }
};

if ($("dl-stim-go")) $("dl-stim-go").onclick = async () => {
  if (!DevLab.pkg) return;
  if ($("dl-msg")) $("dl-msg").textContent = "TX probe running…";
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/stimulate", {
      center_mhz: DevLab.pkg.center_mhz,
      override_band: !!( $("dl-stim-override") && $("dl-stim-override").checked ),
    });
    devLabRenderPkg(r.package, r.guide);
    const s = r.stimulate || {};
    if ($("dl-stim-result"))
      $("dl-stim-result").innerHTML =
        `<p><b>${esc((s.verdict || "").replace(/_/g, " "))}</b></p>` +
        `<p class="note">${esc(s.plain || "")}</p>`;
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = "Stimulate failed: " + e.message; }
};
if ($("dl-stim-skip")) $("dl-stim-skip").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/stimulate", { skip: true });
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

if ($("dl-demod-go")) $("dl-demod-go").onclick = async () => {
  if (!DevLab.pkg) return;
  if ($("dl-msg")) $("dl-msg").textContent = "Analyzing / demodulating DUT…";
  try {
    const mode = ($("dl-demod-mode") && $("dl-demod-mode").value) || "";
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/demod", {
      center_mhz: DevLab.pkg.center_mhz, mode });
    devLabRenderPkg(r.package, r.guide);
    const d = r.demod || {};
    const interp = d.interpretation || {};
    if ($("dl-demod-result"))
      $("dl-demod-result").innerHTML =
        `<p><b>${esc(interp.summary || (d.features && d.features.modulation_class) || "done")}</b></p>` +
        `<p class="note">${esc((d.candidates || []).slice(0, 3).map(c => c.name).join(" · "))}</p>`;
    if (d.audio && $("dl-audio")) {
      $("dl-audio").src = d.audio;
      $("dl-audio").style.display = "";
    }
    if ($("dl-msg")) $("dl-msg").textContent =
      "Ready to commit — review summary and click COMMIT PACKAGE.";
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = "Demod failed: " + e.message; }
};
if ($("dl-demod-skip")) $("dl-demod-skip").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/demod", { skip: true });
    devLabRenderPkg(r.package, r.guide);
    if ($("dl-msg")) $("dl-msg").textContent = "Ready to commit.";
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

if ($("dl-commit")) $("dl-commit").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api("/api/device_lab/" + DevLab.pkg.id + "/commit", {
      notes: ($("dl-commit-notes") && $("dl-commit-notes").value) || "",
      save_signature: !($("dl-save-sig") && !$("dl-save-sig").checked),
    });
    devLabRenderPkg(r.package, r.guide);
    if ($("dl-commit-msg")) $("dl-commit-msg").textContent =
      r.message || "Ready to commit — package saved.";
    if ($("dl-msg")) $("dl-msg").textContent = r.message || "Committed.";
    if (DevLab.stream) {
      DevLab.stream.getTracks().forEach(t => t.stop());
      DevLab.stream = null;
    }
    loadDeviceLab();
  } catch (e) { if ($("dl-msg")) $("dl-msg").textContent = e.message; }
};

/* ---------- device lab LIVE loop (walk test, range ladder, states) ---------- */
const DLLive = {
  timer: null, mode: null, lastPhase: null, walkActive: false,
  rec: null, recChunks: [], recT0: null, autoBusy: false, ac: null,
};

function dlMsg(t) { if ($("dl-msg")) $("dl-msg").textContent = t; }

function dlVoiceOn() { return localStorage.getItem("dl_voice") !== "0"; }
document.querySelectorAll(".dl-voice").forEach(cb => {
  cb.checked = dlVoiceOn();
  cb.onchange = () => {
    localStorage.setItem("dl_voice", cb.checked ? "1" : "0");
    document.querySelectorAll(".dl-voice").forEach(o => { o.checked = cb.checked; });
  };
});
function dlSpeak(txt) {
  if (!dlVoiceOn() || !window.speechSynthesis) return;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(txt);
    u.rate = 1.05;
    speechSynthesis.speak(u);
  } catch (e) { /* voice is best-effort */ }
}
function dlChime(ok = true) {
  try {
    const ac = DLLive.ac || (DLLive.ac =
      new (window.AudioContext || window.webkitAudioContext)());
    const o = ac.createOscillator(), g = ac.createGain();
    o.connect(g); g.connect(ac.destination);
    o.frequency.value = ok ? 880 : 220;
    g.gain.value = 0.08;
    o.start(); o.stop(ac.currentTime + 0.18);
  } catch (e) { /* no audio ctx */ }
}

function dlDrawTrace(cid, trace) {
  const c = $(cid);
  if (!c) return;
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  ctx.fillStyle = "#0a0e14";
  ctx.fillRect(0, 0, W, H);
  const pts = (trace || []).filter(p => p && p[1] != null);
  if (pts.length < 2) {
    ctx.fillStyle = "#7b8794"; ctx.font = "11px monospace";
    ctx.fillText("live power appears here", 12, H / 2);
    return;
  }
  const ys = pts.map(p => p[1]);
  const pmin = Math.min(...ys), pmax = Math.max(...ys);
  const span = Math.max(4, pmax - pmin);
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
  const tspan = Math.max(1, t1 - t0);
  ctx.strokeStyle = "#2ecc71"; ctx.lineWidth = 1.5;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = ((p[0] - t0) / tspan) * (W - 8) + 4;
    const y = H - 6 - ((p[1] - pmin) / span) * (H - 12);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#cfd8e3"; ctx.font = "11px monospace";
  const last = pts[pts.length - 1];
  ctx.fillText(`peak ${last[1].toFixed(1)} dB` +
    (last[2] != null ? `  SNR ${last[2].toFixed(1)} dB` : ""), 8, 12);
}

function dlRenderLadder(pkg) {
  const el = $("dl-ladder");
  if (!el || !pkg) return;
  const base = [["near_field", "Near-field"], ["ten_ft", "10 ft"],
                ["next_room", "Next room"]];
  const all = base.concat((pkg.stops || []).map(s => [s.id, s.label]));
  const cur = (pkg.step || "").startsWith("capture_stop:")
    ? pkg.step.slice("capture_stop:".length)
    : (pkg.step || "").replace("capture_", "");
  el.innerHTML = all.map(([id, label]) => {
    const cap = pkg.captures && pkg.captures[id];
    const active = cur === id;
    const snr = cap && cap.spectrum && cap.spectrum.snr_db;
    return `<span class="pill ${cap ? "ok" : ""}"` +
      `${active ? ' style="border:1px solid var(--cyan);color:var(--cyan)"' : ""}>` +
      `${cap ? "✓ " : ""}${esc(label)}${snr != null ? ` · ${snr} dB` : ""}</span>`;
  }).join(`<span class="note">→</span>`) +
    `<span class="note">→</span><span class="pill">states</span>`;
}

function dlRenderIdent(pkg) {
  const el = $("dl-ident");
  if (!el || !pkg) return;
  const idn = pkg.ident || {};
  const color = { identified: "var(--green)", rf_confirmed: "var(--cyan)",
                  visual_only: "var(--amber)" }[idn.status] || "var(--dim)";
  let html = `<b style="color:${color}">` +
    `${esc(idn.summary || "No evidence yet — go live and capture.")}</b>`;
  if ((idn.visual || []).length)
    html += `<div class="note" style="margin-top:4px">Visual: ` +
      idn.visual.map(v => esc(v.name)).join(" · ") + `</div>`;
  if ((idn.rf || []).length)
    html += `<div class="note">RF: ` + idn.rf.slice(0, 3).map(v =>
      `${esc(v.name)} (${v.seen}×)`).join(" · ") + `</div>`;
  el.innerHTML = html;
}

function dlStatesFor(pkg) {
  const cat = ((DevLab.meta && DevLab.meta.catalog) || [])
    .find(c => c.id === pkg.device_type_id);
  return (cat && cat.states) || [
    { id: "idle", label: "Idle / at rest", instruction: "Power on, untouched." },
    { id: "active", label: "In use", instruction: "Use the device normally." },
    { id: "off", label: "Powered off", instruction: "Cut power — must go silent." },
  ];
}

function dlRenderStates(pkg) {
  const el = $("dl-states");
  if (!el || !pkg) return;
  const states = dlStatesFor(pkg);
  const done = pkg.states || {};
  const nextId = (states.find(s => !done[s.id]) || {}).id;
  el.innerHTML = states.map(s => {
    const d = done[s.id];
    const active = s.id === nextId;
    return `<div class="card" style="min-width:190px;flex:1` +
      `${active ? ";border-color:var(--cyan)" : ""}">` +
      `<b style="color:${d ? "var(--green)" : (active ? "var(--cyan)" : "var(--dim)")}">` +
      `${d ? "✓ " : ""}${esc(s.label)}${active ? " — DO THIS NOW" : ""}</b>` +
      `<p class="note">${esc(s.instruction)}</p>` +
      (d ? `<p class="note">SNR ${(d.spectrum || {}).snr_db ?? "—"} dB · ` +
           `${d.grabs || 0} grabs</p>`
         : (active ? `<button class="btn primary" ` +
                     `onclick="dlStateCapture('${esc(s.id)}')">⦿ CAPTURE STATE</button>` : "")) +
      `</div>`;
  }).join("");
}

window.dlStateCapture = async (id) => {
  if (!DevLab.pkg) return;
  try {
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/state_capture`,
                        { state_id: id });
    dlChime(true);
    devLabRenderPkg(r.package, r.guide);
    const states = dlStatesFor(r.package);
    const nxt = states.find(s => !(r.package.states || {})[s.id]);
    if (nxt) dlSpeak(`Captured. Next state: ${nxt.label}. ${nxt.instruction}`);
    else dlSpeak("All states captured. Press states done.");
  } catch (e) { dlMsg("State capture failed: " + e.message); }
};

function dlSetLiveButtons(on) {
  [["dl-tri-live", "▶ GO LIVE"], ["dl-cap-live", "▶ GO LIVE (hands-free)"],
   ["dl-st-live", "▶ GO LIVE"]].forEach(([id, label]) => {
    if ($(id)) $(id).textContent = on ? "■ STOP LIVE" : label;
  });
}

async function dlLiveStart(mode) {
  if (!DevLab.pkg) return;
  const body = {};
  if (mode === "triage") {
    body.rx_port = ($("dl-tri-port") && $("dl-tri-port").value) || "RXB";
    const c = parseFloat($("dl-tri-center") && $("dl-tri-center").value);
    if (c) body.center_mhz = c;
  } else {
    body.rx_port = ($("dl-cap-port") && $("dl-cap-port").value) || "RXA";
    if (DevLab.pkg.center_mhz) body.center_mhz = DevLab.pkg.center_mhz;
  }
  try {
    await api(`/api/device_lab/${DevLab.pkg.id}/live/start`, body);
  } catch (e) {
    dlMsg("Live start failed: " + e.message);
    return;
  }
  DLLive.mode = mode;
  clearInterval(DLLive.timer);
  DLLive.timer = setInterval(dlLiveTick, 350);
  dlSetLiveButtons(true);
  if (mode === "triage")
    dlSpeak("Live. Hold the probe within one foot of the device.");
}

async function dlLiveStop() {
  clearInterval(DLLive.timer);
  DLLive.timer = null;
  dlSetLiveButtons(false);
  if (DevLab.pkg) {
    try { await api(`/api/device_lab/${DevLab.pkg.id}/live/stop`, {}); }
    catch (e) { /* already stopped */ }
  }
}

async function dlLiveTick() {
  if (!DevLab.pkg) return dlLiveStop();
  let s;
  try { s = await api(`/api/device_lab/${DevLab.pkg.id}/live/state`); }
  catch (e) { return; }
  if (!s.running) {
    if (s.error) { dlMsg("LIVE stopped: " + s.error); dlChime(false); }
    clearInterval(DLLive.timer); DLLive.timer = null;
    dlSetLiveButtons(false);
    return;
  }
  const snrTxt = s.snr_now != null ? `SNR ${s.snr_now.toFixed(1)} dB` : "SNR —";
  const barPct = Math.max(0, Math.min(100, ((s.snr_now || 0) / 40) * 100));
  const barColor = (s.snr_now || 0) >= 6 ? "var(--green)"
    : ((s.snr_now || 0) > 3 ? "var(--amber)" : "var(--red)");
  if (DLLive.mode === "triage") {
    if ($("dl-tri-status"))
      $("dl-tri-status").textContent = s.scanning
        ? `SCANNING… now ${fmtF(s.center_mhz)} — power the DUT / press its button`
        : `LOCKED @ ${fmtF(s.center_mhz)} — energy ${((s.snr_now || 0) >= 6) ? "PRESENT" : "weak"}`;
    if ($("dl-tri-snr")) $("dl-tri-snr").textContent = snrTxt;
    if ($("dl-tri-gain")) $("dl-tri-gain").textContent = dlGainTxt(s);
    dlHotBanner("dl-tri-hot", s);
    if ($("dl-tri-bar")) {
      $("dl-tri-bar").style.width = barPct + "%";
      $("dl-tri-bar").style.background = barColor;
    }
    if ($("dl-tri-center") && !s.scanning && s.center_mhz &&
        document.activeElement !== $("dl-tri-center"))
      $("dl-tri-center").value = s.center_mhz;
    dlDrawTrace("dl-tri-trace", s.trace);
    if (s.peakhold) dlDrawSpectrum(s.peakhold, "dl-tri-spec");
    const w = s.walk;
    if (w && w.active) {
      if ($("dl-walk-phase")) {
        $("dl-walk-phase").style.display = "";
        $("dl-walk-phase").textContent =
          w.phase === "in" ? "⬅ MOVE IN (toward probe)" : "➡ MOVE OUT (away)";
        $("dl-walk-phase").style.color =
          w.phase === "in" ? "var(--cyan)" : "var(--amber)";
      }
      if (w.phase !== DLLive.lastPhase) {
        DLLive.lastPhase = w.phase;
        dlSpeak(w.phase === "in" ? "Move in." : "Move out.");
      }
      if (w.elapsed_s >= w.period_s * 3) dlWalkFinish();
    }
  } else if (DLLive.mode === "ladder") {
    if ($("dl-cap-status")) {
      $("dl-cap-status").textContent =
        `LIVE @ ${fmtF(s.center_mhz)} — peak-hold accumulating (${s.grabs} grabs)`;
      $("dl-cap-status").style.color = barColor;
    }
    if ($("dl-cap-snr")) $("dl-cap-snr").textContent = snrTxt;
    if ($("dl-cap-gain")) $("dl-cap-gain").textContent = dlGainTxt(s);
    dlHotBanner("dl-cap-hot", s);
    if ($("dl-snr-bar")) {
      $("dl-snr-bar").style.width = barPct + "%";
      $("dl-snr-bar").style.background = barColor;
    }
    dlDrawTrace("dl-cap-trace", s.trace);
    if (s.peakhold) dlDrawSpectrum(s.peakhold);
    if ($("dl-cap-stable"))
      $("dl-cap-stable").textContent =
        s.stable_s >= 1 ? `steady ${s.stable_s.toFixed(0)} s` : "";
    const auto = $("dl-cap-auto") && $("dl-cap-auto").checked;
    if (auto && s.stable_s >= 3 && !DLLive.autoBusy) {
      DLLive.autoBusy = true;
      dlAutoCapture().finally(() => { DLLive.autoBusy = false; });
    }
  } else if (DLLive.mode === "states") {
    if ($("dl-st-snr")) $("dl-st-snr").textContent =
      `${snrTxt} · ${s.grabs} grabs accumulated`;
    dlDrawTrace("dl-st-trace", s.trace);
  }
}

async function dlAutoCapture() {
  if (!DevLab.pkg) return;
  const distance_id = ($("dl-cap-go") && $("dl-cap-go").dataset.distance) || "";
  if (!distance_id || (DevLab.pkg.captures || {})[distance_id]) return;
  try {
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/capture`, {
      distance_id,
      rx_port: ($("dl-cap-port") && $("dl-cap-port").value) || "RXA",
      notes: ($("dl-cap-notes") && $("dl-cap-notes").value) || "",
      center_mhz: DevLab.pkg.center_mhz,
    });
    dlChime(true);
    devLabRenderPkg(r.package, r.guide);
    dlShowCaptureVisual(r.capture);
    dlAnnounce(r.guide);
  } catch (e) { dlMsg("Auto-capture failed: " + e.message); }
}

function dlAnnounce(guide) {
  if (!guide) return;
  const first = (guide.do || [])[0] || "";
  dlSpeak(`Captured. ${guide.title}. ${first}`);
  dlMsg(`${guide.title} — ${first}`);
}

async function dlWalkStart() {
  if (!DLLive.timer || DLLive.mode !== "triage") {
    dlMsg("GO LIVE first — the walk test reads the live power trace.");
    return;
  }
  if (!DevLab.stream) {
    try {
      DevLab.stream = await navigator.mediaDevices.getUserMedia(
        { video: { facingMode: "environment" }, audio: false });
    } catch (e) { /* camera optional — RF correlation still works */ }
  }
  if (DevLab.stream && $("dl-tri-cam")) $("dl-tri-cam").srcObject = DevLab.stream;
  DLLive.recChunks = [];
  DLLive.rec = null;
  if (DevLab.stream && window.MediaRecorder) {
    try { DLLive.rec = new MediaRecorder(DevLab.stream, { mimeType: "video/webm" }); }
    catch (e) {
      try { DLLive.rec = new MediaRecorder(DevLab.stream); } catch (e2) { /* none */ }
    }
    if (DLLive.rec) {
      DLLive.rec.ondataavailable = ev => {
        if (ev.data && ev.data.size) DLLive.recChunks.push(ev.data);
      };
      DLLive.rec.start(1000);
      DLLive.recT0 = Date.now() / 1000;
    }
  }
  try {
    await api(`/api/device_lab/${DevLab.pkg.id}/live/walk`, { period_s: 8 });
  } catch (e) { dlMsg("Walk start failed: " + e.message); return; }
  DLLive.walkActive = true;
  DLLive.lastPhase = null;
  if ($("dl-walk-verdict")) $("dl-walk-verdict").innerHTML = "";
  if ($("dl-walk-go")) $("dl-walk-go").textContent = "■ FINISH WALK";
  dlSpeak("Walk test. Three laps. Move the device toward the probe.");
}

async function dlWalkFinish() {
  if (!DLLive.walkActive) return;
  DLLive.walkActive = false;
  if ($("dl-walk-phase")) $("dl-walk-phase").style.display = "none";
  if ($("dl-walk-go")) $("dl-walk-go").textContent = "🚶 START WALK TEST";
  let r = null;
  try {
    r = await api(`/api/device_lab/${DevLab.pkg.id}/live/walk`, { stop: true });
  } catch (e) { dlMsg("Walk scoring failed: " + e.message); }
  if (DLLive.rec && DLLive.rec.state !== "inactive") {
    await new Promise(res => { DLLive.rec.onstop = res; DLLive.rec.stop(); });
    const blob = new Blob(DLLive.recChunks, { type: "video/webm" });
    if (blob.size > 1000 && blob.size < 60e6) {
      const dataUrl = await new Promise(res => {
        const rd = new FileReader();
        rd.onload = () => res(rd.result);
        rd.readAsDataURL(blob);
      });
      try {
        await api(`/api/device_lab/${DevLab.pkg.id}/video`, {
          video: dataUrl, t0_utc: DLLive.recT0,
          duration_s: Date.now() / 1000 - DLLive.recT0, label: "walk_test",
        });
      } catch (e) { dlMsg("Video upload failed (RF verdict still saved): " + e.message); }
    }
  }
  if (r) {
    devLabRenderPkg(r.package, r.guide);
    const w = r.walk || {};
    const good = w.verdict === "tracks_motion";
    if ($("dl-walk-verdict"))
      $("dl-walk-verdict").innerHTML =
        `<b style="color:${good ? "var(--green)" : "var(--amber)"}">` +
        `${esc((w.verdict || "").replace(/_/g, " ").toUpperCase())}</b> ` +
        `r=${w.r ?? "—"} · swing ${w.swing_db ?? "—"} dB — ${esc(w.plain || "")}`;
    dlChime(good);
    dlSpeak(good
      ? "Confirmed. The emission is coming from this device. Record near field, then back away to ten feet."
      : "Walk test inconclusive. Repeat it slower and closer to the probe.");
  }
}

if ($("dl-tri-live")) $("dl-tri-live").onclick = () =>
  DLLive.timer ? dlLiveStop() : dlLiveStart("triage");
if ($("dl-cap-live")) $("dl-cap-live").onclick = () =>
  DLLive.timer ? dlLiveStop() : dlLiveStart("ladder");
if ($("dl-st-live")) $("dl-st-live").onclick = () =>
  DLLive.timer ? dlLiveStop() : dlLiveStart("states");
if ($("dl-walk-go")) $("dl-walk-go").onclick = () =>
  DLLive.walkActive ? dlWalkFinish() : dlWalkStart();

if ($("dl-tri-retune")) $("dl-tri-retune").onclick = async () => {
  if (!DevLab.pkg) return;
  const c = parseFloat($("dl-tri-center") && $("dl-tri-center").value);
  if (!c) { dlMsg("Enter a center frequency first."); return; }
  try {
    await api(`/api/device_lab/${DevLab.pkg.id}/freq`,
              { center_mhz: c, stay: true });
    if (DLLive.timer)
      await api(`/api/device_lab/${DevLab.pkg.id}/live/retune`, { center_mhz: c });
    DevLab.pkg.center_mhz = c;
    dlMsg(`Tuned to ${fmtF(c)}.`);
  } catch (e) { dlMsg("Retune failed: " + e.message); }
};

if ($("dl-tri-next")) $("dl-tri-next").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/capture`, {
      distance_id: "near_field",
      rx_port: ($("dl-tri-port") && $("dl-tri-port").value) || "RXB",
      antenna_id: ($("dl-tri-nf") && $("dl-tri-nf").value) || "hfield_loop",
      notes: ($("dl-tri-notes") && $("dl-tri-notes").value) || "",
      center_mhz: DevLab.pkg.center_mhz,
    });
    dlChime(true);
    devLabRenderPkg(r.package, r.guide);
    dlShowCaptureVisual(r.capture);
    dlAnnounce(r.guide);
    // omni is on RXA for the range ladder — restart the loop on the new port
    if (DLLive.timer) await dlLiveStart("ladder");
  } catch (e) { dlMsg("Near-field record failed: " + e.message); }
};

if ($("dl-stop-add")) $("dl-stop-add").onclick = async () => {
  if (!DevLab.pkg) return;
  const label = ($("dl-stop-label") && $("dl-stop-label").value) || "";
  if (!label.trim()) { dlMsg("Name the stop first (e.g. down the hall)."); return; }
  try {
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/stops`, { label });
    $("dl-stop-label").value = "";
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { dlMsg(e.message); }
};

if ($("dl-st-done")) $("dl-st-done").onclick = async () => {
  if (!DevLab.pkg) return;
  try {
    await dlLiveStop();
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/state_capture`,
                        { done: true });
    devLabRenderPkg(r.package, r.guide);
  } catch (e) { dlMsg(e.message); }
};

/* ---- overload protection UI (attenuator pad) ---- */
function dlGainTxt(s) {
  if (s.gain_db == null) return "";
  const pad = DevLab.pkg && DevLab.pkg.pad_db;
  return `gain ${s.gain_db} dB` + (pad ? ` · pad ${pad} dB` : "") +
    (s.clipping && !s.needs_pad ? " · backing off…" : "");
}
function dlHotBanner(id, s) {
  const el = $(id);
  if (!el) return;
  const hot = !!s.needs_pad;
  el.style.display = hot ? "" : "none";
  if (hot && !DLLive.warnedHot) {
    DLLive.warnedHot = true;
    dlChime(false);
    dlSpeak("Too hot. Front end is clipping at minimum gain. " +
            "Fit the attenuator pad on the probe line.");
  }
  if (!hot) DLLive.warnedHot = false;
}
if ($("dl-pad-set")) $("dl-pad-set").onclick = async () => {
  if (!DevLab.pkg) return;
  const db = parseFloat($("dl-pad-db") && $("dl-pad-db").value) || 0;
  try {
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/pad`, { db });
    DevLab.pkg = r.package;
    if ($("dl-pad-cur")) $("dl-pad-cur").textContent = `pad logged: ${db} dB`;
    dlMsg(`Pad logged: ${db} dB inline — levels recorded with it.`);
  } catch (e) { dlMsg(e.message); }
};

/* ---- WIDE FIND: off/on differential hunt (no guess list) ---- */
const DLWf = { timer: null, lastPhase: null };

async function dlWfStart() {
  if (!DevLab.pkg) return;
  if (DLLive.timer) await dlLiveStop();  // wide find owns the radio
  const span = ($("dl-wf-span") && $("dl-wf-span").value) || "subghz";
  try {
    await api(`/api/device_lab/${DevLab.pkg.id}/wide_find/start`,
              { span, rx_port: ($("dl-tri-port") && $("dl-tri-port").value) || "RXB" });
  } catch (e) { dlMsg("Wide find failed: " + e.message); return; }
  if ($("dl-wf-panel")) $("dl-wf-panel").style.display = "";
  if ($("dl-wf-stop")) $("dl-wf-stop").style.display = "";
  if ($("dl-wf-results")) $("dl-wf-results").innerHTML = "";
  DLWf.lastPhase = null;
  clearInterval(DLWf.timer);
  DLWf.timer = setInterval(dlWfTick, 700);
  dlSpeak("Wide find. Keep the device powered off for pass one.");
}

function dlWfEnd(hidePanel) {
  clearInterval(DLWf.timer);
  DLWf.timer = null;
  if ($("dl-wf-stop")) $("dl-wf-stop").style.display = "none";
  if ($("dl-wf-await")) $("dl-wf-await").style.display = "none";
  if (hidePanel !== false && $("dl-wf-panel"))
    $("dl-wf-panel").style.display = "none";
}

async function dlWfTick() {
  if (!DevLab.pkg) return dlWfEnd();
  let s;
  try { s = await api(`/api/device_lab/${DevLab.pkg.id}/wide_find/state`); }
  catch (e) { return; }
  // WRITTEN instructions come from the server so the words the operator reads
  // are the same words the state machine is acting on — a spoken prompt alone
  // is useless if you missed it, and the old phase labels never said what to DO.
  if ($("dl-wf-phase"))
    $("dl-wf-phase").textContent = s.title || (s.error ? "ERROR" : "—");
  if ($("dl-wf-instruction"))
    $("dl-wf-instruction").textContent = s.instruction || "";
  if ($("dl-wf-cur"))
    $("dl-wf-cur").textContent = s.current_mhz
      ? `${fmtF(s.current_mhz)} · ${Math.round((s.progress || 0) * 100)}%` +
        (s.clip_steps ? ` · ⚠ clipped at ${s.clip_steps} steps` : "")
      : "";
  if ($("dl-wf-bar"))
    $("dl-wf-bar").style.width = Math.round((s.progress || 0) * 100) + "%";
  // the advance button is shown at EVERY gate, labelled with what it will do
  if ($("dl-wf-await"))
    $("dl-wf-await").style.display = s.awaiting_operator ? "" : "none";
  if ($("dl-wf-continue") && s.action_label)
    $("dl-wf-continue").textContent = s.action_label;
  const pb = $("dl-wf-pause");
  if (pb) {
    pb.style.display = (s.phase === "pass_off" || s.phase === "pass_on") ? "" : "none";
    pb.textContent = s.paused ? "▶ RESUME" : "⏸ PAUSE";
  }
  if (s.phase !== DLWf.lastPhase) {
    DLWf.lastPhase = s.phase;
    if (s.phase === "await_off") {
      dlChime(true);
      dlSpeak("Power the device fully off. Take your time, then press the button.");
    }
    if (s.phase === "await_on") {
      dlChime(true);
      dlSpeak("Baseline done. Power the device on and let it finish starting up, "
              + "then press the button.");
    }
    if (s.phase === "pass_on") dlSpeak("Pass two running. Leave the device on.");
  }
  if (s.error) { dlMsg("Wide find: " + s.error); dlChime(false); dlWfEnd(false); return; }
  if (s.phase === "done") { dlWfRender(s.candidates || []); dlWfEnd(false); }
}

function dlWfRender(cands) {
  const el = $("dl-wf-results");
  if (!el) return;
  if (!cands.length) {
    el.innerHTML = "<p class='note'>No new energy in the ON pass — DUT silent " +
      "in this span (try another span, or it may be receive-only/wired).</p>";
    dlSpeak("No new emissions found in this span.");
    return;
  }
  el.innerHTML = "<p class='note'><b>New energy with the DUT on:</b></p>" +
    cands.map(c =>
      `<div class="row" style="gap:8px;align-items:center;margin:2px 0">` +
      `<span class="pill ${c.fundamental ? "ok" : ""}">${fmtF(c.freq_mhz)}</span>` +
      `<span class="note">Δ${c.delta_db} dB · bw ~${c.bw_khz} kHz` +
      (c.harmonic_of_mhz
        ? ` · ${c.n}× harmonic of ${fmtF(c.harmonic_of_mhz)}`
        : ` · FUNDAMENTAL`) + `</span>` +
      `<button class="btn" onclick="dlWfUse(${c.freq_mhz})">USE</button></div>`
    ).join("");
  const f = cands.find(c => c.fundamental) || cands[0];
  dlSpeak(`Found ${cands.length} emissions. Strongest fundamental at ` +
          `${f.freq_mhz.toFixed(2)} megahertz.`);
}

window.dlWfUse = async (freq) => {
  if (!DevLab.pkg) return;
  try {
    const r = await api(`/api/device_lab/${DevLab.pkg.id}/wide_find/adopt`,
                        { freq_mhz: freq });
    DevLab.pkg = r.package;
    if ($("dl-tri-center")) $("dl-tri-center").value = freq;
    dlMsg(`Locked ${fmtF(freq)} from wide find — going live to verify.`);
    await dlLiveStart("triage");
  } catch (e) { dlMsg(e.message); }
};

if ($("dl-wf-go")) $("dl-wf-go").onclick = dlWfStart;
// PAUSE holds the sweep and keeps everything measured so far. STOP is the
// destructive one — it was previously the only control, so a tech who needed a
// moment had to throw the run away and start over from the baseline.
if ($("dl-wf-pause")) $("dl-wf-pause").onclick = async () => {
  if (!DevLab.pkg) return;
  const resume = $("dl-wf-pause").textContent.indexOf("RESUME") >= 0;
  try {
    await api(`/api/device_lab/${DevLab.pkg.id}/wide_find/pause`, { resume });
  } catch (e) { /* poll will re-sync the button */ }
};
if ($("dl-wf-stop")) $("dl-wf-stop").onclick = async () => {
  if (DevLab.pkg) {
    try { await api(`/api/device_lab/${DevLab.pkg.id}/wide_find/stop`, {}); }
    catch (e) { /* already gone */ }
  }
  dlWfEnd();
};
if ($("dl-wf-continue")) $("dl-wf-continue").onclick = async () => {
  if (!DevLab.pkg) return;
  try { await api(`/api/device_lab/${DevLab.pkg.id}/wide_find/continue`, {}); }
  catch (e) { dlMsg(e.message); }
};

initWakeLock();
poll();
// deep-link support: localhost:8765/#wiki opens the library directly —
// the wiki is cross-session and never requires an open sweep session
// always go through show() — the session view's loaders (engagement form,
// case files, tasks) must run on a plain boot too, or the pre-sweep button
// saves a blank form over the real engagement
// COLD BOOT ALWAYS LANDS ON RECENT SWEEPS (operator ask 2026-07-26).
// This used to restore the last view from the URL hash, so reopening the app
// dropped the operator straight back into a finished sweep with its hits still
// on screen — indistinguishable from the job in progress. In-session
// navigation (clicks, hashchange below) is unaffected.
show("recent");
const rr = $("btn-recent-refresh");
if (rr) rr.onclick = () => loadRecentSweeps(App.viewingSessionId, "refreshed");
const bbs = $("btn-back-sessions");
if (bbs) bbs.onclick = () => showSessionsPane();

// Retag FIELD <-> LAB. Every pre-existing session was backfilled to FIELD, so
// bench work recorded before this existed stays client-report eligible until
// it is marked here.
const tkb = $("btn-toggle-kind");
if (tkb) tkb.onclick = async () => {
  const id = App.viewingSessionId;
  if (!id) return;
  const next = App.viewingSessionKind === "lab" ? "field" : "lab";
  try {
    await api("/api/session/kind", { id, kind: next });
  } catch (e) {
    if ($("recent-msg")) $("recent-msg").textContent = "could not retag: " + e.message;
    return;
  }
  loadRecentSweeps(id, next === "lab"
    ? "marked LAB / BENCH — no client report, never pushed to the portal"
    : "marked FIELD — client work, report eligible");
};

// Reviewing a session must never silently redirect new work into it: becoming
// the active session is explicit.
const asb = $("btn-activate-session");
if (asb) asb.onclick = async () => {
  const id = App.viewingSessionId;
  if (!id) return;
  try {
    await api("/api/session/open", { id });
  } catch (e) {
    if ($("recent-msg")) $("recent-msg").textContent = "could not activate: " + e.message;
    return;
  }
  // the previous engagement's detections must not follow us into this one
  App.openSessionId = id;
  App.liveSweepSignals = [];
  App._lastFinishedSweepId = null;
  App._sweepSeenThisSession = false;
  App._ledgerKey = null;
  renderLiveLedger();
  loadRecentSweeps(id, `session #${id} is now active — new sweeps record here`);
};
// + NEW SESSION lives on the landing view because that is where the operator
// realises they are still in the PREVIOUS client's session. Creating one needs
// an address (it geocodes), so this jumps to that form with it focused.
const nsb = $("btn-new-session");
if (nsb) nsb.onclick = () => {
  show("session");
  const grp = $("sess-site");
  if (grp && grp.tagName === "DETAILS" && !grp.open) grp.open = true;
  const addr = $("in-address");
  if (addr) {
    addr.scrollIntoView({ block: "center", behavior: "smooth" });
    addr.focus();
  }
};
window.addEventListener("hashchange", () => {
  const v = location.hash.replace("#", "") || "session";
  if (!document.getElementById("view-" + v).classList.contains("active")) show(v);
});
