/* VPS1_NATIVE_SITE_SHELL_V1. No iframe, cross-origin fetch, or duplicated app logic. */
(() => {
  "use strict";
  const script = document.currentScript || document.querySelector('script[data-unify-sidebar="1"]');
  const app = script && script.dataset ? script.dataset.workspace : "";
  const legacySidebar = !!(script && script.dataset && script.dataset.unifySidebar === "1");
  // Preserve previously shared Voice Studio audio links via the canonical Song Studio page.
  if (app === "voice" && /^\/voice-studio\/app\/?$/.test(window.location?.pathname || "") && window.location?.hash === "#vs-audio" && typeof window.location.replace === "function") {
    window.location.replace("/audio-downloader/");
    return;
  }
  if (!legacySidebar && !["acs", "voice", "uploader", "stream", "library", "song", "song-uploader", "short-uploader", "copyright-uploader", "concat-uploader"].includes(app)) return;
  // During staggered releases an old Scene shell may still embed this document.
  // Never render a second navigation in that legacy framing context.
  if (window.self !== window.top) return;
  if (!legacySidebar) document.documentElement.classList.add("vps1-native-bootstrap");
  const node = (tag, name, value) => {
    const e = document.createElement(tag);
    if (name) e.className = name;
    if (value !== undefined) e.textContent = value;
    return e;
  };
  const addLink = (nav, label, href, active) => {
    const a = node("a", active ? "active" : "", label);
    a.href = href;
    if (active) a.setAttribute("aria-current", "page");
    nav.appendChild(a);
    return a;
  };
  // Canonical navigation shared by independently served native and legacy pages.
  // Never expose Admin from static markup or a client-stored role.
  const PRIMARY_NAV = Object.freeze([
    ["Dashboard", "/", "dashboard"],
    ["Song Studio", "/song-studio/", "song"],
    ["Short Studio", "/short-maker", "short"],
    ["Copyright Studio", "/video-merger/", "copyright"],
    ["Voice Studio", "/voice-studio/app#vps1-production", "voice"],
    ["Concat Studio", "/concat-studio/", "concat"],
    ["Experiment Studio", "/experiment-studio/", "experiment"]
  ]);
  // A verified Scene identity decides permissions. Never trust local storage
  // or an app-provided is_admin flag. Fail closed while identity is pending.
  const applyStudioVisibility = (links, identity) => {
    const authenticated = !!(identity && identity.authenticated === true);
    const admin = authenticated && identity.is_admin === true;
    const granted = authenticated && Array.isArray(identity.allowed_studios)
      ? identity.allowed_studios : [];
    for (const [key, link] of Object.entries(links)) {
      if (!link) continue;
      const visible = key === "dashboard" || (key === "concat" ? authenticated :
        key === "experiment" ? admin : (admin || granted.includes(key)));
      link.hidden = !visible;
      if (visible) link.removeAttribute("hidden");
      else link.setAttribute("hidden", "");
    }
  };
  const activeKeyForPath = (pathname) => {
    if (pathname.startsWith("/local-upload-manager/modes/song/")) return "song";
    if (/^\/(short-maker|short-uploader|short-studio)(\/|$)/.test(pathname)) return "short";
    if (/^\/(video-merger|copyright-uploader|copyright-studio)(\/|$)/.test(pathname)) return "copyright";
    if (/^\/(local-upload-manager|auto-stream)(\/|$)/.test(pathname)) return "experiment";
    if (/^\/(scene-builder|video-studio|auto-caption-studio|queue|settings|audit-media)(\/|$)/.test(pathname)) return "experiment";
    if (/^\/audio-downloader\/?$/.test(pathname)) return "song";
    if (/^\/(song-studio|song-maker|song-uploader)(\/|$)/.test(pathname)) return "song";
    if (/^\/voice-studio(\/|$)/.test(pathname)) return window.location.hash === "#vs-audio" ? "song" : "voice";
    if (/^\/(experiment-studio|self-media-library|auto-script)(\/|$)/.test(pathname)) return "experiment";
    if (/^\/(concat-studio|cloud-render)(\/|$)/.test(pathname)) return "concat";
    if (/^\/(heygen-studio|voice-clone-studio|imagen-studio)(\/|$)/.test(pathname)) return "experiment";
    if (/^\/(admin|storage-manager|user-management)(\/|$)/.test(pathname)) return "admin";
    if (pathname === "/" || pathname === "/dashboard") return "dashboard";
    return "";
  };
  const addPrimaryLinks = (navigation, current) => {
    const links = {};
    for (const [label, href, key] of PRIMARY_NAV) {
      links[key] = addLink(navigation, label, href, current === key);
    }
    return links;
  };
  // Video and Upload remain Experiment Studio children; Voice is a primary Studio.
  // Their existing native app controls remain a third-level rail, not a new page.
  const EXPERIMENT_NAV = Object.freeze([
    ["Antidetect Browser", "/experiment-studio/antidetect-browser/", "antidetect"],
    ["Upload Studio", "/local-upload-manager/app?embedded=1&tab=dashboard", "upload"],
    ["HeyGen Studio", "/heygen-studio/", "heygen"],
    ["Voice Clone", "/voice-clone-studio/", "voice-clone"],
    ["Imagen", "/imagen-studio/", "imagen"]
  ]);
  const experimentSection = pathname => {
    if (/^\/heygen-studio(\/|$)/.test(pathname)) return "heygen";
    if (/^\/voice-clone-studio(\/|$)/.test(pathname)) return "voice-clone";
    if (/^\/imagen-studio(\/|$)/.test(pathname)) return "imagen";
    if (/^\/(scene-builder|video-studio|auto-caption-studio|short-maker|queue|settings|audit-media)(\/|$)/.test(pathname)) return "video";
    if (/^\/(local-upload-manager|auto-stream)(\/|$)/.test(pathname)) return "upload";
    if (/^\/voice-studio(\/|$)/.test(pathname)) return "voice";
    if (/^\/self-media-library(\/|$)/.test(pathname)) return "library";
    if (/^\/auto-script(\/|$)/.test(pathname)) return "auto-script";
    return "antidetect";
  };
  const createExperimentTabs = (isLegacy, selected) => {
    const tabs = node("nav", isLegacy ? "vps1-experiment-tabs" : "vps1-native-experiment-tabs");
    tabs.dataset.vps1Component = "studio-tabs";
    tabs.setAttribute("aria-label", "Experiment Studio");
    const links = {};
    for (const [label, href, key] of EXPERIMENT_NAV)
      links[key] = addLink(tabs, label, href, selected === key);
    return {tabs, links};
  };
  // Reuse Cloud Render; Agent 3 mounts the uploader at the reserved URL.
  const CONCAT_NAV = Object.freeze([
    ["Concat", "/concat-studio/concat/", "concat-render"],
    ["Concat Uploader", "/concat-studio/concat-uploader/", "concat-uploader"]
  ]);
  const concatSection = pathname =>
    /^\/concat-studio\/concat-uploader(\/|$)/.test(pathname) ? "concat-uploader" : "concat-render";
  const createConcatTabs = (isLegacy, selected) => {
    const tabs = node("nav", isLegacy ? "vps1-experiment-tabs vps1-concat-studio-tabs" :
      "vps1-native-experiment-tabs vps1-native-concat-studio-tabs");
    tabs.dataset.vps1Component = "studio-tabs";
    tabs.setAttribute("aria-label", "Concat Studio");
    for (const [label, href, key] of CONCAT_NAV)
      addLink(tabs, label, href, selected === key);
    return tabs;
  };
  function alignVideoTabs() {
    const rail = document.querySelector?.('nav[aria-label="Video Studio"], nav[aria-label="Short Studio"]');
    if (!rail || rail.dataset.videoAligned) return;
    rail.dataset.videoAligned = "1";
    const current = location.pathname.replace(/\/$/, "");
    for (const link of rail.querySelectorAll("a")) {
      const destination = (link.getAttribute("href") || "").split(/[?#]/)[0].replace(/\/$/, "");
      const selected = destination === current || (destination === "/scene-builder" && ["/queue", "/settings", "/audit-media"].includes(current));
      if (!selected) continue;
      link.setAttribute("aria-current", "page");
      link.addEventListener("click", event => {
        if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0)
          event.preventDefault(); // Keep the current form and scroll position.
      });
      requestAnimationFrame(() => {
        const item = link.getBoundingClientRect(), bounds = rail.getBoundingClientRect();
        if (item.right > bounds.right || item.left < bounds.left)
          rail.scrollLeft += item.left - bounds.left - 12;
      });
    }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", alignVideoTabs, {once: true});
  else alignVideoTabs();
  function installLegacySidebar() {
    const sidebar = document.getElementById("app-sidebar");
    if (!sidebar || sidebar.dataset.vps1UnifiedSidebar) return;
    sidebar.dataset.vps1UnifiedSidebar = "1";
    sidebar.setAttribute("aria-label", "Scene Builder navigation");
    let navigation = sidebar.querySelector(":scope > nav");
    if (!navigation) {
      navigation = node("nav");
      sidebar.appendChild(navigation);
    }
    navigation.setAttribute("aria-label", "Applications");
    const current = activeKeyForPath(window.location.pathname);
    const existing = Array.from(navigation.querySelectorAll(":scope > a"));
    const isCanonical = existing.length >= PRIMARY_NAV.length &&
      PRIMARY_NAV.every(([label, href], i) =>
        existing[i].textContent.trim() === label &&
        existing[i].getAttribute("href") === href);
    let primaryLinks;
    if (isCanonical) {
      primaryLinks = Object.fromEntries(PRIMARY_NAV.map(([, , key], i) => [key, existing[i]]));
      // Keep the original nodes and their positions across routes: no
      // unnecessary sidebar repaint or jump while pages switch.
      for (const extra of existing.slice(PRIMARY_NAV.length)) {
        if(extra.getAttribute("href")!=="/admin") extra.remove();
      }
      existing.slice(0, PRIMARY_NAV.length).forEach((link, i) => {
        const selected = PRIMARY_NAV[i][2] === current;
        link.classList.toggle("active", selected);
        if (selected) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      });
    } else {
      navigation.replaceChildren();
      primaryLinks = addPrimaryLinks(navigation, current);
    }
    applyStudioVisibility(primaryLinks, null);
    // Experiment navigation remains admin-only even on independently served pages.
    // Hide while session identity is pending; a trusted live Scene response enables it.
    if (current === "experiment" && !document.querySelector('nav[aria-label="Experiment Studio"]')) {
      const main = document.querySelector("main");
      if (main) main.prepend(createExperimentTabs(true, experimentSection(window.location.pathname)).tabs);
    }
    const experimentTabs = document.querySelector('nav[aria-label="Experiment Studio"]');
    if (experimentTabs) experimentTabs.hidden = true;
    if (current === "concat" && !document.querySelector('nav[aria-label="Concat Studio"]')) {
      const main = document.querySelector("main");
      if (main) main.prepend(createConcatTabs(true, concatSection(window.location.pathname)));
    }
    const concatTabs = document.querySelector('nav[aria-label="Concat Studio"]');
    if (concatTabs) concatTabs.hidden = true;
    let account = sidebar.querySelector(":scope > .sidebar-account");
    if (!account) {
      account = node("div", "sidebar-account");
      const bottom = sidebar.querySelector(":scope > .server, :scope > .server-dot");
      sidebar.insertBefore(account, bottom);
    }
    const serverIdentity = account.dataset.vps1ServerIdentity === "1";
    const serverName = serverIdentity ? account.querySelector(".sidebar-account-name")?.textContent : "";
    account.hidden = false;
    account.replaceChildren();
    const accountName = node("div", "sidebar-account-name", serverName || "Checking session…");
    const loginLink = node("a", "vps1-session-login", "Sign in");
    loginLink.href = "/login";
    loginLink.hidden = true;
    const logout = node("form");
    logout.hidden = !serverIdentity;
    logout.action = "/logout";
    logout.method = "post";
    const logoutButton = node("button", "", "Log out");
    logoutButton.type = "submit";
    logout.appendChild(logoutButton);
    account.append(accountName, loginLink, logout);
    let adminLink = navigation.querySelector('a[href="/admin"]');
    const setIdentity = (identity) => {
      applyStudioVisibility(primaryLinks, identity);
      const authenticated = !!(identity && identity.authenticated === true);
      const isAdmin = authenticated && identity.is_admin === true;
      if (experimentTabs) experimentTabs.hidden = !isAdmin;
      if (concatTabs) concatTabs.hidden = !authenticated;
      if (isAdmin && !adminLink) {
        adminLink = addLink(navigation, "Admin", "/admin",
          /^\/admin(\/|$)/.test(window.location.pathname));
      } else if (!isAdmin && adminLink) {
        adminLink.remove();
        adminLink = null;
      }
      // Never silently remove the entire account area when a login expires,
      // a proxy returns login HTML, or the identity request fails. Only the
      // server-verified is_admin=true response may create the Admin link.
      account.hidden = false;
      account.dataset.vps1IdentityStatus = authenticated ? "verified" : "unavailable";
      accountName.textContent = authenticated ?
        String(identity.username || "Signed in") : "Account verification needed";
      loginLink.hidden = authenticated;
      logout.hidden = !authenticated;
    };
    const refreshIdentity = async () => {
      try {
        const response = await fetch("/api/session/identity", {
          credentials: "same-origin", cache: "no-store",
          headers: {"Accept": "application/json"}
        });
        if (!response.ok) throw new Error("Scene session unavailable");
        setIdentity(await response.json());
      } catch (_) {
        setIdentity(null);
      }
    };
    refreshIdentity();
    window.addEventListener("pageshow", refreshIdentity);
    window.addEventListener("vps1:session-refresh", refreshIdentity);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshIdentity();
    });
  }
  if (script && script.dataset && script.dataset.unifySidebar === "1") {
    if (window.self !== window.top) return;
    if (document.readyState === "loading")
      document.addEventListener("DOMContentLoaded", installLegacySidebar, {once: true});
    else installLegacySidebar();
    return;
  }
  function install() {
    if (document.getElementById("vps1-native-site-sidebar")) return;
    document.body.classList.add("vps1-native-site", "vps1-native-" + app);
    const aside = node("aside");
    aside.id = "vps1-native-site-sidebar";
    aside.setAttribute("aria-label", "Scene Builder navigation");
    const brand = node("div", "vps1-native-brand");
    brand.appendChild(node("div", "vps1-native-logo", "S"));
    const title = node("div");
    title.appendChild(node("strong", "", "Scene Builder"));
    title.appendChild(node("small", "", "Team Studio"));
    brand.appendChild(title);
    aside.appendChild(brand);
    const navigation = node("nav");
    navigation.setAttribute("aria-label", "Applications");
    // Native pages and legacy Video Studio documents use the same menu.
    const current = /^\/(concat-studio|cloud-render)(\/|$)/.test(window.location?.pathname || "") || app === "concat-uploader" ? "concat" :
      app === "copyright-uploader" ? "copyright" : app === "short-uploader" ? "short" : (app === "song" || app === "song-uploader") ? "song" :
      (app === "voice" && (window.location?.hash === "#vs-audio" ||
        /^\/audio-downloader\/?$/.test(window.location?.pathname || ""))) ? "song" : app === "voice" ? "voice" : "experiment";
    const primaryLinks = addPrimaryLinks(navigation, current);
    applyStudioVisibility(primaryLinks, null);
    // All native app screens use the live Scene session role; no local-app
    // guessing or browser-stored role flags. Admin stays hidden until verified.
    let adminLink = null;
    let experimentTabs = null;
    let concatTabs = null;
    const account = node("div", "vps1-native-account");
    account.hidden = false;
    account.appendChild(node("small", "", "ACCOUNT"));
    const accountName = node("strong", "", "Checking session…");
    account.appendChild(accountName);
    const logout = node("form");
    logout.action = "/logout";
    logout.method = "post";
    const logoutButton = node("button", "", "Log out");
    logoutButton.type = "submit";
    logout.appendChild(logoutButton);
    logout.hidden = true;
    account.appendChild(logout);
    const setIdentity = (identity) => {
      applyStudioVisibility(primaryLinks, identity);
      const authorized = !!(identity && identity.authenticated === true);
      const isAdmin = authorized && identity.is_admin === true;
      if (experimentTabs) experimentTabs.hidden = !isAdmin;
      if (concatTabs) concatTabs.hidden = !authorized;
      if (isAdmin && !adminLink) {
        adminLink = node("a", "", "Admin");
        adminLink.href = "/admin";
        navigation.appendChild(adminLink);
      } else if (!isAdmin && adminLink) {
        adminLink.remove();
        adminLink = null;
      }
      account.hidden = false;
      logout.hidden = !authorized;
      accountName.textContent = authorized ? String(identity.username || "Signed in") : "Session verification unavailable";
    };
    const refreshIdentity = async () => {
      try {
        const response = await fetch("/api/session/identity", {
          credentials: "same-origin", cache: "no-store",
          headers: {"Accept": "application/json"}
        });
        if (!response.ok) throw new Error("Scene session unavailable");
        setIdentity(await response.json());
      } catch (_) {
        setIdentity(null);
      }
    };

    aside.appendChild(navigation);
    aside.appendChild(account);
    aside.appendChild(node("div", "vps1-native-bottom", "VPS Connected"));
    document.body.prepend(aside);
    const toggle = node("button", "vps1-native-toggle", "☰ Menu");
    toggle.type = "button";
    toggle.setAttribute("aria-label", "Open navigation menu");
    toggle.setAttribute("aria-controls", aside.id);
    toggle.setAttribute("aria-expanded", "false");
    aside.after(toggle);
    // Keep mobile navigation geometrically stable across all native apps.
    // Backdrop never reflows content, and the same drawer closes on navigation,
    // Escape, resize and history restoration.
    const backdrop = node("div", "vps1-native-nav-backdrop");
    backdrop.setAttribute("aria-hidden", "true");
    toggle.after(backdrop);
    // Render hierarchy: Experiment Studio > Studio > original app controls.
    // Keep independent app URLs and all existing backends untouched.
    const isSongAudio = app === "voice" &&
      (/^\/audio-downloader\/?$/.test(window.location?.pathname || "") ||
       document.body.classList.contains("vs-audio-active"));
    const secondLevel = current !== "concat" && !isSongAudio && ["acs", "uploader", "stream", "library"].includes(app)
      ? createExperimentTabs(false, app === "library" ? "library" :
          app === "acs" ? "video" : app === "voice" ? "voice" : "upload")
      : null;
    if (secondLevel) {
      experimentTabs = secondLevel.tabs;
      experimentTabs.hidden = true;
      toggle.after(experimentTabs);
    }
    if (current === "concat") {
      concatTabs = createConcatTabs(false, concatSection(window.location?.pathname || ""));
      concatTabs.hidden = true;
      toggle.after(concatTabs);
    }
    const attachInnerTabs = tabs => (concatTabs || (secondLevel ? secondLevel.tabs : toggle)).after(tabs);
    const voiceLink = primaryLinks.voice;
    const songLink = primaryLinks.song;
    const selectVoiceOrSong = () => {
      if (app !== "voice") return;
      const inSong = isSongAudio || document.body.classList.contains("vs-audio-active");
      for (const [link, selected] of [[voiceLink, !inSong], [songLink, inSong]]) {
        if (!link) continue;
        link.classList.toggle("active", selected);
        if (selected) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      }
    };
    if (app === "voice" && voiceLink) {
      // Re-selecting an already-open Voice Studio does not reload voice queues.
      voiceLink.addEventListener("click", event => {
        if (/^\/audio-downloader\/?$/.test(window.location?.pathname || "")) return;
        if (event.defaultPrevented || (event.button !== undefined && event.button !== 0) ||
            event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
            typeof window.vps1VoiceStudioTab !== "function") return;
        event.preventDefault();
        if (document.body.classList.contains("vs-audio-active"))
          document.getElementById("vsEngineOmni")?.click();
        window.vps1VoiceStudioTab("production");
        selectVoiceOrSong();
      });
      songLink.addEventListener("click", event => {
        if (/^\/voice-studio\//.test(window.location?.pathname || "")) return;
        if (event.defaultPrevented || (event.button !== undefined && event.button !== 0) ||
            event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        const audioEngine = document.getElementById("vsEngineAudio");
        if (!audioEngine) return;
        event.preventDefault();
        audioEngine.click();
        selectVoiceOrSong();
      });
      window.addEventListener("vps1:voice-engine-changed", selectVoiceOrSong);
      window.addEventListener("hashchange", selectVoiceOrSong);
    }

    let menuOpen = false;
    const setMenuOpen = open => {
      menuOpen = Boolean(open);
      document.body.classList.toggle("vps1-native-open", menuOpen);
      toggle.setAttribute("aria-expanded", menuOpen ? "true" : "false");
      toggle.setAttribute("aria-label", menuOpen ? "Close navigation menu" : "Open navigation menu");
    };
    toggle.addEventListener("click", () => setMenuOpen(!menuOpen));
    backdrop.addEventListener("click", () => setMenuOpen(false));
    navigation.addEventListener("click", () => setMenuOpen(false));
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") setMenuOpen(false);
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 760) setMenuOpen(false);
    });
    refreshIdentity();
    window.addEventListener("pageshow", () => {setMenuOpen(false); return refreshIdentity();});
    window.addEventListener("vps1:session-refresh", refreshIdentity);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshIdentity();
    });
    if (isSongAudio || app === "song" || app === "song-uploader") {
      const tabs = node("nav", "vps1-native-video-tabs vps1-native-song-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Song Studio");
      addLink(tabs, "Audio Downloader", "/audio-downloader/", app === "voice");
      addLink(tabs, "Song Maker", "/song-studio/song-maker/", app === "song");
      addLink(tabs, "Song Uploader", "/song-uploader", app === "song-uploader");
      attachInnerTabs(tabs);
      selectVoiceOrSong();
    }
    if (app === "short-uploader") {
      const tabs = node("nav", "vps1-native-video-tabs vps1-native-short-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Short Studio");
      addLink(tabs, "Short Maker", "/short-maker", false);
      addLink(tabs, "Short Uploader", "/short-uploader", true);
      toggle.after(tabs);
    }
    if (app === "copyright-uploader") {
      const tabs = node("nav", "vps1-native-video-tabs vps1-native-copyright-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Copyright Studio");
      addLink(tabs, "Video Merger", "/video-merger/", false);
      addLink(tabs, "Copyright Uploader", "/copyright-uploader/app?tab=add_video", true);
      toggle.after(tabs);
    }
    if (app === "acs") {
      const tabs = node("nav", "vps1-native-video-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Video Studio");
      addLink(tabs, "Scene Builder", "/scene-builder/", false);
      if (app === "acs") addLink(tabs, "Auto Caption Studio", "/auto-caption-studio/", true);
      else addLink(tabs, "Auto Caption Studio", "/auto-caption-studio/", false);
      attachInnerTabs(tabs);
      alignVideoTabs();
    }

    if (app === "uploader" || app === "stream") {
      const tabs = node("nav", "vps1-native-upload-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Upload Studio");
      addLink(tabs, "Auto Uploader", "/local-upload-manager/app?embedded=1&tab=dashboard", app === "uploader");
      addLink(tabs, "Auto Stream", "/auto-stream/", app === "stream");
      // Upload Studio applications remain the third level under Experiment.
      attachInnerTabs(tabs);
    }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", install, {once: true});
  else install();
})();

