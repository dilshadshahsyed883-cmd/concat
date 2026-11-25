import React, { useState, useEffect } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Popconfirm,
} from "antd";
import { call } from "./bridge";
const row = (label, control) => (
  <label className="service-control">
    <span>{label}</span>
    {control}
  </label>
);
export function GpuPanel({ native, refresh, invoke }) {
  const cfg = native?.services?.CloudGpu || {},
    [values, setValues] = useState({}),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    setValues((v) =>
      Object.keys(v).length
        ? v
        : {
            domain: cfg.domain || "",
            "salad-key": cfg["salad-key"] || "",
            "renderer-token": cfg["renderer-token"] || "",
          },
    );
  }, [cfg.configured]);
  const edit = (k, v) => setValues({ ...values, [k]: v });
  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="service-card">
      <h3>GPU connection and render queue</h3>
      {error && <Alert type="error" message={error} />}
      <div className="service-fields">
        {row(
          "Worker domain",
          <Input
            value={values.domain}
            onChange={(e) => edit("domain", e.target.value)}
          />,
        )}
        {row(
          "Salad API key",
          <Input.Password
            value={values["salad-key"]}
            onChange={(e) => edit("salad-key", e.target.value)}
          />,
        )}
        {row(
          "Renderer API token",
          <Input.Password
            value={values["renderer-token"]}
            onChange={(e) => edit("renderer-token", e.target.value)}
          />,
        )}
      </div>
      <Space wrap>
        <Button
          loading={busy}
          onClick={() =>
            run(() => call("native_invoke", "CloudGpu", "save", [], values))
          }
        >
          Save connection
        </Button>
        <Button
          disabled={busy}
          onClick={() => run(() => invoke("CloudGpu", "test"))}
        >
          Test connection
        </Button>
        <Button
          disabled={busy || !cfg["queue-available"]}
          onClick={() => run(() => call("queue_add_files"))}
        >
          Add project ZIPs
        </Button>
        <Button
          disabled={busy || !cfg["queue-available"] || cfg["queue-started"]}
          onClick={() =>
            run(() => invoke("CloudGpu", "queue-action", ["start", ""]))
          }
        >
          Start queue
        </Button>
        <Button
          disabled={busy || !cfg["queue-started"]}
          onClick={() =>
            run(() => invoke("CloudGpu", "queue-action", ["stop", ""]))
          }
        >
          Stop queue
        </Button>
      </Space>
      <p>{cfg["connection-note"]}</p>
      <p>{cfg["queue-note"]}</p>
      <p>{cfg["metrics-note"]}</p>
      <Table
        size="small"
        pagination={false}
        rowKey="id"
        dataSource={cfg.jobs || []}
        scroll={{ x: 600 }}
        columns={[
          { title: "Project", dataIndex: "title" },
          {
            title: "Status",
            render: (_, job) => (
              <>
                <Tag>{job.state}</Tag>
                <p>{job.detail}</p>
                <small>{job.metrics}</small>
                <Progress percent={job.progress} />
                <small>{job.bytes}</small>
              </>
            ),
          },
          {
            title: "Actions",
            render: (_, job) => (
              <Space wrap>
                {["retry", "cancel", "up", "down"].map((action) => (
                  <Button
                    key={action}
                    size="small"
                    disabled={!job[action] || busy}
                    onClick={() =>
                      run(() =>
                        invoke("CloudGpu", "queue-action", [action, job.id]),
                      )
                    }
                  >
                    {action}
                  </Button>
                ))}
                <Popconfirm
                  title="Delete this queue item?"
                  onConfirm={() =>
                    run(() =>
                      invoke("CloudGpu", "queue-action", ["delete", job.id]),
                    )
                  }
                >
                  <Button danger size="small" disabled={!job.delete || busy}>
                    Delete
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
    </section>
  );
}
export function CaptionsPanel({ native, invoke }) {
  const app = native?.services?.App || {},
    c = app.captions || {},
    [text, setText] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    setText(c.text || "");
  }, [c.open]);
  const act = (name, args = []) =>
    invoke("App", "captions-" + name, args).catch((e) => setError(e.message));
  const models = (app["caption-models"] || app.transcribers || []).map(
    (m, i) => ({ value: i, label: typeof m === "string" ? m : m.name || m.id }),
  );
  return (
    <section className="service-card">
      <h3>Captions and karaoke</h3>
      {error && <Alert type="error" message={error} />}
      <Space wrap>
        <Checkbox
          checked={c["from-voice"]}
          onChange={(e) => act("voice-toggled", [e.target.checked])}
        >
          Use audio
        </Checkbox>
        <Checkbox
          checked={c.karaoke}
          onChange={(e) => act("karaoke-toggled", [e.target.checked])}
        >
          Karaoke
        </Checkbox>
        <Checkbox
          checked={c.gpu}
          onChange={(e) => act("gpu-toggled", [e.target.checked])}
        >
          GPU captions
        </Checkbox>
        <Checkbox
          checked={c["render-after"]}
          onChange={(e) => act("gpu-render-toggled", [e.target.checked])}
        >
          Render after captions
        </Checkbox>
      </Space>
      <div className="service-fields">
        {row(
          "Audio source",
          <Select
            value={c.source}
            options={(c.sources || []).map((label, value) => ({
              label,
              value,
            }))}
            onChange={(v) => act("source-changed", [v])}
          />,
        )}
        {row(
          "Transcription model",
          <Select
            value={c.model}
            options={models}
            onChange={(v) => act("model-changed", [v])}
          />,
        )}
        {!c["from-voice"] &&
          row(
            "Caption script",
            <Input.TextArea
              rows={5}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onBlur={() => act("text-edited", [text])}
            />,
          )}
        {row(
          "Placement",
          <Select
            value={c.placement}
            options={["Bottom", "Center", "Top"].map((label, value) => ({
              label,
              value,
            }))}
            onChange={(v) => act("placement-changed", [v])}
          />,
        )}
        {row(
          "Size",
          <Select
            value={c.size}
            options={["Small", "Medium", "Large"].map((label, value) => ({
              label,
              value,
            }))}
            onChange={(v) => act("size-changed", [v])}
          />,
        )}
      </div>
      <Space wrap>
        <Button
          type="primary"
          disabled={c.running}
          onClick={() => act("begin")}
        >
          Generate captions
        </Button>
        <Button disabled={!c.running} onClick={() => act("cancel")}>
          Cancel
        </Button>
        <Button onClick={() => act("import-srt")}>Import SRT</Button>
        <Button onClick={() => act("export-srt")}>Export SRT</Button>
        <Button onClick={() => act("import-gpu-json")}>Import GPU JSON</Button>
      </Space>
      <p>{c.stage}</p>
      {c.running && <Progress percent={Math.round(100 * c.progress)} />}
      <p>{c.message}</p>
    </section>
  );
}
export function VoiceoverPanel({ native, invoke }) {
  const app = native?.services?.App || {},
    v = app.voiceover || {},
    [error, setError] = useState(""),
    [prompt, setPrompt] = useState("");
  useEffect(() => setPrompt(v.prompt || ""), [v.open]);
  const act = (name, args = []) =>
    invoke("App", "voiceover-" + name, args).catch((e) => setError(e.message));
  return (
    <section className="service-card">
      <h3>Record voiceover</h3>
      {error && <Alert type="error" message={error} />}
      <p>
        {v.waiting
          ? "Opening microphone…"
          : v.ready
            ? "Microphone ready"
            : v.error}
      </p>
      <div className="service-fields">
        {row(
          "Microphone",
          <Select
            value={v.device}
            options={(app["voiceover-devices"] || []).map((label, value) => ({
              label,
              value,
            }))}
            onChange={(n) => act("device-changed", [n])}
          />,
        )}
        {row(
          "Input gain (dB)",
          <InputNumber
            value={v.gain}
            onChange={(n) => act("gain-changed", [n])}
          />,
        )}
        {row(
          "Teleprompter",
          <Input.TextArea
            rows={4}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onBlur={() => act("prompt-edited", [prompt])}
          />,
        )}
      </div>
      <Space wrap>
        <Checkbox
          checked={v.countdown}
          onChange={(e) => act("countdown-changed", [e.target.checked])}
        >
          Count in
        </Checkbox>
        <Checkbox
          checked={v.mute}
          onChange={(e) => act("mute-changed", [e.target.checked])}
        >
          Mute timeline while recording
        </Checkbox>
      </Space>
      <Space wrap style={{ marginTop: 14 }}>
        {["record", "pause", "resume", "stop", "retake", "retry"].map(
          (action) => (
            <Button
              key={action}
              danger={action === "record"}
              disabled={
                (action === "record" && !v.ready) ||
                (action === "retake" && !v["can-retake"])
              }
              onClick={() => act("action", [action])}
            >
              {action}
            </Button>
          ),
        )}
      </Space>
      <p>
        {["Ready", "Count in", "Recording", "Paused"][v.phase]}{" "}
        {v.count > 0 ? v.count : ""}
      </p>
      <p>{v.error}</p>
    </section>
  );
}
export function SpeechPanel({ native, invoke }) {
  const app = native?.services?.App || {},
    s = app.speech || {},
    [text, setText] = useState(""),
    [error, setError] = useState("");
  useEffect(() => setText(s.text || ""), [s.open]);
  const act = (name, args = []) =>
    invoke("App", "speech-" + name, args).catch((e) => setError(e.message));
  const options = (items) =>
    (items || []).map((label, value) => ({ label, value }));
  return (
    <section className="service-card">
      <h3>Text to speech</h3>
      {error && <Alert type="error" message={error} />}
      <Input.TextArea
        rows={6}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => act("text-edited", [text])}
      />
      <div className="service-fields">
        {row(
          "Voice engine",
          <Select
            value={s.model}
            options={options(app["speech-models"])}
            onChange={(v) => act("model-changed", [v])}
          />,
        )}
        {row(
          "Voice",
          <Select
            showSearch
            value={s.voice}
            options={options(app["speech-voices"])}
            onChange={(v) => act("voice-changed", [v])}
          />,
        )}
        {row(
          "Speed",
          <InputNumber
            min={0.5}
            max={2}
            step={0.05}
            value={s.speed}
            onChange={(v) => act("speed-changed", [v])}
          />,
        )}
        {row(
          "Sentence pauses",
          <InputNumber
            min={0}
            max={1}
            step={0.05}
            value={s.pauses}
            onChange={(v) => act("pauses-changed", [v])}
          />,
        )}
        {row(
          "Quality",
          <Select
            value={s.quality}
            options={options(["Quick", "Balanced", "Fine"])}
            onChange={(v) => act("quality-changed", [v])}
          />,
        )}
        {s["samples-offered"] &&
          row(
            "Reference sample",
            <Select
              value={s.sample}
              options={options(app["speech-samples"])}
              onChange={(v) => act("sample-changed", [v])}
            />,
          )}
      </div>
      {s["samples-offered"] && (
        <Checkbox
          checked={s["use-sample"]}
          onChange={(e) => act("use-sample-changed", [e.target.checked])}
        >
          Use reference sample
        </Checkbox>
      )}
      <Space wrap>
        <Button
          type="primary"
          disabled={s.running}
          onClick={() => act("begin")}
        >
          Generate voice
        </Button>
        <Button disabled={!s.running} onClick={() => act("cancel")}>
          Cancel
        </Button>
      </Space>
      <p>
        {s.stage} {s.estimate}
      </p>
      {s.running && <Progress percent={Math.round(100 * s.progress)} />}
      <p>{s.message}</p>
    </section>
  );
}
export function SettingsPanel({ native, invoke }) {
  const app = native?.services?.App || {},
    s = app.settings || {},
    [error, setError] = useState("");
  const act = (name, args = []) =>
    invoke("App", name, args).catch((e) => setError(e.message));
  const set = (key, value) => act("settings-" + key + "-changed", [value]);
  const options = (items) =>
    (items || []).map((label, value) => ({ label, value }));
  const models = [...(app.transcribers || []), ...(app.voices || [])];
  return (
    <section className="service-card">
      <h3>Application settings</h3>
      {error && <Alert type="error" message={error} />}
      <div className="service-fields">
        {row(
          "Language",
          <Select
            value={s.language}
            options={options(app.languages)}
            onChange={(v) => set("language", v)}
          />,
        )}
        {row(
          "Theme",
          <Select
            value={native?.appearance?.dark ? "dark" : "light"}
            options={[
              { value: "light", label: "Light" },
              { value: "dark", label: "Dark" },
            ]}
            onChange={(v) => set("theme", v === "dark")}
          />,
        )}
        {["proxies", "gpu", "magnetic", "playhead-stops"].map((key) =>
          row(
            key,
            <Checkbox
              key={key}
              checked={s[key]}
              disabled={key === "gpu" && !s["gpu-switchable"]}
              onChange={(e) => set(key, e.target.checked)}
            />,
          ),
        )}
        {row(
          "Download source",
          <Select
            value={s["download-source"]}
            options={options([
              "Automatic",
              "Concat mirror",
              "HF mirror",
              "Original",
              "Custom",
            ])}
            onChange={(v) => set("download-source", v)}
          />,
        )}
        {row(
          "Custom download URL",
          <Input
            defaultValue={s["download-base"]}
            onBlur={(e) =>
              act("settings-download-base-edited", [e.target.value])
            }
          />,
        )}
      </div>
      <h4>Installed and available models</h4>
      <Table
        rowKey="id"
        size="small"
        pagination={false}
        dataSource={models}
        columns={[
          {
            title: "Model",
            render: (_, m) => (
              <>
                <b>{m.name}</b>
                <p>{m.note}</p>
                <small>{m.size}</small>
              </>
            ),
          },
          {
            title: "State",
            render: (_, m) => (
              <>
                {m.active
                  ? "Active"
                  : m.installed
                    ? "Installed"
                    : "Not installed"}
                {m.downloading && (
                  <>
                    <Progress percent={100 * m.progress} />
                    {m.transferred} {m.eta}
                  </>
                )}
              </>
            ),
          },
          {
            title: "Actions",
            render: (_, m) => (
              <Space wrap>
                <Button
                  disabled={!m.installed || m.active}
                  onClick={() => act("model-activated", [m.id])}
                >
                  Activate
                </Button>
                <Button
                  disabled={m.installed || m.downloading}
                  onClick={() => act("model-download", [m.id])}
                >
                  Download
                </Button>
                <Button
                  disabled={!m.downloading}
                  onClick={() => act("model-cancel", [m.id])}
                >
                  Cancel
                </Button>
                <Button
                  danger
                  disabled={!m.installed}
                  onClick={() => act("model-remove", [m.id])}
                >
                  Remove
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <p>{s.disk}</p>
      <h4>Keyboard shortcuts</h4>
      {s.capturing && (
        <Alert
          type="info"
          message="Press the new shortcut, or Escape to cancel."
        />
      )}
      <Table
        rowKey="id"
        size="small"
        pagination={{ pageSize: 8 }}
        dataSource={s.shortcuts || []}
        columns={[
          { title: "Action", dataIndex: "label" },
          { title: "Shortcut", dataIndex: "keys" },
          {
            title: "Change",
            render: (_, shortcut) => (
              <Space>
                <Button
                  onClick={() =>
                    act("settings-shortcut-capture", [shortcut.id])
                  }
                >
                  Record shortcut
                </Button>
                <Button
                  disabled={!shortcut.custom}
                  onClick={() => act("settings-shortcut-reset", [shortcut.id])}
                >
                  Reset
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Button onClick={() => act("settings-shortcut-reset-all")}>
        Reset all shortcuts
      </Button>
      <Space>
        <Button onClick={() => act("settings-show-log")}>Open log</Button>
        <Button onClick={() => act("settings-check-updates")}>
          Check updates
        </Button>
      </Space>
      <p>
        {s.version} · {s["update-status"]}
      </p>
    </section>
  );
}
export function BuilderPanel({ native, invoke }) {
  const e = native?.services?.Editor || {},
    a = native?.services?.App || {},
    [error, setError] = useState(""),
    [name, setName] = useState("");
  useEffect(() => setName(e["cb-project-name"] || ""), [e["cb-project-name"]]);
  const act = (target, callback, args = []) =>
    invoke(target, callback, args).catch((e) => setError(e.message));
  const profile = e["cb-profile"] || "{}";
  const plan = (action) =>
    act("Editor", "cb-plan-profile", [profile, "", "", action]);
  return (
    <section className="service-card">
      <h3>CBuilder</h3>
      {error && <Alert type="error" message={error} />}
      <Space wrap>
        <Button onClick={() => act("App", "start-c-builder")}>
          Open builder
        </Button>
        <Button
          disabled={e["cb-busy"]}
          onClick={() => act("Editor", "cb-import-folder")}
        >
          Import folder
        </Button>
        <Button
          disabled={e["cb-busy"]}
          onClick={() => act("Editor", "cb-refresh-folder")}
        >
          Refresh folder
        </Button>
        <Button onClick={() => act("Editor", "cb-choose-narration")}>
          Choose narration
        </Button>
        <Button onClick={() => act("App", "c-builder-choose-output")}>
          Output folder
        </Button>
      </Space>
      <p>{e["cb-media-folder"]}</p>
      <p>
        {e["cb-image-count"]} images · {e["cb-video-count"]} videos ·{" "}
        {e["cb-duration"]} seconds
      </p>
      <p>{e["cb-narration"]}</p>
      <p>Output: {a["cb-output-folder"]}</p>
      <div className="service-fields">
        {row(
          "Project name",
          <Input
            value={name}
            onChange={(v) => setName(v.target.value)}
            onBlur={() => act("Editor", "cb-project-name-edited", [name])}
          />,
        )}
        {row(
          "Planning profile",
          <Select
            options={(e["cb-profile-names"] || []).map((label, value) => ({
              label,
              value,
            }))}
            onChange={(i) => plan("select:" + i)}
          />,
        )}
      </div>
      <Space wrap>
        {[
          "store",
          "duplicate",
          "rename",
          "load",
          "save",
          "preview",
          "reroll",
          "clear-pins",
        ].map((action) => (
          <Button
            key={action}
            disabled={e["cb-busy"]}
            onClick={() => plan(action)}
          >
            {action}
          </Button>
        ))}
      </Space>
      <Checkbox
        checked={e["cb-videos-enabled"]}
        onChange={(v) =>
          act("Editor", "cb-videos-toggled", [profile, v.target.checked])
        }
      >
        Include videos
      </Checkbox>
      {[false, true].map((video) => (
        <div key={String(video)}>
          <h4>{video ? "Video" : "Image"} keyword weights</h4>
          <Button
            size="small"
            onClick={() =>
              act("Editor", "cb-equalize-keywords", [profile, video])
            }
          >
            Equalize
          </Button>
          {(e[video ? "cb-video-keywords" : "cb-image-keywords"] || []).map(
            (k) => (
              <div className="row spaced" key={k.keyword}>
                <Checkbox
                  checked={k.selected}
                  onChange={(v) =>
                    act("Editor", "cb-keyword-changed", [
                      profile,
                      video,
                      k.keyword,
                      v.target.checked,
                      Math.round(k.weight),
                    ])
                  }
                >
                  {k.keyword} · {k.available} available
                </Checkbox>
                <InputNumber
                  min={0}
                  max={100}
                  value={k.weight}
                  onChange={(w) =>
                    act("Editor", "cb-keyword-changed", [
                      profile,
                      video,
                      k.keyword,
                      k.selected,
                      Math.round(w || 0),
                    ])
                  }
                />
              </div>
            ),
          )}
        </div>
      ))}
      <p>{e["cb-status"]}</p>
      {e["cb-busy"] && (
        <Progress
          percent={
            e["cb-scan-total"]
              ? (100 * e["cb-scan-done"]) / e["cb-scan-total"]
              : 0
          }
        />
      )}
      <pre className="builder-preview">{e["cb-preview"]}</pre>
      <Space wrap>
        <Button
          type="primary"
          disabled={e["cb-busy"] || a["cb-generating"] || a["cb-used-pending"]}
          onClick={() =>
            act("Editor", "cb-generate-project", [name, "", "", 20, profile])
          }
        >
          Generate project
        </Button>
        <Button
          disabled={!a["cb-generating"]}
          onClick={() => act("App", "c-builder-cancel-generation")}
        >
          Cancel generation
        </Button>
        <Button
          disabled={!a["cb-generated-project"]}
          onClick={() => act("App", "c-builder-open-generated")}
        >
          Open generated project
        </Button>
        <Button
          disabled={!a["cb-used-pending"]}
          onClick={() => act("App", "c-builder-retry-used")}
        >
          Retry used media
        </Button>
      </Space>
    </section>
  );
}
