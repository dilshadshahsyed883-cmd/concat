/* Copyright Uploader presentation only. All actions use existing authenticated routes. */
(() => {
  'use strict';
  function install() {
    // Keep the original Copyright body markup contract unchanged.
    // Concat uses its styles under the separate engine-instance flag.
    if (document.querySelector('[data-engine-instance="concat"]')) {
      document.body.classList.add('lum-copyright-workspace');
    }
    const profile = document.getElementById('copyright-upload-profile');
    const requested = new URLSearchParams(location.search).get('profile_id');
    if (profile && requested && Array.from(profile.options).some(o => o.value === requested)) profile.value = requested;
    const search = document.querySelector('[data-copyright-search]');
    const status = document.querySelector('[data-copyright-status]');
    const rows = Array.from(document.querySelectorAll('[data-copyright-job]'));
    function filter() {
      const query = (search?.value || '').trim().toLowerCase();
      const selected = status?.value || 'all';
      let count = 0;
      rows.forEach(row => {
        row.hidden = !((selected === 'all' || row.dataset.status === selected) && (row.dataset.search || '').includes(query));
        if (!row.hidden) count++;
      });
      const message = document.querySelector('[data-copyright-count]');
      if (message) message.textContent = count + ' of ' + rows.length + ' videos';
      const empty = document.querySelector('[data-copyright-no-match]');
      if (empty) empty.hidden = count > 0 || rows.length === 0;
    }
    search?.addEventListener('input', filter);
    status?.addEventListener('change', filter);
    filter();
    document.querySelectorAll('[data-copyright-confirm]').forEach(form => {
      form.addEventListener('submit', event => {
        if (!window.confirm(form.dataset.copyrightConfirm)) event.preventDefault();
      });
    });
    // Mark only the active tab for assistive technologies.
    document.querySelectorAll('.lum-tabs a').forEach(link => {
      if (link.classList.contains('active')) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
