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

// Drag-and-drop video upload (dashboard). Posts to /api/posts/upload with progress.
(function () {
  const zone = document.getElementById("dropzone");
  if (!zone) return;
  const input = document.getElementById("dropzone-input");
  const bar = document.getElementById("dz-bar"), status = document.getElementById("dz-status");
  const progress = document.getElementById("dz-progress"), error = document.getElementById("dz-error");
  const maxBytes = Number(zone.dataset.maxMb) * 1024 * 1024;
  const okTypes = [".mp4", ".mov", ".webm"];

  function fail(msg) { error.textContent = msg; error.classList.remove("hidden"); progress.classList.add("hidden"); }

  function send(file) {
    error.classList.add("hidden");
    const ext = "." + file.name.split(".").pop().toLowerCase();
    if (!okTypes.includes(ext)) return fail("Only MP4, MOV or WEBM files are accepted.");
    if (file.size > maxBytes) return fail("That file is larger than " + zone.dataset.maxMb + " MB.");
    const form = new FormData();
    form.append("file", file);
    form.append("topic", document.getElementById("dz-topic").value);
    form.append("caption", document.getElementById("dz-caption").value);
    const xhr = new XMLHttpRequest();
    progress.classList.remove("hidden"); bar.style.width = "0%"; status.textContent = "Uploading " + file.name + "…";
    xhr.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = Math.round(e.loaded / e.total * 100) + "%"; };
    xhr.onload = () => {
      let body = {}; try { body = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300) {
        status.textContent = "Done. Opening your post…"; location.href = body.url || "/queue";
      } else if (xhr.status === 402) {
        const d = body.detail || {}; fail((d.message || "Your plan does not allow uploads.") + " "); 
        const a = document.createElement("a"); a.href = d.upgrade_url || "/billing"; a.textContent = "See plans →"; a.className = "underline"; error.appendChild(a);
      } else if (xhr.status === 401) { location.href = "/login?next=/"; }
      else fail(body.detail || ("Upload failed (HTTP " + xhr.status + ")."));
    };
    xhr.onerror = () => fail("Network error during upload.");
    xhr.open("POST", "/api/posts/upload"); xhr.send(form);
  }

  zone.addEventListener("click", e => { if (e.target.tagName !== "INPUT") input.click(); });
  input.addEventListener("change", () => { if (input.files[0]) send(input.files[0]); });
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("border-sky-500"); }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("border-sky-500"); }));
  zone.addEventListener("drop", e => { const f = e.dataTransfer.files[0]; if (f) send(f); });
})();
