/* Owner-safe Concat Media picker; server is authoritative for queue claims. */
(() => {
  "use strict";
  function install(refreshExisting = false) {
    const root = document.querySelector("[data-concat-media-picker]");
    if (!root) return;
    if (root.dataset.pickerReady === "1") {
      if (refreshExisting) root.reloadConcatMedia?.();
      return;
    }
    root.dataset.pickerReady = "1";
    const select = root.querySelector("#concat-media-video");
    const input = root.querySelector("#concat-media-link");
    const status = root.querySelector("#concat-media-status");
    const refresh = root.querySelector("#concat-media-refresh");
    if (!select || !input) return;
    let sequence = 0;
    async function load() {
      const ticket = ++sequence;
      const previous = select.value;
      select.disabled = true;
      if (refresh) refresh.disabled = true;
      if (status) status.textContent = "Checking available Concat Media videos…";
      try {
        const response = await fetch("/concat-uploader/api/concat/available-media", {
          credentials: "same-origin", cache: "no-store",
          headers: {Accept: "application/json"}
        });
        if (!response.ok) throw new Error("Media list unavailable (" + response.status + ")");
        const data = await response.json();
        if (ticket !== sequence || !root.isConnected) return;
        const items = Array.isArray(data.items) ? data.items : [];
        select.replaceChildren(new Option(
          items.length ? "Select an available video" : "No available videos", ""
        ));
        for (const item of items) {
          if (!item || typeof item.link !== "string" || typeof item.name !== "string") continue;
          const option = new Option(item.name, item.link);
          option.title = item.name;
          select.add(option);
        }
        if (Array.from(select.options).some(option => option.value === previous)) {
          select.value = previous;
        } else {
          select.value = "";
        }
        select.disabled = items.length === 0;
        if (status) status.textContent = items.length
          ? items.length + " available · Queued and uploaded videos hidden"
          : "No completed, unqueued videos in Concat Media";
      } catch (error) {
        if (ticket !== sequence || !root.isConnected) return;
        select.replaceChildren(new Option("Unable to load Concat Media", ""));
        select.disabled = true;
        if (status) status.textContent = error.message + " · Manual link still works";
      } finally {
        if (ticket === sequence && root.isConnected && refresh) refresh.disabled = false;
      }
    }
    root.reloadConcatMedia = load;
    select.addEventListener("change", () => {
      if (!select.value) return;
      input.value = select.value;
      input.dispatchEvent(new Event("input", {bubbles: true}));
    });
    input.addEventListener("input", () => {
      if (select.value && select.value !== input.value) select.value = "";
    });
    if (refresh) refresh.addEventListener("click", load);
    load();
  }
  document.addEventListener("lum:shell-replaced", () => install());
  window.addEventListener("pageshow", () => install(true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) install(true);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, {once: true});
  } else {
    install();
  }
})();
