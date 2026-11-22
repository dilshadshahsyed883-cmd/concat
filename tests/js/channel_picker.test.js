/* Node-only DOM regression test for the real Concat Uploader selector script. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeOption {
  constructor(text, value) {
    this.textContent = text;
    this.value = String(value);
    this.dataset = {};
  }
  cloneNode() {
    const copy = new FakeOption(this.textContent, this.value);
    copy.dataset = {...this.dataset};
    return copy;
  }
}
class FakeSelect {
  constructor(id, options, value) {
    this.id = id;
    this.options = options;
    this.value = value;
    this.dataset = {};
    this.events = {};
    this.disabled = false;
  }
  addEventListener(type, handler) {
    (this.events[type] ||= []).push(handler);
  }
  dispatchEvent(event) {
    for (const handler of this.events[event.type] || []) handler(event);
  }
  replaceChildren(...options) {
    this.options = options;
    this.value = options[0]?.value || '';
  }
  add(option) { this.options.push(option); }
}
class FakeEvent { constructor(type) { this.type = type; } }
function fixture() {
  const channels = [
    new FakeOption('Choose a group', ''),
    new FakeOption('Ronaldo first', '101'),
    new FakeOption('Ronaldo second', '102'),
    new FakeOption('Messi only', '201')
  ];
  channels[1].dataset.groups = ' Ronaldo ; Favourites';
  channels[2].dataset.groups = 'Ronaldo';
  channels[3].dataset.groups = ' Messi ';
  const mode = new FakeSelect('copyright-source-mode', [], 'concat_link');
  return {
    'copyright-source-mode': mode,
    'copyright-channel-group': new FakeSelect('copyright-channel-group', [
      new FakeOption('Select a group', ''),
      new FakeOption('Ronaldo', 'Ronaldo'),
      new FakeOption('Messi', 'Messi'),
      new FakeOption('Empty', 'Empty')
    ], ''),
    'copyright-channel-choice': new FakeSelect('copyright-channel-choice', channels, '')
  };
}
const docEvents = {};
const winEvents = {};
let dom = fixture();
const document = {
  readyState: 'complete',
  getElementById: id => dom[id] || null,
  querySelector: () => null,
  addEventListener: (type, cb) => (docEvents[type] ||= []).push(cb),
  dispatchEvent: event => (docEvents[event.type] || []).forEach(cb => cb(event))
};
const window = {
  addEventListener: (type, cb) => (winEvents[type] ||= []).push(cb)
};
const script = path.resolve(__dirname, '../../reused/uploader/part01/static/copyright_video_picker.js');
vm.runInNewContext(fs.readFileSync(script, 'utf8'), {
  document, window, location: {search: ''}, URLSearchParams, Option: FakeOption, Event: FakeEvent
}, {filename: script});
function select(group) {
  dom['copyright-channel-group'].value = group;
  dom['copyright-channel-group'].dispatchEvent(new FakeEvent('change'));
  return dom['copyright-channel-choice'];
}
let channel = dom['copyright-channel-choice'];
assert.equal(channel.disabled, true, 'initially disabled without group');
assert.equal(channel.options[0].textContent, 'Select a group first');

channel = select('Ronaldo');
assert.equal(channel.disabled, false);
assert.deepEqual(channel.options.map(o => o.value), ['', '101', '102']);
assert.equal(channel.options[0].textContent, 'Select channel from this group');
channel.value = '102';
channel = select('Messi');
assert.deepEqual(channel.options.map(o => o.value), ['', '201']);
assert.equal(channel.value, '', 'previous channel is cleared when group changes');
channel = select('Empty');
assert.equal(channel.disabled, true);
assert.equal(channel.options[0].textContent, 'No channels in this group');

// Simulate a replaced .lum-shell after in-frame soft navigation.
dom = fixture();
document.dispatchEvent(new FakeEvent('lum:shell-replaced'));
channel = select('Ronaldo');
assert.deepEqual(channel.options.map(o => o.value), ['', '101', '102']);
assert.equal(channel.disabled, false);
// Repeated navigation lifecycle events must not duplicate listeners.
document.dispatchEvent(new FakeEvent('lum:shell-replaced'));
assert.equal(dom['copyright-channel-group'].events.change.length, 1);

// A restored group may change without a browser change event.
dom['copyright-channel-group'].value = 'Messi';
for (const cb of winEvents.pageshow || []) cb(new FakeEvent('pageshow'));
assert.deepEqual(dom['copyright-channel-choice'].options.map(o => o.value), ['', '201']);
assert.equal(dom['copyright-channel-choice'].disabled, false);

// No picker elements on the Channels tab; next Upload Engine tab must still work.
dom = {};
document.dispatchEvent(new FakeEvent('lum:shell-replaced'));
dom = fixture();
document.dispatchEvent(new FakeEvent('lum:shell-replaced'));
assert.equal(select('Ronaldo').disabled, false);
console.log('PASS: Issue #24 dependent channel picker lifecycle and group filtering');
