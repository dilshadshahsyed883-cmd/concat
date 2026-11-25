import Stage from "./stage";
import KeyframeEditor from "./keyframes";
import Services from "./services";
import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import {
  ConfigProvider,
  Button,
  Input,
  InputNumber,
  Select,
  Slider,
  Checkbox,
  Tabs,
  Modal,
  Alert,
  Progress,
  Empty,
  Tag,
  Spin,
  message,
  theme,
} from "antd";
import {
  PlayCircleFilled,
  PauseCircleFilled,
  SaveOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  UndoOutlined,
  RedoOutlined,
  ScissorOutlined,
  DeleteOutlined,
  StarOutlined,
  StarFilled,
  SettingOutlined,
  ExportOutlined,
  LockOutlined,
  UnlockOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  SoundOutlined,
} from "@ant-design/icons";
import {
  call,
  clock,
  duration as timelineDuration,
  snappedTime,
} from "./bridge";
import "./style.css";
const NAMES = {
  basic: "Basic",
  physics: "Physics",
  canvas: "Canvas motion",
  effect: "Effects",
  transition: "Transitions",
  adjustment: "Adjustment",
  audio: "Audio",
  titles: "Titles",
  keys: "Keyframes",
};
const words = (s) =>
  s
    .replace(/^.*\./, "")
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (x) => x.toUpperCase());
function App() {
  const [state, setState] = useState(null),
    [boot, setBoot] = useState(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [selected, setSelected] = useState([]),
    [category, setCategory] = useState("basic"),
    [preset, setPreset] = useState("none"),
    [controls, setControls] = useState({}),
    [time, setTime] = useState(0),
    [frame, setFrame] = useState(""),
    [thumbs, setThumbs] = useState({}),
    [waves, setWaves] = useState({}),
    [stageData, setStageData] = useState({}),
    [scale, setScale] = useState(75),
    [job, setJob] = useState(null),
    [video, setVideo] = useState(""),
    [playing, setPlaying] = useState(false),
    [panels, setPanels] = useState({ left: 230, right: 300 }),
    [settings, setSettings] = useState(false),
    [services, setServices] = useState(""),
    [title, setTitle] = useState("Your title"),
    [params, setParams] = useState({}),
    [duration, setDuration] = useState(0.9),
    [expert, setExpert] = useState(false),
    [tool, setTool] = useState("edit_apply"),
    [argumentsText, setArgumentsText] = useState("{}");
  const sheets = useRef({}),
    capturing = useRef(false);
  const notices = useRef(0),
    frameBusy = useRef(false),
    latest = useRef({}),
    vid = useRef(null);
  const view = state?.view,
    p = view?.project,
    timeline =
      p?.timelines.find((x) => x.id === p.activeTimelineId) ||
      p?.timelines?.[0],
    clips = timeline?.clips || [],
    clip = clips.find((c) => c.id === selected[0]),
    end = timelineDuration(timeline),
    media = p?.media || [],
    prefs = state?.preferences || {
      favorites: {},
      defaults: {},
      lockedTracks: [],
    },
    catalog = boot?.catalogue || [];
  const packages = ["effect", "transition", "audio"].includes(category)
    ? catalog.filter((x) =>
        category === "effect"
          ? ["effect", "filter"].includes(x.kind)
          : x.kind === category,
      )
    : [];
  const motion =
      boot?.motion?.presets.filter((x) => x.category === category) || [],
    cards = [...motion, ...packages],
    chosen = cards.find((x) => x.id === preset),
    favorites = prefs.favorites?.[category] || [];
  latest.current = { path: state?.path, revision: state?.revision, time };
  function adopt(s) {
    if (s?.view !== undefined) {
      setState(s);
      setVideo("");
      setSelected((ids) =>
        ids.filter((id) =>
          s.view?.project.timelines.some((t) =>
            t.clips.some((c) => c.id === id),
          ),
        ),
      );
    }
  }
  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      return await fn();
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function action(name, ...args) {
    return run(async () => {
      const s = await call(name, ...args);
      adopt(s);
      return s;
    });
  }
  async function load() {
    await run(async () => {
      const b = await call("boot");
      setBoot(b);
      adopt(b);
    });
  }
  useEffect(() => {
    load();
    const fail = (e) => setError("Could not save project: " + e.detail);
    window.addEventListener("concat-close-error", fail);
    return () => window.removeEventListener("concat-close-error", fail);
  }, []);
  useEffect(() => {
    if (!boot) return;
    let live = true,
      waiting = false;
    const timer = setInterval(async () => {
      if (waiting) return;
      waiting = true;
      try {
        const events = await call("poll");
        if (!live) return;
        if (events.state) adopt(events.state);
        if (events.stage) setStageData(events.stage);
        if (events.selected) setSelected(events.selected);
        capturing.current = !!events.capturing;
        setPlaying(!!events.playing);
        if (events.playing) setTime(events.time);
        if (events.sheets) {
          for (const [name, shown] of Object.entries(events.sheets)) {
            if (shown && !sheets.current[name]) setServices(name);
          }
          sheets.current = events.sheets;
        }
        if (events.waves)
          setWaves(Object.fromEntries(events.waves.map((w) => [w.id, w])));
        const n = events.toast;
        if (n?.token !== notices.current) {
          notices.current = n?.token;
          if (n?.message) {
            if (n.failed) setError(n.message);
            else message.info(n.message);
          }
        }
      } catch (e) {
        if (live) setError(e.message);
      } finally {
        waiting = false;
      }
    }, 900);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [!!boot]);
  useEffect(() => {
    if (!playing) return;
    let live = true,
      waiting = false;
    const timer = setInterval(async () => {
      if (waiting) return;
      waiting = true;
      try {
        const s = await call("playback_status");
        if (live) {
          setTime(s.time);
          setPlaying(s.playing);
        }
      } catch (e) {
        setPlaying(false);
        setError(e.message);
      } finally {
        waiting = false;
      }
    }, 100);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [playing]);
  useEffect(() => {
    const d = prefs.defaults?.[category];
    setPreset(d?.preset || "none");
    setControls(d?.controls || boot?.motion?.defaults?.[category] || {});
    setParams(d?.params || {});
  }, [category, !!boot]);
  useEffect(() => {
    let live = true;
    const timer = setInterval(async () => {
      const a = latest.current;
      if (!a.path || frameBusy.current) return;
      const key = a.revision + ":" + a.time.toFixed(3);
      if (frameBusy.last === key) return;
      frameBusy.last = key;
      frameBusy.current = true;
      try {
        const f = await call("frame", a.time, 960);
        if (live) setFrame(f);
      } catch (e) {
        if (live) setError(e.message);
      } finally {
        frameBusy.current = false;
      }
    }, 160);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    let live = true;
    (async () => {
      for (const c of clips.slice(0, 200)) {
        if (thumbs[c.id]) continue;
        try {
          const f = await call("thumbnail", c.id);
          if (live) setThumbs((v) => ({ ...v, [c.id]: f }));
        } catch {}
      }
    })();
    return () => {
      live = false;
    };
  }, [state?.revision]);
  useEffect(() => {
    if (!job || job.state !== "running") return;
    const timer = setInterval(async () => {
      try {
        const s = await call("job_status", job.job);
        setJob(s);
        if (s.state === "failed") setError(s.error?.message || "Export failed");
        if (s.video) setVideo(s.video);
        if (s.state === "done" && !s.video)
          message.success("Export complete: " + s.output);
      } catch (e) {
        setError(e.message);
      }
    }, 600);
    return () => clearInterval(timer);
  }, [job?.job, job?.state]);
  async function edit(command) {
    return action("edit", command);
  }
  async function apply(
    all = false,
    id = preset,
    setting = controls,
    parameters = params,
  ) {
    if (["effect", "transition", "audio"].includes(category))
      return action(
        "effect",
        category,
        id,
        parameters,
        selected,
        all,
        duration,
      );
    return action("preset", category, id, selected, setting, all);
  }
  async function pref(changes) {
    return action("preferences", changes);
  }
  function toggleFavorite() {
    pref({
      favorites: {
        ...prefs.favorites,
        [category]: favorites.includes(preset)
          ? favorites.filter((x) => x !== preset)
          : [...favorites, preset],
      },
    });
  }
  async function randomFavorite() {
    if (!favorites.length) return message.info("Favorite a preset first");
    const id = favorites[Math.floor(Math.random() * favorites.length)];
    const chosen = cards.find((c) => c.id === id);
    const parameters = Object.fromEntries(
      (chosen?.params || []).map((p) => [p.key, p.default]),
    );
    try {
      const setting =
        chosen?.category &&
        ["basic", "physics", "canvas"].includes(chosen.category)
          ? await call("load_preset_controls", id)
          : controls;
      setPreset(id);
      setControls(setting);
      setParams(parameters);
      await apply(false, id, setting, parameters);
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => {
    const handler = (event) => {
      const editable = event.target?.closest(
        "input,textarea,[contenteditable=true],[role=combobox]",
      );
      if (editable && !capturing.current) return;
      if (event.key === "Tab" && !capturing.current) return;
      if (["Control", "Shift", "Alt", "Meta"].includes(event.key)) return;
      if (event.ctrlKey && event.key.toLowerCase() === "o") {
        event.preventDefault();
        action("open_project");
        return;
      }
      if (event.ctrlKey && event.key.toLowerCase() === "n") {
        event.preventDefault();
        newProject();
        return;
      }
      if (event.ctrlKey && event.key.toLowerCase() === "w") {
        event.preventDefault();
        action("close_project");
        return;
      }
      if (event.ctrlKey && event.key.toLowerCase() === "q") {
        event.preventDefault();
        call("quit").catch((e) => setError(e.message));
        return;
      }
      if (event.ctrlKey && event.key.toLowerCase() === "e") {
        event.preventDefault();
        render();
        return;
      }
      if (event.ctrlKey && event.key === ",") {
        event.preventDefault();
        setServices("Settings");
        return;
      }
      if (!boot || busy) return;
      event.preventDefault();
      call("key", {
        key: event.key,
        shift: event.shiftKey,
        alt: event.altKey,
        ctrl: event.ctrlKey,
        meta: event.metaKey,
      }).catch((e) => setError(e.message));
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [!!boot, busy, state?.path]);
  function resizePanel(side, event) {
    const start = event.clientX,
      base = panels[side];
    event.currentTarget.setPointerCapture(event.pointerId);
    const move = (e) =>
      setPanels((p) => ({
        ...p,
        [side]: Math.max(
          170,
          Math.min(
            480,
            base + (e.clientX - start) * (side === "left" ? 1 : -1),
          ),
        ),
      }));
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
  }
  async function seekTime(v) {
    setTime(v);
    if (vid.current) vid.current.currentTime = v;
    try {
      await call("seek", v);
    } catch (e) {
      setError(e.message);
    }
  }
  async function playPause() {
    try {
      const s = await call("playback_toggle");
      setPlaying(s.playing);
      setTime(s.time);
    } catch (e) {
      setError(e.message);
    }
  }
  function select(c, e) {
    const ids = e?.ctrlKey
      ? selected.includes(c.id)
        ? selected.filter((x) => x !== c.id)
        : [...selected, c.id]
      : [c.id];
    setSelected(ids);
    seekTime(c.start);
    call("select_native", ids).catch((e) => setError(e.message));
  }
  function newProject() {
    Modal.confirm({
      title: "Create project",
      content: (
        <Input
          id="project-name"
          placeholder="Project name"
          defaultValue="New Concat project"
        />
      ),
      okText: "Choose folder",
      onOk: () =>
        action("new_project", document.getElementById("project-name").value),
    });
  }
  function changeClip(key, value) {
    if (!clip) return;
    if (key === "start")
      edit({
        op: "moveClips",
        moves: [{ clipId: clip.id, trackId: clip.trackId, start: value }],
      });
    else if (key === "duration")
      edit({
        op: "trimClip",
        clipId: clip.id,
        edge: "end",
        delta: value - clip.duration,
        ripple: false,
      });
    else if (key === "speed")
      edit({ op: "setClipSpeed", clipId: clip.id, speed: value });
    else if (
      [
        "scale",
        "offsetX",
        "offsetY",
        "rotation",
        "stretchX",
        "stretchY",
      ].includes(key)
    )
      edit({ op: "setClipTransform", clipId: clip.id, [key]: value });
    else edit({ op: "updateClip", clipId: clip.id, patch: { [key]: value } });
  }
  function drop(e, track) {
    e.preventDefault();
    if (busy) return;
    const data = JSON.parse(
        e.dataTransfer.getData("application/concat") || "{}",
      ),
      rect = e.currentTarget.getBoundingClientRect(),
      start = snappedTime(
        (e.clientX - rect.left) / scale,
        view.settings.rateNum,
        view.settings.rateDen,
      );
    if (data.media) action("add_media", [data.media], start, track);
    if (data.clip)
      edit({
        op: "moveClips",
        moves: [{ clipId: data.clip, start, trackId: track }],
      });
  }
  async function render(preview = false) {
    run(async () => {
      const result = await call("export", preview);
      if (result) setJob({ ...result, state: "running", fraction: 0 });
    });
  }
  if (!boot)
    return (
      <div className="loading">
        <Spin size="large" />
        <h2>Connecting Concat engine</h2>
        {error && (
          <>
            <Alert type="error" message={error} />
            <Button onClick={load}>Retry</Button>
            <Button onClick={() => run(() => call("choose_engine"))}>
              Locate Concat engine
            </Button>
          </>
        )}
      </div>
    );
  return (
    <ConfigProvider
      theme={{
        algorithm: state?.appearance?.dark
          ? theme.darkAlgorithm
          : theme.defaultAlgorithm,
        token: {
          colorPrimary: "#3979ef",
          borderRadius: 7,
          fontFamily: "Segoe UI, Arial, sans-serif",
          fontSize: 13,
        },
      }}
    >
      <div className={"app" + (state?.appearance?.dark ? " dark" : "")}>
        <header>
          <div className="brand">
            <span className="brand-icon">C</span>
            <div>
              CONCAT <b>WORKSPACE</b>
              <small>REACT DESKTOP · PREVIEW BRANCH</small>
            </div>
          </div>
          <div className="project-title">
            {view?.settings?.name || "No project open"}
            {state?.dirty ? " •" : ""}
            <span>
              {clips.length} clips · {clock(end)} · Native Concat engine
            </span>
          </div>
          <div className="header-actions">
            <Button
              icon={<SaveOutlined />}
              disabled={busy || !p}
              onClick={() => action("save_project")}
            >
              Save
            </Button>
            <Button
              icon={<SettingOutlined />}
              onClick={() => setServices("Settings")}
            >
              Settings
            </Button>
            <Button
              type="primary"
              icon={<ExportOutlined />}
              disabled={busy || !p || job?.state === "running"}
              onClick={() => render()}
            >
              Export
            </Button>
          </div>
        </header>
        <nav>
          <Button size="small" disabled={busy} onClick={newProject}>
            New project
          </Button>
          <Button
            size="small"
            icon={<FolderOpenOutlined />}
            disabled={busy}
            onClick={() => action("open_project")}
          >
            Open
          </Button>
          <Button
            size="small"
            disabled={busy || !p}
            onClick={() => action("close_project")}
          >
            Close project
          </Button>
          <Button
            size="small"
            icon={<UndoOutlined />}
            disabled={busy || !view?.canUndo}
            onClick={() => action("history", false)}
          >
            Undo
          </Button>
          <Button
            size="small"
            icon={<RedoOutlined />}
            disabled={busy || !view?.canRedo}
            onClick={() => action("history", true)}
          >
            Redo
          </Button>
          <Button
            size="small"
            disabled={busy || !p}
            onClick={() => action("import_media")}
          >
            Import media
          </Button>
          <Button
            size="small"
            disabled={busy || !p}
            onClick={() => edit({ op: "addTrack", bottom: true })}
          >
            Add track
          </Button>
          <Select
            size="small"
            value={p?.activeTimelineId}
            style={{ width: 140 }}
            disabled={!p || busy}
            options={p?.timelines.map((t) => ({ value: t.id, label: t.name }))}
            onChange={(id) => edit({ op: "selectTimeline", timelineId: id })}
          />
          <Button
            size="small"
            disabled={busy || !p}
            onClick={() => setServices("GPU queue")}
          >
            Voice · Captions · GPU queue
          </Button>
          <Button size="small" onClick={() => setExpert(true)}>
            All engine commands
          </Button>
        </nav>
        {error && (
          <Alert
            type="error"
            message={error}
            closable
            onClose={() => setError("")}
            className="banner"
          />
        )}
        <div
          className="workspace"
          style={{
            gridTemplateColumns: `${panels.left}px 6px minmax(300px,1fr) 6px ${panels.right}px`,
          }}
        >
          <aside className="library">
            <div className="panel-head">
              Media library{" "}
              <Button
                size="small"
                icon={<PlusOutlined />}
                disabled={!p || busy}
                onClick={() => action("import_media")}
              />
            </div>
            <div className="scene-list">
              {media.length ? (
                media.map((m) => (
                  <button
                    className="scene-card"
                    key={m.id}
                    draggable
                    onDragStart={(e) =>
                      e.dataTransfer.setData(
                        "application/concat",
                        JSON.stringify({ media: m.id }),
                      )
                    }
                    onDoubleClick={() => action("add_media", [m.id], end, null)}
                  >
                    <div className="media-art">
                      {m.kind === "audio"
                        ? "♫"
                        : m.kind === "image"
                          ? "▧"
                          : "▶"}
                    </div>
                    <div className="scene-meta">
                      <b>{m.name}</b>
                      <small>{m.kind}</small>
                    </div>
                    <p>{m.path}</p>
                    <Button
                      size="small"
                      disabled={busy}
                      onClick={() => action("add_media", [m.id], time, null)}
                    >
                      Add at playhead
                    </Button>
                  </button>
                ))
              ) : !p && boot.recents?.length ? (
                <div className="recent-projects">
                  <h4>Recent projects</h4>
                  {boot.recents.map((r) => (
                    <Button
                      block
                      key={r.path}
                      onClick={() => action("open_project", r.path)}
                    >
                      {r.name}
                    </Button>
                  ))}
                </div>
              ) : (
                <Empty description="Import media to begin" />
              )}
            </div>
          </aside>
          <div
            className="panel-divider"
            role="separator"
            tabIndex={0}
            aria-label="Resize media library"
            onPointerDown={(e) => resizePanel("left", e)}
          />
          <main className="center">
            <div className="preview-top">
              <span>PROGRAM MONITOR</span>
              <Tag>
                {view?.settings?.width} × {view?.settings?.height}
              </Tag>
            </div>
            <div className="preview-area">
              <div
                className="video-canvas"
                style={{
                  aspectRatio: `${view?.settings?.width || 16}/${view?.settings?.height || 9}`,
                }}
              >
                {!video && frame && (
                  <Stage
                    data={stageData}
                    disabled={busy || playing}
                    onError={setError}
                  />
                )}
                {video ? (
                  <video
                    ref={vid}
                    src={video}
                    controls
                    onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
                  />
                ) : frame ? (
                  <img src={frame} />
                ) : (
                  <Empty description="Open a project to preview" />
                )}
              </div>
            </div>
            <div className="transport">
              <Button
                type="text"
                className="play"
                icon={playing ? <PauseCircleFilled /> : <PlayCircleFilled />}
                disabled={!p || busy || job?.state === "running"}
                onClick={playPause}
              />
              <span>{clock(time)}</span>
              <Slider
                min={0}
                max={Math.max(0.01, end)}
                step={
                  view ? view.settings.rateDen / view.settings.rateNum : 0.01
                }
                value={time}
                onChange={seekTime}
                tooltip={{ formatter: clock }}
              />
              <span>{clock(end)}</span>
              <Button
                size="small"
                disabled={!p || busy}
                onClick={() => render(true)}
              >
                Build playback preview
              </Button>
            </div>
            <section className="presets">
              <Tabs
                activeKey={category}
                onChange={setCategory}
                items={Object.entries(NAMES).map(([key, label]) => ({
                  key,
                  label,
                }))}
              />
              {category === "titles" ? (
                <div className="row">
                  <Input
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    style={{ flex: 1 }}
                  />
                  <Button
                    type="primary"
                    disabled={!p || busy}
                    onClick={() =>
                      edit({
                        op: "addTextClip",
                        trackId: null,
                        above: true,
                        start: time,
                        duration: 4,
                        style: { content: title },
                        offsetY: 0,
                      })
                    }
                  >
                    Add title
                  </Button>
                  <Button onClick={() => setServices("Captions")}>
                    Caption studio
                  </Button>
                </div>
              ) : category === "keys" ? (
                <KeyframeEditor
                  clip={clip}
                  time={time}
                  onEdit={edit}
                  onSeek={seekTime}
                  busy={busy}
                />
              ) : (
                <>
                  <div className="preset-grid">
                    <button
                      className={
                        "preset " + (preset === "none" ? "active" : "")
                      }
                      onClick={() => setPreset("none")}
                    >
                      <div className="tile-art">∅</div>
                      <span>None / Clear</span>
                    </button>
                    {cards.map((c) => (
                      <button
                        key={c.id}
                        className={
                          "preset " + (preset === c.id ? "active" : "")
                        }
                        onClick={() => {
                          setPreset(c.id);
                          if (
                            ["basic", "physics", "canvas"].includes(c.category)
                          )
                            call("load_preset_controls", c.id)
                              .then(setControls)
                              .catch((e) => setError(e.message));
                          setParams(
                            Object.fromEntries(
                              (c.params || []).map((p) => [p.key, p.default]),
                            ),
                          );
                        }}
                      >
                        {c.card ? (
                          <img src={"./preset-art/" + c.card} />
                        ) : (
                          <div className="tile-art">
                            {category === "transition"
                              ? "◧"
                              : category === "effect"
                                ? "✦"
                                : category === "adjustment"
                                  ? "▣"
                                  : "↗"}
                          </div>
                        )}
                        <span>
                          {c.name || words(c.id)}
                          {favorites.includes(c.id) && <StarFilled />}
                        </span>
                      </button>
                    ))}
                  </div>
                  <div className="preset-actions">
                    <Button
                      type="primary"
                      disabled={!selected.length || busy}
                      onClick={() => apply()}
                    >
                      Apply to selected
                    </Button>
                    <Button disabled={!p || busy} onClick={() => apply(true)}>
                      Apply all
                    </Button>
                    <Button
                      icon={
                        favorites.includes(preset) ? (
                          <StarFilled />
                        ) : (
                          <StarOutlined />
                        )
                      }
                      disabled={preset === "none" || busy}
                      onClick={toggleFavorite}
                    >
                      Favorite
                    </Button>
                    <Button
                      disabled={!selected.length || busy}
                      onClick={randomFavorite}
                    >
                      Random favorite
                    </Button>
                    <Button
                      onClick={() =>
                        pref({
                          defaults: {
                            ...prefs.defaults,
                            [category]: { preset, controls, params },
                          },
                        })
                      }
                    >
                      Save default
                    </Button>
                    <Button
                      onClick={() => {
                        setPreset("none");
                        setControls(boot.motion.defaults[category] || {});
                        pref({
                          defaults: { ...prefs.defaults, [category]: null },
                        });
                      }}
                    >
                      Reset default
                    </Button>
                  </div>
                </>
              )}
            </section>
          </main>
          <div
            className="panel-divider"
            role="separator"
            tabIndex={0}
            aria-label="Resize inspector"
            onPointerDown={(e) => resizePanel("right", e)}
          />
          <aside className="inspector">
            <div className="panel-head">
              Inspector <span>{clip?.name || "No clip selected"}</span>
            </div>
            <div className="inspector-content">
              {clip ? (
                <>
                  <div className="section-label">CLIP</div>
                  {[
                    ["start", 0, 1e6, 0.1],
                    ["duration", 1 / 60, 1e6, 0.1],
                    ["scale", 0.05, 8, 0.05],
                    ["offsetX", -3, 3, 0.01],
                    ["offsetY", -3, 3, 0.01],
                    ["rotation", -360, 360, 1],
                    ["opacity", 0, 1, 0.05],
                    ["volume", 0, 8, 0.05],
                    ["speed", 0.0625, 16, 0.1],
                    ["fadeIn", 0, 60, 0.1],
                    ["fadeOut", 0, 60, 0.1],
                  ].map(([key, min, max, step]) => (
                    <div className="row spaced" key={clip.id + key}>
                      <label>{words(key)}</label>
                      <InputNumber
                        defaultValue={clip[key] ?? (key === "opacity" ? 1 : 0)}
                        min={min}
                        max={max}
                        step={step}
                        disabled={busy}
                        onBlur={(e) => {
                          const n = Number(e.target.value);
                          if (Number.isFinite(n) && n !== clip[key])
                            changeClip(key, n);
                        }}
                        onPressEnter={(e) => e.currentTarget.blur()}
                      />
                    </div>
                  ))}
                  <div className="row">
                    <Button
                      size="small"
                      disabled={busy}
                      onClick={() =>
                        action("run_tool", "clip_frame", {
                          path: state.path,
                          clipId: clip.id,
                          framing: "fit",
                          at: 0,
                        }).then((r) => r && adopt(r.state))
                      }
                    >
                      Fit
                    </Button>
                    <Button
                      size="small"
                      disabled={busy}
                      onClick={() =>
                        action("run_tool", "clip_frame", {
                          path: state.path,
                          clipId: clip.id,
                          framing: "fill",
                          at: 0,
                        }).then((r) => r && adopt(r.state))
                      }
                    >
                      Fill
                    </Button>
                    <Button
                      size="small"
                      disabled={busy}
                      onClick={() =>
                        edit({
                          op: "updateClip",
                          clipId: clip.id,
                          patch: { flipH: !clip.flipH },
                        })
                      }
                    >
                      Flip
                    </Button>
                  </div>
                  <div className="section-label">CUTOUT BRUSH</div>
                  <div className="row">
                    <Checkbox
                      checked={!!stageData.painting}
                      onChange={(e) =>
                        action("native_invoke", "Editor", "cutout-painting", [
                          e.target.checked,
                        ]).then((r) => r && adopt(r.state))
                      }
                    >
                      Paint on preview
                    </Checkbox>
                    <Select
                      value={stageData["brush-tool"] || 0}
                      options={[
                        "Smart restore",
                        "Restore",
                        "Smart erase",
                        "Erase",
                      ].map((label, value) => ({ label, value }))}
                      onChange={(value) =>
                        action("native_invoke", "Editor", "cutout-tool", [
                          value,
                        ])
                      }
                    />
                  </div>
                  <InputNumber
                    min={0.005}
                    max={0.5}
                    step={0.005}
                    value={stageData["brush-size"] || 0.02}
                    onChange={(value) =>
                      action("native_invoke", "Editor", "cutout-size", [value])
                    }
                  />
                  <Button
                    size="small"
                    onClick={() =>
                      action("native_invoke", "Editor", "cutout-clear", [])
                    }
                  >
                    Clear strokes
                  </Button>
                  <div className="section-label">CROP</div>
                  {["left", "top", "right", "bottom"].map((edge) => (
                    <div className="row spaced" key={clip.id + "crop" + edge}>
                      <label>{words(edge)}</label>
                      <InputNumber
                        min={0}
                        max={0.95}
                        step={0.01}
                        defaultValue={clip.crop?.[edge] || 0}
                        onBlur={(e) =>
                          edit({
                            op: "updateClip",
                            clipId: clip.id,
                            patch: {
                              crop: {
                                left: 0,
                                top: 0,
                                right: 0,
                                bottom: 0,
                                ...clip.crop,
                                [edge]: Number(e.target.value),
                              },
                            },
                          })
                        }
                      />
                    </div>
                  ))}
                  <Button
                    size="small"
                    onClick={() =>
                      edit({
                        op: "updateClip",
                        clipId: clip.id,
                        patch: { crop: null },
                      })
                    }
                  >
                    Clear crop
                  </Button>
                  <div className="row">
                    <Button
                      icon={<ScissorOutlined />}
                      disabled={busy}
                      onClick={() =>
                        edit({ op: "splitClips", clipIds: selected, time })
                      }
                    >
                      Split
                    </Button>
                    <Button
                      icon={<DeleteOutlined />}
                      disabled={busy}
                      onClick={() =>
                        edit({
                          op: "removeClips",
                          clipIds: selected,
                          ripple: false,
                        })
                      }
                    >
                      Remove
                    </Button>
                  </div>
                </>
              ) : (
                <Empty description="Select a timeline clip" />
              )}
              <div className="section-label">
                {NAMES[category].toUpperCase()} PARAMETERS
              </div>
              <strong>{chosen?.name || words(preset)}</strong>
              {Object.entries(controls)
                .filter(([k, v]) =>
                  ["number", "boolean", "string"].includes(typeof v),
                )
                .map(([key, value]) => (
                  <div key={key}>
                    {typeof value === "boolean" ? (
                      <Checkbox
                        checked={value}
                        onChange={(e) =>
                          setControls({ ...controls, [key]: e.target.checked })
                        }
                      >
                        {words(key)}
                      </Checkbox>
                    ) : (
                      <div className="row spaced">
                        <label>
                          {words(key)}
                          {key === "duration" ? " · fraction of clip" : ""}
                        </label>
                        {typeof value === "number" ? (
                          <InputNumber
                            value={value}
                            step={0.05}
                            onChange={(v) =>
                              setControls({ ...controls, [key]: v })
                            }
                          />
                        ) : (
                          <Select
                            value={value}
                            options={["Linear", "Smooth", "Cinematic"].map(
                              (value) => ({ value }),
                            )}
                            onChange={(v) =>
                              setControls({ ...controls, [key]: v })
                            }
                          />
                        )}
                      </div>
                    )}
                  </div>
                ))}
              {chosen?.params?.map((param) => (
                <div key={param.key}>
                  <label>
                    {param.label} {param.unit}
                  </label>
                  {param.type === "enum" ? (
                    <Select
                      value={params[param.key] ?? param.default}
                      options={param.values.map((value, i) => ({
                        value,
                        label: param.labels[i] || String(value),
                      }))}
                      onChange={(v) => setParams({ ...params, [param.key]: v })}
                    />
                  ) : (
                    <Slider
                      value={params[param.key] ?? param.default}
                      min={param.min}
                      max={param.max}
                      step={param.step || 0.01}
                      onChange={(v) => setParams({ ...params, [param.key]: v })}
                    />
                  )}
                </div>
              ))}
              {category === "transition" && (
                <>
                  <label>Duration (seconds)</label>
                  <InputNumber
                    min={0.05}
                    max={30}
                    step={0.05}
                    value={duration}
                    onChange={setDuration}
                  />
                </>
              )}
              <p className="hint">
                Presets and parameters are evaluated by Concat's existing native
                engine. Apply commits one undoable operation.
              </p>
            </div>
          </aside>
        </div>
        <section className="timeline">
          <div className="timeline-head">
            <strong>Timeline</strong>
            <span>
              {clock(time)} / {clock(end)}
            </span>
            <div className="row">
              <span>Zoom</span>
              <Slider
                min={20}
                max={250}
                value={scale}
                onChange={setScale}
                style={{ width: 130 }}
              />
            </div>
          </div>
          <div className="tracks-scroll">
            <div
              className="tracks"
              style={{ width: Math.max(800, end * scale + 100) }}
            >
              <div
                className="ruler"
                style={{ paddingLeft: 90 }}
                onClick={(e) =>
                  seekTime(
                    Math.max(
                      0,
                      (e.clientX -
                        e.currentTarget.getBoundingClientRect().left -
                        90) /
                        scale,
                    ),
                  )
                }
              >
                {Array.from({ length: Math.ceil(end / 5) + 1 }, (_, i) => (
                  <span
                    key={i}
                    style={{ position: "absolute", left: 90 + i * 5 * scale }}
                  >
                    {clock(i * 5)}
                  </span>
                ))}
              </div>
              {timeline?.tracks.map((track, i) => (
                <div className="track-row" key={track.id}>
                  <div className="track-label">
                    Track {i + 1}
                    <Button
                      size="small"
                      type="text"
                      aria-label="Toggle track visibility"
                      icon={
                        track.visible ? (
                          <EyeOutlined />
                        ) : (
                          <EyeInvisibleOutlined />
                        )
                      }
                      onClick={() =>
                        edit({
                          op: "setTrackFlag",
                          trackId: track.id,
                          flag: "visible",
                          value: !track.visible,
                        })
                      }
                    />
                    <Button
                      size="small"
                      type="text"
                      aria-label="Toggle track audio"
                      icon={<SoundOutlined />}
                      danger={track.muted}
                      onClick={() =>
                        edit({
                          op: "setTrackFlag",
                          trackId: track.id,
                          flag: "muted",
                          value: !track.muted,
                        })
                      }
                    />
                    <Button
                      size="small"
                      type="text"
                      icon={
                        prefs.lockedTracks.includes(track.id) ? (
                          <LockOutlined />
                        ) : (
                          <UnlockOutlined />
                        )
                      }
                      onClick={() =>
                        pref({
                          lockedTracks: prefs.lockedTracks.includes(track.id)
                            ? prefs.lockedTracks.filter((id) => id !== track.id)
                            : [...prefs.lockedTracks, track.id],
                        })
                      }
                    />
                  </div>
                  <div
                    className="track-content"
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => drop(e, track.id)}
                  >
                    {clips
                      .filter((c) => c.trackId === track.id)
                      .map((c) => (
                        <button
                          key={c.id}
                          draggable={!prefs.lockedTracks.includes(track.id)}
                          onDragStart={(e) =>
                            e.dataTransfer.setData(
                              "application/concat",
                              JSON.stringify({ clip: c.id }),
                            )
                          }
                          className={
                            "timeline-clip " +
                            (selected.includes(c.id) ? "selected" : "") +
                            " " +
                            c.kind
                          }
                          style={{
                            left: c.start * scale,
                            width: Math.max(5, c.duration * scale),
                          }}
                          onClick={(e) => select(c, e)}
                        >
                          {thumbs[c.id] && <img src={thumbs[c.id]} />}{" "}
                          {waves[c.id] && (
                            <svg
                              className="clip-wave"
                              viewBox="0 0 1 1"
                              preserveAspectRatio="none"
                              style={{
                                left: (waves[c.id].from || 0) * 100 + "%",
                                width: (waves[c.id].span || 1) * 100 + "%",
                                transform: `scaleY(${waves[c.id].volume || 0})`,
                              }}
                            >
                              <path d={waves[c.id].path} />
                            </svg>
                          )}
                          <span>
                            {c.name || c.id}
                            <small>{c.duration.toFixed(2)}s</small>
                          </span>
                        </button>
                      ))}
                  </div>
                </div>
              ))}
              <div className="playhead" style={{ left: 90 + time * scale }} />
            </div>
          </div>
        </section>
        <footer>
          <span className="status-dot" />
          <span>
            {busy
              ? "Working…"
              : job?.state === "running"
                ? `Exporting · ${job.stage || "preparing"}`
                : "Ready"}
          </span>
          {job?.state === "running" && (
            <>
              <Progress
                percent={Math.round(100 * (job.fraction || 0))}
                size="small"
                style={{ width: 220 }}
              />
              <Button
                size="small"
                danger
                onClick={() => action("cancel_export", job.job)}
              >
                Cancel
              </Button>
            </>
          )}
          <span className="footer-right">
            {state?.dirty ? "Unsaved edits" : "Project saved"} · Native
            preview/export calculations
          </span>
        </footer>
        <Services
          open={!!services}
          initial={services}
          onClose={() => setServices("")}
          onState={adopt}
          selection={selected}
        />
        <Modal
          title="All native engine commands"
          open={expert}
          onCancel={() => setExpert(false)}
          width={850}
          okText="Run command"
          onOk={() =>
            run(async () => {
              const r = await call("run_tool", tool, {
                path: state.path,
                ...JSON.parse(argumentsText),
              });
              adopt(r.state);
              message.success("Engine command completed");
            })
          }
        >
          <Alert
            type="info"
            message="Expert tools use the engine's live schema. Normal editing controls are in the workspace; Service controls are available in the Concat tools panel."
          />
          <Select
            showSearch
            value={tool}
            onChange={setTool}
            options={boot.tools.map((t) => ({ value: t.name, label: t.title }))}
            style={{ width: "100%", margin: "14px 0" }}
          />
          <p>{boot.tools.find((t) => t.name === tool)?.description}</p>
          <Input.TextArea
            rows={12}
            value={argumentsText}
            onChange={(e) => setArgumentsText(e.target.value)}
          />
        </Modal>
      </div>
    </ConfigProvider>
  );
}
createRoot(document.getElementById("root")).render(<App />);
