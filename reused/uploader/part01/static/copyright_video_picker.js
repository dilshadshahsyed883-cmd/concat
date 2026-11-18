/* Copyright Uploader owner-scoped VPS folder -> video -> group -> channel picker. */
(() => {
  "use strict";
  function install() {
    const mode = document.getElementById("copyright-source-mode");
    if (!mode) return;
    const folder = document.getElementById("copyright-vps-folder");
    const video = document.getElementById("copyright-folder-file");
    const status = document.getElementById("copyright-folder-summary");
    const refresh = document.getElementById("copyright-folder-refresh");
    const group = document.getElementById("copyright-channel-group");
    const channel = document.getElementById("copyright-channel-choice");
    const allChannels = channel ? Array.from(channel.options).filter(option => option.value) : [];
    const sections = {
      drive_link: document.getElementById("copyright-drive-source"),
      pc_upload: document.getElementById("copyright-pc-source"),
      folder: document.getElementById("copyright-folder-source")
    };
    const sourceInput = {
      drive_link: document.querySelector('#copyright-drive-source input[name="drive_url"]'),
      pc_upload: document.querySelector('#copyright-pc-source input[name="video_file"]')
    };
    const fromQuery = new URLSearchParams(location.search).get("source_mode");
    if (["folder", "pc_upload", "drive_link"].includes(fromQuery)) mode.value = fromQuery;

    function selectChannels() {
      if (!group || !channel) return;
      const selectedGroup = group.value.trim().toLocaleLowerCase();
      const previous = channel.value;
      channel.replaceChildren(new Option(selectedGroup ? "Select channel from this group" : "Select a group first", ""));
      if (selectedGroup) {
        for (const option of allChannels) {
          const matches = String(option.dataset.groups || "")
            .split(/[,;|]/).map(value => value.trim().toLocaleLowerCase());
          if (matches.includes(selectedGroup)) channel.add(option.cloneNode(true));
        }
      }
      channel.disabled = !selectedGroup || channel.options.length === 1;
      if (Array.from(channel.options).some(option => option.value === previous)) channel.value = previous;
    }
    if (group) group.addEventListener("change", selectChannels);
    selectChannels();

    function show() {
      for (const [key, section] of Object.entries(sections)) {
        if (!section) continue;
        const visible = mode.value === key;
        section.hidden = !visible;
        section.style.setProperty("display", visible ? "block" : "none", "important");
        const input = sourceInput[key];
        if (input) { input.required = visible; input.disabled = !visible; }
      }
      if (folder) { folder.required = false; folder.disabled = mode.value !== "folder"; }
      if (video) { video.required = mode.value === "folder"; video.disabled = mode.value !== "folder"; }
    }

    let sequence = 0;
    async function updateFolders() {
      if (mode.value !== "folder" || !folder || !video) return;
      const ticket = ++sequence;
      const selectedFolder = folder.value;
      const previous = video.value;
      video.disabled = true;
      video.replaceChildren(new Option("Loading available videos…", ""));
      if (status) status.textContent = "Refreshing your private VPS video list…";
      try {
        const response = await fetch(
          "/copyright-uploader/api/copyright/folder-videos?folder=" + encodeURIComponent(selectedFolder),
          {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}}
        );
        if (!response.ok) throw new Error("VPS folders unavailable (" + response.status + ")");
        const data = await response.json();
        if (ticket !== sequence || mode.value !== "folder" || folder.value !== selectedFolder) return;
        const names = Array.isArray(data.folders) ? data.folders : [""];
        folder.replaceChildren(...names.map(name => new Option(name || "My VPS Root", name)));
        if (names.includes(selectedFolder)) folder.value = selectedFolder;
        const items = Array.isArray(data.files) ? data.files : [];
        video.replaceChildren(new Option("Select video from selected folder", ""));
        for (const name of items) video.add(new Option(name, name));
        if (items.includes(previous)) video.value = previous;
        video.disabled = false;
        if (status) status.textContent = items.length
          ? items.length + " available videos · Already queued videos are hidden"
          : "No available videos in this folder. Select another folder or refresh.";
      } catch (error) {
        if (ticket !== sequence) return;
        video.replaceChildren(new Option("Unable to load videos", ""));
        if (status) status.textContent = error.message + ". Retry with Refresh Videos.";
      }
    }
    mode.addEventListener("change", () => { show(); if (mode.value === "folder") updateFolders(); });
    if (folder) folder.addEventListener("change", () => { video.value = ""; updateFolders(); });
    if (refresh) refresh.addEventListener("click", updateFolders);
    window.addEventListener("pageshow", () => { show(); if (mode.value === "folder") updateFolders(); });
    show();
    if (mode.value === "folder") updateFolders();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, {once:true});
  else install();
})();
