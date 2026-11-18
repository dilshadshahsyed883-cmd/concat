(() => {
  "use strict";
  const toggle = document.getElementById("nav-toggle");
  const sidebar = document.getElementById("sidebar");
  if (!toggle || !sidebar) return;
  const close = () => {sidebar.classList.remove("open"); toggle.setAttribute("aria-expanded", "false");};
  toggle.addEventListener("click", () => {
    const open = sidebar.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  });
  sidebar.addEventListener("click", e => {if (e.target.closest("a")) close();});
  document.addEventListener("keydown", e => {if (e.key === "Escape") close();});
  document.addEventListener("click", e => {if (!sidebar.contains(e.target) && !toggle.contains(e.target)) close();});
})();
