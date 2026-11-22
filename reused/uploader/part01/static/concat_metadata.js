/* Metadata-only sheet actions; delegated handlers survive uploader tab navigation. */
(() => {
  "use strict";
  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {Accept: "application/json"}, body
    });
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("Unable to read the response; sign in again or retry"); }
    if (!response.ok || !data.ok) {
      throw new Error(typeof data.detail === "string" ? data.detail : "Metadata request failed; retry");
    }
    return data;
  }
  document.addEventListener("click", async event => {
    const open = event.target.closest("[data-concat-metadata-open]");
    if (open) {
      open.closest("[data-concat-metadata-config]").querySelector("dialog").showModal();
      return;
    }
    const close = event.target.closest("[data-concat-metadata-close]");
    if (close) { close.closest("dialog").close(); return; }
    const button = event.target.closest("[data-concat-metadata-sync]");
    if (!button || button.disabled) return;
    const form = button.closest("form");
    const select = form.querySelector("#concat-media-video");
    const link = form.querySelector("#concat-media-link");
    const status = form.querySelector("[data-concat-metadata-status]");
    const title = form.elements.namedItem("title");
    const description = form.elements.namedItem("description");
    if (!select?.value || select.value !== link?.value || select.disabled) {
      status.textContent = "Select an available video from Concat Media first";
      return;
    }
    const snapshot = {link: select.value, title: title.value, description: description.value};
    // Edits that are later reverted must also invalidate a pending response.
    let changed = false;
    const markChanged = () => { changed = true; };
    const watched = [select, link, title, description];
    watched.forEach(field => {
      field.addEventListener("input", markChanged);
      field.addEventListener("change", markChanged);
    });
    button.disabled = true;
    status.textContent = "Reading metadata sheet…";
    try {
      const body = new FormData();
      body.set("csrf_token", form.elements.namedItem("csrf_token").value);
      body.set("concat_video_link", snapshot.link);
      const data = await post(button.dataset.syncUrl, body);
      if (!button.isConnected) return;
      if (changed || select.value !== snapshot.link || link.value !== snapshot.link ||
          title.value !== snapshot.title || description.value !== snapshot.description) {
        status.textContent = "Video selection or text changed during sync; press Sync Sheet again";
        return;
      }
      if (data.video_link !== snapshot.link || typeof data.title !== "string" || typeof data.description !== "string" ||
          !data.title || Array.from(data.title).length > 100 || Array.from(data.description).length > 5000) {
        throw new Error("Sheet returned invalid metadata; existing text was kept");
      }
      title.value = data.title;
      description.value = data.description;
      for (const field of [title, description]) {
        field.dispatchEvent(new Event("input", {bubbles: true}));
        field.dispatchEvent(new Event("change", {bubbles: true}));
      }
      status.textContent = "Title and description synced. Review before adding to queue.";
    } catch (error) {
      if (button.isConnected) status.textContent = error.message;
    } finally {
      watched.forEach(field => {
        field.removeEventListener("input", markChanged);
        field.removeEventListener("change", markChanged);
      });
      if (button.isConnected) button.disabled = false;
    }
  });
  document.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-concat-metadata-sheet-form]")) return;
    event.preventDefault();
    const button = form.querySelector("[type=submit]");
    if (button.disabled) return;
    const status = form.querySelector("[data-concat-metadata-save-status]");
    button.disabled = true;
    status.textContent = "Saving metadata sheet…";
    try {
      await post(form.action, new FormData(form));
      if (!form.isConnected) return;
      const root = form.closest("[data-concat-metadata-config]");
      root.querySelector("[data-concat-metadata-open]").textContent = "Edit Metadata Sheet";
      root.querySelector("[data-concat-metadata-config-status]").textContent = "One metadata sheet connected";
      status.textContent = "Metadata sheet saved";
      form.closest("dialog").close();
    } catch (error) {
      if (form.isConnected) status.textContent = error.message;
    } finally {
      if (form.isConnected) button.disabled = false;
    }
  });
})();
