/* CellMapper tower capture — paste into the DevTools console (F12) of YOUR
 * logged-in cellmapper.net tab, then pan/zoom the map around the sweep area.
 * Every tower the site loads gets collected; click the "DOWNLOAD x towers"
 * button it adds (bottom-right) and save the file as:
 *   spectrum_survey/data/cellmapper_towers.json
 * Then rebuild the baseline — the "CellMapper (import)" source picks it up.
 *
 * This only captures data the site already sends to your own browser session.
 */
(() => {
  const towers = new Map();

  const harvest = (obj) => {
    if (!obj || typeof obj !== "object") return;
    if (Array.isArray(obj)) { obj.forEach(harvest); return; }
    const lat = obj.latitude ?? obj.lat, lon = obj.longitude ?? obj.lon ?? obj.lng;
    if (typeof lat === "number" && typeof lon === "number" &&
        Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && (obj.mcc || obj.MCC || obj.mnc || obj.MNC)) {
      const key = lat.toFixed(5) + "," + lon.toFixed(5);
      towers.set(key, {
        lat, lon,
        mcc: obj.mcc ?? obj.MCC ?? null,
        mnc: obj.mnc ?? obj.MNC ?? null,
        band: obj.band ?? obj.Band ?? null,
        site: obj.siteID ?? obj.site_id ?? obj.cellID ?? null,
      });
      btn.textContent = `DOWNLOAD ${towers.size} towers (cellmapper_towers.json)`;
    }
    Object.values(obj).forEach(v => { if (typeof v === "object") harvest(v); });
  };

  const origFetch = window.fetch;
  window.fetch = async (...args) => {
    const resp = await origFetch(...args);
    try {
      const url = String(args[0]?.url || args[0]);
      if (url.includes("cellmapper")) {
        resp.clone().json().then(harvest).catch(() => {});
      }
    } catch (e) {}
    return resp;
  };
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (...a) {
    this.addEventListener("load", function () {
      try { harvest(JSON.parse(this.responseText)); } catch (e) {}
    });
    return origOpen.apply(this, a);
  };

  const btn = document.createElement("button");
  btn.textContent = "DOWNLOAD 0 towers (pan the map to collect)";
  btn.style.cssText = "position:fixed;bottom:14px;right:14px;z-index:99999;" +
    "padding:10px 16px;background:#103c2c;color:#2ecc71;border:1px solid #2ecc71;" +
    "font:bold 13px monospace;cursor:pointer;border-radius:4px";
  btn.onclick = () => {
    const blob = new Blob([JSON.stringify([...towers.values()], null, 1)],
                          { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "cellmapper_towers.json";
    a.click();
  };
  document.body.appendChild(btn);
  console.log("CellMapper capture armed — pan/zoom the map, then click the green button.");
})();
