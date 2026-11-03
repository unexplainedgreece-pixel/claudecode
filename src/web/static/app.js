// Live refresh of stat tiles and clock. Pages keep working without JS.
(function () {
  const tiles = document.querySelectorAll("[data-stat]");
  async function refreshStats() {
    try {
      const r = await fetch("/api/stats", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      tiles.forEach(el => { const k = el.dataset.stat; if (k in s) el.textContent = s[k]; });
    } catch (e) { /* offline or server restarting: ignore */ }
  }
  if (tiles.length) setInterval(refreshStats, 15000);

  const clock = document.getElementById("clock");
  if (clock) setInterval(() => {
    clock.textContent = new Date().toLocaleString(undefined,
      { weekday: "short", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }, 30000);

  // Hide flash messages after a while and strip ?msg= from the URL.
  const flash = document.querySelector(".flash-ok");
  if (flash) setTimeout(() => {
    flash.remove();
    if (history.replaceState) history.replaceState(null, "", location.pathname);
  }, 6000);
})();
