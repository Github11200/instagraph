(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => (n == null ? "–" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "k" : n.toLocaleString());

  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || r.statusText));
    return j;
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

  const ls = (() => { try { return window.localStorage; } catch { return null; } })();
  const load = (k, d) => { try { const v = ls && ls.getItem("ig:" + k); return v == null ? d : JSON.parse(v); } catch { return d; } };
  const save = (k, v) => { try { ls && ls.setItem("ig:" + k, JSON.stringify(v)); } catch {} };

  // ------------------------------------------------------------ colors
  const PALETTE = ["#529cca", "#e255a1", "#4dab9a", "#ffa344", "#9a6dd7", "#ff7369", "#d4a72c", "#5fb3d9", "#937264", "#7fb550", "#c678dd", "#e5865c"];
  // degree of connection: you, 1st, 2nd, 3rd, 4th, 5th+, unknown
  const DEGREES = [
    { key: "0", label: "You", color: "#ff9f1c" },
    { key: "1", label: "1st", color: "#4f8ff7" },
    { key: "2", label: "2nd", color: "#2ec4a6" },
    { key: "3", label: "3rd", color: "#a66cf0" },
    { key: "4", label: "4th", color: "#f0559a" },
    { key: "5", label: "5th+", color: "#c9a227" },
    { key: "u", label: "Not linked yet", color: "#8a8a8a" },
  ];
  const degKey = (d) => (d >= 50 ? "u" : d >= 5 ? "5" : String(d));
  const DEG_COLOR = Object.fromEntries(DEGREES.map((x) => [x.key, x.color]));

  const view = {
    minDeg: load("minDeg", 2),
    maxNodes: load("maxNodes2", 2000),
    colorBy: load("colorBy2", "depth"),
    mutualOnly: load("mutualOnly", false),
    hiddenDeg: new Set(load("hiddenDeg", [])),
  };

  const S = {
    raw: null,
    nodes: new Map(),        // id -> node object (kept stable so positions persist)
    adj: new Map(),          // id -> Set(neighbour ids)  (drawn links only)
    hover: null,
    selected: null,
    focusId: null,           // node whose full neighbourhood is loaded
    hi: new Set(),
    path: null,              // { ids: [...], set: Set, links: Set("a|b") }
    inflight: new Set(),
    cluster: new Map(),
    clusterColor: new Map(),
    seedId: null,
    version: "",
    fitted: false,
    running: false,
    status: null,
  };

  // ------------------------------------------------------------ theme
  let T = {};
  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    T = { node: v("--g-node"), label: v("--g-label"), link: v("--g-link"), linkMutual: v("--g-link-mutual"), linkHi: v("--g-link-hi"), ring: v("--g-ring"), accent: v("--accent"), bg: v("--bg"), path: v("--g-path") };
  }
  function applyThemePref() {
    const pref = load("theme", "auto");
    if (pref === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", pref);
    readTheme();
    redraw();
  }

  // ------------------------------------------------------------ graph
  const radius = (n) => {
    if (n.id === S.seedId) return 10;
    return Math.min(14, 2.6 + Math.sqrt(n.deg || 1) * 0.6) + (n.c ? 1.2 : 0);
  };
  const nodeColor = (n) => {
    if (view.colorBy === "depth") return DEG_COLOR[degKey(n.d)];
    if (n.id === S.seedId) return DEG_COLOR["0"];
    return S.clusterColor.get(S.cluster.get(n.id)) || T.node;
  };
  const idOf = (x) => (typeof x === "object" ? x.id : x);
  const linkKey = (a, b) => (a < b ? a + "|" + b : b + "|" + a);

  const el = $("#graph");
  const Graph = new ForceGraph(el)
    .backgroundColor("rgba(0,0,0,0)")
    .nodeId("id")
    .nodeRelSize(1)
    .nodeVal((n) => radius(n) ** 2)
    .nodeLabel(() => "")
    .nodeCanvasObjectMode(() => "replace")
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint((n, color, ctx) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(n.x, n.y, radius(n) + 2, 0, 2 * Math.PI);
      ctx.fill();
    })
    .linkColor(linkColor)
    .linkWidth(linkWidth)
    .onNodeHover((n) => { S.hover = n || null; el.style.cursor = n ? "pointer" : ""; updateHighlight(); })
    .onNodeClick((n) => select(n))
    .onNodeRightClick((n) => expand(n))
    .onBackgroundClick(() => select(null))
    .d3AlphaDecay(0.045)
    .d3VelocityDecay(0.4)
    .cooldownTicks(220)
    .onEngineStop(() => {
      if (S.fitPath) { S.fitPath = false; fitPath(); return; }
      if (!S.fitted && S.nodes.size) { Graph.zoomToFit(600, 60); S.fitted = true; }
    });

  Graph.d3Force("charge").strength((n) => (n.c ? -90 : -22)).distanceMax(500).theta(0.95);
  Graph.d3Force("link").distance((l) => (l.m ? 30 : 45));

  function linkColor(l) {
    const a = idOf(l.source), b = idOf(l.target);
    if (S.path && !S.hover) return S.path.links.has(linkKey(a, b)) ? T.path : "rgba(0,0,0,0)";
    if (S.hi.size) {
      const f = (S.hover || S.selected).id;
      return a === f || b === f ? T.linkHi : "rgba(0,0,0,0)";
    }
    return l.m ? T.linkMutual : T.link;
  }
  function linkWidth(l) {
    if (S.path && !S.hover && S.path.links.has(linkKey(idOf(l.source), idOf(l.target)))) return 3.5;
    return S.hi.size ? 1.1 : l.m ? 0.7 : 0.35;
  }

  function drawNode(n, ctx, scale) {
    const r = radius(n);
    const onPath = S.path && S.path.set.has(n.id);
    const dim = S.hover ? !S.hi.has(n.id) : S.path ? !onPath : S.hi.size > 0 && !S.hi.has(n.id);
    ctx.globalAlpha = dim ? 0.08 : 1;
    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = nodeColor(n);
    ctx.fill();
    if (S.inflight.has(n.id)) {
      ctx.setLineDash([2 / scale, 2 / scale]);
      ctx.lineWidth = 2 / scale;
      ctx.strokeStyle = T.ring;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r + 3 / scale + 1, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (n === S.selected || n.id === S.seedId || onPath) {
      ctx.lineWidth = 2 / scale;
      ctx.strokeStyle = T.ring;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
      ctx.stroke();
    }
    if (!dim) {
      const focus = n === S.hover || n === S.selected || n.id === S.seedId || onPath;
      const show = focus || (S.hi.size && scale > 0.9) || r * scale > 9 || scale > 3;
      if (show && n.u) {
        const fs = (focus ? 12.5 : 11) / scale;
        ctx.font = `${focus ? 600 : 400} ${fs}px ui-sans-serif, -apple-system, "Segoe UI", Inter, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = T.label;
        ctx.fillText(n.u, n.x, n.y + r + 2.5 / scale);
      }
    }
    ctx.globalAlpha = 1;
  }

  function redraw() {
    // re-setting an accessor makes force-graph repaint without reheating the simulation
    Graph.linkWidth(linkWidth);
  }

  function updateHighlight() {
    S.hi.clear();
    const f = S.hover || S.selected;
    if (f) {
      S.hi.add(f.id);
      for (const id of S.adj.get(f.id) || []) S.hi.add(id);
    }
    redraw();
  }

  function computeClusters(nodes, adj) {
    const label = new Map();
    for (const n of nodes) label.set(n.id, S.cluster.get(n.id) ?? n.id);
    const order = nodes.map((n) => n.id);
    for (let it = 0; it < 12; it++) {
      for (let i = order.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; [order[i], order[j]] = [order[j], order[i]]; }
      let changed = 0;
      for (const id of order) {
        const nb = adj.get(id);
        if (!nb || !nb.size) continue;
        const cnt = new Map();
        let best = label.get(id), bc = 0;
        for (const m of nb) {
          const L = label.get(m);
          const c = (cnt.get(L) || 0) + 1;
          cnt.set(L, c);
          if (c > bc) { bc = c; best = L; }
        }
        if (best !== label.get(id)) { label.set(id, best); changed++; }
      }
      if (changed < order.length * 0.005) break;
    }
    const size = new Map();
    for (const L of label.values()) size.set(L, (size.get(L) || 0) + 1);
    const big = [...size.entries()].filter(([, s]) => s >= 4).sort((a, b) => b[1] - a[1]).slice(0, PALETTE.length);
    const colors = new Map(), used = new Set();
    for (const [L] of big) { const c = S.clusterColor.get(L); if (c && !used.has(c)) { colors.set(L, c); used.add(c); } }
    for (const [L] of big) { if (colors.has(L)) continue; const c = PALETTE.find((p) => !used.has(p)); if (c) { colors.set(L, c); used.add(c); } }
    S.cluster = label;
    S.clusterColor = colors;
  }

  function rebuild() {
    const data = S.raw;
    if (!data) return;
    const keep = new Map();
    for (const n of data.nodes) {
      const ex = S.nodes.get(n.id);
      keep.set(n.id, ex ? Object.assign(ex, n) : { ...n });
      if (n.d === 0) S.seedId = n.id;
    }
    const visible = (n) => !view.hiddenDeg.has(degKey(n.d)) || n.id === S.seedId || n === S.selected || (S.path && S.path.set.has(n.id));
    let links = data.links.filter((l) => {
      const a = keep.get(l.source), b = keep.get(l.target);
      return a && b && visible(a) && visible(b);
    });
    if (view.mutualOnly) links = links.filter((l) => l.m || (S.path && S.path.links.has(linkKey(l.source, l.target))));

    const adj = new Map();
    for (const id of keep.keys()) adj.set(id, new Set());
    for (const l of links) { adj.get(l.source).add(l.target); adj.get(l.target).add(l.source); }

    let nodes = [...keep.values()].filter(visible);
    if (view.mutualOnly) nodes = nodes.filter((n) => adj.get(n.id).size || n.id === S.seedId);

    // drop new nodes next to an already-placed neighbour instead of at the origin
    for (const n of nodes) {
      if (n.x !== undefined) continue;
      for (const nb of adj.get(n.id)) {
        const m = keep.get(nb);
        if (m && m.x !== undefined) { n.x = m.x + (Math.random() - 0.5) * 50; n.y = m.y + (Math.random() - 0.5) * 50; break; }
      }
    }

    S.nodes = keep;
    S.adj = adj;
    if (view.colorBy === "cluster") computeClusters(nodes, adj);
    Graph.graphData({ nodes, links: links.map((l) => ({ source: l.source, target: l.target, m: l.m })) });

    if (S.selected) S.selected = keep.get(S.selected.id) || null;
    if (S.hover && !keep.has(S.hover.id)) S.hover = null;
    updateHighlight();
    renderDetail();
    renderLegend();
    searchListStale = true;

    $("#empty").classList.toggle("hidden", nodes.length > 0);
    const hiddenCount = Math.max(0, (data.total || 0) - nodes.length);
    $("#counter").textContent = nodes.length
      ? `${fmt(nodes.length)} people · ${fmt(links.length)} links shown` + (hiddenCount ? ` · ${fmt(hiddenCount)} more found but hidden (lower “Min. links” or click someone to see all their connections)` : "")
      : "";
  }

  let graphBusy = false, graphAgain = false;
  async function refreshGraph(force = false) {
    if (graphBusy) { graphAgain = graphAgain || force; return; }
    graphBusy = true;
    try {
      const q = new URLSearchParams({ min_degree: view.minDeg, max_nodes: view.maxNodes });
      if (S.focusId) q.set("focus", S.focusId);
      if (S.path) q.set("extra", S.path.ids.join(","));
      const g = await api(`/api/graph?${q}`);
      if (force || g.version !== S.version) {
        S.version = g.version;
        S.raw = g;
        rebuild();
      }
    } catch (e) {
      console.warn(e);
    } finally {
      graphBusy = false;
      if (graphAgain) { graphAgain = false; refreshGraph(true); }
    }
  }

  // ------------------------------------------------------------ legend
  function renderLegend() {
    const counts = {};
    for (const n of S.nodes.values()) { const k = degKey(n.d); counts[k] = (counts[k] || 0) + 1; }
    const totals = (S.status && S.status.crawl.by_depth) || {};
    const tot = {};
    for (const [d, c] of Object.entries(totals)) { const k = degKey(+d); tot[k] = (tot[k] || 0) + c; }
    const box = $("#legend");
    if (view.colorBy !== "depth") {
      box.innerHTML = `<div class="lg-title">Colors: friend groups (auto-detected)</div>`;
      return;
    }
    box.innerHTML = `<div class="lg-title">Degree of connection</div>` + DEGREES.filter((x) => counts[x.key] || tot[x.key]).map((x) => {
      const off = view.hiddenDeg.has(x.key);
      const shown = counts[x.key] || 0, all = tot[x.key] || shown;
      return `<button class="lg-row ${off ? "off" : ""}" data-k="${x.key}" title="${off ? "Show" : "Hide"} ${esc(x.label)}">
        <span class="sw" style="background:${x.color}"></span><span class="lg-l">${esc(x.label)}</span>
        <span class="lg-c">${fmt(shown)}${all > shown ? ` <span class="lg-of">/ ${fmt(all)}</span>` : ""}</span></button>`;
    }).join("");
    box.querySelectorAll(".lg-row").forEach((b) => (b.onclick = () => {
      const k = b.dataset.k;
      if (k === "0") return;
      view.hiddenDeg.has(k) ? view.hiddenDeg.delete(k) : view.hiddenDeg.add(k);
      save("hiddenDeg", [...view.hiddenDeg]);
      rebuild();
    }));
  }

  // ------------------------------------------------------------ selection / detail
  function select(n) {
    const prevFocus = S.focusId;
    S.selected = n || null;
    S.focusId = n ? n.id : null;
    updateHighlight();
    renderDetail();
    if (S.focusId !== prevFocus) refreshGraph(true);
  }

  function focusNode(n, zoom = 2.5) {
    if (!n) return;
    if (n.x !== undefined) { Graph.centerAt(n.x, n.y, 600); Graph.zoom(zoom, 600); }
    select(n);
  }

  async function expand(n) {
    if (!n) return;
    try {
      const r = await post("/api/expand", { pk: n.id });
      if (!r.queued) toast(n.c ? `@${n.u} is already expanded` : `@${n.u} is already being expanded`);
      else toast(`Expanding @${n.u}…`);
      pollStatus();
    } catch (e) { toast(e.message, true); }
  }

  function renderDetail() {
    const box = $("#detail");
    const n = S.selected;
    if (!n || !S.raw) { box.classList.add("hidden"); return; }
    let followsHere = 0, followedHere = 0, mutual = 0;
    for (const l of S.raw.links) {
      if (l.source !== n.id && l.target !== n.id) continue;
      if (l.m) mutual++;
      else if (l.source === n.id) followsHere++;
      else followedHere++;
    }
    const nbrs = [...(S.adj.get(n.id) || [])].map((id) => S.nodes.get(id)).filter(Boolean).sort((a, b) => (b.c - a.c) || (b.deg - a.deg));
    const dk = degKey(n.d);
    const degLabel = DEGREES.find((x) => x.key === dk).label;
    const badges = [`<span class="badge" style="background:${DEG_COLOR[dk]}22;color:${DEG_COLOR[dk]}">${n.id === S.seedId ? "You" : dk === "u" ? "Not linked yet" : degLabel + " degree"}</span>`];
    if (n.c) badges.push(`<span class="badge accent">${n.s === "partial" ? "Expanded (followers pending)" : "Expanded"}</span>`);
    else if (n.s === "skipped") badges.push(`<span class="badge">Can't expand (private?)</span>`);
    if (n.p) badges.push(`<span class="badge">Private</span>`);
    if (n.v) badges.push(`<span class="badge">Verified</span>`);

    const busy = S.inflight.has(n.id) || (S.status && S.status.crawl.expanding.includes(n.u));
    const expandBtn = n.s === "done"
      ? `<button disabled>Expanded ✓</button>`
      : busy ? `<button disabled>Expanding…</button>`
      : `<button class="primary" data-act="expand">${n.s === "partial" ? "Fetch followers" : "Expand connections"}</button>`;
    const pathBtn = n.id === S.seedId ? "" : `<button data-act="path">Path to me</button>`;

    box.innerHTML = `
      <button class="icon-btn d-close" title="Close" aria-label="Close">✕</button>
      <div class="d-head">
        <img class="avatar" alt="" src="/api/avatar/${esc(n.id)}" onerror="this.style.visibility='hidden'">
        <div class="d-name">
          <a href="https://www.instagram.com/${esc(n.u)}/" target="_blank" rel="noopener">@${esc(n.u)}</a>
          <div>${esc(n.n) || "&nbsp;"}</div>
        </div>
      </div>
      <div class="badges">${badges.join("")}</div>
      <div class="row d-actions">${expandBtn}${pathBtn}</div>
      <div class="d-stats">
        <div class="stat"><div class="v">${fmt(mutual)}</div><div class="k">mutual</div></div>
        <div class="stat"><div class="v">${fmt(followsHere)}</div><div class="k">they follow</div></div>
        <div class="stat"><div class="v">${fmt(followedHere)}</div><div class="k">follow them</div></div>
      </div>
      <div class="d-section">Connected in your graph (${fmt(nbrs.length)})${n.c ? "" : " — expand to load all of their connections"}</div>
      <ul class="people">${nbrs.slice(0, 100).map((m) => `
        <li data-id="${esc(m.id)}"><span class="sw" style="background:${nodeColor(m)}"></span><span class="nm">@${esc(m.u)}</span>${m.c ? `<span class="dg" title="expanded">●</span>` : ""}<span class="dg">${fmt(m.deg)}</span></li>`).join("")}
      </ul>`;
    box.classList.remove("hidden");
    box.querySelector(".d-close").onclick = () => select(null);
    const ex = box.querySelector('[data-act="expand"]');
    if (ex) ex.onclick = () => expand(n);
    const pb = box.querySelector('[data-act="path"]');
    if (pb) pb.onclick = () => { $("#path-input").value = n.u; findPath(); };
    box.querySelectorAll(".people li").forEach((li) => (li.onclick = () => focusNode(S.nodes.get(li.dataset.id), Graph.zoom())));
  }

  // ------------------------------------------------------------ path finder
  async function findPath() {
    const q = $("#path-input").value.trim();
    if (!q) return;
    const out = $("#path-result");
    const btn = $("#path-btn");
    btn.disabled = true;
    out.innerHTML = `<div class="muted">Searching… (if they aren't in your graph yet, their lists are fetched first)</div>`;
    try {
      const r = await post("/api/path", { username: q });
      if (!r.found) {
        out.innerHTML = `<div class="message warn">${esc(r.message)}</div>`;
        clearPath(false);
        return;
      }
      const ids = r.path.map((p) => p.pk);
      const links = new Set();
      for (let i = 1; i < ids.length; i++) links.add(linkKey(ids[i - 1], ids[i]));
      S.path = { ids, set: new Set(ids), links };
      const steps = r.path.map((p, i) =>
        `<li data-id="${esc(p.pk)}"><span class="sw" style="background:${DEG_COLOR[degKey(i)]}"></span>` +
        `<span class="nm">${i === 0 ? "You" : "@" + esc(p.username)}</span><span class="dg">${i ? ordinal(i) : ""}</span></li>` +
        (i < r.path.length - 1 ? `<li class="p-rel">${relText(r.path[i + 1].rel)}</li>` : "")
      ).join("");
      out.innerHTML = `<div class="p-head"><b>${r.hops} ${r.hops === 1 ? "hop" : "hops"}</b> to @${esc(r.path[r.path.length - 1].username)}<button class="link-btn" id="path-clear">Clear</button></div><ol class="people path">${steps}</ol>`;
      out.querySelector("#path-clear").onclick = () => clearPath(true);
      out.querySelectorAll("li[data-id]").forEach((li) => (li.onclick = () => focusNode(S.nodes.get(li.dataset.id), Graph.zoom())));
      S.fitPath = true;
      await refreshGraph(true);
      // the layout is still settling around the newly added path nodes, so re-frame a few times
      for (const ms of [500, 1800, 3500]) setTimeout(fitPath, ms);
    } catch (e) {
      out.innerHTML = `<div class="message error">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  }
  function fitPath() {
    if (S.path) Graph.zoomToFit(600, 140, (n) => S.path && S.path.set.has(n.id));
  }
  const ordinal = (i) => (i === 1 ? "1st" : i === 2 ? "2nd" : i === 3 ? "3rd" : i + "th");
  // rel describes previous -> this node
  const relText = (rel) => (rel === "mutual" ? "↕ follow each other" : rel === "follows" ? "↓ follows" : "↑ followed by");
  function clearPath(clearUi) {
    S.path = null;
    if (clearUi) $("#path-result").innerHTML = "";
    redraw();
    refreshGraph(true);
  }
  $("#path-form").onsubmit = (e) => { e.preventDefault(); findPath(); };

  // ------------------------------------------------------------ toolbar
  const minDeg = $("#minDeg"), maxNodes = $("#maxNodes"), colorBy = $("#colorBy"), mutualOnly = $("#mutualOnly");
  minDeg.value = view.minDeg; $("#minDegVal").textContent = view.minDeg;
  maxNodes.value = String(view.maxNodes); colorBy.value = view.colorBy; mutualOnly.checked = view.mutualOnly;

  let minDegTimer;
  minDeg.oninput = () => {
    $("#minDegVal").textContent = minDeg.value;
    clearTimeout(minDegTimer);
    minDegTimer = setTimeout(() => { view.minDeg = +minDeg.value; save("minDeg", view.minDeg); refreshGraph(true); }, 250);
  };
  maxNodes.onchange = () => { view.maxNodes = +maxNodes.value; save("maxNodes2", view.maxNodes); refreshGraph(true); };
  colorBy.onchange = () => { view.colorBy = colorBy.value; save("colorBy2", view.colorBy); if (view.colorBy === "cluster") rebuild(); else { redraw(); renderDetail(); renderLegend(); } };
  mutualOnly.onchange = () => { view.mutualOnly = mutualOnly.checked; save("mutualOnly", view.mutualOnly); rebuild(); };
  $("#fit").onclick = () => Graph.zoomToFit(600, 60);
  $("#theme").onclick = () => {
    const order = ["auto", "light", "dark"];
    save("theme", order[(order.indexOf(load("theme", "auto")) + 1) % order.length]);
    applyThemePref();
  };
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readTheme(); redraw(); });

  // search: datalist is only rebuilt when the box is focused (it's slow with thousands of options)
  let searchListStale = true;
  const search = $("#search");
  search.addEventListener("focus", () => {
    if (!searchListStale) return;
    searchListStale = false;
    $("#usernames").innerHTML = [...S.nodes.values()].filter((n) => n.u).sort((a, b) => b.deg - a.deg).slice(0, 1500).map((n) => `<option value="${esc(n.u)}">`).join("");
  });
  function doSearch() {
    const q = search.value.trim().replace(/^@/, "").toLowerCase();
    if (!q) return;
    const all = [...S.nodes.values()].filter((n) => n.u);
    const n = all.find((x) => x.u.toLowerCase() === q) ||
      all.filter((x) => x.u.toLowerCase().includes(q) || (x.n || "").toLowerCase().includes(q)).sort((a, b) => b.deg - a.deg)[0];
    if (n) focusNode(n);
    else { $("#path-input").value = q; toast(`@${q} isn't in the graph — use “Path to me” to look them up`); }
  }
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });

  let toastTimer;
  function toast(text, err = false) {
    const t = $("#toast");
    t.textContent = text;
    t.className = "toast show" + (err ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.className = "toast"), 3200);
  }

  // ------------------------------------------------------------ control panel
  const FIELDS = ["depth", "max_accounts", "max_per_list", "concurrency", "delay_ms"];
  const CHECKS = ["include_following", "include_followers", "skip_verified"];
  const savedOpts = load("opts2", null);
  if (savedOpts) {
    for (const f of FIELDS) if (savedOpts[f] != null) $("#" + f).value = savedOpts[f];
    for (const f of CHECKS) if (savedOpts[f] != null) $("#" + f).checked = savedOpts[f];
  }
  $("#url").value = load("url", "");
  if (load("collapsed", false)) $("#control").classList.add("collapsed");
  $("#collapse").onclick = () => { const c = $("#control").classList.toggle("collapsed"); save("collapsed", c); $("#collapse").textContent = c ? "+" : "–"; };
  $("#collapse").textContent = $("#control").classList.contains("collapsed") ? "+" : "–";

  function setMessage(text, kind = "") {
    const m = $("#message");
    m.className = "message " + kind;
    m.textContent = text;
  }

  $("#crawl-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = { url: $("#url").value.trim() };
    for (const f of FIELDS) body[f] = +$("#" + f).value;
    for (const f of CHECKS) body[f] = $("#" + f).checked;
    save("url", body.url);
    save("opts2", body);
    $("#start").disabled = true;
    try {
      await post("/api/crawl/start", body);
      S.fitted = false;
      pollStatus();
    } catch (err) {
      setMessage(err.message, "error");
      $("#start").disabled = false;
    }
  };
  $("#stop").onclick = async () => { $("#stop").disabled = true; await post("/api/crawl/stop").catch(() => {}); pollStatus(); };
  $("#reset").onclick = async () => {
    if (!confirm("Delete the entire crawled graph? This can't be undone.")) return;
    try {
      await post("/api/reset");
      S.nodes.clear(); S.cluster.clear(); S.clusterColor.clear(); S.selected = null; S.focusId = null; S.seedId = null; S.path = null; S.fitted = false;
      $("#path-result").innerHTML = "";
      await refreshGraph(true);
    } catch (err) { setMessage(err.message, "error"); }
  };

  function renderSession(s) {
    const box = $("#session");
    let html;
    if (s.logging_in) {
      html = `<span class="dot busy"></span><span>${esc(s.status || "Logging in…")}</span>`;
    } else if (s.logged_in) {
      html = `<span class="dot on"></span><span>Logged in${s.username ? ` as <b>@${esc(s.username)}</b>` : ""}</span><span class="spacer"></span><button class="link-btn" data-act="logout">Log out</button>`;
    } else {
      const how = s.has_credentials ? "Uses IG_USERNAME / IG_PASSWORD from .env" : s.has_sessionid ? "Uses IG_SESSIONID from .env" : "Opens a browser window so you can log in (or set credentials in .env)";
      html = `<span class="dot"></span><span>Not logged in</span><span class="spacer"></span><button class="link-btn" data-act="login">Log in</button>
              <span class="hint">${esc(how)}</span>`;
    }
    if (s.error && !s.logged_in) html += `<span class="err">${esc(s.error)}</span>`;
    if (box.dataset.html !== html) {
      box.innerHTML = html;
      box.dataset.html = html;
      const b = box.querySelector("[data-act]");
      if (b) b.onclick = async () => { await post(`/api/${b.dataset.act}`).catch((e) => setMessage(e.message, "error")); pollStatus(); };
    }
  }

  function renderCrawl(c) {
    $("#start").disabled = c.running;
    $("#stop").disabled = !c.running;
    $("#reset").disabled = c.running;
    if (!$("#url").value && c.seed) $("#url").value = `https://www.instagram.com/${c.seed}/`;

    const auto = c.mode === "auto";
    const pct = auto && c.opts.max_accounts ? Math.min(100, (c.crawled_run / c.opts.max_accounts) * 100) : 0;
    const prog = $("#progress");
    const indet = c.running && (!auto || c.crawled_run === 0);
    prog.classList.toggle("indeterminate", indet);
    prog.firstElementChild.style.width = indet ? "" : c.running || c.crawled_run ? pct + "%" : "0";

    const stat = (v, k) => `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`;
    $("#stats").innerHTML =
      stat(fmt(c.done), "expanded") +
      stat(fmt(c.queued), "in queue") +
      stat(c.running ? c.rps.toFixed(1) : fmt(c.requests), c.running ? "requests / s" : "requests") +
      stat(fmt(c.users), "people found") +
      stat(fmt(c.edges), "follow links") +
      stat(fmt((c.by_depth && c.by_depth["1"]) || 0), "1st degree");

    if (c.error) setMessage(c.error, "error");
    else if (c.message) setMessage(c.message, "warn");
    else if (c.running) {
      const who = c.inflight.slice(0, 3).map((u) => "@" + u).join(", ");
      const more = c.expanding.length ? ` · next: ${c.expanding.slice(0, 3).map((u) => "@" + u).join(", ")}` : "";
      setMessage(c.phase === "crawling" ? (who ? `Fetching ${who}${more}` : "Working…") : c.phase.charAt(0).toUpperCase() + c.phase.slice(1) + "…");
    } else if (c.phase === "finished" && auto) setMessage(`Finished. Click anyone in the graph and press “Expand connections” to go deeper (or right-click them).`);
    else if (c.phase === "stopped") setMessage("Stopped. Start again to continue where you left off.");
    else if (c.done) setMessage("Click anyone in the graph and press “Expand connections” to go deeper (or right-click them).");
    else setMessage("");

    const lastLog = c.log.length ? c.log[c.log.length - 1].t : 0;
    const logEl = $("#log");
    if (logEl.dataset.last !== String(lastLog)) {
      logEl.dataset.last = String(lastLog);
      logEl.innerHTML = c.log.slice().reverse().map((l) => `<li>${new Date(l.t * 1000).toLocaleTimeString()} ${esc(l.msg)}</li>`).join("");
    }
  }

  let wasRunning = false, lastInflight = "", lastExpanding = "";
  async function pollStatus() {
    try {
      const s = await api("/api/status");
      S.status = s;
      renderSession(s.session);
      renderCrawl(s.crawl);
      S.running = s.crawl.running;
      const inf = s.crawl.inflight_pks.join(","), exq = s.crawl.expanding.join(",");
      if (inf !== lastInflight || exq !== lastExpanding) {
        lastInflight = inf; lastExpanding = exq;
        S.inflight = new Set(s.crawl.inflight_pks);
        redraw();
        renderDetail();
      }
      if (wasRunning && !S.running) refreshGraph(true);
      wasRunning = S.running;
    } catch {
      setMessage("Can't reach the Instagraph server — is it still running?", "error");
    }
  }

  // ------------------------------------------------------------ boot
  window.addEventListener("resize", () => Graph.width(innerWidth).height(innerHeight));
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if (S.path) clearPath(true); else select(null); }
  });
  window.instagraph = { Graph, state: S }; // handy for poking at things from the devtools console
  applyThemePref();
  pollStatus().then(() => refreshGraph(true));
  setInterval(pollStatus, 1000);
  setInterval(() => { if (S.running) refreshGraph(); }, 3000);
})();
