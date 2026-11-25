import React, { useState, useEffect } from "react";
import { Button, Empty, InputNumber, Select, Space, Table } from "antd";
const PROPERTIES = [
  "scale",
  "offsetX",
  "offsetY",
  "rotation",
  "opacity",
  "volume",
  "maskX",
  "maskY",
  "maskWidth",
  "maskHeight",
  "maskRotation",
  "maskRoundness",
  "maskFeather",
];
const EASES = {
  Linear: [0, 0, 1, 1],
  "Ease in": [0.42, 0, 1, 1],
  "Ease out": [0, 0, 0.58, 1],
  "Ease in/out": [0.42, 0, 0.58, 1],
};
export default function Keyframes({ clip, time, onEdit, onSeek, busy }) {
  const [property, setProperty] = useState("scale"),
    [at, setAt] = useState(0),
    [value, setValue] = useState(1),
    [ease, setEase] = useState(EASES.Linear);
  useEffect(() => {
    if (clip) {
      setAt(Math.min(1, Math.max(0, (time - clip.start) / clip.duration)));
      setValue(
        clip[property] ??
          (property === "opacity" || property === "scale" ? 1 : 0),
      );
    }
  }, [clip?.id, time, property]);
  if (!clip) return <Empty description="Select a clip to edit its keyframes" />;
  const keys = (clip.keys || [])
      .filter((k) => k.property === property)
      .sort((a, b) => a.at - b.at),
    min = Math.min(0, ...keys.map((k) => k.value)),
    max = Math.max(1, ...keys.map((k) => k.value));
  function save() {
    return onEdit({
      op: "setClipKey",
      clipId: clip.id,
      property,
      at,
      value,
      ease,
    });
  }
  return (
    <div className="keyframe-panel">
      <Space wrap>
        <Select
          value={property}
          options={PROPERTIES.map((value) => ({ value }))}
          onChange={setProperty}
        />
        <span>Clip position</span>
        <InputNumber min={0} max={1} step={0.01} value={at} onChange={setAt} />
        <span>Value</span>
        <InputNumber value={value} step={0.05} onChange={setValue} />
        <Button type="primary" disabled={busy} onClick={save}>
          Add / update key
        </Button>
        <Button
          disabled={busy}
          onClick={() =>
            onEdit({ op: "clearClipKeys", clipId: clip.id, property })
          }
        >
          Clear property
        </Button>
      </Space>
      <div className="keyframe-charts">
        <svg
          viewBox="0 0 500 100"
          preserveAspectRatio="none"
          aria-label="Keyframe values"
        >
          <path
            d={keys
              .map(
                (k, i) =>
                  `${i ? "L" : "M"}${k.at * 500} ${95 - ((k.value - min) / (max - min)) * 90}`,
              )
              .join(" ")}
            fill="none"
            stroke="#3979ef"
            strokeWidth="2"
          />
          {keys.map((k) => (
            <circle
              key={k.at}
              cx={k.at * 500}
              cy={95 - ((k.value - min) / (max - min)) * 90}
              r="4"
              fill="#3979ef"
              onClick={() => {
                setAt(k.at);
                setValue(k.value);
                setEase(Array.isArray(k.ease) ? k.ease : EASES.Linear);
                onSeek(clip.start + k.at * clip.duration);
              }}
            />
          ))}
        </svg>
        <div>
          <small>Easing into selected key</small>
          <Select
            options={Object.keys(EASES).map((value) => ({ value }))}
            placeholder="Easing preset"
            onChange={(name) => setEase(EASES[name])}
          />
          <div className="row">
            {["x1", "y1", "x2", "y2"].map((name, i) => (
              <label key={name}>
                {name}
                <InputNumber
                  min={i % 2 === 0 ? 0 : -3}
                  max={i % 2 === 0 ? 1 : 3}
                  value={ease[i]}
                  step={0.05}
                  onChange={(v) =>
                    setEase(ease.map((x, j) => (i === j ? v : x)))
                  }
                />
              </label>
            ))}
          </div>
          <svg viewBox="0 0 100 100" aria-label="Bezier easing curve">
            <path
              d={`M0 100 C${ease[0] * 100} ${100 - ease[1] * 100},${ease[2] * 100} ${100 - ease[3] * 100},100 0`}
              stroke="#3979ef"
              strokeWidth="2"
              fill="none"
            />
          </svg>
        </div>
      </div>
      <Table
        size="small"
        rowKey={(k) => k.property + ":" + k.at}
        pagination={{ pageSize: 5 }}
        dataSource={keys}
        columns={[
          {
            title: "Position in clip",
            dataIndex: "at",
            render: (n) => n.toFixed(4),
          },
          { title: "Value", dataIndex: "value", render: (n) => n.toFixed(4) },
          {
            title: "Actions",
            render: (_, key) => (
              <Space>
                <Button
                  size="small"
                  onClick={() => {
                    setAt(key.at);
                    setValue(key.value);
                    setEase(Array.isArray(key.ease) ? key.ease : EASES.Linear);
                    onSeek(clip.start + key.at * clip.duration);
                  }}
                >
                  Edit
                </Button>
                <Button
                  size="small"
                  danger
                  disabled={busy}
                  onClick={() =>
                    onEdit({
                      op: "clearClipKey",
                      clipId: clip.id,
                      property,
                      at: key.at,
                    })
                  }
                >
                  Remove
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <p className="hint">
        The value plot joins key positions for navigation. Preview and export
        evaluate the actual native Bezier interpolation.
      </p>
    </div>
  );
}
