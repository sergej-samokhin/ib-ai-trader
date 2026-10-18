"use strict";

/* ibtrader dashboard. Vanilla JS. All data is untrusted: only textContent / createElement. */

const TABS = ["overview", "trading", "signals", "audit", "radar"];
const REFRESH_MS = 30000;
const FETCH_TIMEOUT_MS = 15000;
const SVG_NS = "http://www.w3.org/2000/svg";
const REDDIT_PREFIXES = ["https://www.reddit.com/", "https://reddit.com/", "https://old.reddit.com/"];
const REDDIT_HOSTS = ["www.reddit.com", "reddit.com", "old.reddit.com"];

const state = {
  tab: "overview",
  overview: null,
  audit: null,
  auditLimit: 100,
  auditFilter: "",
  radar: null,
  radarDemo: false,
  radarSort: { key: "rank", dir: 1 },
  radarSelected: null,
  lastUpdated: null,
  errors: {},
  refreshing: false,
  refreshQueued: false,
  scanBusy: false,
};

const $ = (id) => document.getElementById(id);

/* ---------- helpers ---------- */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function svg(tag, attrs) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  return n;
}

const usdFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const intFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const dec2Fmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function usd(v) {
  return isNum(v) ? usdFmt.format(v) : "—";
}

function usdNode(v) {
  const s = el("span", isNum(v) ? (v < 0 ? "neg" : v > 0 ? "pos" : "") : "", usd(v));
  return s;
}

function dtFormatter() {
  return new Intl.DateTimeFormat("en-GB", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false, timeZoneName: "short",
  });
}
const dtf = dtFormatter();

function fmtDate(d) {
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return "—";
  return dtf.format(d).replace(",", "");
}
/* trader data uses ISO strings, radar uses epoch seconds */
function fmtIso(s) {
  if (s === null || s === undefined || s === "") return "—";
  return fmtDate(new Date(s));
}
function fmtEpoch(s) {
  return isNum(s) ? fmtDate(new Date(s * 1000)) : "—";
}

function dash(v) {
  return v === null || v === undefined || v === "" ? "—" : String(v);
}

function safeRedditUrl(u) {
  if (typeof u !== "string" || !REDDIT_PREFIXES.some((p) => u.startsWith(p))) return null;
  try {
    const parsed = new URL(u);
    if (parsed.protocol !== "https:" || !REDDIT_HOSTS.includes(parsed.hostname)) return null;
    return parsed.href;
  } catch (e) {
    return null;
  }
}

