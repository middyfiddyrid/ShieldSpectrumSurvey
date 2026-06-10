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

const App = {
  state: null, baseline: null, anomalies: [], dirQueue: [], dirIndex: 0,
  seq: null,          // active multi-step sweep sequence
  overlayHidden: false,
};

/* ---------- navigation ---------- */
document.querySelectorAll("nav .step").forEach(el => {
  el.onclick = () => show(el.dataset.view);
});
function show(view) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll("nav .step").forEach(s => s.classList.remove("active"));
  $("view-" + view).classList.add("active");
  document.querySelector(`nav .step[data-view=${view}]`).classList.add("active");
  if (view === "baseline") loadBaseline();
  if (view === "rooms") loadRooms();
  if (view === "results") loadResults();
  if (view === "directional") loadDirectional();
}

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

  // nav dots
  dot("session", st.session ? "done" : "");
  const blStatus = st.baseline.running ? "live" : (st.baseline.result || (App.baseline && App.baseline.count) ? "done" : "");
  dot("baseline", blStatus);
  const phases = { ambient: "ambient", sweepL: "left", sweepR: "right" };
  const done = (p, m) => (st.sweeps || []).some(s => s.phase === p && (!m || s.module === m) && s.status === "done");
  dot("ambient", st.sweep.running && st.sweep.phase === "ambient" ? "live" : done("ambient") ? "done" : "");
  dot("sweepL", st.sweep.running && st.sweep.phase === "interior" && st.sweep.module === "left" ? "live" : done("interior", "left") ? "done" : "");
  dot("sweepR", st.sweep.running && st.sweep.phase === "interior" && st.sweep.module === "right" ? "live" : done("interior", "right") ? "done" : "");
  const rooms = new Set((st.sweeps || []).filter(s => s.phase === "interior" && s.status === "done").map(s => s.room));
  dot("rooms", rooms.size > 1 ? "done" : "");
  dot("directional", st.sweep.running && st.sweep.phase === "directional" ? "live" : "");
  dot("results", (st.sweeps || []).some(s => s.status === "done") ? "done" : "");
  dot("calibration", st.calibration.running ? "live" : st.calibration.result ? "done" : "");

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
  const so = $("sweep-overlay");
  if (st.sweep.running && st.sweep.phase !== "directional" && !App.overlayHidden) {
    so.classList.add("show");
    $("sweep-overlay-title").textContent =
      `${(st.sweep.phase || "").toUpperCase()} SWEEP — ${(st.sweep.module || "").toUpperCase()} MODULE` +
      (st.sweep.room ? ` — ${st.sweep.room.toUpperCase()}` : "");
    $("sweep-bar").style.width = (st.sweep.percent || 0) + "%";
    const cs = st.sweep.current_segment;
    $("sweep-overlay-detail").textContent =
      `segment ${st.sweep.segment_index}/${st.sweep.segment_total}` +
      (cs ? ` · ${cs.low_mhz.toFixed(2)}–${cs.high_mhz.toFixed(2)} MHz · RBW ${cs.rbw_khz} kHz [${cs.band_tag}]` : "") +
      ` · signals so far: ${st.sweep.signal_count}`;
    const eta = st.sweep.eta_seconds || 0;
    $("sweep-overlay-eta").textContent =
      `${st.sweep.percent || 0}%  ·  ETA ${Math.floor(eta / 60)}m ${eta % 60}s`;
    if (st.sweep.live_trace) drawTrace($("livechart"), st.sweep.live_trace);
  } else if (!st.sweep.running) {
    so.classList.remove("show");
    App.overlayHidden = false;
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

/* ---------- canvas traces ---------- */
function drawTrace(canvas, t) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 800;
  const H = canvas.height = canvas.clientHeight || 230;
  ctx.fillStyle = "#0d1319"; ctx.fillRect(0, 0, W, H);
  const amps = t.amps, n = amps.length;
  if (!n) return;
  const aMin = -115, aMax = -20;
  ctx.strokeStyle = "#1f2a37";
  for (let g = -110; g <= -30; g += 20) {
    const y = H - (g - aMin) / (aMax - aMin) * H;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillStyle = "#42506b"; ctx.font = "9px monospace"; ctx.fillText(g + "dBm", 4, y - 2);
  }
  ctx.strokeStyle = "#1abc9c"; ctx.lineWidth = 1.4; ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1) * W;
    const y = H - (Math.max(aMin, Math.min(aMax, amps[i])) - aMin) / (aMax - aMin) * H;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  if (t.freqs && t.freqs.length) {
    ctx.fillStyle = "#7b8794"; ctx.font = "10px monospace";
    ctx.fillText(fmtF(t.freqs[0]), 4, H - 4);
    const lastLabel = fmtF(t.freqs[t.freqs.length - 1]);
    ctx.fillText(lastLabel, W - ctx.measureText(lastLabel).width - 4, H - 4);
  }
}

