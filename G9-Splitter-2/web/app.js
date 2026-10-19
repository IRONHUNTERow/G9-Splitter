// G9 Splitter 2 - interface. Talks to the Python engine through a local API.
(function () {
  "use strict";
  const qs = new URLSearchParams(location.search);
  const TOKEN = qs.get("t") || "";
  const HOST = qs.get("host") || "browser";
  document.body.dataset.host = HOST;

  const THEMES = {
    "Light": "light", "Dark": "dark", "Overwatch style": "overwatch",
    "Destiny 2 style": "destiny", "No Man's Sky style": "nms",
    "Cyberpunk 2077 style": "cyber",
  };
  const ZONE_NAME = { left: "Left", center: "GAME", right: "Right" };
  const $ = (s) => document.querySelector(s);
  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let S = null;            // latest state from the engine
  let pending = null;      // state that arrived mid-drag
  let selected = null;     // selected window hwnd
  let press = null;        // pointer pressed on a draggable thing
  let dragging = null;     // active drag
  let lastStatusId = -1;
  let modern = false;      // modern layout on?
  let tab = "windows";     // open tab in the modern layout
  const thumbURLs = {};    // hwnd -> object URL of its latest preview

  // ---------------- engine API ----------------
  async function api(action, args) {
    try {
      const r = await fetch("/api/action", {
        method: "POST",
        headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
        body: JSON.stringify({ action, args: args || {} }),
      });
      const j = await r.json();
      if (j.state) render(j.state);
      return j;
    } catch (e) {
      setStatus("Can't reach the G9 Splitter engine.");
    }
  }
  async function poll() {
    try {
      const r = await fetch("/api/state", { headers: { "X-Token": TOKEN } });
      if (!r.ok) return;
      const s = await r.json();
      lostEngine = 0;
      if (press || dragging) pending = s; else render(s);
    } catch (e) {
      lostEngine++;
      // The engine stopped (it shouldn't - g9_splitter_run.log / _crash.log say why).
      // After a few seconds, start it again; it opens a fresh window and this one closes.
      const api = window.pywebview && window.pywebview.api;
      if (lostEngine >= 6 && api && api.restart_engine && !restarting) {
        restarting = true;
        setStatus("The G9 Splitter engine stopped - starting it again...");
        try { await api.restart_engine(); } catch (err) { }
      } else if (!restarting) {
        setStatus("Can't reach the G9 Splitter engine - is it still running?");
      }
    }
  }
  let lostEngine = 0, restarting = false;
  async function allowFocus() {
    try { if (window.pywebview && window.pywebview.api) await window.pywebview.api.allow_fg(); } catch (e) { }
  }
  window.addEventListener("pagehide", () => {
    try { navigator.sendBeacon && fetch("/api/bye", { method: "POST", headers: { "X-Token": TOKEN }, keepalive: true }); } catch (e) { }
  });

  // ---------------- rendering ----------------
  // Only touch the page when something really changed (re-setting identical text/styles makes the
  // browser repaint, which shows up as a faint blink - especially on the tilted side screens).
  const setText = (el, t) => { if (el && el.textContent !== t) el.textContent = t; };
  const setStyle = (el, k, v) => { if (el.style[k] !== v) el.style[k] = v; };
  const setVar = (el, k, v) => { if (el.style.getPropertyValue(k) !== v) el.style.setProperty(k, v); };
  function setStatus(text, flash) {
    const el = $("#status");
    el.textContent = text;
    if (modern && flash !== false && text) toast(text);
    if (flash) {
      const bar = document.querySelector(".statusbar");
      bar.classList.remove("flash"); void bar.offsetWidth; bar.classList.add("flash");
    }
  }

  function fillSelect(sel, items, value) {
    if (ddOpen && ddOpen.sel === sel) return;   // don't change a menu while it's open
    const sig = items.map((i) => i.value + "|" + i.label).join("\n");
    if (sel.dataset.sig !== sig) {
      sel.innerHTML = items.map((i) => `<option value="${esc(i.value)}">${esc(i.label)}</option>`).join("");
      sel.dataset.sig = sig;
    }
    if (value !== undefined && sel.value !== value) sel.value = value;
    ddSync(sel);
  }

  // ---------------- themed drop-down menus ----------------
  // Windows draws <select> menus itself (white, unstyled), so each select gets a
  // themed button + menu instead. The hidden <select> still holds the value.
  let ddOpen = null;
  function ddEnhance(sel) {
    const wrap = document.createElement("div");
    wrap.className = "dd";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dd-btn";
    btn.innerHTML = '<span class="dd-label"></span><span class="dd-caret" aria-hidden="true"></span>';
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    wrap.appendChild(btn);
    sel.classList.add("dd-native");
    sel.tabIndex = -1;
    sel._dd = { wrap, btn };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (ddOpen && ddOpen.sel === sel) ddClose(); else ddOpenFor(sel);
    });
    btn.addEventListener("keydown", (e) => {
      if (!["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) return;
      e.preventDefault();
      if (!ddOpen) ddOpenFor(sel); else ddKey(e);
    });
    ddSync(sel);
  }
  function ddSync(sel) {
    if (!sel._dd) return;
    const o = sel.options[sel.selectedIndex];
    setText(sel._dd.btn.querySelector(".dd-label"), o ? o.text : "");
  }
  function ddOpenFor(sel) {
    ddClose();
    const menu = document.createElement("div");
    menu.className = "dd-menu";
    [...sel.options].forEach((o, i) => {
      const it = document.createElement("div");
      it.className = "dd-item" + (i === sel.selectedIndex ? " selected" : "");
      it.textContent = o.text;
      it.addEventListener("mouseenter", () => ddActivate(i));
      it.addEventListener("click", (e) => { e.stopPropagation(); ddChoose(i); });
      menu.appendChild(it);
    });
    document.body.appendChild(menu);
    const r = sel._dd.btn.getBoundingClientRect();
    menu.style.minWidth = r.width + "px";
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
    const below = window.innerHeight - r.bottom - 12;
    if (menu.offsetHeight > below && r.top > below) {
      menu.classList.add("up");
      menu.style.bottom = (window.innerHeight - r.top + 4) + "px";
      menu.style.maxHeight = Math.min(360, r.top - 12) + "px";
    } else {
      menu.style.top = (r.bottom + 4) + "px";
      menu.style.maxHeight = Math.min(360, below) + "px";
    }
    sel._dd.wrap.classList.add("open");
    ddOpen = { sel, menu, active: sel.selectedIndex };
    ddActivate(sel.selectedIndex);
  }
  function ddActivate(i) {
    if (!ddOpen) return;
    ddOpen.active = i;
    [...ddOpen.menu.children].forEach((el, k) => el.classList.toggle("active", k === i));
    const el = ddOpen.menu.children[i];
    if (el) el.scrollIntoView({ block: "nearest" });
  }
  function ddChoose(i) {
    const sel = ddOpen.sel;
    ddClose();
    if (i >= 0 && sel.selectedIndex !== i) {
      sel.selectedIndex = i;
      ddSync(sel);
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    sel._dd.btn.focus();
  }
  function ddClose() {
    if (!ddOpen) return;
    ddOpen.menu.remove();
    ddOpen.sel._dd.wrap.classList.remove("open");
    ddOpen = null;
  }
  function ddKey(e) {
    const n = ddOpen.sel.options.length;
    if (e.key === "ArrowDown") ddActivate(Math.min(n - 1, ddOpen.active + 1));
    else if (e.key === "ArrowUp") ddActivate(Math.max(0, ddOpen.active - 1));
    else ddChoose(ddOpen.active);
  }
  document.addEventListener("keydown", (e) => {
    if (!ddOpen) return;
    if (e.key === "Escape") { const b = ddOpen.sel._dd.btn; ddClose(); b.focus(); }
    else if (["ArrowDown", "ArrowUp", "Enter"].includes(e.key) && document.activeElement !== ddOpen.sel._dd.btn) { e.preventDefault(); ddKey(e); }
  });
  document.addEventListener("pointerdown", (e) => {
    if (ddOpen && !e.target.closest(".dd-menu") && !ddOpen.sel._dd.wrap.contains(e.target)) ddClose();
  }, true);
  window.addEventListener("resize", ddClose);
  window.addEventListener("blur", ddClose);

  function applyTheme(name) {
    document.body.dataset.theme = THEMES[name] || "dark";
    setModern(true);   // every theme uses the modern layout
  }

  // ---------------- modern layout ----------------
  const homes = {};   // where the moved pieces live in the classic layout
  function setModern(on) {
    if (on === modern) return;
    modern = on;
    document.body.classList.toggle("modern", on);
    const pieces = [".layout-controls", ".layout-more", ".toggles", ".help-panel .keys", ".help-panel .opts"];
    if (on) {
      pieces.forEach((sel) => {
        const el = $(sel);
        if (!el) return;
        homes[sel] = { el, parent: el.parentNode, next: el.nextSibling };
        $("#settings-body").appendChild(el);
      });
    } else {
      pieces.slice().reverse().forEach((sel) => {
        const h = homes[sel];
        if (h) h.parent.insertBefore(h.el, h.next && h.next.parentNode === h.parent ? h.next : null);
      });
    }
    for (const k in sigs) delete sigs[k];        // redraw lists in the new style
    showTab(tab);
    requestAnimationFrame(() => { if (S) { layoutStage(); renderChips(); renderWindows(); renderRules(); } });
  }
  function showTab(name) {
    tab = name;
    const panels = { windows: ".windows-panel", rules: ".rules-panel", settings: ".settings-panel" };
    for (const [k, sel] of Object.entries(panels)) $(sel).classList.toggle("tab-on", k === name);
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    moveInk();
    if (name === "windows") refreshThumbs();
  }
  function moveInk() {
    const b = document.querySelector("#tabs [data-tab].on"), ink = document.querySelector("#tabs .tab-ink");
    if (!b || !ink) return;
    ink.style.width = b.offsetWidth + "px";
    ink.style.transform = `translateX(${b.offsetLeft}px)`;
  }
  document.querySelectorAll("#tabs [data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  function toast(text) {
    const box = $("#toasts");
    // the same message again (e.g. every second while the engine is unreachable): keep one
    for (const old of box.children) {
      if (old.textContent === text && !old.classList.contains("out")) {
        clearTimeout(old._timer);
        old._timer = setTimeout(() => { old.classList.add("out"); setTimeout(() => old.remove(), 400); }, 3800);
        return;
      }
    }
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = text;
    box.appendChild(t);
    while (box.children.length > 3) box.firstChild.remove();
    t._timer = setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 3800);
  }

  const iconURL = (h) => `/api/icon?h=${h}&t=${encodeURIComponent(TOKEN)}`;
  function iconBadge(w, extra) {
    if (!modern) return `<span class="badge">${badge(w)}</span>`;
    return `<span class="badge appicon ${extra || ""}"><b>${badge(w)}</b>` +
      `<img src="${iconURL(w.hwnd)}" alt="" draggable="false" onload="this.parentNode.classList.add('has-img')" onerror="this.remove()"></span>`;
  }

  // Live previews: fetched one by one every few seconds, only while you can see them.
  // No flicker: an unchanged picture is skipped, and a new one is fully decoded
  // before it replaces the old one (the old one is freed a bit later).
  const thumbSig = {};
  function quickHash(bytes) {
    let h = 2166136261 >>> 0;
    const step = Math.max(1, Math.floor(bytes.length / 4096));   // sample - plenty to spot a change
    for (let i = 0; i < bytes.length; i += step) h = Math.imul(h ^ bytes[i], 16777619) >>> 0;
    return bytes.length + ":" + h;
  }
  let thumbBusy = false;
  async function refreshThumbs() {
    // Only while G9 Splitter is the window you're using - no wasted work in the background.
    if (!modern || tab !== "windows" || document.hidden || !document.hasFocus() || thumbBusy || press || dragging) return;
    thumbBusy = true;
    try {
      for (const el of [...document.querySelectorAll("#winlist .card .thumb")]) {
        if (!document.hasFocus() || document.hidden) break;   // you switched away mid-way: stop
        const h = el.dataset.thumb;
        const r = await fetch(`/api/thumb?h=${h}&t=${encodeURIComponent(TOKEN)}&ts=${Date.now()}`);
        if (r.status !== 200) {                       // e.g. minimized: keep the last picture
          if (!thumbURLs[h] && el.classList.contains("has-img")) el.classList.remove("has-img");
          continue;
        }
        const buf = new Uint8Array(await r.arrayBuffer());
        const sig = quickHash(buf);
        if (thumbSig[h] === sig) continue;            // nothing changed - don't touch it
        const url = URL.createObjectURL(new Blob([buf], { type: "image/png" }));
        const img = new Image();
        img.src = url;
        try { await img.decode(); } catch (e) { URL.revokeObjectURL(url); continue; }
        const old = thumbURLs[h];
        thumbURLs[h] = url;
        thumbSig[h] = sig;
        const cur = document.querySelector(`#winlist .thumb[data-thumb="${h}"]`);
        if (cur) {
          cur.style.backgroundImage = `url("${url}")`;
          if (!cur.classList.contains("has-img")) cur.classList.add("has-img");
        }
        if (old) setTimeout(() => URL.revokeObjectURL(old), 3000);
      }
    } catch (e) { } finally { thumbBusy = false; }
  }
  setInterval(refreshThumbs, 3000);
  document.addEventListener("visibilitychange", refreshThumbs);
  window.addEventListener("focus", () => setTimeout(refreshThumbs, 50));   // catch up as soon as you come back
  window.addEventListener("resize", moveInk);

  function render(s) {
    S = s;
    applyTheme(s.theme);
    fillSelect($("#theme"), Object.keys(THEMES).map((k) => ({ value: k, label: k })), s.theme);
    fillSelect($("#monitor"), s.monitors.map((m) => ({ value: m.device, label: m.label })), s.monitor);
    fillSelect($("#preset"), s.presets.map((p) => ({ value: p, label: p })), s.preset);
    if (s.layout && s.layout_choices) {
      const L = s.layout, C = s.layout_choices;
      const heights = C.heights.map((p) => ({ value: String(p), label: p === 100 ? "Full height (100%)" : `${p}% of the monitor height` }));
      if (!L.height_pct) heights.unshift({ value: "0", label: "Custom (exact pixels)" });
      fillSelect($("#game-height"), heights, String(L.height_pct || 0));
      fillSelect($("#game-pos"), C.positions.map(([v, l]) => ({ value: v, label: l })), L.position);
      fillSelect($("#game-valign"), C.valigns.map(([v, l]) => ({ value: v, label: l })), L.valign);
      fillSelect($("#mon-shape"), [{ value: "auto", label: "Automatic (curved if ultrawide)" }, { value: "curved", label: "Curved" }, { value: "flat", label: "Flat" }], L.shape);
      fillSelect($("#new-game"), C.new_game.map(([v, l]) => ({ value: v, label: l })), L.new_game_action);
      $("#opt-new-lock").checked = !!L.new_game_lock;
      const [, , mw0, mh0] = s.mon, [, , gw, gh] = s.zones.center;
      setText($("#layout-info"), `Monitor ${mw0} × ${mh0} (${L.mon_aspect}) · game area ${gw} × ${gh} (${L.game_aspect})`);
      const flat = L.shape === "flat" || (L.shape === "auto" && mw0 / mh0 < 2.2);
      document.body.classList.toggle("flat-monitor", flat);
    }
    fillSelect($("#rule-where"), s.zone_choices.map((p) => ({ value: p, label: p })));
    if (document.activeElement !== $("#cw")) $("#cw").value = s.zones.center[2];
    if (document.activeElement !== $("#ch")) $("#ch").value = s.zones.center[3];
    $("#opt-ontop").checked = s.options.game_on_top;
    $("#opt-auto").checked = s.options.auto_apply;
    $("#opt-fps").checked = !!s.options.fps_overlay;
    $("#opt-detect").checked = s.options.auto_detect_games !== false;
    $("#opt-startup").checked = s.startup;
    $("#restart-admin").style.display = s.is_admin ? "none" : "";
    if (s.status_id !== lastStatusId) {
      setStatus(s.status, lastStatusId !== -1);
      lastStatusId = s.status_id;
    }
    layoutStage();
    renderChips();
    renderWindows();
    renderRules();
  }

  const px = (n) => (Math.round(n * 100) / 100) + "px";
  const SS = 3;   // side screens are drawn at 3x and shrunk back, for sharp text on the tilt
  function layoutStage() {
    if (!S) return;
    const st = $("#stage");
    const W = st.clientWidth;
    const [mx, my, mw, mh] = S.mon;
    // leave room at the sides for the tilted side screens, which reach out toward you
    const padX = document.body.classList.contains("modern") ? Math.max(24, W * 0.06) : 24;
    // the preview takes the shape of your monitor: short for a super-ultrawide, taller for 16:9
    const H = Math.round(Math.min(440, Math.max(180, (W - padX) * (mh / mw) * 0.9 + 20)));
    setStyle(st, "height", px(H));
    const sc = Math.min((W - padX) / mw, (H - 20) / mh);
    const ox = (W - mw * sc) / 2, oy = (H - mh * sc) / 2;
    for (const z of ["left", "center", "right"]) {
      const [x, y, w, h] = S.zones[z];
      const el = st.querySelector(`.zone[data-zone="${z}"]`);
      setStyle(el, "display", w > 0 ? "" : "none");
      const L = ox + (x - mx) * sc, T = oy + (y - my) * sc, ZW = w * sc, ZH = h * sc;
      if (modern && z !== "center") {
        // Tilted side screens are drawn as a picture and then bent into shape, which softens text.
        // So they're drawn at SS x the size (CSS zoom) and shrunk back by the same factor in the
        // tilt: the browser then has SS x the pixels to work from and the text stays sharp.
        // Same place, same size, same tilt as before - only sharper.
        const hinge = z === "left" ? L + ZW : L;                  // edge that meets GAME
        const left = z === "left" ? hinge - ZW * SS : hinge;      // laid-out box, before shrinking
        const top = T + ZH / 2 - ZH * SS / 2;
        setStyle(el, "zoom", String(SS));                         // zoom also multiplies left/top/width/height
        setStyle(el, "left", px(left / SS));
        setStyle(el, "top", px(top / SS));
        setStyle(el, "width", px(ZW));
        setStyle(el, "height", px(ZH));
        setVar(el, "--unzoom", String(1 / SS));
      } else {
        setStyle(el, "zoom", "");
        setStyle(el, "left", px(L));
        setStyle(el, "top", px(T));
        setStyle(el, "width", px(ZW));
        setStyle(el, "height", px(ZH));
      }
      setText(el.querySelector(".zone-head em"), `${w} × ${h}`);
    }
    const mf = $("#monframe");            // outline of the whole monitor (shows unused space)
    if (mf) {
      setStyle(mf, "left", px(ox)); setStyle(mf, "top", px(oy));
      setStyle(mf, "width", px(mw * sc)); setStyle(mf, "height", px(mh * sc));
    }
    const [wx, wy, ww, wh] = S.work;
    const tb = $("#taskbar");
    if (wh < mh) {
      const top = wy > my ? my : wy + wh;
      setStyle(tb, "display", "");
      setStyle(tb, "left", px(ox));
      setStyle(tb, "width", px(mw * sc));
      setStyle(tb, "top", px(oy + (top - my) * sc));
      setStyle(tb, "height", px(Math.max(4, (mh - wh) * sc)));
    } else {
      setStyle(tb, "display", "none");
    }
  }

  function badge(w) {
    const n = (w.exe || w.title || "?").replace(/\.exe$/i, "");
    return esc(n.charAt(0).toUpperCase());
  }

  const sigs = {};
  function changed(key, data) {
    const sig = JSON.stringify(data);
    if (sigs[key] === sig) return false;
    sigs[key] = sig;
    return true;
  }

  // Updates in place: chips that are already there are kept (no re-animation, no icon reload),
  // only new ones are added and gone ones removed - nothing blinks while you look elsewhere.
  function renderChips() {
    const placedWins = S.windows.filter((w) => w.zone);
    if (!changed("chips", [modern, placedWins.map((w) => [w.hwnd, w.title, w.exe, w.zone, !!w.game])])) return;
    // the middle screen is called GAME while a game is in it, otherwise Middle
    const hasGame = placedWins.some((w) => w.zone === "center" && w.game);
    const centerName = $('#stage .zone[data-zone="center"] .zone-head b');
    setText(centerName, hasGame ? "GAME" : "Middle");
    centerName.parentElement.parentElement.classList.toggle("has-game", hasGame);
    const have = {};
    document.querySelectorAll("#stage .chip").forEach((c) => {
      if (c.dataset.modern !== String(modern)) c.remove(); else have[c.dataset.hwnd] = c;
    });
    const keep = new Set(placedWins.map((w) => w.hwnd));
    for (const h in have) if (!keep.has(h)) { have[h].remove(); delete have[h]; }
    for (const z of ["left", "center", "right"]) {
      const el = $(`#stage .zone[data-zone="${z}"]`);
      const box = el.querySelector(".chips");
      const wins = placedWins.filter((w) => w.zone === z);
      el.classList.toggle("has-apps", wins.length > 0);
      let prev = null;
      for (const w of wins) {
        let c = have[w.hwnd];
        if (!c) {
          c = document.createElement("div");
          c.className = "chip";
          c.dataset.hwnd = w.hwnd;
          c.dataset.modern = String(modern);
          c.innerHTML = `${iconBadge(w)}<span class="chip-title"></span>` +
            `<button class="chip-x" data-restore="${w.hwnd}" title="Back to a normal window">&times;</button>`;
        }
        if (c.title !== w.title) c.title = w.title;
        setText(c.querySelector(".chip-title"), w.title);
        const want = prev ? prev.nextSibling : box.firstChild;
        if (want !== c) box.insertBefore(c, want);   // also moves a chip between boxes without rebuilding it
        prev = c;
      }
    }
  }

  function renderWindows() {
    const q = ($("#search").value || "").toLowerCase();
    const list = $("#winlist");
    const wins = S.windows.filter((w) => !q || (w.title + " " + w.exe).toLowerCase().includes(q));
    if (selected && !S.windows.some((w) => w.hwnd === selected)) selected = null;
    setText($("#count-windows"), String(S.windows.length));
    // Selecting a window never rebuilds the list (no blinking) - see applySelection().
    if (changed("windows", [modern, q, wins.map((w) => [w.hwnd, w.title, w.exe, w.zone, w.borderless, w.muted, w.ignored])])) {
      if (modern) renderCards(list, wins); else renderRows(list, wins);
    }
    applySelection();
  }

  const whereTag = (w) => !w.zone && w.ignored ? `<span class="where where-ignored">Ignored</span>` : w.zone ? `<span class="where where-${w.zone}">${w.zone === "center" && !w.borderless ? "Middle" : ZONE_NAME[w.zone]}</span>` : "";

  function renderRows(list, wins) {
    list.innerHTML = wins.map((w) =>
      `<div class="row" data-hwnd="${w.hwnd}">` +
      `<span class="badge">${badge(w)}</span>` +
      `<span class="row-main"><span class="row-title">${esc(w.title)}</span><span class="row-exe">${esc(w.exe || "protected program")}</span></span>` +
      whereTag(w) + `<span class="grip" aria-hidden="true">&#8942;&#8942;</span></div>`
    ).join("") || `<div class="empty">No windows found.</div>`;
  }

  // Modern cards are updated in place: existing cards keep their preview and
  // don't animate again; only new windows slide in.
  let cardsShown = false;
  // speaker button on each window: mutes that program in Windows' volume mixer
  const SPEAKER = '<path d="M11 5 6 9H2v6h4l5 4z" fill="currentColor"/>';
  const MUTE_ICONS = {
    on: SPEAKER + '<path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
    idle: SPEAKER + '<path d="M15.5 8.5a5 5 0 0 1 0 7"/>',
    muted: SPEAKER + '<path d="m16 9 6 6"/><path d="m22 9-6 6"/>',
  };
  const MUTE_TIPS = { on: "Mute this program", idle: "Mute this program (it isn't making sound right now)", muted: "Unmute" };
  function setMuteButton(b, w) {
    if (!b) return;
    const st = w.muted === true ? "muted" : w.muted === false ? "on" : "idle";
    if (b.dataset.state === st) return;
    b.dataset.state = st;
    b.title = MUTE_TIPS[st];
    b.setAttribute("aria-label", MUTE_TIPS[st]);
    b.innerHTML = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${MUTE_ICONS[st]}</svg>`;
  }
  function renderCards(list, wins) {
    if (wins.length) [...list.children].forEach((c) => { if (!c.classList.contains("card")) c.remove(); });
    const have = {};
    list.querySelectorAll(".card").forEach((el) => { have[el.dataset.hwnd] = el; });
    const keep = new Set(wins.map((w) => w.hwnd));
    for (const h in have) if (!keep.has(h)) have[h].remove();
    let i = 0;
    let prev = null;
    for (const w of wins) {
      let el = have[w.hwnd];
      const sig = JSON.stringify([w.title, w.exe, w.zone, w.borderless]);
      if (!el) {
        el = document.createElement("div");
        el.className = "row card";
        el.dataset.hwnd = w.hwnd;
        el.style.setProperty("--i", i++);
        const t = thumbURLs[w.hwnd];
        el.innerHTML = `<div class="thumb${t ? " has-img" : ""}" data-thumb="${w.hwnd}"${t ? ` style="background-image:url('${t}')"` : ""}>` +
          `${iconBadge(w, "big")}<span class="where-slot">${whereTag(w)}</span></div>` +
          `<div class="card-info">${iconBadge(w)}<span class="row-main"><span class="row-title"></span>` +
          `<span class="row-exe"></span></span></div>` +
          (w.exe ? `<button class="mute-btn" data-mute="${w.hwnd}"></button>` : "");   // pinned bottom-right of the card
        // only the very first list slides in; windows that open later just appear (no blinking)
        if (cardsShown) el.classList.add("seen"); else setTimeout(() => el.classList.add("seen"), 700);
      }
      if (el.dataset.sig !== sig) {
        el.dataset.sig = sig;
        el.querySelector(".row-title").textContent = w.title;
        el.querySelector(".row-exe").textContent = w.exe || "protected program";
        const slot = el.querySelector(".where-slot"), tag = whereTag(w);
        if (slot._html !== tag) { slot.innerHTML = tag; slot._html = tag; }
      }
      setMuteButton(el.querySelector(".mute-btn"), w);
      const want = prev ? prev.nextSibling : list.firstChild;
      if (want !== el) list.insertBefore(el, want);
      prev = el;
    }
    if (wins.length) cardsShown = true;
    if (!wins.length && !list.querySelector(".empty")) list.innerHTML = `<div class="empty">No windows found.</div>`;
    setTimeout(refreshThumbs, 50);
  }

  // Light up the button that matches where the selected window is right now.
  function updateActions() {
    const w = S && selected && S.windows.find((x) => x.hwnd === selected);
    document.querySelectorAll(".actions [data-place]").forEach((b) => {
      const z = b.dataset.place;
      const on = !!w && w.zone === z && (z !== "center" || (b.dataset.borderless === "1") === !!w.borderless);
      b.classList.toggle("active", on);
    });
  }

  // Mark the selected window everywhere: list, buttons and "Selected:" labels.
  function applySelection() {
    document.querySelectorAll("#winlist .row").forEach((el) => el.classList.toggle("selected", el.dataset.hwnd === selected));
    updateActions();
    const w = S && selected && S.windows.find((x) => x.hwnd === selected);
    const html = w
      ? `<span class="sel-label">Selected</span>${iconBadge(w)}<span class="sel-name">${esc(w.title)}</span>`
      : `<span class="sel-label">Selected</span><span class="sel-none">nothing - click a window</span>`;
    document.querySelectorAll(".sel-info").forEach((el) => {
      if (el.dataset.sel !== String(selected) + modern) { el.innerHTML = html; el.dataset.sel = String(selected) + modern; }
      el.classList.toggle("has-sel", !!w);
    });
    document.querySelectorAll("[data-needs-sel]").forEach((b) => b.classList.toggle("disabled", !w));
    const ig = $("#win-ignore");
    if (ig) {
      setText(ig.querySelector("span"), w && w.ignored ? "\u2298 Stop ignoring" : "\u2298 Ignore");
      ig.classList.toggle("active", !!(w && w.ignored));
    }
  }

  // Only rows that actually changed are replaced - the rest stay exactly as they are.
  function renderRules() {
    const list = $("#rulelist");
    const ignored = S.ignored || [];
    setText($("#count-rules"), String(S.rules.length + ignored.length));
    if (!changed("rules", [modern, S.rules, ignored, modern ? S.windows.map((w) => w.exe) : 0])) return;
    const ruleBadge = (r) => {
      const w = modern && S.windows.find((x) => x.exe && x.exe.toLowerCase() === (r.app || "").toLowerCase());
      return w ? iconBadge(w) : `<span class="badge">${esc((r.app || "?").charAt(0).toUpperCase())}</span>`;
    };
    const rows = S.rules.map((r) =>
      `<div class="row rule">${ruleBadge(r)}` +
      `<span class="row-main"><span class="row-title">${esc(r.app.replace(/\.exe$/i, ""))}</span><span class="row-exe">${esc(r.where)}</span></span>` +
      `<button class="pill${r.lock_mouse ? " on" : ""}" data-lock="${r.index}" title="Lock the mouse to it while you play"><span>Mouse lock</span></button>` +
      `<button class="icon-x" data-forget="${r.index}" title="Forget"><span>&times;</span></button></div>`).concat(
      ignored.map((exe) =>
        `<div class="row rule ignored">${ruleBadge({ app: exe })}` +
        `<span class="row-main"><span class="row-title">${esc(exe.replace(/\.exe$/i, ""))}</span><span class="row-exe">Ignored · left alone, can run full screen</span></span>` +
        `<button class="icon-x" data-unignore="${esc(exe)}" title="Stop ignoring"><span>&times;</span></button></div>`));
    if (!rows.length) {
      if (!list.querySelector(".empty")) list.innerHTML = `<div class="empty">Nothing remembered yet. Select a window and click "Remember selected".</div>`;
      return;
    }
    list.querySelectorAll(".empty").forEach((e) => e.remove());
    const els = [...list.querySelectorAll(".row.rule")];
    rows.forEach((html, i) => {
      const el = els[i];
      if (el && el._html === html) return;
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      const n = tmp.firstChild;
      n._html = html;
      if (el) el.replaceWith(n); else list.appendChild(n);
    });
    els.slice(rows.length).forEach((e) => e.remove());
  }

  // little "equipped" flash on the box a window just went into
  async function placed(promise, zone) {
    const res = await promise;
    if (!res || !res.ok) return;
    const el = document.querySelector(`#stage .zone[data-zone="${zone}"]`);
    if (!el) return;
    el.classList.remove("equipped"); void el.offsetWidth; el.classList.add("equipped");
    setTimeout(() => el.classList.remove("equipped"), 900);
  }

  // ---------------- drag & drop (inside the app only) ----------------
  const ghost = $("#ghost");
  function zoneAt(x, y) {
    const el = document.elementFromPoint(x, y);
    const z = el && el.closest(".zone");
    return z ? z.dataset.zone : null;
  }
  function highlight(zone) {
    document.querySelectorAll("#stage .zone").forEach((z) => z.classList.toggle("hot", z.dataset.zone === zone));
  }
  function beginPress(e, hwnd, from) {
    if (e.button !== 0) return;
    const w = S && S.windows.find((x) => x.hwnd === hwnd);
    press = { x: e.clientX, y: e.clientY, hwnd, from, title: w ? w.title : "window", w };
  }
  document.addEventListener("pointermove", (e) => {
    if (!press) return;
    if (!dragging) {
      if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < 6) return;
      dragging = press;
      ghost.innerHTML = `${press.w ? iconBadge(press.w) : '<span class="badge">?</span>'}<span>${esc(press.title.slice(0, 48))}</span>`;
      ghost.classList.add("show");
      document.body.classList.add("is-dragging");
    }
    ghost.style.transform = `translate(${e.clientX + 16}px, ${e.clientY + 14}px)`;
    const z = e.shiftKey ? null : zoneAt(e.clientX, e.clientY);
    highlight(z);
    document.body.classList.toggle("ctrl-held", e.ctrlKey);
  });
  document.addEventListener("pointerup", async (e) => {
    const d = dragging, p = press;
    press = null; dragging = null;
    ghost.classList.remove("show");
    document.body.classList.remove("is-dragging", "ctrl-held");
    highlight(null);
    if (!d) {
      if (p && p.from === "list") { selected = p.hwnd; if (S) applySelection(); }
      if (pending) { render(pending); pending = null; }
      return;
    }
    const z = e.shiftKey ? null : zoneAt(e.clientX, e.clientY);
    if (pending) { render(pending); pending = null; }
    if (!z) { setStatus("Drag cancelled - drop it on one of the boxes."); return; }
    if (z === d.from) return;
    // In the middle the app decides: games -> GAME (no border), everyday apps -> Middle. Ctrl = always Middle.
    const borderless = z === "center" && !e.ctrlKey ? "auto" : false;
    selected = d.hwnd;
    applySelection();
    await allowFocus();
    placed(api("place", { hwnd: d.hwnd, zone: z, borderless }), z);
  });
  window.addEventListener("blur", () => {
    if (dragging || press) { press = null; dragging = null; ghost.classList.remove("show"); highlight(null); document.body.classList.remove("is-dragging"); }
  });

  $("#winlist").addEventListener("click", (e) => {
    const b = e.target.closest(".mute-btn");
    if (!b) return;
    e.stopPropagation();
    const mute = b.dataset.state !== "muted";
    setMuteButton(b, { muted: mute });               // show it right away
    api("mute", { hwnd: b.dataset.mute, mute });
  });
  $("#winlist").addEventListener("pointerdown", (e) => {
    if (e.target.closest(".mute-btn")) return;      // the speaker is a button, not a drag handle
    const row = e.target.closest(".row");
    if (!row) return;
    e.preventDefault();
    beginPress(e, row.dataset.hwnd, "list");
  });
  $("#stage").addEventListener("pointerdown", (e) => {
    if (e.target.closest(".chip-x")) return;
    const chip = e.target.closest(".chip");
    if (!chip) return;
    e.preventDefault();
    beginPress(e, chip.dataset.hwnd, chip.closest(".zone").dataset.zone);
  });
  $("#stage").addEventListener("click", (e) => {
    const x = e.target.closest(".chip-x");
    if (x) api("restore", { hwnd: x.dataset.restore });
  });

  // ---------------- buttons & settings ----------------
  document.querySelectorAll("[data-place]").forEach((b) => b.addEventListener("click", async () => {
    if (!selected) return setStatus("First click a window in the list (or just drag it onto a box).", true);
    const borderless = b.dataset.borderless === "1";
    await allowFocus();
    placed(api("place", { hwnd: selected, zone: b.dataset.place, borderless }), b.dataset.place);
  }));
  $("#restore").addEventListener("click", () => {
    if (!selected) return setStatus("First click a window in the list.", true);
    api("restore", { hwnd: selected });
  });
  $("#refresh").addEventListener("click", () => api("refresh"));
  $("#search").addEventListener("input", () => S && renderWindows());
  $("#theme").addEventListener("change", (e) => { applyTheme(e.target.value); api("set_option", { key: "theme", value: e.target.value }); });
  $("#monitor").addEventListener("change", (e) => api("set_monitor", { device: e.target.value }));
  $("#preset").addEventListener("change", (e) => api("set_preset", { name: e.target.value }));
  const layoutPick = (sel, key, num) => $(sel).addEventListener("change", (e) =>
    api("set_layout", { key, value: num ? +e.target.value : e.target.value }));
  layoutPick("#game-height", "game_height_pct", true);
  layoutPick("#game-pos", "game_position");
  layoutPick("#game-valign", "game_valign");
  layoutPick("#mon-shape", "monitor_shape");
  layoutPick("#new-game", "new_game_action");
  $("#opt-new-lock").addEventListener("change", (e) => api("set_layout", { key: "new_game_lock", value: e.target.checked }));
  $("#apply-size").addEventListener("click", () => api("set_size", { width: +$("#cw").value, height: +$("#ch").value }));
  $("#show-zones").addEventListener("click", () => api("show_zones"));
  $("#opt-ontop").addEventListener("change", (e) => api("set_option", { key: "game_on_top", value: e.target.checked }));
  $("#opt-auto").addEventListener("change", (e) => api("set_option", { key: "auto_apply", value: e.target.checked }));
  $("#opt-fps").addEventListener("change", (e) => api("set_option", { key: "fps_overlay", value: e.target.checked }));
  $("#opt-detect").addEventListener("change", (e) => api("set_option", { key: "auto_detect_games", value: e.target.checked }));
  $("#opt-startup").addEventListener("change", (e) => api("startup", { on: e.target.checked }));
  $("#desktop-shortcut").addEventListener("click", () => api("desktop_shortcut"));
  $("#restart-admin").addEventListener("click", () => api("restart_admin"));
  const SPOT_LABEL = { left: "Left screen", right: "Right screen" };
  $("#win-remember").addEventListener("click", () => {
    const w = S && selected && S.windows.find((x) => x.hwnd === selected);
    if (!w) return setStatus("First click a window in the list.", true);
    let where, lock;
    if (w.ignored) {          // ignored: remember it as ignored (it stays left alone)
      where = "Ignored (full screen)";
      lock = false;
    } else if (w.zone) {   // remember the spot it's in right now
      where = w.zone === "center" ? (w.borderless ? "GAME" : "Middle") : SPOT_LABEL[w.zone];
      lock = w.zone === "center" && w.borderless ? $("#rule-lock").checked : false;
    } else {        // not placed yet: use the choice from the Remembered section
      where = $("#rule-where").value;
      lock = $("#rule-lock").checked;
    }
    api("add_rule", { hwnd: selected, where, lock_mouse: lock });
  });
  $("#win-ignore").addEventListener("click", () => {
    const w = S && selected && S.windows.find((x) => x.hwnd === selected);
    if (!w) return setStatus("First click a window in the list.", true);
    api("ignore", { hwnd: selected, on: !w.ignored });
  });
  $("#rule-add").addEventListener("click", () => {
    if (!selected) return setStatus("First click a window in the list.", true);
    api("add_rule", { hwnd: selected, where: $("#rule-where").value, lock_mouse: $("#rule-lock").checked });
  });
  $("#rulelist").addEventListener("click", (e) => {
    const u = e.target.closest("[data-unignore]");
    if (u) return api("ignore", { exe: u.dataset.unignore, on: false });
    const f = e.target.closest("[data-forget]");
    if (f) return api("remove_rule", { index: +f.dataset.forget });
    const l = e.target.closest("[data-lock]");
    if (l) return api("toggle_rule_lock", { index: +l.dataset.lock });
  });

  // window buttons (only in our own app window)
  const pv = () => (window.pywebview && window.pywebview.api) || null;
  $("#btn-min").addEventListener("click", () => pv() && pv().minimize());
  $("#btn-max").addEventListener("click", () => pv() && pv().toggle_max());
  $("#btn-close").addEventListener("click", () => pv() && pv().close());
  document.querySelector(".titlebar").addEventListener("dblclick", (e) => {
    if (!e.target.closest("select, button, label") && pv()) pv().toggle_max();
  });

  window.addEventListener("resize", () => S && layoutStage());
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && dragging) { press = null; dragging = null; ghost.classList.remove("show"); highlight(null); document.body.classList.remove("is-dragging"); } });

  document.querySelectorAll("select").forEach(ddEnhance);
  poll();
  setInterval(poll, 1000);
})();
