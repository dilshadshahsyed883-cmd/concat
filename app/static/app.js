(() => {
  "use strict";

  // Independent Concat sidebar: same desktop rail / mobile drawer behavior as
  // Scene Builder. No cross-app state, tokens or network requests.
  const toggle = document.getElementById("nav-toggle");
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("nav-backdrop");
  if (!toggle || !sidebar) return;

  const isMobile = () => window.matchMedia("(max-width: 900px)").matches;

  function close({ restoreFocus = false } = {}) {
    const wasOpen = sidebar.classList.contains("open");
    sidebar.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
    document.body.classList.remove("nav-open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open navigation");
    if (restoreFocus && wasOpen && isMobile()) toggle.focus();
  }

  function open() {
    if (!isMobile()) return;
    sidebar.classList.add("open");
    if (backdrop) backdrop.classList.add("open");
    document.body.classList.add("nav-open");
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", "Close navigation");
    const active = sidebar.querySelector('a[aria-current="page"]');
    const focusTarget = active || sidebar.querySelector("a");
    if (focusTarget) focusTarget.focus({ preventScroll: true });
  }

  toggle.addEventListener("click", () => {
    if (sidebar.classList.contains("open")) close({ restoreFocus: true });
    else open();
  });

  if (backdrop) backdrop.addEventListener("click", () => close({ restoreFocus: true }));
  sidebar.addEventListener("click", event => {
    if (event.target.closest("a")) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") close({ restoreFocus: true });
  });
  window.matchMedia("(max-width: 900px)").addEventListener("change", event => {
    if (!event.matches) close();
  });

  const signOut = document.getElementById("nav-logout");
  if (signOut) {
    signOut.addEventListener("click", async () => {
      const feedback = document.getElementById("nav-logout-feedback");
      signOut.disabled = true;
      if (feedback) feedback.hidden = true;
      try {
        const csrfResponse = await fetch("/api/v1/auth/csrf", {
          method: "GET", credentials: "same-origin", cache: "no-store",
          headers: { Accept: "application/json" }
        });
        if (csrfResponse.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!csrfResponse.ok) throw new Error("CSRF token unavailable");
        const payload = await csrfResponse.json();
        if (!payload.csrf_token) throw new Error("CSRF token missing");
        const response = await fetch("/auth/logout", {
          method: "POST", credentials: "same-origin", cache: "no-store",
          redirect: "manual",
          headers: { "Origin": window.location.origin, "X-CSRF-Token": payload.csrf_token }
        });
        if (response.status === 303 || response.type === "opaqueredirect") {
          window.location.assign("/auth/login");
          return;
        }
        throw new Error("Sign out unsuccessful");
      } catch (_) {
        if (feedback) {
          feedback.textContent = "Could not sign out. Try again.";
          feedback.hidden = false;
        }
      } finally {
        signOut.disabled = false;
      }
    });
  }
})();
