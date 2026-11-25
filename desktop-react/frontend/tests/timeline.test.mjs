import test from "node:test";
import assert from "node:assert/strict";
import {
  clipsAt,
  duration,
  clock,
  snappedTime,
  stateFromReply,
} from "../src/bridge.js";
test("Timeline duration follows the latest clip end across overlapping tracks and gaps", () => {
  const timeline = {
    clips: [
      { id: "audio", start: 0, duration: 10 },
      { id: "a", start: 0, duration: 4 },
      { id: "b", start: 6, duration: 3 },
    ],
  };
  assert.equal(duration(timeline), 10);
  assert.deepEqual(
    clipsAt(timeline, 4).map((c) => c.id),
    ["audio"],
  );
  assert.deepEqual(
    clipsAt(timeline, 6).map((c) => c.id),
    ["audio", "b"],
  );
  assert.equal(clipsAt(timeline, 10).length, 0);
});
test("Seek and drag snap to integer and fractional frame rates", () => {
  assert.equal(snappedTime(-1), 0);
  assert.equal(snappedTime(0.034), 1 / 30);
  assert.equal(snappedTime(1, 30000, 1001), (30 * 1001) / 30000);
  assert.throws(() => snappedTime(NaN));
  assert.throws(() => snappedTime(1, 0));
});
test("Clock and empty timeline", () => {
  assert.equal(clock(61.5), "01:01.50");
  assert.equal(duration(null), 0);
  assert.deepEqual(clipsAt(null, 0), []);
});

test("Controller replies retain preferences and revision when adopting project state", () => {
  const state = {
    view: { project: {} },
    preferences: { favorites: { physics: ["spring"] } },
    revision: 8,
  };
  assert.equal(stateFromReply({ view: state.view, state }), state);
  assert.equal(stateFromReply(state), state);
  assert.equal(stateFromReply(null), null);
});
