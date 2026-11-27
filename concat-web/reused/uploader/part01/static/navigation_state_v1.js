/* LUM_REFRESH_STATE_FIX_V1
   Persists the server-rendered main LUM tab across outer Scene Builder refreshes.
   Nested Channels and Auto Engine panels remain owned by their existing controllers. */
(() => {
  'use strict';

  const SONG_URL = /^\/song-uploader(?:\/|$)/.test(window.location.pathname);
  const KEY = SONG_URL ? 'lum.song.main.tab' : window.location.pathname.startsWith('/short-uploader') ? 'lum.shortstudio.main.tab' : 'lum.main.tab';
  const VALID = new Set([
    'upload','authentication',
    'dashboard','channels','sheets','auto_engine',
    'add_video','bulk_import','queue','logs'
  ]);

  function currentTab() {
    try {
      const value = new URL(window.location.href).searchParams.get('tab') || '';
      return VALID.has(value) ? value : '';
    } catch (_) { return ''; }
  }

  function tabFromHref(href) {
    try {
      const value = new URL(href, window.location.href).searchParams.get('tab') || '';
      return VALID.has(value) ? value : '';
    } catch (_) { return ''; }
  }

  function save(tab) {
    if (!VALID.has(tab)) return;
    try { localStorage.setItem(KEY, tab); } catch (_) {}
  }

  function saved() {
    try {
      const value = localStorage.getItem(KEY) || '';
      return VALID.has(value) ? value : '';
    } catch (_) { return ''; }
  }

  function available(tab) {
    return !![...document.querySelectorAll('.lum-tabs a[href]')]
      .find(a => tabFromHref(a.href) === tab);
  }

  function gotoSavedIfOuterReset() {
    // Canonical Song URL always defaults to Auto Engine; never restore
    // another application's previous dashboard tab over its direct entrypoint.
    if (SONG_URL) { if (currentTab()) save(currentTab()); return; }
    const now = currentTab();
    const wanted = saved();

    // Existing LUM/outer integration reloads the iframe at its mode default.
    // If the user last chose another valid visible tab, restore it.
    if (!wanted || wanted === now || !available(wanted)) {
      if (now) save(now);
      return;
    }

    const defaults = new Set(['', 'dashboard', 'upload']);
    if (!defaults.has(now)) {
      save(now);
      return;
    }

    const target = [...document.querySelectorAll('.lum-tabs a[href]')]
      .find(a => tabFromHref(a.href) === wanted);

    if (target) {
      // replace avoids adding a fake "dashboard" entry during refresh restoration.
      window.location.replace(target.href);
    }
  }

  // Save the intended main tab before existing soft-navigation handlers run.
  document.addEventListener('click', event => {
    const a = event.target.closest('.lum-tabs a[href]');
    if (!a) return;
    const tab = tabFromHref(a.href);
    if (tab) save(tab);
  }, true);

  // Forms/actions can redirect to another main tab. Persist the resulting real tab.
  window.addEventListener('pageshow', () => {
    const now = currentTab();
    if (now && !['dashboard','upload'].includes(now)) save(now);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', gotoSavedIfOuterReset, {once:true});
  } else {
    gotoSavedIfOuterReset();
  }
})();
