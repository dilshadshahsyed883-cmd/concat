/* Run real delegated thumbnail handlers against a minimal DOM. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const limit = Number(process.argv[2] || 50 * 1024 * 1024);
const root = path.join(__dirname, '../../reused/uploader/part01/static');

for (const [script, id] of [['concat_upload_preview.js', 'concat-thumbnail']]) {
  const listeners = {};
  let reads = 0;
  let alerts = [];
  const image = {removeAttribute() {}};
  const preview = {hidden: true, querySelector: () => image};
  const error = {hidden: true, textContent: ''};
  const input = {id, value: 'selected', isConnected: true,
    dataset: {maxThumbnailBytes: String(limit)},
    matches: () => false, classList: {contains: () => false},
    setCustomValidity(message) { this.validationMessage = message; },
    form: {querySelector: selector => selector.includes('error') ? error : preview}};
  const document = {
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    getElementById: id => id === 'lum-thumb-preview' ? preview : null,
    querySelectorAll: () => [], querySelector: () => null,
  };
  class FileReader { readAsDataURL() { reads++; } }
  vm.runInNewContext(fs.readFileSync(path.join(root, script), 'utf8'), {
    document, FileReader, alert: message => alerts.push(message),
    window: {addEventListener() {}, parent: {postMessage() {}}},
    localStorage: {getItem: () => null, setItem() {}},
    requestAnimationFrame: callback => callback(),
  });
  function change(size, type = 'image/png', name = 'thumb.png') {
    const before = reads;
    alerts = [];
    input.files = [{size, type, name}];
    input.value = 'selected';
    for (const handler of listeners.change) handler({target: input});
    return reads > before;
  }
  for (const size of [2 * 1024 * 1024, 2 * 1024 * 1024 + 1, limit]) {
    assert.equal(change(size), true, `${script} accepts ${size}`);
  }
  assert.equal(change(limit + 1), false, `${script} rejects over limit`);
  assert.match(input.validationMessage || alerts[0], /50 MB/);
  if (id === 'concat-thumbnail') {
    assert.equal(change(1024, 'image/gif', 'bad.gif'), false);
    assert.match(input.validationMessage, /JPG or PNG/);
    assert.equal(change(limit, 'image/jpeg', 'thumb.jpg'), true);
    assert.equal(input.validationMessage, '');
  }
}
console.log('PASS Concat thumbnail browser validator at 2 MB, 50 MB, and over limit');
