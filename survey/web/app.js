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
  if (view === "wiki") loadWiki();
  if (view === "session") { loadCaseFiles(); loadEngagement(); loadTechTasks(); loadReportOptions(); }
  if (view === "poi") loadPoi();
  if (view === "cases") loadCases();
  if (view === "triage") loadTriage();
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
    render(App.state);
  } catch (e) { $("ft-msg").textContent = "server unreachable: " + e.message; }
  setTimeout(poll, 1000);
}

function dot(id, cls) {
  const d = $("dot-" + id);
  if (d) d.className = "dot" + (cls ? " " + cls : "");
}

function render(st) {
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
    } else {
      lb.textContent = "○ IDLE"; lb.style.color = "#6b7b90";
    }
  }
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
  } else if (App._wasSweepRunning && !App.seq) {
    const info = App._activeSweep;
    if (info && info.phase === "interior") maybeOpenTriageAfterSweep(info.sweep_id);
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
      ? "⏸ PAUSED — tap NEXT SEGMENT to continue"
      : `${st.sweep.percent || 0}%  ·  ETA ${Math.floor(eta / 60)}m ${eta % 60}s`;
    const nextBtn = $("btn-next-segment");
    if (nextBtn) nextBtn.style.display = st.sweep.awaiting_advance ? "" : "none";
    if (st.sweep.live_trace) {
      drawTrace($("livechart"), st.sweep.live_trace, muteRangesFor());
      buildSweepLabelOverlays($("livechart"), st.sweep.live_trace,
        sweepChartSignals(st.sweep.live_trace));
      pushWaterfall($("waterfall"), st.sweep.live_trace);
    }
    refreshLiveSweepSignals(st.sweep.sweep_id);
    renderSegPanel(st);
  } else if (!st.sweep.running) {
    so.classList.remove("show");
    App.overlayHidden = false;
    App.liveSweepSignals = [];
    // F3 focus/mute are sweep-overlay-only — clear them so they don't bleed into
    // other views (e.g. the baseline strip) after the sweep ends
    if (App.focusAnomalies || Object.keys(App.mutedKeys).length) {
      App.focusAnomalies = false; App.mutedKeys = {}; syncFocusButton();
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
    if (d.trace) drawTrace($("dirchart"), { freqs: d.trace.freqs, amps: d.trace.amps });
  }

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
  if (tier === "ANOMALY") return `ANOMALY · ${fmtF(s.center_mhz)}`;
  return s.display_label || s.matched_label || fmtF(s.center_mhz);
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
function drawTrace(canvas, t, muteRanges) {
  /* three traces drawn together: realtime (green, filled), average (cyan),
     max-hold (yellow) on a black scope grid — RF Explorer for Windows look.
     F3: muteRanges (MHz {lo,hi}[]) pulls those bins to the floor ("dampen the
     expected") and the chart auto-rescales to what's left, so weak anomalies
     fill the view instead of hugging the noise floor. */
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 800;
  const H = canvas.height = canvas.clientHeight || 230;
  ctx.fillStyle = "#000208"; ctx.fillRect(0, 0, W, H);
  const n = (t.amps || []).length;
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
  const plot = (arr, color, width) => {
    if (!arr || arr.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
    for (let i = 0; i < arr.length; i++) {
      const x = i / (arr.length - 1) * W;
      i ? ctx.lineTo(x, Y(v(arr, i))) : ctx.moveTo(x, Y(v(arr, i)));
    }
    ctx.stroke();
  };
  // realtime: filled area under the trace like the vendor app
  ctx.beginPath();
  ctx.moveTo(0, H);
  for (let i = 0; i < n; i++) ctx.lineTo(i / (n - 1) * W, Y(v(t.amps, i)));
  ctx.lineTo(W, H); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, "rgba(0,255,90,0.35)");
  grad.addColorStop(1, "rgba(0,255,90,0.04)");
  ctx.fillStyle = grad; ctx.fill();
  plot(t.maxpeak, "#ffd000", 1.1);    // max-hold (yellow)
  plot(t.avg, "#00c8ff", 1.1);        // average (cyan)
  plot(t.amps, "#00ff5a", 1.5);       // realtime (green) on top
  ctx.font = "10px monospace";
  let lx = W - 250;
  [["REALTIME", "#00ff5a", true], ["AVG", "#00c8ff", !!t.avg], ["MAX HOLD", "#ffd000", !!t.maxpeak]]
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
    const [r, g, b] = wfColor(t.amps[Math.floor(x / W * n)]);
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
    const header = "<tr><th>#</th><th>Frequency</th><th>Peak</th><th>Width</th><th>Status</th><th>Identity</th></tr>";
    const body = shown.slice().reverse().map((s, i) => {
      const k = tierName(s);
      const cls = s.identity_tier || (s.classification === "VERIFIED" ? "IDENTIFIED"
        : s.classification === "PLAUSIBLE" ? "KNOWN_BAND" : "ANOMALY");
      const id = s.display_label || s.matched_label || s.match_reason || "";
      return `<tr><td>${shown.length - i}</td><td>${fmtF(s.center_mhz)}</td>` +
        `<td>${s.peak_dbm} dBm</td><td>${s.bw_khz != null ? Math.round(s.bw_khz) + " kHz" : "—"}</td>` +
        `<td><span class="pill ${cls}">${k}</span>${s.caution ? " ⚠" : ""}</td>` +
        `<td>${esc(id).slice(0, 70)}</td></tr>`;
    }).join("");
    // apply the empty-state to the BODY only (header is always non-empty, so the
    // old `header+body || fallback` could never show it); colspan spans all cols
    const empty = App.focusAnomalies
      ? "<tr><td colspan=6 class='note'>no anomalies — all detected signals are expected/known</td></tr>"
      : "<tr><td colspan=6 class='note'>no signals detected yet</td></tr>";
    $("tbl-live-ledger").innerHTML = header + (body || empty);
  } catch (e) { /* ledger is best-effort during sweep */ }
}