/* KOKORO_UI_AUDIT_12_V1: presentation only; no API writes or stored roles. */
(() => {
  'use strict';
  if(typeof document.querySelector !== 'function')return;
  const initialEngineHash = location.hash;
  const $ = id => document.getElementById(id);
  const all = selector => Array.from(document.querySelectorAll(selector));
  const text = (el, value) => {if (el && el.textContent !== String(value)) el.textContent = String(value);};
  const attr = (el, name, value) => {if (el.getAttribute(name) !== String(value)) el.setAttribute(name, String(value));};
  const make = (tag, value, cls) => {const el=document.createElement(tag);if(value!==undefined)el.textContent=value;if(cls)el.className=cls;return el;};
  function runtimeCards(id) {
    const target=$(id); if(!target || !target.textContent.trim().startsWith('{')) return;
    let data; try {data=JSON.parse(target.textContent);} catch (_) {return;}
    const fields=[['Worker',data.ready===true?'Ready':data.ready===false?'Unavailable':'Not reported'],['Mode',data.mode||'Not reported']];
    if(data.reason) fields.push(['Reason',data.reason]);
    for(const [key,count] of Object.entries(data.queue||{}))fields.push(['Queue · '+key.replaceAll('_',' '),String(count)]);
    if(Array.isArray(data.output_formats))fields.push(['Formats',data.output_formats.join(', ').toUpperCase()]);
    const list=make('dl',undefined,'vps1-ui-runtime');
    for(const [key,value]of fields){const row=make('div');row.append(make('dt',key),make('dd',value));list.append(row);}
    target.replaceChildren(list);
  }
  function voiceList(id, separator) {
    const target=$(id);if(!target || target.querySelector('.vps1-ui-voices'))return;
    const raw=target.textContent.trim();if(!raw || !raw.includes(separator))return;
    const names=raw.split(separator).map(s=>s.trim()).filter(Boolean);
    const search=make('input');search.type='search';search.className='vps1-ui-search';search.placeholder='Search voice name or language';search.setAttribute('aria-label','Search '+(id==='vskVoiceCards'?'Kokoro':'Edge')+' voices');
    const count=make('p',names.length+' voices','vps1-ui-voice-count');count.setAttribute('role','status');
    const list=make('div',undefined,'vps1-ui-voices');list.setAttribute('role','list');
    const rows=names.map(name=>{const row=make('div',name,'vps1-ui-voice');row.setAttribute('role','listitem');list.append(row);return row;});
    search.addEventListener('input',()=>{let visible=0;rows.forEach(row=>{row.hidden=!row.textContent.toLowerCase().includes(search.value.trim().toLowerCase());if(!row.hidden)visible++;});text(count,visible+' of '+names.length+' voices');});
    target.replaceChildren(search,count,list);
  }
  let pageNumber=0, previousFirst=null;
  function analysisPager() {
    const host=$('aiJobs');if(!host)return;
    const rows=Array.from(host.children).filter(el=>el.classList.contains('sml-ai-job'));
    if(!rows.length)return;
    if(previousFirst!==rows[0]){previousFirst=rows[0];pageNumber=Math.min(pageNumber,Math.ceil(rows.length/8)-1);}
    let pager=$('vps1-ai-pager');
    if(!pager){pager=make('nav',undefined,'vps1-ui-pager');pager.id='vps1-ai-pager';pager.setAttribute('aria-label','Analysis queue pages');const prev=make('button','Previous'),label=make('span'),next=make('button','Next');prev.type=next.type='button';label.setAttribute('role','status');prev.onclick=()=>{pageNumber--;analysisPager();};next.onclick=()=>{pageNumber++;analysisPager();};pager.append(prev,label,next);host.after(pager);}
    const pages=Math.ceil(rows.length/8);pageNumber=Math.max(0,Math.min(pageNumber,pages-1));
    rows.forEach((row,i)=>{const hidden=Math.floor(i/8)!==pageNumber;if(row.hidden!==hidden)row.hidden=hidden;});
    const [prev,label,next]=pager.children;prev.disabled=pageNumber===0;next.disabled=pageNumber===pages-1;text(label,`Page ${pageNumber+1} of ${pages} · ${rows.length} jobs`);
  }
  function ranges() {
    for(const input of all('input[type=range]')){
      const output=input.dataset.output?$(input.dataset.output):null;
      if(output)text(output,input.value);
      const label=input.closest('label');
      if(label&&!input.dataset.vps1RangeName){const copy=label.cloneNode(true);copy.querySelectorAll('input,output,small,.sb-number').forEach(n=>n.remove());const name=copy.textContent.trim();if(name)input.dataset.vps1RangeName=name;}
      if(input.dataset.vps1RangeName)attr(input,'aria-label',input.dataset.vps1RangeName);
      attr(input,'aria-valuenow',input.value);
      if(!output){const field=input.closest('.field');const label=field?.querySelector('label');const note=field?.querySelector('span.small');if(label?.textContent.trim()==='Text Width' && note)text(note,input.value+'%');}
    }
    // The dynamic FG library select owns the persisted value and handler.
    const old=$('presetFgSelect'),live=$('presetFgLibrary');if(old&&live){const field=old.closest('.field');if(field&&!field.hidden)field.hidden=true;old.disabled=true;attr(old,'aria-hidden','true');}
  }
  let controlId=0;
  function accessibleControls() {
    for(const field of all('.field,.form-field,.vsa-field')){
      const label=field.querySelector('label'),control=field.querySelector('input,select,textarea');
      if(!label||!control||label.contains(control)||label.htmlFor)continue;
      if(!control.id)control.id='vps1-ui-control-'+(++controlId);
      label.htmlFor=control.id;
    }
    const navs=all('nav[aria-label],.vs-engine-selector,.sm-tabs,.vsa-tabs,.vsk-tabs,.vse-tabs,.lum-tabs,.lum-ae-tabs,.tabs');
    for(const nav of navs){
      const items=Array.from(nav.children).filter(el=>el.matches('a,button'));
      if(!items.length)continue;
      const trueTabs=nav.getAttribute('role')==='tablist'&&items.every(el=>el.getAttribute('role')==='tab');
      const selectable=items.some(el=>el.hasAttribute('aria-selected')||el.classList.contains('active')||el.classList.contains('is-active'));
      // Section buttons retain native button semantics and expose their pressed state.
      if(nav.getAttribute('role')==='tablist'&&!trueTabs)attr(nav,'role','navigation');
      for(const el of items){
        if(el.matches('a')&&el.classList.contains('active'))attr(el,'aria-current','page');
        if(selectable&&el.matches('button')&&el.getAttribute('role')!=='tab'){
          const selected=el.getAttribute('aria-selected');const pressed=selected!==null?selected==='true':el.classList.contains('active')||el.classList.contains('is-active');
          attr(el,'aria-pressed',pressed);
        }
        if(trueTabs)el.tabIndex=el.getAttribute('aria-selected')==='true'?0:-1;
      }
      if(!nav.dataset.vps1Keyboard){nav.dataset.vps1Keyboard='1';nav.addEventListener('keydown',event=>{
        if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
        const buttons=Array.from(nav.children).filter(el=>el.matches('a,button')&&!el.disabled&&!el.hidden);const index=buttons.indexOf(document.activeElement);if(index<0)return;
        event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
        buttons[next].focus(); // Enter/Space activates; arrow navigation never submits a job.
      });}
    }
    const labels={'channel-search':'Search channels'};
    for(const control of all('main select,main input:not([type=hidden]),main textarea')){
      if(control.labels?.length||control.hasAttribute('aria-label')||control.hasAttribute('aria-labelledby'))continue;
      const parent=control.parentElement;
      const preceding=control.previousElementSibling;
      const name=preceding?.matches('label')?preceding.textContent.trim():control.getAttribute('placeholder')||labels[control.id]||(control.tagName==='SELECT'?control.options[0]?.textContent.trim():'')||(control.id?control.id.replace(/[-_]/g,' '):'');
      if(name)attr(control,'aria-label',name);
    }
  }
  function statusPresentation() {
    const state=$('vskEngineState'),message=$('vskMessage');
    if(state?.textContent.trim()==='Kokoro ready'&&message?.textContent.trim()==='Kokoro inference service is not yet verified.')text(message,'Worker ready. Drive delivery is verified separately for each job.');
    for(const label of all('.lum-card p,.card p,.mode-copy,.mode-description')){
      if(label.textContent.trim()==='Desktop feature retained behind a mode toggle. Current production remains single-project submission.')text(label,'Bulk Import is not available yet. Use Add Video for single-project submission.');
    }
    for(const note of all('.lum-switch small')){
      if(note.textContent.trim()==='Preserved; OFF for current single-upload workflow')text(note,'Workspace visibility; scheduling is controlled separately.');
    }
    for(const card of all('.lum-card')){
      const title=card.querySelector('h2');
      if(title?.textContent.trim()==='Bulk Import'){
        const badge=card.querySelector('.lum-badge');if(badge)text(badge,'Not available');
      }
      if(title?.textContent.trim()==='Auto Uploader · Three Modes'&&!card.querySelector('.vps1-ui-details')){
        const notes=Array.from(card.querySelectorAll('p'));if(notes.length){const detail=make('details',undefined,'vps1-ui-details');detail.append(make('summary','About the three independent upload modes'));for(const note of notes)detail.append(note);card.append(detail);}
      }
    }
    for(const el of all('h3,h4,p,small,.generic,.badge,.aev2-summary')){
      if(el.children.length)continue;
      if(el.textContent.trim()==='undefined · AUTO · -16 LUFS · duck 11 dB · limiter -1.5 dBTP')text(el,'Preset unavailable · AUTO · -16 LUFS · duck 11 dB · limiter -1.5 dBTP');
    }
  }
  function reviewUsability(){
    const names={images:'Choose images',audio:'Choose narration audio','aev2-files':'Choose Studio narration files','aev2-music':'Choose Studio music',imageFiles:'Choose images',clipMin:'Minimum clip length in seconds',clipMax:'Maximum clip length in seconds',sceneThreshold:'Scene detection sensitivity'};
    for(const [id,label] of Object.entries(names)){const input=$(id);if(input)attr(input,'aria-label',label);}
    for(const form of all('form[data-lum-upload-form]')){
      if(form.dataset.vps1Validity)continue;form.dataset.vps1Validity='1';
      const update=()=>{const ok=Array.from(form.querySelectorAll('[required]')).every(n=>n.checkValidity());for(const b of form.querySelectorAll('button[type=submit]')){b.disabled=!ok;b.title=ok?'':'Complete the required video, channel and thumbnail fields.';}};
      form.addEventListener('input',update);form.addEventListener('change',update);update();
    }
    for(const list of all('.report-list')){
      if(list.dataset.vps1Reports)continue;list.dataset.vps1Reports='1';
      const rows=Array.from(list.querySelectorAll('.report-row'));if(!rows.length)continue;
      let page=0;const bar=make('div',undefined,'vps1-report-controls'),search=make('input'),previous=make('button','Previous'),next=make('button','Next'),count=make('span');
      search.type='search';search.placeholder='Search reports';search.setAttribute('aria-label','Search VPS reports');previous.type=next.type='button';bar.append(search,previous,count,next);list.before(bar);
      const show=()=>{const match=rows.filter(r=>r.textContent.toLowerCase().includes(search.value.toLowerCase()));page=Math.max(0,Math.min(page,Math.ceil(match.length/10)-1));const visible=new Set(match.slice(page*10,page*10+10));rows.forEach(r=>r.hidden=!visible.has(r));previous.disabled=page===0;next.disabled=(page+1)*10>=match.length;count.textContent=match.length?`${page*10+1}–${Math.min((page+1)*10,match.length)} of ${match.length}`:'No matching reports';};
      search.addEventListener('input',()=>{page=0;show();});previous.addEventListener('click',()=>{page--;show();});next.addEventListener('click',()=>{page++;show();});show();
    }
    for(const time of all('time,.report-row>span:last-of-type,main td,main small,main span,main p')){
      if(time.dataset.vps1Time||time.children.length)continue;const raw=time.getAttribute('datetime')||time.textContent.trim();
      if(!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(raw))continue;
      const value=new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw)?raw:raw.replace(' ','T')+'Z');if(!Number.isFinite(value.getTime()))continue;
      time.dataset.vps1Time=raw;time.title=raw;text(time,new Intl.DateTimeFormat('en-GB',{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Karachi'}).format(value)+' PKT');
    }
  }
  function polish() {
    // A late Omni startup response can rewrite the URL after engine selection.
    // Keep refresh on the visible engine; Omni keeps its own workspace hashes.
    const engineHash={kokoro:'#vs-kokoro',edge:'#vs-edge','audio-downloader':'#vs-audio'}[document.documentElement.dataset.vsEngine];
    if (/^\/audio-downloader\/?$/.test(location.pathname)) {
      if (document.documentElement.dataset.vsEngine === "audio-downloader" && location.hash)
        history.replaceState(null, "", "/audio-downloader/");
    } else if(engineHash&&location.hash!==engineHash)history.replaceState(null,'',engineHash);
    const proxy=$('vsaProxy'),audio=$('vsAudioWorkspace');
    if(proxy&&audio&&!audio.contains(proxy))audio.append(proxy);
    runtimeCards('vskRuntimeDetails');runtimeCards('vseDetails');
    voiceList('vskVoiceCards',' · ');voiceList('vseVoiceRows','  |  ');
    ranges();analysisPager();accessibleControls();statusPresentation();reviewUsability();
  }
  function boot() {
    if(!document.querySelector('#app-sidebar,#vps1-native-site-sidebar'))return;
    // Load the same canonical stylesheet last, after app inline styles, with a
    // release key. Old app documents receive this UI without worker restarts.
    if(!$('vps1-kokoro-audit-css')){const link=make('link');link.id='vps1-kokoro-audit-css';link.rel='stylesheet';link.href='/static/vps1_native_site_shell.css?ui=kokoro-audit12-v2';document.head.append(link);}
    polish();document.addEventListener('input',ranges);
    // The old Omni initializer rewrites the hash before the other engines boot.
    // Restore only explicit engine deep links after all initializers have run.
    const restoreEngine = () => {
      const targetHash = /^\/audio-downloader\/?$/.test(location.pathname) ? '#vs-audio' : initialEngineHash;
      const id={'#vs-kokoro':'vsEngineKokoro','#vs-edge':'vsEngineEdge','#vs-audio':'vsEngineAudio'}[targetHash];
      const button=id&&$(id);if(button&&button.getAttribute('aria-selected')!=='true')button.click();
    };
    if(document.readyState==='complete')restoreEngine();
    else document.addEventListener('DOMContentLoaded',restoreEngine,{once:true});
    let pending=false;
    new MutationObserver(()=>{if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;polish();});}).observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['class','hidden','aria-selected']});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