const BAND_COLORS = { broadcast: "#2e86c1", land_mobile: "#7d3c98", ism: "#e67e22",
  cellular: "#16a085", aviation: "#5d6d7e", gps: "#c0392b", ham: "#7f8c8d", other: "#566573" };
const CLASS_COLORS = { VERIFIED: "#2ecc71", PLAUSIBLE: "#f39c12", UNVERIFIED: "#e74c3c" };

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
    ctx.strokeStyle = CLASS_COLORS[s.classification] || "#e74c3c";
    ctx.beginPath(); ctx.moveTo(X(s.center_mhz), 4); ctx.lineTo(X(s.center_mhz), 28); ctx.stroke();
  });
  ctx.fillStyle = "#42506b"; ctx.font = "9px monospace";
  for (let g = 0; g <= hi; g += 1000) ctx.fillText((g / 1000) + "G", X(g) + 2, 12);
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
  $("device-msg").textContent = "connecting…";
  try {
    const d = await api("/api/device/connect", {});
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

async function loadSessions() {
  try {
    const rows = await api("/api/sessions");
    $("tbl-sessions").innerHTML = "<tr><th>#</th><th>created</th><th>address</th><th></th></tr>" +
      rows.map(s => `<tr><td>${s.id}</td><td>${new Date(s.created_utc * 1000).toLocaleString()}</td>` +
        `<td>${esc(s.address)}</td><td><button class="btn" onclick="openSession(${s.id})">OPEN</button></td></tr>`).join("");
  } catch (e) { /* no sessions yet */ }
}
window.openSession = async (id) => { await api("/api/session/open", { id }); loadBaseline(); };

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
    (!f || JSON.stringify(r).toLowerCase().includes(f)));
  $("tbl-baseline").innerHTML =
    "<tr><th>Frequency</th><th>Service</th><th>Callsign/Label</th><th>Licensee</th>" +
    "<th>Dist mi</th><th>Brg</th><th>Source</th><th>Confidence</th><th></th></tr>" +
    rows.slice(0, 1500).map(r => {
      const f1 = r.freq_low_mhz === r.freq_high_mhz ? fmtF(r.freq_low_mhz) :
        fmtF(r.freq_low_mhz) + " – " + fmtF(r.freq_high_mhz);
      return `<tr><td>${f1}</td><td>${esc(r.service)}</td>` +
        `<td>${esc(r.callsign || r.label)}</td><td>${esc(r.licensee)}</td>` +
        `<td>${r.distance_mi != null ? r.distance_mi.toFixed(1) : ""}</td>` +
        `<td>${r.bearing_deg != null ? r.bearing_deg.toFixed(0) + "°" : ""}</td>` +
        `<td>${esc(r.source)}</td><td>${esc(r.confidence)}</td>` +
        `<td>${r.caution ? '<span class="caution">⚠</span>' : ""}</td></tr>`;
    }).join("");
}
$("baseline-filter").oninput = renderBaselineTable;
$("baseline-source-filter").onchange = renderBaselineTable;