/* ---------- session / device / baseline ---------- */
$("btn-create").onclick = async () => {
  try {
    $("session-msg").textContent = "geocoding…";
    const r = await api("/api/session", {
      address: $("in-address").value, radius_broadcast: $("in-rb").value,
      radius_lmr: $("in-rl").value, operator: $("in-op").value });
    $("session-msg").textContent = `session #${r.session_id} @ ${r.geocode.lat.toFixed(5)}, ` +
      `${r.geocode.lon.toFixed(5)} (${r.geocode.geocoder})`;
    $("btn-baseline").disabled = false;
  } catch (e) { $("session-msg").textContent = "ERROR: " + e.message; }
};

$("btn-connect").onclick = async () => {
  const src = $("device-source") ? $("device-source").value : null;
  $("device-msg").textContent = `connecting to ${src === "pluto" ? "Pluto" : "RF Explorer"}…`;
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
  } catch (e) { $("device-msg").textContent = "FAILED: " + e.message; }
};

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
window.openSession = async (id) => {
  await api("/api/session/open", { id });
  loadBaseline();
  loadEngagement();
  loadCaseFiles();
  loadTechTasks();
  loadReportOptions();
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
async function listenStart(center, modeOverride, onState) {
  listenStop();
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) { onState("Web Audio not supported — using <audio> fallback", false); return false; }
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
    return false;
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
  listenAt(parseFloat(a.dataset.center), a.dataset.label || "", a.dataset.mode || undefined);
}
["tbl-anomalies", "tbl-signals", "tbl-wiki", "tbl-hunt", "tbl-triage"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("click", freqLinkDelegate, true);  // capture: beat the row onclick
});

