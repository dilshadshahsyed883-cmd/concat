'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Field {
  constructor(value = '') { this.value = value; this.events = {}; this.isConnected = true; this.dataset = {}; }
  addEventListener(type, cb) { (this.events[type] ||= new Set()).add(cb); }
  removeEventListener(type, cb) { this.events[type]?.delete(cb); }
  dispatchEvent(event) { for (const cb of this.events[event.type] || []) cb(event); }
  closest(selector) { return this.parents?.[selector] || null; }
}
class FakeFormData {
  constructor(form) { this.data = {...(form?.data || {})}; }
  set(name, value) { this.data[name] = value; }
}
const events = {};
const requests = [];
const document = {addEventListener: (type, cb) => { events[type] = cb; }};
const fetch = (url, options) => new Promise(resolve => requests.push({url, options, resolve}));
vm.runInNewContext(fs.readFileSync('reused/uploader/part01/static/concat_metadata.js', 'utf8'), {
  document, fetch, FormData: FakeFormData, Event: class { constructor(type) { this.type = type; } }
});
const link = 'https://concat.test/api/cloud-render/media/video/file';
function fixture() {
  const title = new Field('Manual title'), description = new Field('Manual description');
  const select = new Field(link), input = new Field(link), csrf = new Field('csrf');
  const status = {}, form = {elements: {namedItem: name => ({title, description, csrf_token: csrf}[name])}};
  form.querySelector = selector => ({'#concat-media-video': select, '#concat-media-link': input, '[data-concat-metadata-status]': status}[selector]);
  const button = new Field();
  button.parents = {'[data-concat-metadata-sync]': button, form};
  button.dataset.syncUrl = '/prefix/api/concat/metadata';
  return {title, description, select, input, status, button};
}
function click(button) { return events.click({target: button}); }
function respond(index, data = {ok: true, video_link: link, title: '<b>New title</b>', description: 'Line 1\nLine 2'}, ok = true) {
  requests[index].resolve({ok, json: async () => data});
}
(async () => {
  let ui = fixture();
  ui.select.value = '';
  await click(ui.button);
  assert.equal(requests.length, 0, 'no automatic fetch on selection or empty selection');
  assert.match(ui.status.textContent, /Select an available/);
  ui = fixture();
  const pending = click(ui.button);
  assert.equal(ui.button.disabled, true);
  assert.equal(requests[0].url, '/prefix/api/concat/metadata');
  assert.equal(requests[0].options.body.data.concat_video_link, link);
  assert.equal(requests[0].options.body.data.csrf_token, 'csrf');
  assert.equal(requests[0].options.credentials, 'same-origin');
  await click(ui.button);
  assert.equal(requests.length, 1, 'no duplicate request while syncing');
  respond(0); await pending;
  assert.equal(ui.title.value, '<b>New title</b>', 'sheet text is a value, not HTML');
  assert.equal(ui.description.value, 'Line 1\nLine 2');
  assert.equal(ui.button.disabled, false);
  assert.equal(ui.title.events.input.size, 0, 'temporary listeners removed');
  // Request failure leaves manually edited fields untouched.
  let next = click(ui.button);
  respond(1, {detail: 'Duplicate video names'}, false); await next;
  assert.equal(ui.title.value, '<b>New title</b>');
  assert.match(ui.status.textContent, /Duplicate/);
  // Edits, including edits that are reverted, invalidate a pending response.
  next = click(ui.button);
  ui.title.dispatchEvent({type: 'input'});
  respond(2); await next;
  assert.match(ui.status.textContent, /changed during sync/);
  // A changed video must never receive the previous video's response.
  next = click(ui.button);
  ui.select.value = 'other';
  respond(3); await next;
  assert.match(ui.status.textContent, /changed during sync/);
  ui.select.value = link;
  next = click(ui.button);
  respond(4, {ok: true, video_link: 'other', title: 'Wrong', description: 'Wrong'}); await next;
  assert.match(ui.status.textContent, /invalid metadata/);
  // Detached shells never get a stale update; next shell remains functional.
  next = click(ui.button);
  ui.button.isConnected = false;
  respond(5); await next;
  ui = fixture();
  next = click(ui.button);
  respond(6); await next;
  assert.equal(ui.title.value, '<b>New title</b>');

  const button = new Field(), status = {}, opener = {}, summary = {}, dialog = {close: () => { dialog.closed = true; }};
  const root = {querySelector: selector => ({'[data-concat-metadata-open]': opener, '[data-concat-metadata-config-status]': summary}[selector])};
  const form = new Field();
  form.matches = s => s === '[data-concat-metadata-sheet-form]';
  form.action = '/prefix/actions/concat/metadata-sheet';
  form.data = {csrf_token: 'csrf', sheet_url: 'https://docs.google.com/spreadsheets/d/' + 'a'.repeat(30) + '/edit'};
  form.querySelector = selector => ({'[type=submit]': button, '[data-concat-metadata-save-status]': status}[selector]);
  form.parents = {'[data-concat-metadata-config]': root, dialog};
  let prevented = false;
  next = events.submit({target: form, preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  respond(7, {ok: true}); await next;
  assert.equal(opener.textContent, 'Edit Metadata Sheet');
  assert.equal(summary.textContent, 'One metadata sheet connected');
  assert.equal(dialog.closed, true);
  console.log('PASS: metadata explicit sync, manual/stale-response protection, failures, popup save and lifecycle');
})().catch(error => { console.error(error); process.exitCode = 1; });