/* ---------- ETA previews ---------- */
async function updateEta(profileSel, minutesInput, etaEl) {
  const profile = profileSel.value;
  minutesInput.style.display = profile === "custom" ? "" : "none";
  try {
    const plans = await api(`/api/plan?profile=${profile}&custom_minutes=${minutesInput.value || 45}`);
    const totalMin = Object.values(plans).reduce((a, p) => a + p.estimated_minutes, 0);
    const segs = Object.values(plans).reduce((a, p) => a + p.segment_count, 0);
    const rbw = Object.values(plans).flatMap(p => p.segments.map(s =>
      (s.high_mhz - s.low_mhz) * 1000 / 112));
    etaEl.textContent = `est. ${totalMin.toFixed(0)} min total · ${segs} segments · ` +
      `RBW ${Math.min(...rbw).toFixed(0)}–${Math.max(...rbw).toFixed(0)} kHz/bin`;
  } catch (e) { etaEl.textContent = e.message; }
}
[["ambient-profile", "ambient-minutes", "ambient-eta"],
 ["profile-L", "minutes-L", "eta-L"], ["profile-R", "minutes-R", "eta-R"]]
  .forEach(([p, m, e]) => {
    $(p).onchange = () => updateEta($(p), $(m), $(e));
    $(m).oninput = () => updateEta($(p), $(m), $(e));
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

async function startPhase(phase, module, room, profile, minutes) {
  App.overlayHidden = false;
  await api("/api/sweep/start", { phase, module, room, profile, custom_minutes: minutes });
}

/* sequence = remaining steps after the current sweep finishes */
async function advanceSeq() {
  const seq = App.seq;
  if (!seq || !seq.steps.length) { App.seq = null; return; }
  App.seq = null;                       // pause advancement while prompting
  const step = seq.steps.shift();
  const ok = await prompt2(step.msg, step.sub);
  if (!ok) return;
  await startPhase(step.phase, step.module, step.room, seq.profile, seq.minutes);
  App.seq = seq.steps.length ? seq : null;
}

function modulesAvailable() {
  const dev = App.state && App.state.device;
  return dev ? dev.modules.map(m => m.side) : [];
}

$("btn-ambient").onclick = async () => {
  const mods = modulesAvailable();
  if (!mods.length) return alert("Connect the device first (Session tab).");
  const profile = $("ambient-profile").value, minutes = $("ambient-minutes").value;
  const steps = [];
  if (mods.includes("left")) steps.push({
    phase: "ambient", module: "left", room: "exterior",
    msg: "ATTACH THE NON-DIRECTIONAL (OMNI) ANTENNA TO THE LEFT SMA PORT",
    sub: "Ambient reference — run this OUTSIDE the premises / vehicle at the curb" });
  if (mods.includes("right")) steps.push({
    phase: "ambient", module: "right", room: "exterior",
    msg: "MOVE THE OMNI ANTENNA TO THE RIGHT SMA PORT",
    sub: "Ambient reference continues on the right module" });
  if (!steps.length) return alert("No modules reported by device.");
  App.seq = { steps, profile, minutes };
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
    sub: "Phase A — interior sweep, room: " + room }],
    profile: $("profile-L").value, minutes: $("minutes-L").value };
  advanceSeq();
};

$("btn-sweep-R").onclick = async () => {
  if (!modulesAvailable().includes("right"))
    return alert("RIGHT module not reported by device.");
  const room = $("in-room-R").value || $("in-room-L").value || "unspecified";
  App.seq = { steps: [{ phase: "interior", module: "right", room,
    msg: "MOVE THE OMNI ANTENNA TO THE RIGHT SMA PORT",
    sub: "Phase B — interior sweep, room: " + room }],
    profile: $("profile-R").value, minutes: $("minutes-R").value };
  advanceSeq();
};

$("btn-abort").onclick = () => api("/api/sweep/abort", {});
$("btn-hide-overlay").onclick = () => {
  App.overlayHidden = true; $("sweep-overlay").classList.remove("show");
};

