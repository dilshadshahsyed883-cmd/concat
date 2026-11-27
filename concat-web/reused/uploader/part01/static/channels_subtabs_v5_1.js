/* LUM_CHANNELS_SUBTABS_HOTFIX_V5_1
   Independent capture-phase controller so Channel Manager / Accounts & OAuth /
   PC Data Sync keep working even if the main LUM bundle has another runtime error. */
(() => {
  'use strict';

  const STORAGE_KEY = window.location.pathname.startsWith('/short-uploader/')
    ? 'lum.shortstudio.channel.panel' : 'lum.channel.panel';
  const PANELS = ['manager', 'accounts', 'pcsync'];

  function normalize(panel) {
    return PANELS.includes(panel) ? panel : 'manager';
  }

  function panelExists(panel) {
    return !!document.querySelector(`[data-lum-channel-panel="${panel}"]`);
  }

  function apply(panel, persist = true) {
    let wanted = normalize(panel);
    if (!panelExists(wanted)) wanted = 'manager';

    document.querySelectorAll('[data-lum-channel-panel]').forEach(el => {
      const active = el.getAttribute('data-lum-channel-panel') === wanted;
      el.hidden = !active;
      el.setAttribute('aria-hidden', active ? 'false' : 'true');
    });

    document.querySelectorAll('[data-lum-channel-panel-target]').forEach(btn => {
      const active = btn.getAttribute('data-lum-channel-panel-target') === wanted;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.setAttribute('tabindex', active ? '0' : '-1');
    });

    if (persist) {
      try { localStorage.setItem(STORAGE_KEY, wanted); } catch (_) {}
    }

    try {
      window.parent.postMessage({type: 'lum-resize'}, '*');
    } catch (_) {}
  }

  function restore() {
    let wanted = 'manager';
    try { wanted = localStorage.getItem(STORAGE_KEY) || 'manager'; } catch (_) {}
    apply(wanted, false);
  }

  // Capture phase intentionally wins before any broken/competing bubble handler.
  document.addEventListener('click', event => {
    const btn = event.target.closest('[data-lum-channel-panel-target], [data-open-channel-panel]');
    if (!btn) return;

    const panel =
      btn.getAttribute('data-lum-channel-panel-target') ||
      btn.getAttribute('data-open-channel-panel');

    if (!panel) return;

    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
    apply(panel, true);
  }, true);

  document.addEventListener('keydown', event => {
    const btn = event.target.closest('[data-lum-channel-panel-target]');
    if (!btn) return;

    const buttons = [...document.querySelectorAll('[data-lum-channel-panel-target]')];
    const index = buttons.indexOf(btn);
    if (index < 0) return;

    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = buttons.length - 1;
    else return;

    event.preventDefault();
    buttons[next].focus();
    apply(buttons[next].getAttribute('data-lum-channel-panel-target'), true);
  }, true);

  document.addEventListener('DOMContentLoaded', restore, {once: true});
  window.addEventListener('pageshow', restore);

  // Main LUM soft navigation replaces .lum-shell without re-running scripts.
  // Observe that replacement and restore the selected local panel.
  const observer = new MutationObserver(mutations => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches?.('.lum-shell') || node.querySelector?.('[data-lum-channel-panel]')) {
          queueMicrotask(restore);
          return;
        }
      }
    }
  });

  if (document.documentElement) {
    observer.observe(document.documentElement, {childList: true, subtree: true});
  }

  // Apply immediately when script loads after markup.
  restore();
})();