function inRangeClient(center) {
  const dev = App.state && App.state.device;
  if (!dev || !dev.modules || !dev.modules.length)
    return { ok: false, msg: "connect a device first (Session tab)." };
  if (dev.driver !== "pluto")
    return { ok: false, msg: "tune / view / listen needs the Pluto (SDR) — the RF Explorer is amplitude-only." };
  const lo = Math.min(...dev.modules.map(m => m.min_mhz));
  const hi = Math.max(...dev.modules.map(m => m.max_mhz));
  if (center < lo || center > hi)
    return { ok: false, msg: `${fmtF(center)} is outside the radio's ${fmtF(lo)}–${fmtF(hi)} range.` };
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
function inspectByFreq(center, label, mode) {
  const tuneId = (App.tuneId = (App.tuneId || 0) + 1);
  App.inspCenter = center; App.inspLabel = label;   // for mode <select> re-tune
  App.demodMhz = center;                             // demod tuner starts at band center (offset 0)
  $("inspect-panel").style.display = "";
  $("insp-title").textContent = `${fmtF(center)} · ${label || ""}`;   // fmtF already carries the unit
  $("inspect-panel").scrollIntoView({ behavior: "smooth", block: "center" });
  const ao = $("insp-analyze-out"); if (ao) ao.style.display = "none";
  loadRecordings();
  stopSpectrumPoll();
  listenStop();
  const au = $("insp-audio"); au.pause(); au.removeAttribute("src");
  clearInspectCanvases();
  const rc = inRangeClient(center);
  if (!rc.ok) {
    $("insp-note").textContent = rc.msg +
      (App.inspContext === "baseline" ? "  (Next steps through the rest.)" : "");
    $("insp-msg").textContent = "";
    const ac = $("insp-analyzer").getContext("2d");
    ac.fillStyle = "#e0a030"; ac.font = "13px monospace";
    ac.fillText(rc.msg, 12, 26);
    return;
  }
  $("insp-note").textContent = "";
  if (mode === "pocsag") { inspectPocsag(center, label, tuneId); return; }
  $("insp-decode").style.display = "none";
  $("insp-msg").textContent = "tuning & streaming…";
  // Prefer smooth Web Audio chunked playback (/api/listen_pcm); fall back to the
  // plain <audio> element (/api/listen) only if Web Audio / fetch-streaming is
  // unavailable or the stream fails to start.
  const onState = (msg, live, m) => {
    $("insp-msg").textContent = msg;
    if (m) $("insp-mode").value = m;
  };
  listenStart(center, mode, onState).then((ok) => {
    if (!ok && App.tuneId === tuneId) {     // guard: don't stomp a newer tune
      const ibw = ($("insp-ibw") || {}).value || "2.4";
      const blockS = ($("insp-block-s") || {}).value || "0.5";
      au.src = `/api/listen?center_mhz=${encodeURIComponent(center)}` +
               (mode ? `&mode=${encodeURIComponent(mode)}` : "") +
               `&ibw_mhz=${encodeURIComponent(ibw)}&block_s=${encodeURIComponent(blockS)}`;
      au.play().catch(() => {});
    }
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

// Entry point for the NON-baseline lists (Results / detected signals / Wiki).
// Disables Next/Prev (no list to step) and tunes the given center+label.
function listenAt(center, label, mode) {
  if (center == null || isNaN(center)) return;
  stopAuto();                       // a manual freq pick takes manual control
  App.inspContext = "freq";
  App.inspIdx = null;
  setInspNav(false);
  inspectByFreq(center, label, mode);
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
  const blockS = parseFloat(($("insp-block-s") || {}).value) || 0.5;
  return blockS <= 0.2 ? 50 : blockS <= 0.25 ? 80 : 100;
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
    if (s.mode) $("insp-mode").value = s.mode;
    $("insp-msg").textContent = "● live";
    const mode = s.mode || ($("insp-mode") || {}).value;
    if (s.analyzer) {
      const meter = computeMeterStats(s.analyzer, s.center_mhz, App.demodMhz, mode);
      if (meter) { updateSmeterDisplay(meter); pushSignalHistory(meter); }
      drawAnalyzer($("insp-analyzer"), s.analyzer, meter);
      drawSignalHistory($("insp-history"));
    }
    if (s.waterfall) drawWaterfall($("insp-waterfall"), s.waterfall);
  }, specPollMs());
}

function stopSpectrumPoll() {
  if (App.specPoll) { clearInterval(App.specPoll); App.specPoll = null; }
}

function smoothSpectrumTrace(p) {
  const n = p.length;
  if (!App._specSmooth || App._specSmooth.length !== n) {
    App._specSmooth = p.slice();
    return App._specSmooth;
  }
  const decay = 0.18;
  for (let i = 0; i < n; i++) {
    const raw = p[i], prev = App._specSmooth[i];
    App._specSmooth[i] = raw > prev ? raw : prev - (prev - raw) * decay;
  }
  return App._specSmooth;
}

function drawAnalyzer(cv, a, meter) {
  const { ctx, W, H } = fitCanvas(cv);
  ctx.clearRect(0, 0, W, H);
  const p = a.power_dbm, fr = a.freqs_mhz, n = p.length;
  if (!n) return;
  App.bandLo = fr[0]; App.bandHi = fr[n - 1];
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

// Vertical cursor at the current demod tuner (App.demodMhz) within [f0,f1].
function drawDemodMarker(ctx, W, H, f0, f1) {
  if (App.demodMhz == null || f1 === f0) return;
  const mx = W * (App.demodMhz - f0) / (f1 - f0);
  if (mx < 0 || mx > W) return;
  ctx.strokeStyle = "#ffd24a"; ctx.lineWidth = 1; ctx.beginPath();
  ctx.moveTo(Math.round(mx) + 0.5, 0); ctx.lineTo(Math.round(mx) + 0.5, H); ctx.stroke();
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
  App._meterPeakHold = null;
}
if ($("insp-analyzer")) $("insp-analyzer").addEventListener("click", (e) => tuneDemodFromClick(e, $("insp-analyzer")));
if ($("insp-waterfall")) $("insp-waterfall").addEventListener("click", (e) => tuneDemodFromClick(e, $("insp-waterfall")));

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
    rows.push("<pre style='white-space:pre-wrap;background:#04070c;border:1px solid #26303d;padding:6px;margin:2px 0'>" +
      d.messages.map(m => esc(decodeMsgLine(d.protocol, m))).join("\n") + "</pre>");
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
    rows.push("<div class='note' style='margin-top:4px'>CANDIDATES (sigidwiki — feature match, confirm by ear/eye):</div>");
    rows.push("<table><tr><th>Signal</th><th>Class</th><th>BW</th><th>TSCM note</th><th></th></tr>" +
      cands.map(c => `<tr><td>${esc(c.name)}</td><td>${esc(c.class)}</td><td>${c.bandwidth_khz} kHz</td>` +
        `<td class="note" style="max-width:240px">${esc(c.tscm || "")}</td>` +
        `<td><a href="${esc(c.url)}" target="_blank" rel="noopener" class="note">ref ↗</a></td></tr>`).join("") +
      "</table>");
  }
  out.innerHTML = rows.join("");
  out.style.display = "";
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

if ($("insp-analyze")) $("insp-analyze").onclick = analyzeSignal;
if ($("insp-record")) $("insp-record").onclick = () => toggleRecord("audio");
if ($("insp-record-iq")) $("insp-record-iq").onclick = () => toggleRecord("iq");

/* ---------- MANUAL gated triage (post-hoc, no radio) ---------- */
const TRIAGE_LABELS = { cleared: "Cleared", suspect: "Suspect", flagged: "Flag", ask_client: "Ask client" };

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
    if (f.status && st !== f.status) return false;
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
    "<th>Decision</th><th>Notes</th><th>Monitor</th></tr>" +
    visible.map(triageRow).join("");
}

async function loadTriage() {
  try {
    const roles = await api("/api/device/roles");
    const pill = $("triage-mode");
    if (pill) pill.textContent = roles.concurrent_enabled
      ? "LIVE triage (dual-device: Pluto monitors while the RFE sweeps)"
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
    `<button class="btn ${st === k ? "primary" : ""}" style="padding:2px 6px" ` +
    `onclick="triageDecide(${s.id},'${k}')">${TRIAGE_LABELS[k]}</button>`).join(" ");
  const novel = s.is_novel_to_address
    ? `<span class="pill" style="color:#ffd24a" title="${esc(s.novelty_reason || "")}">▲ NEW</span>` : "";
  const lbl = (s.matched_label || s.classification || "signal").replace(/'/g, "");
  return `<tr data-sid="${s.id}">` +
    `<td>${(s.anomaly_score || 0).toFixed(0)}</td>` +
    `<td><a href="#" class="freqlink" data-center="${s.center_mhz}" data-label="${esc(lbl)}">${fmtF(s.center_mhz)}</a></td>` +
    `<td class="note">${esc(s.room || "—")}</td>` +
    `<td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span>${s.caution ? " ⚠" : ""}</td>` +
    `<td>${novel}</td>` +
    `<td>${btns}<div class="note" style="margin-top:2px">${st === "unreviewed" ? "— unreviewed" : esc(TRIAGE_LABELS[st] || st)}</div></td>` +
    `<td><input class="triage-note" data-sid="${s.id}" value="${esc(s.triage_notes || "")}" placeholder="notes…" style="width:150px"></td>` +
    `<td><button class="btn" onclick="monitorFreq(${s.center_mhz},'${esc(lbl)}')">▶ monitor</button></td>` +
    `</tr>`;
}

async function triageDecide(id, status) {
  const noteEl = document.querySelector(`.triage-note[data-sid="${id}"]`);
  const notes = noteEl ? noteEl.value : "";
  try { await api(`/api/signals/${id}/triage`, { status, notes }); }
  catch (e) { alert("Triage failed: " + (e.message || e)); return; }
  loadTriage();
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
function monitorFreq(center, label) {
  listenAt(center, label);
  setTimeout(analyzeSignal, 400);
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
  const a = $("insp-audio"); a.pause(); a.removeAttribute("src");
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
  // [seg, avg, maxhold, manual-advance checkbox]
  const map = {
    "ambient-profile": ["ambient-seg", "ambient-avg", "ambient-mh", "manual-ambient"],
    "bug-profile": ["bug-seg", "bug-avg", "bug-mh", "manual-bug"],
    "profile-L": ["seg-L", "avg-L", "mh-L", "manual-L"],
    "profile-R": ["seg-R", "avg-R", "mh-R", "manual-R"],
    "profile-main": ["seg-main", "avg-main", "mh-main", "manual-main"],
  };
  const ids = map[profileId] || [];
  const seg = $(ids[0]), avg = $(ids[1]), mh = $(ids[2]), man = $(ids[3]);
  const out = {};
  const sw = seg && parseFloat(seg.value);
  if (sw && sw >= 2) out.segment_width_mhz = sw;
  const ap = avg && parseFloat(avg.value);
  if (ap && ap >= 3) out.custom_avg_passes = Math.round(ap);
  const md = mh && parseFloat(mh.value);
  if (md && md >= 2) out.custom_maxhold_s = md;
  if (man && man.checked) out.manual_advance = true;   // F5 (server honors only for custom)
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
    const rbw = Object.values(plans).flatMap(p => p.segments.map(s =>
      (s.high_mhz - s.low_mhz) * 1000 / 112));
    etaEl.textContent = `est. ${totalMin.toFixed(0)} min total · ${segs} segments · ` +
      `RBW ${Math.min(...rbw).toFixed(0)}–${Math.max(...rbw).toFixed(0)} kHz/bin`;
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
["ambient-seg", "ambient-avg", "ambient-mh", "bug-seg", "bug-avg", "bug-mh",
 "seg-L", "avg-L", "mh-L", "seg-R", "avg-R", "mh-R", "seg-main", "avg-main", "mh-main"].forEach(id => {
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

// true when the CONNECTED radio is the single-RX Pluto (drives antenna-prompt
// wording). Keyed off the device driver so it's correct regardless of how the
// source was picked; falls back to the selected source before connect.
function isPlutoDevice() {
  const dev = App.state && App.state.device;
  if (dev && dev.driver) return dev.driver === "pluto";
  return App.state && App.state.source === "pluto";
}

// F4: true when the device has a single RX (Pluto) → one "Main sweep" step.
// Post-connect uses the real reported module count; pre-connect honors the
// device-source picker (so the UI collapses before you even connect).
function isSingleRx() {
  const dev = App.state && App.state.device;
  if (dev && dev.modules && dev.modules.length) return dev.modules.length === 1;
  const sel = $("device-source");
  const src = (sel && sel.value) || (App.state && App.state.source);
  return src === "pluto";
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
    `<div class="seg-item anom"><span class="f">${fmtF(a.center_mhz)}</span>` +
    `<span class="n">${esc(a.label || a.reason || "")}</span></div>`).join("");
}

/* ---------- sigidwiki signal identification ---------- */
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
    html += `<tr><td>${esc(c.name)}</td><td>${c.score}</td><td>${esc(c.mode)}</td>`
      + `<td class="note">${c.reasons.map(esc).join("; ")}`
      + (c.tscm ? `<br><b>TSCM:</b> ${esc(c.tscm)}` : "") + "</td>"
      + `<td><a href="${esc(c.url)}" target="_blank" rel="noopener">sigidwiki ↗</a></td></tr>`;
  }
  $("id-results").innerHTML = html + "</table>";
}

$("btn-identify").onclick = async () => {
  const c = parseFloat($("id-center").value);
  if (!c) return alert("Enter a center frequency in MHz (e.g. 433.92).");
  const bw = parseFloat($("id-bw").value);
  $("id-msg").textContent = isPlutoDevice() ? "capturing IQ & analyzing…" : "matching…";
  $("id-results").innerHTML = "";
  try {
    const r = await api("/api/identify", { center_mhz: c, bw_khz: isNaN(bw) ? null : bw });
    $("id-msg").textContent = r.note || (r.features ? "identified from live IQ" : "");
    renderIdentify(r);
  } catch (e) { $("id-msg").textContent = "FAILED: " + e.message; }
};

function moduleRange(side) {
  const dev = App.state && App.state.device;
  const m = dev && dev.modules.find(x => x.side === side);
  return m ? `${fmtF(m.min_mhz)} – ${fmtF(m.max_mhz)}` : "";
}

$("btn-ambient").onclick = async () => {
  const mods = modulesAvailable();
  if (!mods.length) return alert("Connect the device first (Session tab).");
  const profile = $("ambient-profile").value, minutes = $("ambient-minutes").value;
  const pluto = isPlutoDevice();
  const steps = [];
  if (pluto) {
    // single wideband RX — one step, no SMA-port dance
    steps.push({
      phase: "ambient", module: "right", room: "exterior",
      msg: "ATTACH THE OMNI ANTENNA TO THE PLUTO RX SMA",
      sub: `Pluto sweeps ${moduleRange("right")} — ambient reference, run OUTSIDE / vehicle at the curb` });
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
  if (mods.includes("left")) steps.push({
    phase: "interior", module: "left", room,
    msg: "ATTACH THE NON-DIRECTIONAL (OMNI) ANTENNA TO THE LEFT SMA PORT",
    sub: `Bug-frequency scan — known surveillance bands within ${moduleRange("left")}` });
  if (mods.includes("right")) steps.push({
    phase: "interior", module: "right", room,
    msg: "MOVE THE OMNI ANTENNA TO THE RIGHT SMA PORT",
    sub: `Bug-frequency scan — known surveillance bands within ${moduleRange("right")}` });
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

$("btn-abort").onclick = () => api("/api/sweep/abort", {});
$("btn-hide-overlay").onclick = () => {
  App.overlayHidden = true; $("sweep-overlay").classList.remove("show");
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
  App.dirQueue = App.dirShowAll ? App.anomalies : App.anomalies.slice(0, DIR_TRIAGE);
  if (App.dirQueue.length) {
    const top = App.dirQueue[0];
    const hidden = App.anomalies.length - App.dirQueue.length;
    $("dir-instruction").innerHTML =
      `<b style="color:#fff">ATTACH THE DIRECTIONAL ANTENNA</b> and return to ` +
      `<b style="color:var(--amber)">${esc(top.strongest_room || top.room || "the premises")}</b>. ` +
      `Triage: hunting the top ${App.dirQueue.length} by anomaly score` +
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
}

window.dirLock = async (i) => {
  App.dirIndex = i;
  const a = App.dirQueue[i];
  if (!a) return;
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

/* ---------- results ---------- */
async function loadResults() {
  try {
    const anomalies = await api("/api/anomalies");
    $("tbl-anomalies").innerHTML =
      "<tr><th>Score</th><th>Frequency</th><th>Peak dBm</th><th>BW kHz</th><th>Persist</th>" +
      "<th>In/Out Δ</th><th>Strongest room</th><th>Class</th><th>Why not verified</th><th></th></tr>" +
      anomalies.map(s =>
        `<tr><td class="${s.anomaly_score >= 60 ? "score-hi" : "score-md"}">${(s.anomaly_score || 0).toFixed(0)}</td>` +
        `<td><a href="#" class="freqlink" data-center="${s.center_mhz}" ` +
        `data-label="${esc(s.matched_label || s.match_reason || "anomaly")}">${fmtF(s.center_mhz)}</a></td>` +
        `<td>${s.peak_dbm}</td><td>${s.bw_khz}</td>` +
        `<td>${((s.persistence || 0) * 100).toFixed(0)}%</td>` +
        `<td>${s.interior_delta_db != null ? "+" + s.interior_delta_db + " dB" : "—"}</td>` +
        `<td>${esc(s.strongest_room || "")}${s.localization_score ? ` (+${s.localization_score} dB)` : ""}</td>` +
        `<td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span>${s.caution ? " ⚠" : ""}</td>` +
        `<td style="max-width:300px">${esc(s.match_reason)}</td>` +
        `<td><button class="btn" onclick="captureOne(${s.id})">CAPTURE</button></td></tr>`).join("");
  } catch (e) {}
  loadHunt();
  loadSignals();
  loadReports();
  loadExpected();
  loadReportOptions();
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
        return `<tr${!x.found && x.expectation === "strong" ? ' style="background:#1f1212"' : ""}>` +
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
    $("tbl-signals").innerHTML =
      "<tr><th>Frequency</th><th>Peak</th><th>Persist</th><th>Phase</th><th>Module</th>" +
      "<th>Room</th><th>Class</th><th>Match</th></tr>" +
      sigs.map(s => `<tr><td><a href="#" class="freqlink" data-center="${s.center_mhz}" ` +
        `data-label="${esc(s.matched_label || s.classification || "signal")}">${fmtF(s.center_mhz)}</a></td>` +
        `<td>${s.peak_dbm}</td>` +
        `<td>${((s.persistence || 0) * 100).toFixed(0)}%</td><td>${s.phase}</td><td>${s.module}</td>` +
        `<td>${esc(s.room)}</td><td><span class="pill ${s.classification}">${esc(statusLabel(s.classification))}</span></td>` +
        `<td>${esc(s.matched_label)}</td></tr>`).join("");
  } catch (e) {}
}
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
        return `<tr${loud ? ' style="background:#1f1212"' : ""}>` +
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

initWakeLock();
poll();
// deep-link support: localhost:8765/#wiki opens the library directly —
// the wiki is cross-session and never requires an open sweep session
// always go through show() — the session view's loaders (engagement form,
// case files, tasks) must run on a plain boot too, or the pre-sweep button
// saves a blank form over the real engagement
const bootView = location.hash.replace("#", "") || "session";
show(bootView);
window.addEventListener("hashchange", () => {
  const v = location.hash.replace("#", "") || "session";
  if (!document.getElementById("view-" + v).classList.contains("active")) show(v);
});
