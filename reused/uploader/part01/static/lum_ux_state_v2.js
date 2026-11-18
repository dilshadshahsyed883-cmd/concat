/* LUM_DARK_UX_CONSOLIDATED_V1
   Navigation/context reliability layer.
   - Server-rendered active tab is the source of truth.
   - Preserves exact heavy/simple workspace across outer refreshes.
   - Preserves flash messages when recovering from an invalid/coerced tab.
   - Leaves feature behavior to existing LUM controllers. */
(() => {
  'use strict';

  // SONG_UPLOADER_ROUTE_STATE_ISOLATION_V1: A saved Upload Studio route must
  // never cause the canonical Song Uploader to navigate to Engine 1.
  const CURRENT_PATH = window.location.pathname;
  const SONG_STUDIO = /^\/song-uploader(?:\/|$)/.test(CURRENT_PATH);
  const COPYRIGHT_STUDIO = /^\/copyright-uploader(?:\/|$)/.test(CURRENT_PATH);
  const SHORT_STUDIO = /^\/short-uploader(?:\/|$)/.test(CURRENT_PATH);
  const ISOLATED_MODE = /^\/local-upload-manager\/modes\/(engine2|shorts|song)\/app$/.exec(CURRENT_PATH);
  const ROOT_PATH = COPYRIGHT_STUDIO ? '/copyright-uploader'
    : SONG_STUDIO ? '/song-uploader'
    : SHORT_STUDIO ? '/short-uploader'
    : ISOLATED_MODE ? '/local-upload-manager/modes/' + ISOLATED_MODE[1]
    : '/local-upload-manager';
  // Keep the existing Short Studio storage names literal for its parity
  // contract, while giving Song and each uploader mode independent keys.
  const TAB_KEY = COPYRIGHT_STUDIO ? 'lum.copyright.main.tab'
    : SONG_STUDIO ? 'lum.song.main.tab'
    : SHORT_STUDIO ? 'lum.shortstudio.main.tab'
    : ISOLATED_MODE ? 'lum.' + ISOLATED_MODE[1] + '.main.tab' : 'lum.main.tab';
  const ROUTE_KEY = COPYRIGHT_STUDIO ? 'lum.copyright.route'
    : SONG_STUDIO ? 'lum.song.route'
    : SHORT_STUDIO ? 'lum.shortstudio.route'
    : ISOLATED_MODE ? 'lum.' + ISOLATED_MODE[1] + '.route' : 'lum.route';
  const VALID_TABS = new Set([
    'upload','authentication','dashboard','channels','sheets','auto_engine',
    'add_video','profiles','bulk_import','queue','logs'
  ]);

  const safeGet = key => { try { return localStorage.getItem(key) || ''; } catch (_) { return ''; } };
  const safeSet = (key, value) => { try { localStorage.setItem(key, value); } catch (_) {} };
  const safeRemove = key => { try { localStorage.removeItem(key); } catch (_) {} };

  function tabFromHref(href) {
    try {
      const value = new URL(href, window.location.href).searchParams.get('tab') || '';
      return VALID_TABS.has(value) ? value : '';
    } catch (_) { return ''; }
  }

  function requestedTab() {
    try {
      const value = new URL(window.location.href).searchParams.get('tab') || '';
      return VALID_TABS.has(value) ? value : '';
    } catch (_) { return ''; }
  }

  function activeTab() {
    const a = document.querySelector('.lum-tabs a.active[href]');
    return a ? tabFromHref(a.href) : '';
  }

  function linkFor(tab) {
    if (!VALID_TABS.has(tab)) return null;
    return [...document.querySelectorAll('.lum-tabs a[href]')]
      .find(a => tabFromHref(a.href) === tab) || null;
  }

  function sanitizedRoute(raw) {
    try {
      const u = new URL(raw || '', window.location.origin);
      // Only routes of THIS studio can be restored; reject same-origin routes
      // from Upload Studio, Short Studio or other isolated uploader engines.
      const owned = u.pathname === ROOT_PATH + '/app'
        || (SONG_STUDIO && (u.pathname === ROOT_PATH || u.pathname === ROOT_PATH + '/'))
        || (SHORT_STUDIO && u.pathname === ROOT_PATH);
      if (u.origin !== window.location.origin || !owned) return '';
      const out = new URL(u.pathname, window.location.origin);
      const tab = u.searchParams.get('tab') || '';
      if (VALID_TABS.has(tab)) out.searchParams.set('tab', tab);
      const profile = u.searchParams.get('auto_profile_id');
      if (profile && /^\d+$/.test(profile)) out.searchParams.set('auto_profile_id', profile);
      if (document.body.classList.contains('lum-embedded')) out.searchParams.set('embedded', '1');
      return out.pathname + (out.search ? out.search : '');
    } catch (_) { return ''; }
  }

  function currentCleanRoute() {
    const actual = activeTab();
    if (!actual) return '';
    try {
      const u = new URL(window.location.href);
      const out = new URL(u.pathname, u.origin);
      out.searchParams.set('tab', actual);
      if (actual === 'auto_engine') {
        const profile = u.searchParams.get('auto_profile_id');
        if (profile && /^\d+$/.test(profile)) out.searchParams.set('auto_profile_id', profile);
      }
      if (document.body.classList.contains('lum-embedded')) out.searchParams.set('embedded', '1');
      return out.pathname + out.search;
    } catch (_) { return ''; }
  }

  function saveActualState() {
    const actual = activeTab();
    if (actual) safeSet(TAB_KEY, actual);
    const route = currentCleanRoute();
    if (route) safeSet(ROUTE_KEY, route);
    notifyParent();
  }

  function flashParams() {
    try {
      const u = new URL(window.location.href);
      const result = {};
      ['message','error'].forEach(k => {
        const v = u.searchParams.get(k);
        if (v) result[k] = v;
      });
      return result;
    } catch (_) { return {}; }
  }

  function targetWithFlash(baseHref) {
    const u = new URL(baseHref, window.location.href);
    const flash = flashParams();
    Object.entries(flash).forEach(([k,v]) => u.searchParams.set(k,v));
    if (document.body.classList.contains('lum-embedded')) u.searchParams.set('embedded','1');
    return u.href;
  }

  function restoreIfNeeded() {
    const actual = activeTab();
    if (!actual) { notifyParent(); return; }

    const requested = requestedTab();
    // Direct Song Uploader entrypoint always opens its native Auto Engine tab.
    // Do not restore *any* previous app route on this canonical URL, even
    // from Song's own history. Explicit ?tab= links still take precedence.
    if (SONG_STUDIO && !requested) {
      saveActualState();
      return;
    }
    const savedTab = safeGet(TAB_KEY);
    const savedRoute = sanitizedRoute(safeGet(ROUTE_KEY));

    // If server rendered exactly what URL asked for, the URL wins. This covers
    // intentional direct navigation and normal tab clicks.
    if (requested && requested === actual) {
      saveActualState();
      return;
    }

    // If URL has a different requested tab than what the server actually rendered,
    // the server coerced an invalid tab (e.g. Heavy mode receiving tab=authentication).
    // Do NOT overwrite the previously valid workspace with the fallback Dashboard.
    const serverCoerced = !!requested && requested !== actual;

    // Outer /local-upload-manager refresh has no tab in the iframe URL. Restore the
    // last valid workspace if that tab exists in this mode.
    const outerReset = !requested;

    if ((serverCoerced || outerReset) && VALID_TABS.has(savedTab)) {
      const link = linkFor(savedTab);
      if (link) {
        let target = savedRoute;
        if (!target || tabFromHref(target) !== savedTab) target = link.href;
        const next = targetWithFlash(target);
        if (next !== window.location.href) {
          window.location.replace(next);
          return;
        }
      }
    }

    // Saved state may belong to the other Simple/Heavy mode. In that case accept
    // the server's actual tab and replace the stale state.
    saveActualState();
  }

  function notifyParent() {
    if (window.parent === window) return;
    const route = currentCleanRoute();
    try {
      window.parent.postMessage({type:'lum-route-state', route, tab:activeTab()}, window.location.origin);
    } catch (_) {}
    try { window.parent.postMessage({type:'lum-resize'}, '*'); } catch (_) {}
  }

  // Save destination BEFORE the existing soft-navigation controller runs.
  document.addEventListener('click', event => {
    const a = event.target.closest('.lum-tabs a[href],a[data-lum-soft-link][href]');
    if (a) {
      const tab = tabFromHref(a.href);
      if (tab) {
        safeSet(TAB_KEY, tab);
        const route = sanitizedRoute(a.href);
        if (route) safeSet(ROUTE_KEY, route);
      }
    }
  }, true);

  // Mode switch intentionally changes the set of available tabs, so let the server
  // choose the destination instead of restoring a stale tab from the other mode.
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.matches('.lum-mode-toggle,.admin-mode-toggle') || /\/mode(?:\?|$)/.test(form.action || '')) {
      safeRemove(TAB_KEY);
      safeRemove(ROUTE_KEY);
      return;
    }

    // Non-invasive busy feedback; values/actions remain unchanged.
    const submitter = event.submitter || form.querySelector('button[type="submit"],input[type="submit"]');
    if (submitter && !submitter.disabled) {
      submitter.setAttribute('aria-busy','true');
      form.classList.add('lum-form-busy');
      setTimeout(() => {
        if (submitter.isConnected) {
          submitter.removeAttribute('aria-busy');
          form.classList.remove('lum-form-busy');
        }
      }, 30000);
    }
  }, true);

  // Existing LUM soft navigation updates browser history. Mirror those changes to
  // the outer host so a full browser refresh starts at the same inner workspace.
  for (const method of ['pushState','replaceState']) {
    const original = history[method];
    if (typeof original !== 'function' || original.__lumUxWrapped) continue;
    const wrapped = function(...args) {
      const value = original.apply(this,args);
      queueMicrotask(saveActualState);
      return value;
    };
    wrapped.__lumUxWrapped = true;
    history[method] = wrapped;
  }

  window.addEventListener('popstate', () => setTimeout(saveActualState, 0));
  window.addEventListener('pageshow', () => setTimeout(restoreIfNeeded, 0));

  const observer = new MutationObserver(mutations => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches?.('.lum-shell') || node.querySelector?.('.lum-shell')) {
          queueMicrotask(() => {
            restoreIfNeeded();
            notifyParent();
          });
          return;
        }
      }
    }
  });
  if (document.documentElement) observer.observe(document.documentElement,{childList:true,subtree:true});

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreIfNeeded, {once:true});
  } else {
    restoreIfNeeded();
  }
})();
