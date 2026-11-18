/* LUM_CHANNELS_PC_SYNC_V5 */
(() => {
  const SHELL = '.lum-shell';
  let navController = null;
  let navSerial = 0;
  const selectedChannelIds = new Set();

  const notifyResize = () => {
    try { window.parent.postMessage({type:'lum-resize'}, '*'); } catch (_) {}
  };

  const sameFrameUrl = href => {
    try {
      const url = new URL(href, window.location.href);
      // Replacing DOM across independent engines keeps stale scripts, CSRF and
      // CSS from the previous app; only soft-navigate inside this exact engine.
      return url.origin === window.location.origin && url.pathname === window.location.pathname;
    } catch (_) { return false; }
  };

  const prepareUploadForm = form => {
    if (!form || !form.matches('[data-lum-upload-form]')) return;
    const field = form.querySelector('input[name="app_upload_at"]');
    if (!field || !field.value || field.type === 'hidden') return;
    const parsed = new Date(field.value);
    if (Number.isNaN(parsed.getTime())) return;
    field.type = 'hidden';
    field.value = parsed.toISOString();
  };

  const currentPanel = () => localStorage.getItem('lum.channel.panel') || 'manager';

  const setChannelPanel = panel => {
    const available = [...document.querySelectorAll('[data-lum-channel-panel]')];
    if (!available.length) return;
    const valid = available.some(el => el.dataset.lumChannelPanel === panel) ? panel : 'manager';
    available.forEach(el => { el.hidden = el.dataset.lumChannelPanel !== valid; });
    document.querySelectorAll('[data-lum-channel-panel-target]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.lumChannelPanelTarget === valid);
    });
    localStorage.setItem('lum.channel.panel', valid);
    requestAnimationFrame(notifyResize);
  };

  const BASIC_COLUMNS = new Set(['id','status','channel','gmail','groups','config','token','profile','proxy','sheet']);
  const allColumnKeys = () => [...document.querySelectorAll('[data-col-key]')]
    .map(el => el.dataset.colKey).filter((v,i,a) => v && a.indexOf(v)===i);

  const loadColumns = () => {
    const saved = localStorage.getItem('lum.channel.columns');
    if (!saved) return new Set(BASIC_COLUMNS);
    try { return new Set(JSON.parse(saved)); } catch (_) { return new Set(BASIC_COLUMNS); }
  };

  const applyColumns = set => {
    allColumnKeys().forEach(key => {
      document.querySelectorAll(`[data-col-key="${CSS.escape(key)}"]`).forEach(el => {
        el.hidden = !set.has(key);
      });
      const toggle = document.querySelector(`[data-column-toggle="${CSS.escape(key)}"]`);
      if (toggle) toggle.checked = set.has(key);
    });
    localStorage.setItem('lum.channel.columns', JSON.stringify([...set]));
    notifyResize();
  };

  const selectedIdsArray = () => [...selectedChannelIds].sort((a,b)=>a-b);

  const updateSelectionUI = () => {
    document.querySelectorAll('.lum-row-select').forEach(cb => {
      cb.checked = selectedChannelIds.has(Number(cb.value));
    });
    const count = selectedChannelIds.size;
    document.querySelectorAll('[data-selected-count]').forEach(el => {
      el.textContent = `${count} selected`;
    });
    const pageChecks = [...document.querySelectorAll('tr[data-channel-row]:not([hidden]) .lum-row-select')];
    const header = document.getElementById('lum-select-page');
    if (header) {
      header.checked = pageChecks.length > 0 && pageChecks.every(cb => selectedChannelIds.has(Number(cb.value)));
      header.indeterminate = pageChecks.some(cb => selectedChannelIds.has(Number(cb.value))) && !header.checked;
    }
  };

  const channelState = { page: 1, filtered: [] };

  const rowMatches = row => {
    const search = (document.getElementById('lum-channel-search')?.value || '').trim().toLowerCase();
    const status = document.getElementById('lum-filter-status')?.value || 'All Status';
    const config = document.getElementById('lum-filter-config')?.value || '';
    const token = document.getElementById('lum-filter-token')?.value || '';
    const group = document.getElementById('lum-filter-group')?.value || '';
    const sheet = document.getElementById('lum-filter-sheet')?.value || '';
    if (search && !(row.dataset.search || '').includes(search)) return false;
    if (status !== 'All Status' && row.dataset.status !== status) return false;
    if (config && row.dataset.config !== config) return false;
    if (token && row.dataset.token !== token) return false;
    if (group && !(row.dataset.groups || '').split(',').map(x=>x.trim()).includes(group)) return false;
    if (sheet && row.dataset.sheet !== sheet) return false;
    return true;
  };

  const applyChannelFilters = (resetPage=true) => {
    const rows = [...document.querySelectorAll('tr[data-channel-row]')];
    const filtered = rows.filter(rowMatches);
    channelState.filtered = filtered;
    if (resetPage) channelState.page = 1;
    const pageSize = Number(document.getElementById('lum-channel-page-size')?.value || 50);
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    channelState.page = Math.max(1, Math.min(channelState.page, pages));
    const start = (channelState.page - 1) * pageSize;
    const visible = new Set(filtered.slice(start, start + pageSize));
    rows.forEach(row => { row.hidden = !visible.has(row); });
    const showing = document.querySelector('[data-channel-showing]');
    if (showing) showing.textContent = `Showing ${filtered.length ? start+1 : 0}–${Math.min(start+pageSize, filtered.length)} of ${filtered.length} filtered · ${rows.length} total`;
    const label = document.querySelector('[data-page-label]');
    if (label) label.textContent = `Page ${channelState.page} of ${pages}`;
    const prev = document.querySelector('[data-page-prev]');
    const next = document.querySelector('[data-page-next]');
    if (prev) prev.disabled = channelState.page <= 1;
    if (next) next.disabled = channelState.page >= pages;
    updateSelectionUI();
    notifyResize();
  };

  const clearFilters = () => {
    const ids = ['lum-channel-search','lum-filter-status','lum-filter-config','lum-filter-token','lum-filter-group','lum-filter-sheet'];
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.tagName === 'SELECT') el.selectedIndex = 0; else el.value = '';
    });
    applyChannelFilters(true);
  };

  const prepareSelectionForm = form => {
    form.querySelectorAll('input.lum-selected-id').forEach(el => el.remove());
    const ids = selectedIdsArray();
    if (!ids.length) {
      alert('Select one or more channel rows first.');
      return false;
    }
    ids.forEach(id => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'channel_ids';
      input.value = String(id);
      input.className = 'lum-selected-id';
      form.appendChild(input);
    });
    return true;
  };

  const initChannelManager = () => {
    setChannelPanel(currentPanel());
    applyColumns(loadColumns());
    applyChannelFilters(false);
  };

  async function replaceShell(response, requestedUrl, pushState) {
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const nextShell = doc.querySelector(SHELL);
    const currentShell = document.querySelector(SHELL);
    if (!nextShell || !currentShell) {
      window.location.href = response.url || requestedUrl;
      return false;
    }
    currentShell.replaceWith(nextShell);
    if (doc.title) document.title = doc.title;
    if (pushState) history.pushState({lumSoft:true}, '', response.url || requestedUrl);
    requestAnimationFrame(() => {
      initChannelManager();
      notifyResize();
      const active = document.querySelector('.lum-tabs a.active');
      if (active?.scrollIntoView) active.scrollIntoView({block:'nearest', inline:'nearest'});
    });
    return true;
  }

  async function softNavigate(url, pushState=true) {
    const shell = document.querySelector(SHELL);
    if (!shell) { window.location.href = url; return; }
    const serial = ++navSerial;
    if (navController) navController.abort();
    navController = new AbortController();
    shell.dataset.lumLoading = '1';
    try {
      const response = await fetch(url, {
        method:'GET', credentials:'same-origin',
        headers:{'X-LUM-Soft-Navigation':'1'},
        signal:navController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (serial !== navSerial) return;
      await replaceShell(response, url, pushState);
    } catch (error) {
      if (error?.name === 'AbortError') return;
      window.location.href = url;
    } finally {
      const current = document.querySelector(SHELL);
      if (current) delete current.dataset.lumLoading;
    }
  }

  async function softSubmit(form) {
    prepareUploadForm(form);
    if (form.matches('[data-lum-selection-form]') && !prepareSelectionForm(form)) return true;
    const confirmText = form.dataset.confirm;
    if (confirmText && !confirm(confirmText)) return true;
    const shell = document.querySelector(SHELL);
    if (!shell) return false;
    shell.dataset.lumLoading = '1';
    try {
      const response = await fetch(form.action || window.location.href, {
        method:(form.method || 'POST').toUpperCase(),
        body:new FormData(form), credentials:'same-origin',
        headers:{'X-LUM-Soft-Navigation':'1'},
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await replaceShell(response, form.action || window.location.href, true);
    } catch (_) {
      const current = document.querySelector(SHELL);
      if (current) delete current.dataset.lumLoading;
      return false;
    }
  }

  const renderPcPreview = data => {
    const target = document.getElementById('lum-pc-sync-result');
    if (!target) return;
    target.innerHTML = `
      <div class="lum-pc-preview">
        <div class="lum-pc-preview-grid">
          <div><span>Current PC channels</span><strong>${Number(data.pc_channels_current||0)}</strong></div>
          <div><span>Archived/history rows</span><strong>${Number(data.pc_channels_archived_only||0)}</strong></div>
          <div><span>Sheet sources</span><strong>${Number(data.pc_sheet_sources||0)}</strong></div>
          <div><span>OAuth config JSONs</span><strong>${Number(data.pc_config_jsons||0)}</strong></div>
          <div><span>Token JSONs</span><strong>${Number(data.pc_token_jsons||0)}</strong></div>
          <div><span>PC database</span><strong>${data.pc_db_found ? 'FOUND' : 'NOT FOUND'}</strong></div>
          <div><span>New channels</span><strong>${Number(data.comparison?.new||0)}</strong></div>
          <div><span>Updates</span><strong>${Number(data.comparison?.update_available||0)}</strong></div>
          <div><span>Exact matches</span><strong>${Number(data.comparison?.exact_match||0)}</strong></div>
          <div><span>Conflicts</span><strong>${Number(data.comparison?.conflict||0)}</strong></div>
          <div><span>Recoverable tokens</span><strong>${Number(data.comparison?.token_recoverable||0)}</strong></div>
          <div><span>Recoverable clients</span><strong>${Number(data.comparison?.client_recoverable||0)}</strong></div>
        </div>
        <p>${data.message || 'Preview ready.'}</p>
        <button class="lum-button primary" type="button" data-pc-sync-apply data-snapshot-id="${data.snapshot_id}">Sync Safe Changes with Channels</button>
      </div>`;
  };

  const uploadPcSnapshot = button => {
    const folder = document.getElementById('lum-pc-sync-folder');
    const zip = document.getElementById('lum-pc-sync-zip');
    const label = document.getElementById('lum-pc-sync-label');
    const folderFiles = [...(folder?.files || [])];
    const zipFiles = [...(zip?.files || [])];
    const files = folderFiles.length ? folderFiles : zipFiles;
    if (!files.length) { alert('Select the PC data/app folder or a ZIP first.'); return; }
    const fd = new FormData();
    fd.append('csrf_token', button.dataset.csrf || '');
    fd.append('source_label', label?.value || '');
    files.forEach(file => {
      const name = file.webkitRelativePath || file.name;
      fd.append('files', file, name);
    });
    const progress = document.querySelector('.lum-upload-progress');
    const bar = progress?.querySelector('i');
    const text = progress?.querySelector('span');
    if (progress) progress.hidden = false;
    if (bar) bar.style.width = '0%';
    if (text) text.textContent = '0%';
    button.disabled = true;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${window.location.pathname.replace(/\/app$/, '')}/actions/pc-sync/upload`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.max(0, Math.min(100, Math.round(e.loaded/e.total*100)));
      if (bar) bar.style.width = `${pct}%`;
      if (text) text.textContent = `${pct}%`;
    };
    xhr.onload = () => {
      button.disabled = false;
      try {
        const data = JSON.parse(xhr.responseText || '{}');
        if (xhr.status < 200 || xhr.status >= 300) throw new Error(data.detail || `HTTP ${xhr.status}`);
        renderPcPreview(data);
      } catch (err) {
        const target = document.getElementById('lum-pc-sync-result');
        if (target) target.innerHTML = `<div class="lum-alert error">${String(err.message || err)}</div>`;
      }
      notifyResize();
    };
    xhr.onerror = () => {
      button.disabled = false;
      const target = document.getElementById('lum-pc-sync-result');
      if (target) target.innerHTML = '<div class="lum-alert error">PC snapshot upload failed.</div>';
    };
    xhr.send(fd);
  };

  const applyPcSnapshot = async button => {
    if (!confirm('Sync this PC snapshot into VPS Channels? Existing valid VPS tokens/configs win, live uploader.db will NOT be replaced, and the current Google Sheet remains final inventory authority.')) return;
    const csrf = document.querySelector('[data-pc-sync-upload]')?.dataset.csrf || '';
    const fd = new FormData();
    fd.append('csrf_token', csrf);
    fd.append('snapshot_id', button.dataset.snapshotId || '');
    button.disabled = true;
    button.textContent = 'Synchronizing…';
    const target = document.getElementById('lum-pc-sync-result');
    try {
      const url = `${window.location.pathname.replace(/\/app$/, '')}/actions/pc-sync/apply`;
      const response = await fetch(url, {method:'POST', body:fd, credentials:'same-origin'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      target.innerHTML = `<div class="lum-alert success">${data.summary || 'PC Data Sync completed.'}</div>
        <div class="lum-pc-result-detail">Channels: ${data.channels_synced||0} · Configs installed: ${data.configs_installed||0} · Tokens installed: ${data.tokens_installed||0} · Token conflicts: ${data.token_conflicts||0} · Sheet resync: ${data.sheet_resync_ok||0} OK / ${data.sheet_resync_failed||0} failed</div>`;
      setTimeout(() => softNavigate(window.location.href, false), 900);
    } catch (err) {
      target.innerHTML = `<div class="lum-alert error">${String(err.message || err)}</div>`;
      button.disabled = false;
      button.textContent = 'Sync Safe Changes with Channels';
    }
    notifyResize();
  };

  document.addEventListener('input', event => {
    if (event.target?.id === 'lum-channel-search') applyChannelFilters(true);
  });
  document.addEventListener('change', event => {
    if (['lum-filter-status','lum-filter-config','lum-filter-token','lum-filter-group','lum-filter-sheet','lum-channel-page-size'].includes(event.target?.id)) {
      applyChannelFilters(true);
      return;
    }
    if (event.target?.classList.contains('lum-row-select')) {
      const id = Number(event.target.value);
      if (event.target.checked) selectedChannelIds.add(id); else selectedChannelIds.delete(id);
      updateSelectionUI();
      return;
    }
    if (event.target?.matches('[data-column-toggle]')) {
      const set = loadColumns();
      const key = event.target.dataset.columnToggle;
      if (event.target.checked) set.add(key); else set.delete(key);
      set.add('id');
      applyColumns(set);
      return;
    }
    if (event.target?.id === 'lum-thumbnail') {
      const file = event.target;
      const preview = document.getElementById('lum-thumb-preview');
      const selected = file.files?.[0];
      if (!preview || !selected) return;
      if (selected.size > 2*1024*1024) { file.value=''; alert('Thumbnail must be 2 MB or smaller.'); return; }
      const reader = new FileReader();
      reader.onload = e => { preview.innerHTML = `<img alt="Thumbnail preview" src="${e.target.result}">`; };
      reader.readAsDataURL(selected);
    }
  });

  document.addEventListener('click', async event => {
    const tab = event.target.closest('.lum-tabs a[href], a[data-lum-soft-link][href]');
    if (tab && event.button===0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && sameFrameUrl(tab.href)) {
      event.preventDefault(); softNavigate(tab.href, true); return;
    }

    const panelBtn = event.target.closest('[data-lum-channel-panel-target], [data-open-channel-panel]');
    if (panelBtn) {
      setChannelPanel(panelBtn.dataset.lumChannelPanelTarget || panelBtn.dataset.openChannelPanel);
      return;
    }

    if (event.target.closest('[data-clear-channel-filters]')) { clearFilters(); return; }
    if (event.target.closest('[data-select-filtered]')) {
      channelState.filtered.forEach(row => selectedChannelIds.add(Number(row.dataset.id)));
      updateSelectionUI(); return;
    }
    if (event.target.closest('[data-clear-selection]')) { selectedChannelIds.clear(); updateSelectionUI(); return; }
    if (event.target.closest('[data-page-prev]')) { channelState.page--; applyChannelFilters(false); return; }
    if (event.target.closest('[data-page-next]')) { channelState.page++; applyChannelFilters(false); return; }
    if (event.target.closest('[data-channel-refresh]')) { softNavigate(window.location.href, false); return; }
    if (event.target.closest('[data-columns-basic]')) { applyColumns(new Set(BASIC_COLUMNS)); return; }
    if (event.target.closest('[data-columns-all]')) { applyColumns(new Set(allColumnKeys())); return; }

    const row = event.target.closest('tr[data-channel-row]');
    if (row && !event.target.closest('a,button,input,select,textarea,summary,details,label,form')) {
      const cb = row.querySelector('.lum-row-select');
      if (cb) {
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event('change', {bubbles:true}));
      }
      return;
    }

    if (event.target?.id === 'lum-select-page') {
      const visible = [...document.querySelectorAll('tr[data-channel-row]:not([hidden]) .lum-row-select')];
      visible.forEach(cb => {
        const id = Number(cb.value);
        if (event.target.checked) selectedChannelIds.add(id); else selectedChannelIds.delete(id);
      });
      updateSelectionUI(); return;
    }

    const copy = event.target.closest('[data-copy]');
    if (copy) {
      const input = document.querySelector(copy.dataset.copy);
      if (!input) return;
      try {
        await navigator.clipboard.writeText(input.value);
        const original=copy.textContent; copy.textContent='Copied';
        setTimeout(()=>copy.textContent=original||'Copy link',1400);
      } catch (_) { input.select(); document.execCommand('copy'); }
      return;
    }

    const upload = event.target.closest('[data-pc-sync-upload]');
    if (upload) { uploadPcSnapshot(upload); return; }
    const apply = event.target.closest('[data-pc-sync-apply]');
    if (apply) { applyPcSnapshot(apply); return; }
  });

  document.addEventListener('submit', async event => {
    const form = event.target;
    prepareUploadForm(form);
    if (!form.matches('[data-lum-soft-form]') || event.defaultPrevented) return;
    event.preventDefault();
    const ok = await softSubmit(form);
    if (!ok) {
      form.removeAttribute('data-lum-soft-form');
      form.submit();
    }
  });

  window.addEventListener('popstate', () => {
    if (document.querySelector(SHELL)) softNavigate(window.location.href, false);
  });

  initChannelManager();
  notifyResize();
})();
