/* Explicit sheet drafts; manual upload remains independent and fully usable. */
(() => {
  'use strict';
  const fieldNames = ['title', 'description', 'group_name', 'channel_db_id', 'privacy_status', 'app_upload_at', 'timezone_name'];
  const value = (form, name) => form.elements.namedItem(name)?.value || '';
  const node = (tag, text, cls) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (cls) element.className = cls;
    return element;
  };
  async function post(url, body, preview = false) {
    const response = await fetch(url, {method: 'POST', credentials: 'same-origin', cache: 'no-store', body,
      headers: {Accept: 'application/json', ...(preview ? {'X-Concat-Sheet-Preview': '1'} : {})}});
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error('Response unavailable. Check Queue before retrying.'); }
    if (!response.ok || !data.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
    return data;
  }
  function errors(row, catalog) {
    const result = [...row.matchErrors];
    if (!row.title.trim() || Array.from(row.title).length > 100) result.push('Title must be 1–100 characters');
    if (Array.from(row.description).length > 5000) result.push('Description exceeds 5000 characters');
    if (!catalog.groups.includes(row.group_name)) result.push('Select group');
    if (!catalog.channels.some(c => String(c.id) === row.channel_db_id && c.groups.includes(row.group_name))) result.push('Select channel in group');
    if (!['public', 'unlisted', 'private'].includes(row.privacy_status)) result.push('Invalid visibility');
    try { new Intl.DateTimeFormat('en', {timeZone: row.timezone_name}); } catch (_) { result.push('Invalid timezone'); }
    if (row.app_upload_at && !/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?$/.test(row.app_upload_at)) result.push('Use YYYY-MM-DD HH:mm');
    // Server validates future time, timezones and DST again on admission.
    if (row.scheduleError) result.push(row.scheduleError);
    return result;
  }
  function show(form, catalog) {
    const panel = form.querySelector('[data-sheet-review]');
    panel.hidden = false;
    const body = panel.querySelector('tbody'); body.replaceChildren();
    const summary = panel.querySelector('[data-sheet-summary]');
    const add = panel.querySelector('[data-sheet-add]');
    const clear = panel.querySelector('[data-sheet-clear]');
    const rows = catalog.items.map(item => ({...item, checked: !item.errors.length,
      matchErrors: item.errors.filter(e => /matching video|Duplicate video|needs video name/i.test(e)),
      scheduleError: item.errors.find(e => /scheduled|timezone|time or|time zone/i.test(e)) || '',
      queued: false, uncertain: false}));
    panel.sheetState = {rows, catalog};
    function refresh() {
      let ready = 0, selected = 0, issues = 0;
      for (const row of rows) {
        const problems = errors(row, catalog);
        if (!row.queued && !row.uncertain && !problems.length) ready++;
        if (!row.queued && problems.length) issues++;
        if (row.checked && !problems.length && !row.queued && !row.uncertain) selected++;
        row.status.textContent = row.queued ? 'Queued #' + row.project_id : row.uncertain ? 'Check Queue before retrying' : row.serverError || problems.join(' · ') || 'Ready';
        row.status.className = row.queued || !problems.length && !row.serverError && !row.uncertain ? 'sheet-ready' : 'sheet-error';
        row.checkbox.disabled = row.queued || row.uncertain || problems.length > 0 || panel.dataset.busy === '1';
        row.checkbox.checked = row.checked && !row.queued && !row.uncertain && !problems.length;
      }
      summary.textContent = rows.length + ' rows · ' + ready + ' ready · ' + issues + ' need attention';
      add.textContent = 'Add ' + selected + ' videos to Queue';
      add.disabled = !selected || panel.dataset.busy === '1';
    }
    function control(row, key, choices) {
      let input;
      if (choices) {
        input = node('select');
        input.add(new Option('Select…', ''));
        for (const [v, label] of choices) input.add(new Option(label, v));
        if (row[key] && !Array.from(input.options).some(o => o.value === row[key])) input.add(new Option(row[key] + ' (unavailable)', row[key]));
      } else {
        input = node(key === 'description' ? 'textarea' : 'input');
        if (key === 'description') input.rows = 3;
        if (key === 'app_upload_at') input.placeholder = 'YYYY-MM-DD HH:mm';
      }
      input.value = row[key];
      input.setAttribute('aria-label', key.replaceAll('_', ' ') + ' for ' + row.video_name);
      input.addEventListener('input', () => {
        row[key] = input.value; row.serverError = '';
        if (key === 'app_upload_at' || key === 'timezone_name') row.scheduleError = '';
        if (key === 'group_name') {
          row.channel_db_id = '';
          row.channelSelect.replaceChildren(new Option('Select channel', ''));
          for (const c of catalog.channels.filter(c => c.groups.includes(input.value))) row.channelSelect.add(new Option(c.channel_name || c.channel_id, String(c.id)));
        }
        refresh();
      });
      return input;
    }
    for (const row of rows) {
      const tr = node('tr'); row.element = tr;
      const cell = (...children) => { const td = node('td'); td.append(...children); tr.append(td); return td; };
      row.checkbox = node('input'); row.checkbox.type = 'checkbox'; row.checkbox.setAttribute('aria-label', 'Queue ' + row.video_name);
      row.checkbox.addEventListener('change', () => { row.checked = row.checkbox.checked; refresh(); });
      cell(row.checkbox); cell(node('strong', row.video_name));
      cell(control(row, 'title'), control(row, 'description'));
      cell(control(row, 'group_name', catalog.groups.map(g => [g, g])));
      row.channelSelect = control(row, 'channel_db_id', catalog.channels.filter(c => c.groups.includes(row.group_name)).map(c => [String(c.id), c.channel_name || c.channel_id]));
      cell(row.channelSelect);
      cell(control(row, 'privacy_status', ['public', 'unlisted', 'private'].map(v => [v, v])));
      cell(control(row, 'app_upload_at'), control(row, 'timezone_name'));
      const thumb = node('div', undefined, 'sheet-thumbnail');
      function drawThumbnail() {
        thumb.replaceChildren();
        if (row.thumbnail) { const img = node('img'); img.src = row.thumbnail.url; img.alt = row.thumbnail.name; thumb.append(img, node('small', row.thumbnail.name)); }
        else thumb.append(node('small', row.thumbnail_message || 'Optional — no thumbnail'));
      }
      drawThumbnail();
      const library = node('button', 'Thumbnail Library', 'lum-button secondary'); library.type = 'button';
      library.addEventListener('click', () => {
        form.querySelector('[data-thumbnail-library-open]').click();
        const dialog = form.querySelector('[data-thumbnail-library]');
        dialog.onThumbnailSelected = item => { row.thumbnail = item; drawThumbnail(); };
      });
      const none = node('button', 'Clear', 'lum-button secondary'); none.type = 'button';
      none.addEventListener('click', () => { row.thumbnail = null; row.thumbnail_message = 'Optional — no thumbnail'; drawThumbnail(); });
      cell(thumb, library, none);
      row.status = node('span'); cell(row.status);
      const remove = node('button', 'Remove', 'lum-button secondary'); remove.type = 'button';
      remove.addEventListener('click', () => { rows.splice(rows.indexOf(row), 1); tr.remove(); refresh(); });
      cell(remove); body.append(tr);
    }
    clear.onclick = () => { if (panel.dataset.busy !== '1') { panel.hidden = true; panel.sheetState = null; } };
    add.onclick = async () => {
      if (panel.dataset.busy === '1' || form.dataset.queueBusy === '1') return;
      const selected = rows.filter(r => r.checked && !r.queued && !r.uncertain && !errors(r, catalog).length);
      if (!selected.length) return;
      panel.dataset.busy = '1'; form.dataset.sheetQueueBusy = '1';
      const sync = form.querySelector('[data-concat-metadata-sync]'); sync.disabled = true;
      const manual = form.querySelector('[data-concat-add-queue]'); manual.disabled = true;
      // Snapshot every selected row before asynchronous work begins.
      const jobs = selected.map(row => ({row, data: {...row}, body: new FormData(form)}));
      panel.querySelectorAll('input,select,textarea,button').forEach(e => { e.disabled = true; });
      const queueStatus = panel.querySelector('[data-sheet-queue-status]');
      try {
        for (const [index, job] of jobs.entries()) {
          const {row, data, body} = job;
          fieldNames.forEach(name => body.set(name, data[name]));
          body.set('concat_video_link', data.video_link); body.set('source_mode', 'concat_link');
          // Batch rows use their own library selection; never duplicate the manual file.
          body.delete('thumbnail'); body.set('library_thumbnail_id', data.thumbnail?.id || '');
          body.set('profile_id', '0');
          queueStatus.textContent = 'Adding ' + (index + 1) + ' / ' + jobs.length + ': ' + row.video_name;
          let response;
          try {
            response = await fetch(form.action, {method: 'POST', credentials: 'same-origin', body, headers: {Accept: 'application/json'}});
            const result = await response.json();
            if (response.ok && result.ok && result.project_id) { row.queued = true; row.project_id = result.project_id; row.checked = false; }
            else if (result.project_id) { row.uncertain = true; break; }
            else if (response.status === 400 && result.ok === false) row.serverError = result.detail || 'Admission rejected';
            else { row.uncertain = true; break; }
          } catch (_) { row.uncertain = true; break; }
          refresh();
        }
      } finally {
        delete panel.dataset.busy; delete form.dataset.sheetQueueBusy;
        panel.querySelectorAll('input,select,textarea,button').forEach(e => { e.disabled = false; });
        rows.filter(r => r.queued || r.uncertain).forEach(r => r.element.querySelectorAll('input,select,textarea,button').forEach(e => { e.disabled = true; }));
        sync.disabled = false; manual.disabled = false; refresh();
        queueStatus.textContent = rows.some(r => r.uncertain) ? 'Connection outcome unknown. Check Queue, then refresh and sync again before retrying.' : rows.filter(r => r.queued).length + ' videos queued. Failed rows remain for correction. Unscheduled entries become eligible after five minutes unless paused.';
        form.querySelector('[data-concat-media-picker]')?.reloadConcatMedia?.();
      }
    };
    refresh();
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-concat-metadata-sync][data-sheet-preview]');
    if (!button) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (button.disabled) return;
    const form = button.closest('form');
    if (form.dataset.queueBusy === '1' || form.dataset.sheetQueueBusy === '1') return;
    const select = form.querySelector('#concat-media-video');
    const status = form.querySelector('[data-concat-metadata-status]');
    const selected = select.value;
    // Any edits during the read invalidate the response, even edits later reverted.
    let changed = false;
    const mark = () => { changed = true; };
    form.addEventListener('input', mark); form.addEventListener('change', mark);
    button.disabled = true; status.textContent = selected ? 'Syncing selected video…' : 'Syncing all available videos…';
    try {
      const body = new FormData(); body.set('csrf_token', value(form, 'csrf_token')); body.set('concat_video_link', selected);
      const result = await post(button.dataset.syncUrl, body, true);
      if (!button.isConnected) return;
      if (changed || select.value !== selected) { status.textContent = 'Form changed during sync; press Sync Sheet again.'; return; }
      if (selected && (result.items.length !== 1 || result.items[0].video_link !== selected)) throw new Error('Video changed; refresh and sync again');
      if (selected) {
        const row = result.items[0];
        // Only valid resolved values replace manual destination fields.
        for (const name of fieldNames) {
          const input = form.elements.namedItem(name);
          if (input && (name !== 'channel_db_id' || row[name])) {
            input.value = row[name]; input.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }
        globalThis.ConcatThumbnailLibrary?.apply(form, row.thumbnail);
      }
      show(form, result);
      status.textContent = result.items.length + ' video(s) synced. Review rows below before adding to Queue. Manual upload remains available.';
    } catch (error) { if (button.isConnected) status.textContent = error.message; }
    finally { form.removeEventListener('input', mark); form.removeEventListener('change', mark); if (button.isConnected) button.disabled = false; }
  }, true);
})();
