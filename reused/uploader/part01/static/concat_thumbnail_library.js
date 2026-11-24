/* Serial asynchronous uploads keep the popup and the rest of the uploader responsive. */
(() => {
  'use strict';
  const query = (root, selector) => root.querySelector(selector);
  const jobs = {running: false, cancelled: false, rows: [], done: 0, active: null};
  const dialogNow = () => document.querySelector('[data-thumbnail-library]');
  function clear(form) {
    const id = query(form, '[data-library-thumbnail-id]');
    const file = query(form, '#concat-thumbnail');
    if (id) { id.value = ''; id.dispatchEvent(new Event('change', {bubbles: true})); }
    if (file) { file.value = ''; file.setCustomValidity(''); }
    const preview = query(form, '[data-concat-thumbnail-preview]');
    if (preview) { preview.hidden = true; query(preview, 'img').removeAttribute('src'); }
    const error = query(form, '[data-concat-thumbnail-error]');
    if (error) { error.hidden = true; error.textContent = ''; }
  }
  function apply(form, item) {
    clear(form);
    if (!item) return;
    const id = query(form, '[data-library-thumbnail-id]');
    const preview = query(form, '[data-concat-thumbnail-preview]');
    id.value = item.id;
    id.dispatchEvent(new Event('change', {bubbles: true}));
    const image = query(preview, 'img');
    image.onload = () => { if (id.value === item.id) preview.hidden = false; };
    image.onerror = () => {
      if (id.value !== item.id) return;
      id.value = '';
      preview.hidden = true;
      const error = query(form, '[data-concat-thumbnail-error]');
      error.textContent = 'Thumbnail preview unavailable; select it again'; error.hidden = false;
    };
    image.src = item.url;
    image.alt = item.name;
  }
  window.ConcatThumbnailLibrary = {apply, clear};
  function csrf(form) { return form.elements.namedItem('csrf_token').value; }
  async function request(url, body) {
    const response = await fetch(url, {method: body ? 'POST' : 'GET', body, credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}});
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('Response unavailable; sign in again or refresh'); }
    if (!response.ok || !data.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
    return data;
  }
  function status(dialog, text) { query(dialog, '[data-thumbnail-library-status]').textContent = text; }
  async function load(dialog) {
    const serial = (dialog.loadSerial || 0) + 1;
    dialog.loadSerial = serial;
    status(dialog, 'Loading thumbnails…');
    try {
      const data = await request(dialog.dataset.libraryUrl);
      if (!dialog.isConnected || dialog.loadSerial !== serial) return;
      const grid = query(dialog, '[data-thumbnail-library-grid]');
      grid.replaceChildren();
      for (const item of data.items) {
        const tile = document.createElement('div'); tile.className = 'concat-thumbnail-tile';
        const image = document.createElement('img'); image.src = item.url; image.alt = item.name; image.loading = 'lazy';
        const label = document.createElement('p'); label.textContent = item.name;
        const choice = document.createElement('input'); choice.type = 'checkbox'; choice.dataset.thumbnailChoice = item.id; choice.disabled = item.in_use; choice.setAttribute('aria-label', 'Select ' + item.name + ' for deletion');
        const select = document.createElement('button'); select.type = 'button'; select.className = 'lum-button secondary'; select.textContent = 'Use';
        select.addEventListener('click', () => { if (dialog.onThumbnailSelected) { dialog.onThumbnailSelected(item); dialog.onThumbnailSelected = null; } else apply(dialog.closest('form'), item); dialog.close(); });
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'lum-button danger'; remove.textContent = item.in_use ? 'In use' : 'Delete'; remove.disabled = item.in_use;
        remove.addEventListener('click', () => deleteItems(dialog, [item.id]));
        tile.append(image, label, choice, select, remove); grid.append(tile);
      }
      status(dialog, data.items.length ? data.items.length + ' thumbnail(s)' : 'No thumbnails yet. Add multiple JPG/PNG files above.');
    } catch (error) { if (dialog.isConnected) status(dialog, error.message); }
  }
  async function deleteItems(dialog, ids) {
    if (!ids.length) return status(dialog, 'Select thumbnails to delete');
    if (!window.confirm('Delete ' + ids.length + ' thumbnail(s) from your library?')) return;
    const body = new FormData(); body.set('csrf_token', csrf(dialog.closest('form')));
    ids.forEach(id => body.append('ids', id));
    try {
      const data = await request(dialog.dataset.libraryUrl + '/delete', body);
      const selected = query(dialog.closest('form'), '[data-library-thumbnail-id]');
      if (data.deleted.includes(selected.value)) clear(dialog.closest('form'));
      await load(dialog);
      status(dialog, data.deleted.length + ' deleted' + (data.errors.length ? ' · ' + data.errors.map(e => e.message).join('; ') : ''));
    } catch (error) { status(dialog, error.message); }
  }
  function progress() {
    const dialog = dialogNow(); if (!dialog) return;
    const root = query(dialog, '[data-thumbnail-upload-progress]');
    if (root.uploadRows !== jobs.rows) {
      root.replaceChildren(); root.uploadRows = jobs.rows;
      root.summary = document.createElement('p'); root.append(root.summary);
      root.views = jobs.rows.map(() => {
        const line = document.createElement('div'); line.className = 'concat-thumbnail-progress';
        const text = document.createElement('span');
        const bar = document.createElement('progress'); bar.max = 100;
        line.append(text, bar); root.append(line); return {text, bar};
      });
    }
    root.summary.textContent = jobs.done + ' / ' + jobs.rows.length + ' completed' + (jobs.running ? ' · Uploading in background' : '');
    jobs.rows.forEach((job, index) => {
      const view = root.views[index], text = job.name + ' · ' + job.state;
      if (view.text.textContent !== text) view.text.textContent = text;
      if (view.bar.value !== job.percent) view.bar.value = job.percent;
    });
    query(dialog, '[data-thumbnail-bulk-files]').disabled = jobs.running;
    query(dialog, '[data-thumbnail-cancel-upload]').hidden = !jobs.running;
  }
  function upload(url, body, job) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); jobs.active = xhr;
      xhr.open('POST', url); xhr.withCredentials = true; xhr.responseType = 'json'; xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.onprogress = event => {
        job.percent = event.lengthComputable ? Math.min(99, Math.round(event.loaded * 100 / event.total)) : 0;
        job.state = 'Uploading ' + job.percent + '%'; progress();
      };
      xhr.upload.onload = () => { job.state = 'Validating and saving…'; progress(); };
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300 && xhr.response?.ok ? resolve(xhr.response) : reject(new Error(typeof xhr.response?.detail === 'string' ? xhr.response.detail : 'Upload failed'));
      xhr.onerror = () => reject(new Error('Connection interrupted; refresh library before retrying'));
      xhr.onabort = () => reject(new Error('Cancelled; refresh library to check if saved'));
      job.state = 'Connecting…'; progress();
      xhr.send(body);
    });
  }
  document.addEventListener('change', async event => {
    const input = event.target;
    if (input.id === 'concat-thumbnail') {
      const id = query(input.form, '[data-library-thumbnail-id]');
      if (id) { id.value = ''; id.dispatchEvent(new Event('change', {bubbles: true})); }
    }
    if (input.id === 'concat-media-video') clear(input.closest('form'));
    if (!input.matches('[data-thumbnail-bulk-files]') || jobs.running) return;
    const dialog = input.closest('dialog'); const form = dialog.closest('form');
    const files = Array.from(input.files || []); if (!files.length) return;
    const replace = query(dialog, '[data-thumbnail-replace]').checked;
    if (replace && !window.confirm('Replace matching unused thumbnail filenames?')) { input.value = ''; return; }
    jobs.running = true; jobs.cancelled = false; jobs.done = 0;
    jobs.rows = files.map(file => ({name: file.name, percent: 0, state: 'Waiting'})); progress();
    const token = csrf(form), url = dialog.dataset.libraryUrl + '/add';
    for (let i = 0; i < files.length; i++) {
      const file = files[i], job = jobs.rows[i];
      if (jobs.cancelled) { job.state = 'Cancelled'; jobs.done++; progress(); continue; }
      try {
        if (!/\.(jpe?g|png)$/i.test(file.name) || file.size > 50 * 1024 * 1024 || !file.size) throw new Error('Choose a nonempty JPG/PNG, maximum 50 MB');
        const body = new FormData(); body.set('csrf_token', token); body.set('thumbnail', file); body.set('replace_existing', replace ? '1' : '');
        await upload(url, body, job); job.state = 'Saved'; job.percent = 100;
      } catch (error) { job.state = error.message; }
      jobs.done++; progress();
    }
    jobs.running = false; jobs.active = null; input.value = ''; progress();
    const current = dialogNow(); if (current) await load(current);
  });
  document.addEventListener('input', event => { if (event.target.id === 'concat-media-link') clear(event.target.closest('form')); });
  document.addEventListener('click', event => {
    const target = event.target;
    if (target.closest('[data-thumbnail-library-open]')) {
      const dialog = query(target.closest('form'), '[data-thumbnail-library]'); dialog.onThumbnailSelected = null; dialog.showModal(); progress(); load(dialog);
    }
    const dialog = target.closest('[data-thumbnail-library]'); if (!dialog) return;
    if (target.closest('[data-thumbnail-library-close]')) dialog.close();
    if (target.closest('[data-thumbnail-library-refresh]')) load(dialog);
    if (target.closest('[data-thumbnail-delete-selected]')) deleteItems(dialog, Array.from(dialog.querySelectorAll('[data-thumbnail-choice]:checked')).map(e => e.dataset.thumbnailChoice));
    if (target.closest('[data-thumbnail-cancel-upload]')) { jobs.cancelled = true; jobs.active?.abort(); }
  });
  document.addEventListener('reset', event => { if (query(event.target, '[data-library-thumbnail-id]')) clear(event.target); });

  // Keep admission on the current page with an immediate spinner. Do not
  // automatically resubmit a request whose outcome is unknown after a network error.
  document.addEventListener('submit', event => {
    const form = event.target;
    const button = query(form, '[data-concat-add-queue]');
    if (!button) return;
    event.preventDefault(); if (form.dataset.queueBusy === '1' || form.dataset.sheetQueueBusy === '1') return;
    const body = new FormData(form), status = query(form, '[data-concat-queue-status]');
    form.dataset.queueBusy = '1'; button.disabled = true; button.classList.add('concat-processing'); button.textContent = 'Adding to Queue…';
    status.textContent = 'Submitting video details…';
    const xhr = new XMLHttpRequest(); xhr.open('POST', form.action); xhr.withCredentials = true; xhr.responseType = 'json'; xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = e => { status.textContent = e.lengthComputable ? 'Sending details / thumbnail ' + Math.round(e.loaded * 100 / e.total) + '%' : 'Sending details / thumbnail…'; };
    xhr.upload.onload = () => { status.textContent = 'Verifying and preparing the video. Please wait; this may take a while for large MP4s…'; };
    function finish(message) { if (!form.isConnected) return; status.textContent = message; button.disabled = false; button.classList.remove('concat-processing'); button.textContent = '+ Add Video to Queue'; delete form.dataset.queueBusy; }
    xhr.onload = () => {
      if (!form.isConnected) return;
      const data = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300 && data?.ok && data.project_id && typeof data.queue_url === 'string' && data.queue_url.startsWith('/') && !data.queue_url.startsWith('//')) {
        status.textContent = 'Added to Queue #' + data.project_id; window.location.assign(data.queue_url);
      } else finish((typeof data?.detail === 'string' ? data.detail : 'Unable to confirm admission. Check Queue before retrying.') + (data?.project_id ? ' · Review queue #' + data.project_id : ''));
    };
    xhr.onerror = () => finish('Connection interrupted. Check Queue before retrying; the request may have completed.');
    xhr.send(body);
  });
})();