/* ---------- rooms ---------- */
$("btn-next-room").onclick = () => {
  const room = $("in-next-room").value.trim();
  if (!room) return alert("Enter the room label first.");
  $("in-room-L").value = room; $("in-room-R").value = room;
  show("sweepL");
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
async function loadDirectional() {
  try { App.anomalies = await api("/api/anomalies"); } catch (e) { App.anomalies = []; }
  App.dirQueue = App.anomalies;
  if (App.dirQueue.length) {
    const top = App.dirQueue[0];
    $("dir-instruction").innerHTML =
      `<b style="color:#fff">ATTACH THE DIRECTIONAL ANTENNA</b> and return to ` +
      `<b style="color:var(--amber)">${esc(top.strongest_room || top.room || "the premises")}</b>. ` +
      `${App.dirQueue.length} anomalies queued — click LOCK to hunt each one.`;
  } else {
    $("dir-instruction").textContent =
      "No unverified/flagged anomalies pending. Complete sweeps first — or all clear.";
  }
  $("tbl-dir-queue").innerHTML =
    "<tr><th>#</th><th>Score</th><th>Frequency</th><th>Class</th><th>Strongest room</th><th></th></tr>" +
    App.dirQueue.map((a, i) =>
      `<tr><td>${i + 1}</td><td class="${a.anomaly_score >= 60 ? "score-hi" : "score-md"}">${(a.anomaly_score || 0).toFixed(0)}</td>` +
      `<td>${fmtF(a.center_mhz)}</td><td><span class="pill ${a.classification}">${a.classification}</span></td>` +
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
      "<th>In/Out Δ</th><th>Strongest room</th><th>Class</th><th>Why not verified</th></tr>" +
      anomalies.map(s =>
        `<tr><td class="${s.anomaly_score >= 60 ? "score-hi" : "score-md"}">${(s.anomaly_score || 0).toFixed(0)}</td>` +
        `<td>${fmtF(s.center_mhz)}</td><td>${s.peak_dbm}</td><td>${s.bw_khz}</td>` +
        `<td>${((s.persistence || 0) * 100).toFixed(0)}%</td>` +
        `<td>${s.interior_delta_db != null ? "+" + s.interior_delta_db + " dB" : "—"}</td>` +
        `<td>${esc(s.strongest_room || "")}${s.localization_score ? ` (+${s.localization_score} dB)` : ""}</td>` +
        `<td><span class="pill ${s.classification}">${s.classification}</span>${s.caution ? " ⚠" : ""}</td>` +
        `<td style="max-width:330px">${esc(s.match_reason)}</td></tr>`).join("");
  } catch (e) {}
  loadSignals();
  loadReports();
}
async function loadSignals() {
  const phase = $("signals-phase").value;
  try {
    const sigs = await api("/api/signals" + (phase ? "?phase=" + phase : ""));
    $("tbl-signals").innerHTML =
      "<tr><th>Frequency</th><th>Peak</th><th>Persist</th><th>Phase</th><th>Module</th>" +
      "<th>Room</th><th>Class</th><th>Match</th></tr>" +
      sigs.map(s => `<tr><td>${fmtF(s.center_mhz)}</td><td>${s.peak_dbm}</td>` +
        `<td>${((s.persistence || 0) * 100).toFixed(0)}%</td><td>${s.phase}</td><td>${s.module}</td>` +
        `<td>${esc(s.room)}</td><td><span class="pill ${s.classification}">${s.classification}</span></td>` +
        `<td>${esc(s.matched_label)}</td></tr>`).join("");
  } catch (e) {}
}
$("signals-phase").onchange = loadSignals;

async function loadReports() {
  try {
    const reps = await api("/api/reports");
    $("tbl-reports").innerHTML = "<tr><th>Generated</th><th>Kind</th><th>File</th></tr>" +
      reps.map(r => `<tr><td>${new Date(r.generated_utc * 1000).toLocaleString()}</td>` +
        `<td><span class="pill ${r.kind === "FINAL" ? "ok" : "stale"}">${r.kind}</span></td>` +
        `<td><a style="color:var(--cyan)" href="/reports/${encodeURIComponent(r.filename)}" target="_blank">${esc(r.filename)}</a></td></tr>`).join("");
  } catch (e) {}
}

$("btn-report").onclick = async () => {
  $("btn-report").disabled = true;
  $("btn-report").textContent = "RENDERING…";
  try {
    const r = await api("/api/report/generate", {});
    window.open("/reports/" + encodeURIComponent(r.filename), "_blank");
    loadReports();
  } catch (e) { alert("Report failed: " + e.message); }
  $("btn-report").textContent = "GENERATE REPORT";
  $("btn-report").disabled = false;
};
$("btn-csv").onclick = () => { window.location = "/api/export/csv"; };

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
poll();
