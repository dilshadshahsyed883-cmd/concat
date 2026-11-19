/* Delegation keeps file previews working after uploader tab navigation. */
(() => {
  "use strict";
  function clear(form) {
    const preview = form.querySelector("[data-concat-thumbnail-preview]");
    const error = form.querySelector("[data-concat-thumbnail-error]");
    if (preview) {
      preview.hidden = true;
      preview.querySelector("img").removeAttribute("src");
    }
    if (error) { error.hidden = true; error.textContent = ""; }
  }
  document.addEventListener("change", event => {
    const input = event.target;
    if (input.id !== "concat-thumbnail" || !input.form) return;
    const form = input.form;
    clear(form);
    input.setCustomValidity("");
    const file = input.files?.[0];
    if (!file) return;
    const preview = form.querySelector("[data-concat-thumbnail-preview]");
    const error = form.querySelector("[data-concat-thumbnail-error]");
    if (!preview) return;
    const current = () => input.isConnected && input.files?.[0] === file;
    function fail(message) {
      if (!current()) return;
      input.setCustomValidity(message);
      if (error) { error.textContent = message; error.hidden = false; }
    }
    if (file.size > 2 * 1024 * 1024) return fail("Thumbnail must be 2 MB or smaller.");
    if (!["image/jpeg", "image/png"].includes(file.type) &&
        !(!file.type && /\.(jpe?g|png)$/i.test(file.name))) {
      return fail("Choose a JPG or PNG thumbnail.");
    }
    const image = preview.querySelector("img");
    const reader = new FileReader();
    reader.onerror = () => fail("Unable to read this thumbnail. Select it again.");
    reader.onload = () => {
      if (!current()) return;
      image.onload = () => { if (current()) preview.hidden = false; };
      image.onerror = () => fail("Unable to preview this image. Choose a JPG or PNG.");
      image.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
  document.addEventListener("reset", event => {
    const input = event.target.querySelector?.("#concat-thumbnail");
    if (input) { input.setCustomValidity(""); clear(event.target); }
  });
})();
