/* Exercise the actual picker without network or a browser dependency. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.events = {}; this.dataset = {}; this.value = ''; this.options = []; this.isConnected = true; }
  addEventListener(type, cb) { (this.events[type] ||= []).push(cb); }
  dispatchEvent(event) { for (const cb of this.events[event.type] || []) cb(event); }
  replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; }
  add(option) { this.options.push(option); }
}
class Option { constructor(text, value) { this.textContent = text; this.value = value; } }
class Event { constructor(type) { this.type = type; } }
const doc = new Element(), win = new Element();
let root, fields, requests = [], intervals = [];
function fixture() {
  root = new Element();
  root.dataset.availableMediaUrl = '/custom-prefix/api/concat/available-media';
  fields = Object.fromEntries(['video', 'link', 'status', 'refresh'].map(id => [id, new Element()]));
  root.querySelector = selector => fields[selector.replace('#concat-media-', '')];
  return root;
}
fixture();
doc.readyState = 'complete';
doc.querySelector = () => root;
win.setInterval = (cb, delay) => { assert.equal(delay, 30000); intervals.push(cb); };
const fetch = (url, options) => {
  assert.equal(url, '/custom-prefix/api/concat/available-media');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  return new Promise(resolve => requests.push(resolve));
};
const script = 'reused/uploader/part01/static/concat_media_picker.js';
vm.runInNewContext(fs.readFileSync(script, 'utf8'), {document: doc, window: win, fetch, Option, Event});
const tick = () => new Promise(resolve => setImmediate(resolve));
const items = [{id: 'one', name: '<img src=x onerror=alert(1)>.mp4', link: 'https://concat.example/video-one'}];
function respond(index, data = items, ok = true) { requests[index]({ok, status: ok ? 200 : 503, json: async () => ({items: data})}); }
(async () => {
  assert.equal(fields.video.disabled, true);
  respond(0); await tick();
  assert.equal(fields.video.options[1].textContent, items[0].name, 'names remain text');
  fields.video.value = items[0].link;
  fields.video.dispatchEvent(new Event('change'));
  assert.equal(fields.link.value, items[0].link);
  fields.link.value = 'https://concat.example/manual';
  fields.link.dispatchEvent(new Event('input'));
  assert.equal(fields.video.value, '');
  fields.refresh.dispatchEvent(new Event('click'));
  respond(1, []); await tick();
  assert.equal(fields.video.disabled, true);
  assert.equal(fields.link.value, 'https://concat.example/manual', 'refresh preserves manual input');
  win.dispatchEvent(new Event('pageshow'));
  respond(2, [], false); await tick();
  assert.match(fields.status.textContent, /Manual link still works/);
  assert.equal(fields.link.disabled, undefined);
  // Multiple responses must not let an earlier availability list win.
  win.dispatchEvent(new Event('pageshow'));
  doc.dispatchEvent(new Event('lum:shell-replaced'));
  respond(4, []); await tick();
  respond(3); await tick();
  assert.equal(fields.video.options.length, 1);
  assert.equal(fields.refresh.events.click.length, 1, 'lifecycle does not duplicate listeners');
  const previousRoot = root;
  previousRoot.isConnected = false;
  fixture();
  doc.dispatchEvent(new Event('lum:shell-replaced'));
  respond(5); await tick();
  assert.equal(fields.video.options.length, 2);
  doc.hidden = true;
  intervals[0]();
  assert.equal(requests.length, 6);
  doc.hidden = false;
  intervals[0]();
  respond(6, []); await tick();
  assert.equal(fields.video.options.length, 1, 'poll refreshes after queue changes');
  root = null;
  doc.dispatchEvent(new Event('lum:shell-replaced'));
  console.log('PASS: media picker lifecycle, autofill, manual entry, refresh, errors, races and escaped names');
})().catch(error => { console.error(error); process.exitCode = 1; });
