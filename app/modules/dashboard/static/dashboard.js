(() => {
  "use strict";
  const button = document.getElementById("dashboard-refresh");
  const message = document.getElementById("dashboard-message");
  if (!button || !message) return;

  const byId = id => document.getElementById(id);
  const setText = (id, value) => {
    const node = byId(id);
    if (node) node.textContent = String(value);
  };
  const stat = (key, value) => {
    const node = document.querySelector('[data-stat="' + key + '"]');
    if (node) node.textContent = String(value);
  };
  const stamp = value => {
    if (!value) return "Time unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString();
  };

  function renderRows(id, rows, emptyText) {
    const container = byId(id);
    if (!container) return;
    container.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      const empty = document.createElement("li");
      empty.className = "dash-empty";
      empty.textContent = emptyText;
      container.append(empty);
      return;
    }
    for (const item of rows) {
      const row = document.createElement("li");
      const main = document.createElement("span");
      main.className = "dash-job-main";
      const title = document.createElement("strong");
      title.className = "dash-job-title";
      title.textContent = item.title;
      const when = document.createElement("small");
      when.className = "dash-job-date";
      when.textContent = stamp(item.updated_at);
      main.append(title, when);
      const state = document.createElement("span");
      state.className = "dash-state";
      state.textContent = item.status;
      state.dataset.state = item.status;
      row.append(main, state);
      container.append(row);
    }
  }

  function display(data) {
    for (const area of ["renders", "uploads"]) {
      for (const key of ["total", "active", "completed"]) {
        stat(area + "." + key, data[area][key].toLocaleString());
      }
      for (const key of ["queued", "active"]) {
        stat(area + ".queue." + key, data[area].queue[key].toLocaleString());
      }
    }
    stat("videos.total", data.videos.total.toLocaleString());
    setText("render-queue-state", data.renders.queue.state);
    setText("upload-queue-state", data.uploads.queue.state);
    setText("gpu-availability",
      data.gpu.available === true ? "Online" : data.gpu.available === false ? "Offline" : "Unknown");
    setText("gpu-status", data.gpu.status +
      (data.gpu.checked_at ? " · " + stamp(data.gpu.checked_at) : ""));
    renderRows("render-jobs", data.renders.jobs, "No render jobs yet");
    renderRows("upload-jobs", data.uploads.jobs, "No upload jobs yet");
    renderRows("video-jobs", data.videos.items, "No verified videos yet");
    renderRows("dashboard-activity", data.recent_activity.map(item => ({
      ...item, title: item.kind + " · " + item.title
    })), "No recent activity");
  }

  async function refresh() {
    button.disabled = true;
    message.dataset.error = "false";
    message.textContent = "Refreshing owner-scoped activity…";
    try {
      const response = await fetch("/api/v1/dashboard/overview?limit=8", {
        credentials: "same-origin", cache: "no-store", headers: {"Accept": "application/json"}
      });
      if (!response.ok) throw new Error("Dashboard sources are not ready (" + response.status + ")");
      display(await response.json());
      message.textContent = "Live data · updated " + new Date().toLocaleTimeString();
    } catch (_error) {
      message.dataset.error = "true";
      message.textContent =
        "Live dashboard data is unavailable. No estimated or shared-user counts are shown. Try Refresh data.";
    } finally {
      button.disabled = false;
    }
  }

  button.addEventListener("click", refresh);
  refresh();
})();