function safeHttpsUrl(u) {
  if (typeof u !== "string") return null;
  try {
    const parsed = new URL(u);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch (e) {
    return null;
  }
}

function link(href, text) {
  const a = el("a", null, text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

function legsText(legs) {
  if (!Array.isArray(legs) || legs.length === 0) return "—";
  return legs
    .map((l) => {
      if (l === null || typeof l !== "object") return String(l);
      const parts = [l.action, l.ratio, l.right, l.strike, l.expiry]
        .filter((x) => x !== undefined && x !== null && x !== "")
        .map(String);
      return parts.length ? parts.join(" ") : JSON.stringify(l);
    })
    .join(" / ");
}

function statusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "awaiting_approval") return "pill-yellow";
  if (/reject|fail|error|cancel|expire|invalid/.test(s)) return "pill-red";
  if (/fill|done|complete|active|accepted/.test(s)) return "pill-green";
  return "";
}

function pill(text, cls) {
  return el("span", "pill" + (cls ? " " + cls : ""), text);
}

function shadowPill() {
  return pill("shadow", "pill-shadow");
}

/* ---------- table builder ---------- */

/* cols: [{label, num?, nowrap?, cell(row) -> string|Node}] */
function buildTable(cols, rows, rowClass) {
  const wrap = el("div", "table-scroll");
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  for (const c of cols) {
    const th = el("th", c.num ? "num" : "", c.label);
    th.scope = "col";
    hr.append(th);
  }
  thead.append(hr);
  const tbody = el("tbody");
  for (const r of rows) {
    const tr = el("tr", rowClass ? rowClass(r) : "");
    for (const c of cols) {
      const td = el("td", (c.num ? "num " : "") + (c.nowrap ? "nowrap " : "") + (c.cls || ""));
      const v = c.cell(r);
      if (v instanceof Node) td.append(v);
      else td.textContent = v === undefined || v === null ? "—" : String(v);
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

function emptyBox(text) {
  return el("div", "empty", text);
}

function tile(label, valueNode, note, cls) {
  const t = el("div", "tile" + (cls ? " " + cls : ""));
  t.append(el("div", "tile-label", label));
  const v = el("div", "tile-value");
  if (valueNode instanceof Node) v.append(valueNode);
  else v.textContent = String(valueNode);
  t.append(v);
  if (note) t.append(el("div", "tile-note", note));
  return t;
}

/* ---------- fetching ---------- */

async function fetchJson(url, opts) {
  const res = await fetch(url, {
    cache: "no-store",
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    ...(opts || {}),
  });
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    body = null;
  }
  if (!res.ok) {
    const msg = body && typeof body.error === "string" ? body.error : "HTTP " + res.status;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  if (body === null || typeof body !== "object") throw new Error("Unexpected response from server");
  return body;
}

async function loadOverview() {
  state.overview = await fetchJson("/api/overview");
}
async function loadAudit() {
  const d = await fetchJson("/api/audit?limit=" + encodeURIComponent(state.auditLimit));
  if (!Array.isArray(d.events)) throw new Error("Unexpected audit response");
  state.audit = d;
}
async function loadRadar() {
  const demo = state.radarDemo;
  const d = await fetchJson("/api/radar" + (demo ? "?demo=1" : ""));
  if (demo !== state.radarDemo) return; // toggled while loading
  state.radar = d;
}

async function refresh() {
  if (state.refreshing) {
    state.refreshQueued = true;
    return;
  }
  state.refreshing = true;
  $("refresh-button").disabled = true;
  const jobs = [["overview", loadOverview]];
  if (state.tab === "audit") jobs.push(["audit", loadAudit]);
  if (state.tab === "radar") jobs.push(["radar", loadRadar]);
  const results = await Promise.allSettled(jobs.map(([, fn]) => fn()));
  let ok = false;
  results.forEach((r, i) => {
    const name = jobs[i][0];
    if (r.status === "fulfilled") {
      delete state.errors[name];
      ok = true;
    } else {
      state.errors[name] = r.reason && r.reason.message ? r.reason.message : "request failed";
    }
  });
  if (ok) state.lastUpdated = new Date();
  state.refreshing = false;
  $("refresh-button").disabled = false;
  renderAll();
  if (state.refreshQueued) {
    state.refreshQueued = false;
    refresh();
  }
}

/* ---------- rendering: shell ---------- */

function renderShell() {
  const o = state.overview;
  const modeBadge = $("mode-badge");
  const killBadge = $("kill-badge");
  if (o) {
    const live = o.mode === "live";
    modeBadge.textContent = live ? "LIVE" : "PAPER";
    modeBadge.className = "badge " + (live ? "badge-red" : "badge-neutral");
    $("live-banner").hidden = !live;
    $("kill-banner").hidden = !o.kill_switch;
    killBadge.hidden = !o.kill_switch;
    killBadge.textContent = "KILL SWITCH ACTIVE";
    killBadge.className = "badge badge-red";
    $("nodb-banner").hidden = o.db_available !== false;
  } else {
    modeBadge.textContent = "MODE —";
    modeBadge.className = "badge badge-neutral";
  }
  $("last-updated").textContent = state.lastUpdated ? fmtDate(state.lastUpdated) : "—";

  const names = Object.keys(state.errors);
  const banner = $("error-banner");
  if (names.length) {
    const msgs = names.map((n) => n + ": " + state.errors[n]).join(" | ");
    const keep = state.lastUpdated ? " Showing last data from " + fmtDate(state.lastUpdated) + "." : " No data loaded yet.";
    banner.textContent = "Refresh failed (" + msgs + ")." + keep + " Retrying automatically.";
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

/* ---------- rendering: overview ---------- */

function renderOverview() {
  const o = state.overview;
  const pnl = $("pnl-tiles"), shadow = $("shadow-tiles"), counts = $("count-tiles");
  if (!o) {
    for (const n of [pnl, shadow, counts]) n.replaceChildren();
    $("paused-box").replaceChildren(emptyBox("Loading…"));
    $("scorecard-box").replaceChildren(emptyBox("Loading…"));
    return;
  }
  const p = o.pnl || {};
  pnl.replaceChildren(
    tile("P&L today", usdNode(p.today)),
    tile("P&L month", usdNode(p.month))
  );
  shadow.replaceChildren(
    tile("shadow (simulated) today", usdNode(p.today_shadow), "not real money", "tile-shadow"),
    tile("shadow (simulated) month", usdNode(p.month_shadow), "not real money", "tile-shadow")
  );
  const len = (a) => (Array.isArray(a) ? a.length : 0);
  counts.replaceChildren(
    tile("Open positions", String(len(o.open_positions))),
    tile("Open intents", String(len(o.open_intents))),
    tile("Active signals", String(len(o.active_signals))),
    tile("Paused producers", String(len(o.paused_producers)))
  );

  const paused = o.paused_producers || [];
  $("paused-box").replaceChildren(
    paused.length
      ? buildTable(
          [
            { label: "Producer", cell: (r) => dash(r.producer) },
            { label: "Paused at", nowrap: true, cell: (r) => fmtIso(r.paused_at) },
            { label: "Reason", cell: (r) => dash(r.reason) },
          ],
          paused
        )
      : emptyBox(o.db_available === false ? "No trading database yet (run ibtrader once)" : "No paused producers.")
  );

  $("scorecard-box").replaceChildren(renderScorecard(o));
}

function scoreCell(v) {
  if (v === null || v === undefined) return "—";
  if (isNum(v)) {
    const s = Number.isInteger(v) ? intFmt.format(v) : dec2Fmt.format(v);
    return el("span", v < 0 ? "neg" : "", s);
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function renderScorecard(o) {
  const rows = Array.isArray(o.scorecard) ? o.scorecard : [];
  if (!rows.length) {
    return emptyBox(o.db_available === false ? "No trading database yet (run ibtrader once)" : "No scorecard data yet.");
  }
  const keys = [];
  for (const r of rows) for (const k of Object.keys(r || {})) if (!keys.includes(k)) keys.push(k);
  keys.sort((a, b) => (a === "producer" ? -1 : b === "producer" ? 1 : 0));
  const cols = keys.map((k) => ({
    label: k.replace(/_/g, " "),
    num: rows.some((r) => isNum(r[k])),
    cell: (r) => scoreCell(r[k]),
  }));
  return buildTable(cols, rows);
}

/* ---------- rendering: trading ---------- */

function renderTrading() {
  const o = state.overview;
  const pos = $("positions-box"), intents = $("intents-box");
  if (!o) {
    pos.replaceChildren(emptyBox("Loading…"));
    intents.replaceChildren(emptyBox("Loading…"));
    return;
  }
  const nodb = o.db_available === false;
  const positions = o.open_positions || [];
  pos.replaceChildren(
    positions.length
      ? buildTable(
          [
            { label: "Underlying", cell: (r) => cellWithShadow(dash(r.underlying), r.shadow) },
            { label: "Strategy", cell: (r) => dash(r.strategy) },
            { label: "Qty", num: true, cell: (r) => dash(r.quantity) },
            { label: "Entry price (per share)", num: true, cell: (r) => usd(r.entry_price) },
            { label: "Legs", cls: "legs", cell: (r) => legsText(r.legs) },
            { label: "Opened", nowrap: true, cell: (r) => fmtIso(r.opened_at) },
            { label: "Producer", cell: (r) => dash(r.producer) },
            { label: "Signal", nowrap: true, cell: (r) => dash(r.signal_id) + (r.version != null ? " v" + r.version : "") },
          ],
          positions
        )
      : emptyBox(nodb ? "No trading database yet (run ibtrader once)" : "No open positions.")
  );

  const list = o.open_intents || [];
  intents.replaceChildren(
    list.length
      ? buildTable(
          [
            { label: "Status", cell: (r) => cellWithShadow(pill(dash(r.status), statusClass(r.status)), r.shadow) },
            { label: "Kind", cell: (r) => dash(r.kind) },
            { label: "Underlying", cell: (r) => dash(r.underlying) },
            { label: "Strategy", cell: (r) => dash(r.strategy) },
            { label: "Qty", num: true, cell: (r) => dash(r.quantity) },
            { label: "Limit", num: true, cell: (r) => usd(r.limit_price) },
            { label: "Max price", num: true, cell: (r) => usd(r.max_price) },
            { label: "Reason", cell: (r) => dash(r.reason) },
            { label: "Created", nowrap: true, cell: (r) => fmtIso(r.created_at) },
            { label: "Approval deadline", nowrap: true, cell: (r) => fmtIso(r.approval_deadline) },
            { label: "Producer", cell: (r) => dash(r.producer) },
            { label: "Signal", nowrap: true, cell: (r) => dash(r.signal_id) + (r.version != null ? " v" + r.version : "") },
          ],
          list,
          (r) => (r.status === "awaiting_approval" ? "row-attention" : "")
        )
      : emptyBox(nodb ? "No trading database yet (run ibtrader once)" : "No open intents.")
  );
}

function cellWithShadow(content, shadow) {
  if (!shadow) return content;
  const span = el("span");
  if (content instanceof Node) span.append(content);
  else span.append(document.createTextNode(String(content)));
  span.append(document.createTextNode(" "), shadowPill());
  return span;
}

/* ---------- rendering: signals ---------- */

function renderSignals() {
  const o = state.overview;
  const box = $("signals-box");
  if (!o) {
    box.replaceChildren(emptyBox("Loading…"));
    return;
  }
  const sigs = o.active_signals || [];
  box.replaceChildren(
    sigs.length
      ? buildTable(
          [
            { label: "Producer", cell: (r) => dash(r.producer) },
            { label: "Signal", nowrap: true, cell: (r) => dash(r.signal_id) },
            { label: "Version", num: true, cell: (r) => dash(r.version) },
            { label: "Kind", cell: (r) => dash(r.kind) },
            { label: "Underlying", cell: (r) => dash(r.underlying) },
            { label: "Strategy", cell: (r) => dash(r.strategy) },
            { label: "Status", cell: (r) => pill(dash(r.status), statusClass(r.status)) },
            { label: "Valid until", nowrap: true, cell: (r) => fmtIso(r.valid_until) },
            { label: "Received", nowrap: true, cell: (r) => fmtIso(r.received_at) },
          ],
          sigs
        )
      : emptyBox(o.db_available === false ? "No trading database yet (run ibtrader once)" : "No active signals.")
  );
}

/* ---------- rendering: audit ---------- */

const JSON_PREVIEW = 120;

function compactJson(v) {
  try {
    return JSON.stringify(v === undefined ? null : v);
  } catch (e) {
    return "[unserializable]";
  }
}

function jsonCell(data) {
  const full = compactJson(data);
  const code = el("code", "json");
  if (full.length <= JSON_PREVIEW) {
    code.textContent = full;
    return code;
  }
  const short = full.slice(0, JSON_PREVIEW) + "…";
  code.textContent = short;
  code.tabIndex = 0;
  code.setAttribute("role", "button");
  code.title = "Click to expand";
  let open = false;
  const toggle = () => {
    open = !open;
    code.textContent = open ? full : short;
    code.classList.toggle("expanded", open);
    code.title = open ? "Click to collapse" : "Click to expand";
  };
  code.addEventListener("click", toggle);
  code.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });
  return code;
}

function renderAudit() {
  const box = $("audit-box");
  if (!state.audit) {
    box.replaceChildren(emptyBox(state.errors.audit ? "Audit events unavailable." : "Loading…"));
    $("audit-count").textContent = "";
    return;
  }
  const q = state.auditFilter.trim().toLowerCase();
  const all = state.audit.events;
  const rows = q
    ? all.filter((e) =>
        [e.ts, e.event, e.producer, e.signal_id, e.intent_id, compactJson(e.data)]
          .map((x) => (x === null || x === undefined ? "" : String(x)))
          .join(" ")
          .toLowerCase()
          .includes(q)
      )
    : all;
  $("audit-count").textContent = rows.length + " of " + all.length + " events";
  if (!rows.length) {
    box.replaceChildren(emptyBox(all.length ? "No events match the filter." : "No audit events."));
    return;
  }
  box.replaceChildren(
    buildTable(
      [
        { label: "Time", nowrap: true, cell: (r) => fmtIso(r.ts) },
        { label: "Event", nowrap: true, cell: (r) => dash(r.event) },
        { label: "Producer", cell: (r) => dash(r.producer) },
        { label: "Signal", nowrap: true, cell: (r) => dash(r.signal_id) + (r.version != null ? " v" + r.version : "") },
        { label: "Intent", nowrap: true, cell: (r) => dash(r.intent_id) },
        { label: "Data", cell: (r) => jsonCell(r.data) },
      ],
      rows
    )
  );
}

/* ---------- rendering: radar ---------- */

const RADAR_MODES = { arctic_shift: "Arctic Shift", reddit: "Reddit", rss: "RSS", demo: "Demo" };

function sortValue(t, key) {
  switch (key) {
    case "ticker": return t.ticker || "";
    case "name": return (t.company && t.company.name) || "";
    case "sentiment": return t.sentiment;
    case "authors": return t.unique_authors;
    default: return t[key];
  }
}

function sortedTickers(list) {
  const { key, dir } = state.radarSort;
  const rows = list.slice();
  rows.sort((a, b) => {
    const va = sortValue(a, key), vb = sortValue(b, key);
    const na = va === null || va === undefined, nb = vb === null || vb === undefined;
    if (na || nb) return na === nb ? 0 : na ? 1 : -1; // nulls always last
    if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * dir;
    return (va - vb) * dir;
  });
  return rows;
}

function sparkline(values) {
  const s = svg("svg", { viewBox: "0 0 96 28", class: "spark", role: "img" });
  s.setAttribute("aria-label", "Mentions per hour, last 24 hours");
  const v = Array.isArray(values) ? values.filter(isNum) : [];
  if (v.length < 2) return s;
  const max = Math.max(1, ...v);
  const step = 94 / (v.length - 1);
  const pts = v.map((x, i) => [1 + i * step, 26 - (x / max) * 24]);
  const line = pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  s.append(
    svg("polygon", { class: "area", points: "1,26 " + line + " " + pts[pts.length - 1][0].toFixed(1) + ",26" }),
    svg("polyline", { class: "line", points: line })
  );
  return s;
}

function sentimentCell(t) {
  const b = Math.max(0, Number(t.bullish) || 0), r = Math.max(0, Number(t.bearish) || 0), n = Math.max(0, Number(t.neutral) || 0);
  const total = b + r + n;
  const wrap = el("div", "sent");
  const label = ["bullish", "bearish", "neutral"].includes(t.sentiment_label) ? t.sentiment_label : "neutral";
  const head = el("div", "sent-label " + label, label);
  head.append(el("span", "sent-counts", b + "/" + r + "/" + n));
  wrap.append(head);
  const bar = el("div", "sent-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", "bullish " + b + ", bearish " + r + ", neutral " + n);
  if (total) {
    for (const [cls, v] of [["b", b], ["r", r], ["n", n]]) {
      const seg = el("span", cls);
      seg.style.width = ((v / total) * 100).toFixed(1) + "%";
      bar.append(seg);
    }
  }
  wrap.append(bar);
  return wrap;
}

function growthCell(t) {
  if (!isNum(t.growth)) return el("span", null, "—");
  const g = t.growth;
  return el("span", g < 0 ? "neg" : g > 0 ? "pos" : "", (g > 0 ? "+" : "") + intFmt.format(g) + "%");
}

function fixed(v, d) {
  return isNum(v) ? v.toFixed(d) : "—";
}

const RADAR_COLS = [
  { key: "ticker", label: "Ticker" },
  { key: "name", label: "Company" },
  { key: "mentions_24h", label: "Mentions 24h", num: true },
  { key: "mentions_1h", label: "1h", num: true },
  { key: "growth", label: "Growth", num: true },
  { key: "sentiment", label: "Sentiment" },
  { key: "attention_z", label: "Attention z", num: true },
  { key: "attention_accel", label: "Accel", num: true },
  { key: "authors", label: "Authors / breadth", num: true },
  { key: "rank", label: "Rank", num: true },
  { key: "hot_score", label: "Hot score", num: true },
  { key: null, label: "24h" },
];

function renderRadar() {
  const box = $("radar-box");
  const d = state.radar;
  const demoBanner = $("synthetic-banner");
  if (!d) {
    demoBanner.hidden = !state.radarDemo;
    box.replaceChildren(emptyBox(state.errors.radar ? "WSB Radar data unavailable." : "Loading…"));
    updateScanButton();
    return;
  }
  const cov = d.coverage || {};
  const st = d.status || {};
  const synthetic = d.mode === "demo" || st.state === "demo" || cov.synthetic === true || state.radarDemo;
  demoBanner.hidden = !synthetic;
  updateScanButton();

  const frag = document.createDocumentFragment();

  if (d.enabled === false && !synthetic) {
    const n = el("div", "banner banner-info");
    n.textContent = "WSB Radar is disabled. Set the environment variable RADAR_ENABLED=true and restart ibtrader to enable it, or switch on Demo data to preview the layout.";
    frag.append(n);
    box.replaceChildren(frag);
    return;
  }

  // status line
  const sl = el("div", "radar-status");
  const item = (k, v) => {
    const s = el("span", "item");
    s.append(el("span", "k", k));
    if (v instanceof Node) s.append(v);
    else s.append(document.createTextNode(String(v)));
    return s;
  };
  const stateCls = { ok: "pill-green", error: "pill-red", partial: "pill-yellow", demo: "pill-yellow" }[st.state] || "";
  sl.append(
    item("Provider", RADAR_MODES[d.mode] || dash(d.mode)),
    item("State", pill(dash(st.state) + (st.running ? " (running)" : ""), stateCls)),
    item("Last success", fmtEpoch(st.last_success)),
    item("Next run", fmtEpoch(st.next_run))
  );
  if (isNum(st.interval_seconds)) sl.append(item("Interval", Math.round(st.interval_seconds / 60) + " min"));
  frag.append(sl);
  if (st.stale) {
    frag.append(el("div", "banner banner-warn", "Data is stale: the last successful scan is older than expected."));
  }
  if (st.error) {
    frag.append(el("div", "banner banner-error", "Collector error: " + st.error));
  }

  // coverage
  const cv = el("div");
  if (cov.note) cv.append(el("p", "note", "Coverage: " + cov.note));
  if (cov.partial) cv.append(el("p", "note", "Partial coverage: some discussions may be missing."));
  if (Array.isArray(cov.warnings) && cov.warnings.length) {
    const ul = el("ul", "warn-list");
    for (const w of cov.warnings) ul.append(el("li", null, w));
    cv.append(ul);
  }
  if (cov.warmup) cv.append(el("p", "note", cov.warmup_note || "Building a baseline: growth and attention metrics need more history."));
  frag.append(cv);

  // metrics
  const m = d.metrics || {};
  const tiles = el("div", "tiles");
  tiles.append(
    tile("Items 24h", isNum(m.items_24h) ? intFmt.format(m.items_24h) : "—"),
    tile("Posts 24h", isNum(m.posts_24h) ? intFmt.format(m.posts_24h) : "—"),
    tile("Comments 24h", isNum(m.comments_24h) ? intFmt.format(m.comments_24h) : "—"),
    tile("Tickers", isNum(m.tickers) ? intFmt.format(m.tickers) : "—")
  );
  frag.append(tiles);

  // table
  const tickers = Array.isArray(d.tickers) ? d.tickers : [];
  frag.append(el("h2", "section-title", "Tickers"));
  if (!tickers.length) {
    frag.append(emptyBox("No tickers in the current sample yet."));
  } else {
    frag.append(radarTable(tickers));
  }
  if (d.method) frag.append(el("p", "note", "Method: " + d.method));

  // detail
  const sel = tickers.find((t) => t.ticker === state.radarSelected);
  const detail = el("div");
  detail.id = "radar-detail";
  if (sel) detail.append(radarDetail(sel));
  frag.append(detail);

  box.replaceChildren(frag);
}

function radarTable(tickers) {
  const wrap = el("div", "table-scroll");
  const table = el("table");
  const hr = el("tr");
  const { key: sk, dir } = state.radarSort;
  for (const c of RADAR_COLS) {
    const th = el("th", (c.num ? "num " : "") + (c.key ? "sortable" : ""));
    th.scope = "col";
    if (c.key) {
      const btn = el("button", null, c.label);
      btn.type = "button";
      const arrow = el("span", "arrow", sk === c.key ? (dir === 1 ? "▲" : "▼") : "");
      btn.append(" ", arrow);
      btn.addEventListener("click", () => {
        if (state.radarSort.key === c.key) state.radarSort.dir = -state.radarSort.dir;
        else state.radarSort = { key: c.key, dir: c.key === "ticker" || c.key === "name" || c.key === "rank" ? 1 : -1 };
        renderRadar();
      });
      th.append(btn);
      th.setAttribute("aria-sort", sk === c.key ? (dir === 1 ? "ascending" : "descending") : "none");
    } else {
      th.textContent = c.label;
    }
    hr.append(th);
  }
  const thead = el("thead");
  thead.append(hr);
  const tbody = el("tbody");
  for (const t of sortedTickers(tickers)) {
    const tr = el("tr", "ticker-row" + (t.ticker === state.radarSelected ? " selected" : ""));
    tr.addEventListener("click", () => selectTicker(t.ticker));

    const c1 = el("td", "nowrap");
    const btn = el("button", "ticker-btn", t.ticker);
    btn.type = "button";
    btn.setAttribute("aria-expanded", String(t.ticker === state.radarSelected));
    c1.append(btn);
    if (t.is_new) c1.append(el("span", "pill pill-new", "NEW"));
    if (t.is_etf) c1.append(el("span", "pill pill-etf", "ETF"));

    const authors = isNum(t.unique_authors) || isNum(t.breadth)
      ? (isNum(t.unique_authors) ? intFmt.format(t.unique_authors) : "—") + " / " + fixed(t.breadth, 2)
      : "—";
    const cells = [
      c1,
      el("td", null, (t.company && t.company.name) || "—"),
      el("td", "num", isNum(t.mentions_24h) ? intFmt.format(t.mentions_24h) : "—"),
      el("td", "num", isNum(t.mentions_1h) ? intFmt.format(t.mentions_1h) : "—"),
      el("td", "num"),
      el("td", null),
      el("td", "num", fixed(t.attention_z, 2)),
      el("td", "num", fixed(t.attention_accel, 2)),
      el("td", "num", authors),
      el("td", "num", dash(t.rank)),
      el("td", "num", fixed(t.hot_score, 2)),
      el("td", null),
    ];
    cells[4].append(growthCell(t));
    cells[5].append(sentimentCell(t));
    cells[11].append(sparkline(t.sparkline));
    tr.append(...cells);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

function selectTicker(sym) {
  state.radarSelected = state.radarSelected === sym ? null : sym;
  renderRadar();
  const d = $("radar-detail");
  if (state.radarSelected && d && d.scrollIntoView) d.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function dl(pairs) {
  const d = el("dl");
  for (const [k, v] of pairs) {
    d.append(el("dt", null, k));
    const dd = el("dd");
    if (v instanceof Node) dd.append(v);
    else dd.textContent = v === null || v === undefined || v === "" ? "—" : String(v);
    d.append(dd);
  }
  return d;
}

function radarDetail(t) {
  const co = t.company || {};
  const root = el("section", "detail");
  root.setAttribute("aria-label", "Details for " + t.ticker);

  const head = el("div", "detail-head");
  head.append(el("h3", null, t.ticker + (co.name ? " · " + co.name : "")));
  const close = el("button", "btn", "Close");
  close.type = "button";
  close.addEventListener("click", () => selectTicker(t.ticker));
  head.append(close);
  root.append(head);

  const grid = el("div", "detail-grid");

  const left = el("div");
  left.append(el("h4", null, "Company card"));
  const site = safeHttpsUrl(co.website);
  left.append(
    dl([
      ["Name", co.name],
      ["Sector", co.sector],
      ["Description", co.description],
      ["Website", site ? link(site, site) : co.website ? "(not a https link)" : "—"],
      ["As of", co.as_of],
    ])
  );
  left.append(el("p", "note", "static reference, not research"));

  const mid = el("div");
  mid.append(el("h4", null, "Sentiment breakdown"));
  mid.append(sentimentCell(t));
  mid.append(
    dl([
      ["Bullish", t.bullish],
      ["Bearish", t.bearish],
      ["Neutral", t.neutral],
      ["Uncertain", t.uncertain_count],
      ["Score (-1..1)", fixed(t.sentiment, 2)],
      ["First seen", fmtEpoch(t.first_seen)],
      ["Posts / comments", dash(t.posts) + " / " + dash(t.comments)],
      ["History", isNum(t.history_days) ? t.history_days + " days" : "—"],
    ])
  );
  grid.append(left, mid);
  root.append(grid);

  root.append(el("h4", null, "Evidence"));
  const ev = Array.isArray(t.evidence) ? t.evidence.slice(0, 5) : [];
  if (!ev.length) {
    root.append(el("p", "note", "No evidence excerpts available."));
  } else {
    const ul = el("ul", "evidence");
    for (const e of ev) {
      const li = el("li");
      li.append(el("div", "text", e && e.text !== undefined ? e.text : ""));
      const meta = el("div", "meta");
      meta.append(el("span", null, e && e.kind === "comment" ? "comment" : "post"));
      meta.append(el("span", null, isNum(e && e.score) ? "score " + e.score : "score —"));
      const href = e ? safeRedditUrl(e.url) : null;
      if (href) meta.append(link(href, "Open on Reddit"));
      else if (e && e.url) meta.append(el("span", null, "link withheld (not a Reddit URL)"));
      li.append(meta);
      ul.append(li);
    }
    root.append(ul);
  }
  return root;
}

/* ---------- scan button ---------- */

function updateScanButton() {
  const btn = $("scan-button");
  const d = state.radar;
  const st = (d && d.status) || {};
  const blocked = state.radarDemo || !d || d.enabled === false || d.mode === "demo" || st.running || state.scanBusy;
  btn.disabled = !!blocked;
  btn.title = state.radarDemo ? "Scanning is unavailable for demo data" : "";
}

async function scanNow() {
  if (state.scanBusy || state.radarDemo) return;
  const msg = $("scan-message");
  state.scanBusy = true;
  updateScanButton();
  msg.className = "muted";
  msg.textContent = "Requesting scan…";
  try {
    const res = await fetch("/api/radar/scan", { method: "POST", signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      const m = body && typeof body.error === "string" ? body.error : "HTTP " + res.status;
      msg.className = "neg";
      msg.textContent = "Scan not started (" + res.status + "): " + m;
    } else {
      msg.className = "muted";
      msg.textContent = "Scan accepted. Results appear when collection finishes.";
      setTimeout(refresh, 2000);
    }
  } catch (e) {
    msg.className = "neg";
    msg.textContent = "Scan request failed: " + (e && e.message ? e.message : "network error");
  } finally {
    state.scanBusy = false;
    updateScanButton();
  }
}

/* ---------- routing and wiring ---------- */

function currentTabFromHash() {
  const h = location.hash.replace(/^#/, "");
  return TABS.includes(h) ? h : "overview";
}

function showTab(tab) {
  state.tab = tab;
  for (const t of TABS) $("tab-" + t).hidden = t !== tab;
  document.querySelectorAll("#tabs .tab").forEach((a) => {
    const on = a.dataset.tab === tab;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function renderAll() {
  renderShell();
  renderOverview();
  renderTrading();
  renderSignals();
  renderAudit();
  renderRadar();
}

function onRoute() {
  showTab(currentTabFromHash());
  renderAll();
  refresh();
}

function init() {
  $("audit-limit").value = String(state.auditLimit);
  $("audit-filter").addEventListener("input", (e) => {
    state.auditFilter = e.target.value;
    renderAudit();
  });
  $("audit-limit").addEventListener("change", (e) => {
    state.auditLimit = Number(e.target.value) || 100;
    state.audit = null;
    renderAudit();
    refresh();
  });
  $("demo-toggle").addEventListener("change", (e) => {
    state.radarDemo = e.target.checked;
    state.radar = null;
    state.radarSelected = null;
    $("scan-message").textContent = "";
    renderRadar();
    refresh();
  });
  $("scan-button").addEventListener("click", scanNow);
  $("refresh-button").addEventListener("click", refresh);
  window.addEventListener("hashchange", onRoute);

  setInterval(() => {
    if (!document.hidden) refresh();
  }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });

  onRoute();
}

init();
