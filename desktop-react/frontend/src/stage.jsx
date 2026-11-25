import React, { useRef, useState } from "react";
import { call } from "./bridge";
// Geometry and brush mapping come from Concat. This layer only forwards normalized pointer events.
export default function Stage({ data = {}, disabled, onError }) {
  const root = useRef(null),
    dragging = useRef(false),
    queue = useRef(Promise.resolve()),
    last = useRef(0),
    [stroke, setStroke] = useState([]);
  function send(action, args) {
    queue.current = queue.current
      .then(() => call("stage", action, args))
      .catch((e) => {
        dragging.current = false;
        onError(e.message);
      });
  }
  function point(event) {
    const r = root.current.getBoundingClientRect();
    return [
      (event.clientX - r.left) / r.width,
      (event.clientY - r.top) / r.height,
    ];
  }
  function down(event) {
    if (disabled) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragging.current = true;
    const [x, y] = point(event),
      grip = event.target.closest("[data-grip]");
    if (grip)
      send("stage-grip-pressed", [
        grip.dataset.id,
        Number(grip.dataset.grip),
        x,
        y,
      ]);
    else send("stage-pressed", [x, y, event.ctrlKey || event.metaKey]);
    setStroke(data.painting ? [[x, y]] : []);
  }
  function move(event) {
    if (!dragging.current) return;
    const [x, y] = point(event);
    if (data.painting) setStroke((s) => [...s, [x, y]]);
    if (performance.now() - last.current < 35) return;
    last.current = performance.now();
    send("stage-dragged", [x, y, event.shiftKey]);
  }
  function up(event) {
    if (!dragging.current) return;
    dragging.current = false;
    send("stage-dragged", [...point(event), event.shiftKey]);
    send("stage-released", []);
    setStroke([]);
  }
  return (
    <div
      ref={root}
      className={"stage-overlay" + (data.painting ? " painting" : "")}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
    >
      {!data.painting &&
        (data.items || [])
          .filter((i) => i.selected)
          .map((item) => (
            <React.Fragment key={item.id}>
              <div
                className="stage-box"
                style={{
                  left: item.cx * 100 + "%",
                  top: item.cy * 100 + "%",
                  width: item.w * 100 + "%",
                  height: item.h * 100 + "%",
                  transform: `translate(-50%,-50%) rotate(${item.rotation}deg)`,
                }}
              >
                {[
                  [0, 0, 0],
                  [1, 100, 0],
                  [2, 100, 100],
                  [3, 0, 100],
                  [4, 50, -12],
                  [5, 50, 0],
                  [6, 100, 50],
                  [7, 50, 100],
                  [8, 0, 50],
                ].map(([grip, x, y]) => (
                  <button
                    type="button"
                    aria-label={grip === 4 ? "Rotate clip" : "Resize clip"}
                    key={grip}
                    data-grip={grip}
                    data-id={item.id}
                    style={{ left: x + "%", top: y + "%" }}
                  />
                ))}
              </div>
              {item.mask > 0 &&
                [
                  ["mask-cx", "mask-cy", 20],
                  ["mask-wx", "mask-wy", 21],
                  ["mask-hx", "mask-hy", 22],
                  ["mask-kx", "mask-ky", 23],
                  ["mask-rx", "mask-ry", 24],
                  ["mask-fx", "mask-fy", 25],
                ].map(([x, y, grip]) => (
                  <button
                    type="button"
                    aria-label="Adjust mask"
                    className="mask-grip"
                    key={grip}
                    data-grip={grip}
                    data-id={item.id}
                    style={{
                      left: item[x] * 100 + "%",
                      top: item[y] * 100 + "%",
                    }}
                  />
                ))}
            </React.Fragment>
          ))}
      {stroke.length > 0 && (
        <svg viewBox="0 0 1 1" preserveAspectRatio="none">
          <polyline
            points={stroke.map(([x, y]) => `${x},${y}`).join(" ")}
            fill="none"
            stroke="#ff5869"
            strokeWidth={Math.max(0.003, data["brush-size"] || 0.02)}
            strokeLinecap="round"
            strokeLinejoin="round"
            opacity=".6"
          />
        </svg>
      )}
    </div>
  );
}
