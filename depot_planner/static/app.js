/* 補給地点シミュレーター GUI
 *
 * 状態は state.scenario(scenarios/*.json と同じ形)に集約し、
 * 一覧・地図・フォームはすべてそこから描き直す。
 * 実行は /api/runs にジョブを投げ、進捗をポーリングする。
 */
"use strict";

// ---------------------------------------------------------------------------
// 定数・状態
// ---------------------------------------------------------------------------
const DARK = window.matchMedia("(prefers-color-scheme: dark)").matches;
// 採用拠点の色(カテゴリ色。固定順で割り当て、9 件目以降は灰色 + 文字ラベルで区別)
const SERIES = DARK
  ? ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
  : ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const OTHER = "#898781";
const STATUS_LABEL = { candidate: "候補", required: "必須", excluded: "除外" };
const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

const state = {
  config: null,
  scenario: null,
  file: null,          // 保存先ファイル名(未保存なら null)
  dirty: false,
  selected: null,      // {kind, id}
  job: null,           // 実行中ジョブ {id, timer}
  result: null,        // 直近の結果
  resultJob: null,     // 結果を出したジョブ id(ダウンロード用)
  resultStale: false,  // 結果の後に条件を変えたか
  showResult: true,
  vehicle: null,       // 地図で強調する車両番号(ルート最適化の結果)
};
let map = null;
const markers = { targets: new Map(), candidates: new Map() };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx".replace(/x/g, () => ((Math.random() * 16) | 0).toString(16)));
const fmt = (v) => (Math.round(v * 10) / 10).toFixed(1);

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : text || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), ms);
}

// ---------------------------------------------------------------------------
// モーダル(ブラウザ標準の prompt/confirm は使わない)
// ---------------------------------------------------------------------------
function modal({ title, body, buttons, onOpen, validate }) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-bg"><div class="modal" role="dialog" aria-modal="true">
      <h2>${esc(title)}</h2><div class="body">${body}</div><div class="err"></div>
      <div class="actions">${buttons.map((b, i) => `<button data-i="${i}" class="${b.primary ? "primary" : ""}">${esc(b.label)}</button>`).join("")}</div>
    </div></div>`;
    const box = $(".modal", root);
    const close = (v) => { root.innerHTML = ""; resolve(v); };
    $$("button[data-i]", box).forEach((btn) => btn.addEventListener("click", async () => {
      const b = buttons[+btn.dataset.i];
      if (b.value !== null && validate) {
        const err = await validate(b.value, box);
        if (err) { $(".err", box).textContent = err; return; }
      }
      close(b.value === undefined ? null : b.value);
    }));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close(null);
      if (e.key === "Enter" && e.target.tagName === "INPUT") {
        const p = buttons.findIndex((b) => b.primary);
        if (p >= 0) $(`button[data-i="${p}"]`, box).click();
      }
    });
    if (onOpen) onOpen(box);
    const first = $("input, textarea, select", box) || $("button.primary", box);
    if (first) first.focus();
  });
}

// ---------------------------------------------------------------------------
// トークン
// ---------------------------------------------------------------------------
async function askToken(message) {
  let saved = null;
  await modal({
    title: "Mapbox アクセストークンの設定",
    body: `<p>${esc(message || ".env に Mapbox のアクセストークンがありません。")}</p>
      <p><a href="https://account.mapbox.com/access-tokens/" target="_blank" rel="noopener">account.mapbox.com</a> で発行した
      <code>pk.</code> から始まるトークンを入力してください。検証後、プロジェクトの <code>.env</code> に保存します。</p>
      <input id="token-input" placeholder="pk.eyJ..." spellcheck="false">`,
    buttons: [{ label: "保存して続ける", value: "save", primary: true }],
    validate: async (_, box) => {
      const token = $("#token-input", box).value.trim();
      if (!token) return "トークンを入力してください";
      try {
        const r = await api("/api/token", { method: "POST", body: { token } });
        saved = r.token;
        return null;
      } catch (e) { return e.message; }
    },
  });
  return saved;
}

// ---------------------------------------------------------------------------
// シナリオ操作
// ---------------------------------------------------------------------------
function markDirty(affectsResult = true) {
  state.dirty = true;
  $("#dirty-mark").hidden = false;
  if (affectsResult && state.result && !state.resultStale) {
    state.resultStale = true;
    renderResults();
  }
  scheduleValidate();
}

function setScenario(sc, file) {
  state.scenario = sc;
  state.file = file;
  state.dirty = false;
  state.selected = null;
  state.result = null;
  state.resultJob = null;
  state.resultStale = false;
  $("#dirty-mark").hidden = true;
  renderAll();
  fitAll();
}

function nextId(prefix, items) {
  const used = new Set(items.map((x) => x.id));
  for (let n = 1; ; n++) {
    const id = `${prefix}${String(n).padStart(3, "0")}`;
    if (!used.has(id)) return id;
  }
}

function addPoint(kind, place) {
  const items = state.scenario[kind];
  const dup = items.find((x) => Math.abs(x.lat - place.lat) < 1e-6 && Math.abs(x.lon - place.lon) < 1e-6);
  if (dup) { toast(`既に登録済みです: ${dup.name}`); select(kind, dup.id, true); return; }
  const item = {
    id: nextId(kind === "targets" ? "T" : "C", items),
    name: place.name || `${place.lat.toFixed(5)}, ${place.lon.toFixed(5)}`,
    lat: place.lat, lon: place.lon, address: place.address || "",
    mapbox_id: place.mapbox_id || null, note: "",
  };
  if (kind === "targets") item.enabled = true;
  else { item.group = ""; item.status = "candidate"; }
  items.push(item);
  markDirty();
  renderList(kind);
  renderMarkers();
  select(kind, item.id, true);
  toast(`${kind === "targets" ? "目的地" : "補給地点"}に追加: ${item.name}`);
}

function removePoint(kind, id) {
  state.scenario[kind] = state.scenario[kind].filter((x) => x.id !== id);
  if (state.selected && state.selected.id === id) state.selected = null;
  markDirty();
  renderList(kind);
  renderMarkers();
}

function select(kind, id, fly = false) {
  state.selected = { kind, id };
  $$(".pitem.selected").forEach((el) => el.classList.remove("selected"));
  const li = $(`#list-${kind} [data-id="${CSS.escape(id)}"]`);
  if (li) { li.classList.add("selected"); li.scrollIntoView({ block: "nearest" }); }
  for (const k of ["targets", "candidates"]) {
    markers[k].forEach((m, mid) => m.getElement().classList.toggle("sel", k === kind && mid === id));
  }
  if (fly && map) {
    const p = state.scenario[kind].find((x) => x.id === id);
    if (p) map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(map.getZoom(), 12) });
  }
}

// ---------------------------------------------------------------------------
// 検索ボックス(suggest → retrieve を同じ session で呼ぶ)
// ---------------------------------------------------------------------------
function createSearch(root) {
  const kind = root.dataset.kind;
  root.innerHTML = `<input type="search" placeholder="${kind === "targets" ? "目的地" : "補給地点"}を検索（施設名・住所）" aria-autocomplete="list">
    <ul class="suggest" hidden></ul>`;
  const input = $("input", root);
  const list = $("ul", root);
  let session = uuid();
  let items = [];
  let active = -1;
  let timer = null;
  let seq = 0;

  const close = () => { list.hidden = true; active = -1; };
  const paint = () => $$("li", list).forEach((li, i) => li.classList.toggle("active", i === active));

  async function query() {
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    const my = ++seq;
    const params = new URLSearchParams({ q, session });
    if (map) { const c = map.getCenter(); params.set("lat", c.lat); params.set("lon", c.lng); }
    try {
      const res = await api(`/api/search/suggest?${params}`);
      if (my !== seq) return;
      items = res;
      active = res.length ? 0 : -1;
      list.innerHTML = res.length
        ? res.map((s, i) => `<li data-i="${i}"><div class="s-name">${esc(s.name)}</div>
            <div class="s-sub">${esc(s.address)}${s.category ? " ・ " + esc(s.category) : ""}${s.distance_m != null ? ` ・ ${(s.distance_m / 1000).toFixed(1)}km` : ""}</div></li>`).join("")
        : `<li class="s-empty">見つかりませんでした</li>`;
      list.hidden = false;
      paint();
    } catch (e) {
      list.innerHTML = `<li class="s-empty">${esc(e.message)}</li>`;
      list.hidden = false;
    }
  }

  async function choose(i) {
    const s = items[i];
    if (!s) return;
    close();
    try {
      const place = await api(`/api/search/retrieve?${new URLSearchParams({ id: s.mapbox_id, session })}`);
      if (!place.name) place.name = s.name;
      // retrieve の住所はローマ字混じりになることがあるので、候補一覧の住所を優先する
      if (s.address) place.address = s.address;
      addPoint(kind, place);
      input.value = "";
    } catch (e) { toast(`取得に失敗しました: ${e.message}`); }
    session = uuid();   // 1 件選んだらセッションを切り替える
  }

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(query, 280); });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); paint(); e.preventDefault(); }
    else if (e.key === "Enter") { if (active >= 0) choose(active); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); choose(+li.dataset.i); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("focus", () => { if (items.length && input.value.trim().length >= 2) { list.hidden = false; paint(); } });
}

// ---------------------------------------------------------------------------
// 一覧
// ---------------------------------------------------------------------------
function renderList(kind) {
  const ol = $(`#list-${kind}`);
  const items = state.scenario[kind];
  $(`#count-${kind}`).textContent = items.length;
  if (!items.length) {
    ol.innerHTML = `<li class="empty">まだありません。上の検索ボックスか地図のクリックで追加してください。</li>`;
    return;
  }
  ol.innerHTML = items.map((p, i) => {
    const addr = p.address || `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`;
    if (kind === "targets") {
      return `<li class="pitem ${p.enabled ? "" : "off"}" data-id="${esc(p.id)}">
        <span class="num" title="地図で表示">${i + 1}</span>
        <input class="pname" data-f="name" value="${esc(p.name)}" aria-label="名称">
        <span class="pctl"><label class="en" title="計算に含める"><input type="checkbox" data-f="enabled" ${p.enabled ? "checked" : ""}>対象</label>
          <button class="icon" data-act="del" title="削除" aria-label="削除">×</button></span>
        <span class="paddr" title="${esc(addr)}">${esc(addr)}</span>
      </li>`;
    }
    return `<li class="pitem ${p.status === "excluded" ? "off" : ""}" data-id="${esc(p.id)}">
      <span class="num sq ${p.status}" title="地図で表示">${i + 1}</span>
      <input class="pname" data-f="name" value="${esc(p.name)}" aria-label="名称">
      <span class="pctl"><button class="icon" data-act="del" title="削除" aria-label="削除">×</button></span>
      <span class="paddr" title="${esc(addr)}">${esc(addr)}</span>
      <span class="pextra">
        <input data-f="group" value="${esc(p.group)}" placeholder="区分（例: 国交省）" list="group-options" aria-label="区分">
        <select data-f="status" aria-label="状態">${Object.entries(STATUS_LABEL).map(([v, l]) => `<option value="${v}" ${p.status === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      </span>
      <span class="pnote"><input data-f="note" value="${esc(p.note)}" placeholder="メモ（集合駅・レンタカーなど。報告書に載ります）" aria-label="メモ"></span>
    </li>`;
  }).join("");
  if (kind === "candidates") {
    const groups = [...new Set(items.map((c) => c.group).filter(Boolean))];
    $("#group-options").innerHTML = groups.map((g) => `<option value="${esc(g)}">`).join("");
  }
  if (state.selected && state.selected.kind === kind) {
    const li = $(`[data-id="${CSS.escape(state.selected.id)}"]`, ol);
    if (li) li.classList.add("selected");
  }
}

function bindList(kind) {
  const ol = $(`#list-${kind}`);
  const find = (el) => {
    const li = el.closest(".pitem");
    return li ? state.scenario[kind].find((x) => x.id === li.dataset.id) : null;
  };
  ol.addEventListener("click", (e) => {
    const p = find(e.target);
    if (!p) return;
    if (e.target.closest('[data-act="del"]')) { removePoint(kind, p.id); return; }
    if (e.target.closest(".num") || e.target.classList.contains("paddr")) select(kind, p.id, true);
  });
  ol.addEventListener("focusin", (e) => { const p = find(e.target); if (p) select(kind, p.id); });
  ol.addEventListener("change", (e) => {
    const p = find(e.target);
    const f = e.target.dataset.f;
    if (!p || !f) return;
    p[f] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    // 名称・メモの変更は計算結果に影響しない
    markDirty(!["name", "note", "group"].includes(f));
    if (f === "enabled" || f === "status") { renderList(kind); }
    renderMarkers();
  });
}

// ---------------------------------------------------------------------------
// 条件フォーム
// ---------------------------------------------------------------------------
function renderParams() {
  const f = $("#params-form");
  const sc = state.scenario, p = sc.params;
  f.name.value = sc.name;
  f.description.value = sc.description || "";
  f.num_depots.value = p.num_depots;
  f.objective.value = p.objective;
  f.service_min.value = p.service_min;
  f.reload_min.value = p.reload_min;
  f.profile.value = p.profile;
  f.provider.value = p.provider;
  f.sens_enabled.checked = !!p.sensitivity.enabled;
  f.sens_min.value = p.sensitivity.min;
  f.sens_max.value = p.sensitivity.max;
  f.max_exact_combinations.value = p.max_exact_combinations;
  f.rt_enabled.checked = !!p.routing.enabled;
  f.rt_capacity.value = p.routing.vehicle_capacity;
  f.rt_max_deliveries.value = p.routing.max_deliveries ?? "";
  f.rt_time_limit.value = p.routing.time_limit_sec;
  syncRoutingInputs();
}

function syncRoutingInputs() {
  const f = $("#params-form");
  for (const n of ["rt_capacity", "rt_max_deliveries", "rt_time_limit"]) f[n].disabled = !f.rt_enabled.checked;
}

function readParams() {
  const f = $("#params-form");
  const sc = state.scenario, p = sc.params;
  const num = (v, d) => (v === "" || isNaN(+v) ? d : +v);
  sc.name = f.name.value.trim() || "無題";
  sc.description = f.description.value;
  p.num_depots = Math.round(num(f.num_depots.value, p.num_depots));
  p.objective = f.objective.value;
  p.service_min = num(f.service_min.value, 0);
  p.reload_min = num(f.reload_min.value, 0);
  p.profile = f.profile.value;
  p.provider = f.provider.value;
  p.sensitivity = { enabled: f.sens_enabled.checked, min: Math.round(num(f.sens_min.value, 1)), max: Math.round(num(f.sens_max.value, 10)) };
  p.max_exact_combinations = Math.round(num(f.max_exact_combinations.value, 5000000));
  p.routing = {
    enabled: f.rt_enabled.checked,
    vehicle_capacity: Math.round(num(f.rt_capacity.value, 1)),
    max_deliveries: f.rt_max_deliveries.value === "" ? null : Math.round(num(f.rt_max_deliveries.value, 1)),
    time_limit_sec: Math.round(num(f.rt_time_limit.value, 30)),
  };
  syncRoutingInputs();
}

function scheduleValidate() {
  clearTimeout(scheduleValidate._t);
  scheduleValidate._t = setTimeout(async () => {
    try {
      const { errors } = await api("/api/validate", { method: "POST", body: state.scenario });
      $("#validation").innerHTML = errors.map((e) => `<li>${esc(e)}</li>`).join("");
    } catch { /* 検証失敗は実行時に表示される */ }
  }, 300);
}

// ---------------------------------------------------------------------------
// 地図
// ---------------------------------------------------------------------------
function initMap(token) {
  mapboxgl.accessToken = token;
  map = new mapboxgl.Map({
    container: "map",
    style: DARK ? "mapbox://styles/mapbox/dark-v11" : "mapbox://styles/mapbox/streets-v12",
    center: [139.55, 36.05], zoom: 8.5, language: "ja",
  });
  map.addControl(new mapboxgl.NavigationControl(), "top-right");
  map.addControl(new mapboxgl.ScaleControl(), "bottom-right");
  map.on("load", () => {
    map.addSource("links", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "links", type: "line", source: "links",
      paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.75 },
      layout: { "line-cap": "round" },
    });
    map.addSource("routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "routes", type: "line", source: "routes",
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["case", ["get", "sel"], 5, 2.5],
        "line-opacity": ["case", ["get", "dim"], 0.12, 0.85],
      },
      layout: { "line-cap": "round", "line-join": "round", "line-sort-key": ["case", ["get", "sel"], 1, 0] },
    });
    renderMarkers();
    fitAll();
  });
  map.on("click", onMapClick);
}

let popup = null;
async function onMapClick(e) {
  const { lng, lat } = e.lngLat;
  if (popup) popup.remove();
  popup = new mapboxgl.Popup({ closeOnClick: true, maxWidth: "300px" })
    .setLngLat([lng, lat]).setHTML(`<div class="muted">地点情報を取得中…</div>`).addTo(map);
  let place = { name: "", address: "", lat: +lat.toFixed(7), lon: +lng.toFixed(7) };
  try { place = await api(`/api/search/reverse?lat=${lat}&lon=${lng}`); } catch { /* 名称なしで続行 */ }
  if (!place.name) place.name = place.address || `${place.lat.toFixed(5)}, ${place.lon.toFixed(5)}`;
  const el = document.createElement("div");
  el.innerHTML = `<div><b>${esc(place.name)}</b></div>
    <div class="muted" style="font-size:12px">${esc(place.address && place.address !== place.name ? place.address : "")}</div>
    <div class="muted" style="font-size:11px">${place.lat.toFixed(6)}, ${place.lon.toFixed(6)}</div>
    <div class="popup-actions"><button class="small primary" data-k="targets">目的地に追加</button>
    <button class="small" data-k="candidates">補給地点に追加</button></div>`;
  el.addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-k]");
    if (!b) return;
    addPoint(b.dataset.k, place);
    popup.remove();
  });
  popup.setDOMContent(el);
}

function resultIndex() {
  // 結果表示用: 拠点 id → {letter, color}, 目的地 id → 拠点 id
  if (!state.result || !state.showResult) return null;
  const depots = new Map();
  state.result.depots.forEach((d, i) => depots.set(d.id, { i, letter: LETTERS[i] || "?", color: i < SERIES.length ? SERIES[i] : OTHER, d }));
  const owner = new Map();
  state.result.assignments.forEach((a) => owner.set(a.target_id, a.depot_id));
  // 目的地 id → {車両, 立ち寄り}(ルート最適化の結果があるとき)
  const visit = new Map();
  const rt = state.result.routing;
  if (rt) rt.vehicles.forEach((v) => v.stops.forEach((st) => { if (st.kind === "deliver") visit.set(st.id, { v, st }); }));
  return { depots, owner, visit, routing: rt };
}

function renderMarkers() {
  if (!map) return;
  for (const k of ["targets", "candidates"]) { markers[k].forEach((m) => m.remove()); markers[k].clear(); }
  const ri = resultIndex();

  state.scenario.targets.forEach((p, i) => {
    const el = document.createElement("div");
    el.className = "mk" + (p.enabled ? "" : " dim");
    el.textContent = i + 1;
    let title = `${p.name}`;
    if (ri && ri.owner.has(p.id)) {
      const dep = ri.depots.get(ri.owner.get(p.id));
      el.style.background = dep.color;
      const a = state.result.assignments.find((x) => x.target_id === p.id);
      title += `\n担当: ${dep.letter} ${dep.d.name}（${fmt(a.time_min)} 分）`;
      const vis = ri.visit.get(p.id);
      if (vis) {
        title += `\n車両 ${vis.v.no}（${vis.v.start}発）で ${fmt(vis.st.done_min)} 分に作業完了`;
        if (state.vehicle != null && state.vehicle !== vis.v.no) el.classList.add("dim");
      }
    }
    el.title = title;
    addMarker("targets", p, el);
  });
  state.scenario.candidates.forEach((p, i) => {
    const el = document.createElement("div");
    el.className = `mk cand ${p.status}`;
    el.textContent = i + 1;
    let title = `${p.name}（${STATUS_LABEL[p.status]}${p.group ? " / " + p.group : ""}）`;
    if (ri && ri.depots.has(p.id)) {
      const dep = ri.depots.get(p.id);
      el.classList.add("adopted");
      el.style.background = dep.color;
      el.textContent = dep.letter;
      title += `\n採用 ${dep.letter}: 機材 ${dep.d.stock} 個・車両 ${dep.d.vehicles} 台 / 最長 ${fmt(dep.d.max_min)} 分`;
    } else if (ri) {
      el.classList.add("dim");
    }
    el.title = title;
    addMarker("candidates", p, el);
  });
  if (state.selected) select(state.selected.kind, state.selected.id);
  renderLinks(ri);
  renderLegend(ri);
}

function addMarker(kind, p, el) {
  el.addEventListener("click", (e) => { e.stopPropagation(); showTab(kind); select(kind, p.id); });
  const m = new mapboxgl.Marker({ element: el, draggable: true }).setLngLat([p.lon, p.lat]).addTo(map);
  m.on("dragend", () => {
    const ll = m.getLngLat();
    p.lat = +ll.lat.toFixed(7);
    p.lon = +ll.lng.toFixed(7);
    p.mapbox_id = null;
    markDirty();
    renderList(kind);
    toast(`位置を変更しました: ${p.name}`);
  });
  markers[kind].set(p.id, m);
}

function renderLinks(ri) {
  const src = map && map.getSource && map.getSource("links");
  if (!src) return;
  renderRoutes(ri);
  const features = [];
  if (ri && !ri.routing) {   // ルートがあるときは担当の線ではなく順路の線を描く
    const cand = new Map(state.scenario.candidates.map((c) => [c.id, c]));
    for (const t of state.scenario.targets) {
      const did = ri.owner.get(t.id);
      const c = did && cand.get(did);
      if (!c) continue;
      features.push({
        type: "Feature", properties: { color: ri.depots.get(did).color },
        geometry: { type: "LineString", coordinates: [[c.lon, c.lat], [t.lon, t.lat]] },
      });
    }
  }
  src.setData({ type: "FeatureCollection", features });
}

// 車両ごとの順路: 待機拠点 → 立ち寄り順に直線で結ぶ(道路形状ではない)
function renderRoutes(ri) {
  const src = map.getSource("routes");
  if (!src) return;
  const features = [];
  if (ri && ri.routing) {
    for (const v of ri.routing.vehicles) {
      const coords = [[v.start_lon, v.start_lat], ...v.stops.map((st) => [st.lon, st.lat])];
      const sel = state.vehicle === v.no;
      features.push({
        type: "Feature",
        properties: { color: (ri.depots.get(v.start_id) || { color: OTHER }).color, sel, dim: state.vehicle != null && !sel },
        geometry: { type: "LineString", coordinates: coords },
      });
    }
  }
  src.setData({ type: "FeatureCollection", features });
}

function selectVehicle(no) {
  state.vehicle = state.vehicle === no ? null : no;
  $$("tr[data-vehicle]").forEach((tr) => tr.classList.toggle("sel", +tr.dataset.vehicle === state.vehicle));
  renderMarkers();
  const v = state.vehicle != null && state.result.routing.vehicles.find((x) => x.no === state.vehicle);
  if (v && map) {
    const b = new mapboxgl.LngLatBounds();
    b.extend([v.start_lon, v.start_lat]);
    v.stops.forEach((st) => b.extend([st.lon, st.lat]));
    map.fitBounds(b, { padding: 80, maxZoom: 12, duration: 600 });
  }
}

function routeText(v) {
  return v.stops.map((st) => st.kind === "pickup"
    ? `補(${st.name})${st.count > 1 ? "×" + st.count : ""}`
    : `配[${st.name}]`).join(" → ");
}

function renderLegend(ri) {
  const el = $("#map-legend");
  let html = `<div class="li"><span class="dot"></span>目的地</div>
    <div class="li"><span class="sq"></span>補給地点（緑=必須・灰=除外）</div>`;
  if (ri) {
    html += `<div class="li muted" style="margin-top:4px">採用拠点（線は${ri.routing ? "車両の順路・直線表示" : "担当"}）</div>` +
      [...ri.depots.values()].map((d) => `<div class="li"><span class="sw" style="background:${d.color}">${d.letter}</span>${esc(d.d.name)}</div>`).join("");
  }
  el.innerHTML = html;
}

function fitAll() {
  if (!map || !state.scenario) return;
  const pts = [...state.scenario.targets, ...state.scenario.candidates];
  if (!pts.length) return;
  const b = new mapboxgl.LngLatBounds();
  pts.forEach((p) => b.extend([p.lon, p.lat]));
  map.fitBounds(b, { padding: 60, maxZoom: 13, duration: 600 });
}

// ---------------------------------------------------------------------------
// 実行
// ---------------------------------------------------------------------------
async function runSimulation() {
  if (state.job) return;
  readParams();
  let job;
  try {
    job = await api("/api/runs", { method: "POST", body: state.scenario });
  } catch (e) {
    showTab("results");
    $("#results").innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
    return;
  }
  state.job = { id: job.id };
  $("#btn-run").disabled = true;
  $("#run-progress").hidden = false;
  updateProgress(0, "開始");
  poll();
}

function updateProgress(frac, msg) {
  $("#run-progress-fill").style.width = `${Math.round(frac * 100)}%`;
  $("#run-progress-text").textContent = `${Math.round(frac * 100)}% ${msg || ""}`;
}

async function poll() {
  if (!state.job) return;
  let j;
  try { j = await api(`/api/runs/${state.job.id}`); }
  catch (e) { finishJob(); toast(e.message); return; }
  updateProgress(j.progress, j.message);
  if (j.status === "running") { state.job.timer = setTimeout(poll, 400); return; }
  const id = state.job.id;
  finishJob();
  showTab("results");
  if (j.status === "done") {
    state.result = j.result;
    state.resultJob = id;
    state.resultStale = false;
    state.showResult = true;
    state.vehicle = null;
    renderResults();
    renderMarkers();
    fitAll();
  } else if (j.status === "cancelled") {
    toast("中止しました");
  } else {
    $("#results").innerHTML = `<div class="banner err">実行に失敗しました\n${esc(j.error)}</div>` + ($("#results").dataset.keep || "");
  }
}

function finishJob() {
  if (state.job && state.job.timer) clearTimeout(state.job.timer);
  state.job = null;
  $("#btn-run").disabled = false;
  $("#run-progress").hidden = true;
}

// ---------------------------------------------------------------------------
// 結果表示
// ---------------------------------------------------------------------------
function renderResults() {
  const box = $("#results");
  const r = state.result;
  if (!r) { box.innerHTML = `<p class="empty">まだ実行していません。条件を確認して「シミュレーション実行」を押してください。</p>`; return; }
  const s = r.summary, p = r.params;
  const ri = new Map(r.depots.map((d, i) => [d.id, { letter: LETTERS[i] || "?", color: i < SERIES.length ? SERIES[i] : OTHER }]));
  const sw = (id) => { const d = ri.get(id); return d ? `<span class="sw" style="background:${d.color}">${d.letter}</span>` : ""; };
  const profile = { driving: "自動車", "driving-traffic": "自動車(交通)", walking: "徒歩", cycling: "自転車" }[p.profile] || p.profile;
  const rt = r.routing;

  box.innerHTML = `
    ${state.resultStale ? `<div class="banner warn">この結果の後に条件や地点を変更しています。再実行すると反映されます。</div>` : ""}
    ${r.warnings.map((w) => `<div class="banner warn">${esc(w)}</div>`).join("")}
    <div class="muted" style="font-size:12px">${esc(r.scenario_name)} ・ ${esc(r.run_at)}</div>
    ${rt
      ? `<div class="hero"><span class="v">${fmt(rt.makespan_min)}</span><span class="u">分　最も遅い車両の所要時間（ルート最適化後）</span></div>
         <div class="hero-sub">選定時点（各目的地へ直行する前提）の最長は ${fmt(s.makespan_min)} 分。拠点数を増やしても直行で ${fmt(s.lower_bound_min)} 分未満にはならない（${esc(s.bottleneck_target)} がボトルネック）</div>`
      : `<div class="hero"><span class="v">${fmt(s.makespan_min)}</span><span class="u">分　最も遅い車両の所要時間</span></div>
         <div class="hero-sub">拠点数を増やしても ${fmt(s.lower_bound_min)} 分未満にはならない（${esc(s.bottleneck_target)} が最寄りの補給地点からも遠い）</div>`}
    <div class="tiles">
      <div class="tile"><div class="k">採用拠点</div><div class="v">${s.num_depots} / ${s.num_candidates}</div></div>
      <div class="tile"><div class="k">車両（積載 ${s.vehicle_capacity} 個）</div><div class="v">${s.num_vehicles} 台</div></div>
      <div class="tile"><div class="k">平均所要</div><div class="v">${fmt(s.avg_min)} 分</div></div>
      <div class="tile"><div class="k">総所要</div><div class="v">${fmt(s.total_min)} 分</div></div>
      <div class="tile"><div class="k">解法</div><div class="v">${s.method === "exact" ? "厳密解" : "近似解"}</div></div>
      <div class="tile"><div class="k">移動時間</div><div class="v" style="font-size:13px">${esc(r.provider)} / ${esc(profile)}</div></div>
    </div>
    <label class="toggle"><input type="checkbox" id="toggle-result" ${state.showResult ? "checked" : ""}> 地図に結果を表示</label>
    <div class="downloads">
      <button class="small" data-dl="docx">Word 報告書</button>
      <button class="small" data-dl="csv">CSV（担当一覧）</button>
      ${rt ? `<button class="small" data-dl="routes">CSV（順路）</button>` : ""}
      <button class="small" data-dl="json">JSON（全結果）</button>
    </div>

    <h3>採用した補給地点と車両配分</h3>
    <table class="t"><thead><tr><th>拠点</th><th class="n">機材</th><th class="n">台数</th><th class="n">最長(分)</th><th>担当</th></tr></thead><tbody>
      ${r.depots.map((d) => `<tr class="click" data-cand="${esc(d.id)}"><td>${sw(d.id)}${esc(d.name)}${d.group ? `<div class="muted">${esc(d.group)}</div>` : ""}</td>
        <td class="n">${d.stock}</td><td class="n">${d.vehicles}</td><td class="n">${fmt(d.max_min)}</td><td>${d.targets.map(esc).join(", ")}</td></tr>`).join("")}
    </tbody></table>

    ${rt ? `<h3>車両ごとの順路</h3>
      <p class="muted" style="font-size:12px;margin:0 0 6px">積載 ${rt.vehicle_capacity} 個・1 台最大 ${rt.max_deliveries} 件 ／ 探索 ${fmt(rt.elapsed_sec)} 秒。行を押すと地図でその車両を強調します。</p>
      <table class="t"><thead><tr><th class="n">車両</th><th>待機拠点</th><th class="n">件数</th><th class="n">所要(分)</th><th>順路</th></tr></thead><tbody>
      ${rt.vehicles.map((v) => `<tr class="click${state.vehicle === v.no ? " sel" : ""}" data-vehicle="${v.no}"><td class="n">${v.no}</td><td>${sw(v.start_id)}${esc(v.start)}</td>
        <td class="n">${v.deliveries}</td><td class="n${v.time_min === rt.makespan_min ? " hl" : ""}">${fmt(v.time_min)}</td><td style="font-size:12px">${esc(routeText(v))}</td></tr>`).join("")}
      </tbody></table>` : ""}

    ${r.sensitivity.length > 1 ? `<h3>採用数ごとの最長所要時間</h3><div class="chart" id="sens-chart"></div>
      <details><summary>表で見る</summary><table class="t"><thead><tr><th class="n">拠点数</th><th class="n">最長(分)</th><th class="n">総所要(分)</th><th>採用拠点（機材数）</th></tr></thead><tbody>
      ${r.sensitivity.map((x) => `<tr class="${x.k === s.num_depots ? "hl" : ""}"><td class="n">${x.k}</td><td class="n">${fmt(x.makespan_min)}</td><td class="n">${fmt(x.total_min)}</td>
        <td>${x.depots.map((d) => `${esc(d.name)}(${d.stock})`).join(" / ")}</td></tr>`).join("")}
      </tbody></table></details>` : ""}

    <h3>目的地ごとの担当（所要時間の長い順）</h3>
    <table class="t"><thead><tr><th>目的地</th><th>担当拠点</th><th class="n">移動</th><th class="n">所要</th></tr></thead><tbody>
      ${r.assignments.map((a) => `<tr class="click" data-target="${esc(a.target_id)}"><td>${esc(a.target)}</td><td>${sw(a.depot_id)}${esc(a.depot)}</td>
        <td class="n">${fmt(a.travel_min)}</td><td class="n">${fmt(a.time_min)}</td></tr>`).join("")}
    </tbody></table>`;

  $("#toggle-result").addEventListener("change", (e) => { state.showResult = e.target.checked; renderMarkers(); });
  $$("[data-dl]", box).forEach((b) => b.addEventListener("click", () => download(b.dataset.dl)));
  $$("tr[data-cand]", box).forEach((tr) => tr.addEventListener("click", () => select("candidates", tr.dataset.cand, true)));
  $$("tr[data-target]", box).forEach((tr) => tr.addEventListener("click", () => select("targets", tr.dataset.target, true)));
  $$("tr[data-vehicle]", box).forEach((tr) => tr.addEventListener("click", () => selectVehicle(+tr.dataset.vehicle)));
  if (r.sensitivity.length > 1) drawSensitivity($("#sens-chart"), r);
}

function download(fmtName) {
  if (!state.resultJob) return;
  const a = document.createElement("a");
  a.href = `/api/runs/${state.resultJob}/export/${fmtName}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// 採用数 K → 最長所要時間の折れ線(1 系列)。下限を破線で示す。
function drawSensitivity(el, r) {
  const data = r.sensitivity;
  const s = r.summary;
  const W = Math.max(el.clientWidth || 380, 280), H = 190;
  const m = { l: 40, r: 12, t: 14, b: 28 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const ys = data.map((d) => d.makespan_min).concat([s.lower_bound_min]);
  let y0 = Math.floor(Math.min(...ys) * 0.95 / 5) * 5, y1 = Math.ceil(Math.max(...ys) * 1.03 / 5) * 5;
  if (y1 <= y0) y1 = y0 + 5;
  const ks = data.map((d) => d.k);
  const x = (k) => m.l + (ks.length === 1 ? iw / 2 : ((k - ks[0]) / (ks[ks.length - 1] - ks[0])) * iw);
  const y = (v) => m.t + ih - ((v - y0) / (y1 - y0)) * ih;
  const step = Math.max(5, Math.ceil((y1 - y0) / 4 / 5) * 5);
  const ticks = [];
  for (let v = y0; v <= y1 + 1e-9; v += step) ticks.push(v);
  const path = data.map((d, i) => `${i ? "L" : "M"}${x(d.k).toFixed(1)},${y(d.makespan_min).toFixed(1)}`).join("");
  const colW = ks.length > 1 ? iw / (ks.length - 1) : iw;

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="採用数ごとの最長所要時間">
    ${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>
      <text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)" style="font-variant-numeric:tabular-nums">${v}</text>`).join("")}
    <line x1="${m.l}" x2="${W - m.r}" y1="${m.t + ih}" y2="${m.t + ih}" stroke="var(--axis)"/>
    ${ks.map((k) => `<text x="${x(k)}" y="${H - 10}" text-anchor="middle" font-size="11" fill="var(--muted)">${k}</text>`).join("")}
    <text x="${W - m.r}" y="${H - 10 + 0}" text-anchor="end" font-size="11" fill="var(--muted)" dx="0" dy="0"></text>
    <line x1="${m.l}" x2="${W - m.r}" y1="${y(s.lower_bound_min)}" y2="${y(s.lower_bound_min)}" stroke="var(--ink-2)" stroke-dasharray="4 4" stroke-width="1"/>
    <text x="${W - m.r}" y="${y(s.lower_bound_min) - 5}" text-anchor="end" font-size="11" fill="var(--ink-2)">下限 ${fmt(s.lower_bound_min)} 分</text>
    <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
    ${data.map((d) => {
      const sel = d.k === s.num_depots;
      return `<circle cx="${x(d.k)}" cy="${y(d.makespan_min)}" r="${sel ? 6 : 4}" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>`;
    }).join("")}
    ${(() => { const d = data.find((q) => q.k === s.num_depots); return d ? `<text x="${x(d.k)}" y="${y(d.makespan_min) - 11}" text-anchor="middle" font-size="11" font-weight="600" fill="var(--ink)">${fmt(d.makespan_min)}</text>` : ""; })()}
    <line class="xh" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" stroke="var(--axis)" visibility="hidden"/>
    ${data.map((d) => `<rect data-k="${d.k}" x="${x(d.k) - colW / 2}" y="${m.t}" width="${colW}" height="${ih}" fill="transparent"/>`).join("")}
  </svg><div class="muted" style="font-size:11px;text-align:center">採用する補給地点の数</div><div class="tip" hidden></div>`;

  const tip = $(".tip", el), xh = $(".xh", el), svg = $("svg", el);
  $$("rect[data-k]", el).forEach((rect) => {
    rect.addEventListener("mouseenter", () => {
      const d = data.find((q) => q.k === +rect.dataset.k);
      const px = x(d.k) * (svg.clientWidth / W);
      xh.setAttribute("x1", x(d.k)); xh.setAttribute("x2", x(d.k)); xh.setAttribute("visibility", "visible");
      tip.innerHTML = `<div><b>${d.k}</b> 拠点${d.k === s.num_depots ? "（今回の設定）" : ""}</div>
        <div>最長 <b>${fmt(d.makespan_min)}</b> 分 ・ 総 <b>${fmt(d.total_min)}</b> 分</div>
        <div class="muted" style="margin-top:3px">${d.depots.map((q) => esc(q.name)).join("、")}</div>`;
      tip.hidden = false;
      const tw = tip.offsetWidth;
      tip.style.left = `${Math.min(Math.max(px - tw / 2, 0), svg.clientWidth - tw)}px`;
      tip.style.top = `${H + 4}px`;
    });
    rect.addEventListener("mouseleave", () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); });
  });
}

// ---------------------------------------------------------------------------
// 保存・読込
// ---------------------------------------------------------------------------
async function refreshScenarioList() {
  const list = await api("/api/scenarios");
  const sel = $("#scenario-select");
  sel.innerHTML = `<option value="">${state.file ? "シナリオを開く…" : "（未保存のシナリオ）"}</option>` +
    list.map((s) => `<option value="${esc(s.file)}" ${s.file === state.file ? "selected" : ""}>${esc(s.name)}（目的地 ${s.targets}・補給地点 ${s.candidates}）</option>`).join("");
}

async function confirmDiscard() {
  if (!state.dirty) return true;
  const v = await modal({
    title: "未保存の変更があります",
    body: "<p>保存していない変更は失われます。続けますか？</p>",
    buttons: [{ label: "キャンセル", value: null }, { label: "破棄して続ける", value: "ok", primary: true }],
  });
  return v === "ok";
}

async function openScenario(file) {
  if (!(await confirmDiscard())) { await refreshScenarioList(); return; }
  try {
    const r = await api(`/api/scenarios/${encodeURIComponent(file)}`);
    setScenario(r.scenario, r.file);
    await refreshScenarioList();
    try { localStorage.setItem("lastScenario", r.file); } catch { /* 保存できなくても動く */ }
  } catch (e) { toast(e.message); }
}

async function save(asNew = false) {
  readParams();
  let file = state.file;
  if (!file || asNew) {
    let name = null;   // モーダルを閉じると入力欄が消えるので validate の中で拾う
    const ok = await modal({
      title: "名前を付けて保存",
      body: `<p>scenarios/ フォルダに JSON として保存します（CLI からも同じファイルを実行できます）。</p><input id="save-name" value="${esc(state.scenario.name)}">`,
      buttons: [{ label: "キャンセル", value: null }, { label: "保存", value: "ok", primary: true }],
      validate: (_, box) => {
        name = $("#save-name", box).value.trim();
        return name ? null : "名前を入力してください";
      },
    });
    if (ok !== "ok") return;
    const existing = (await api("/api/scenarios")).find((s) => s.name === name || s.file === `${name}.json`);
    if (existing && existing.file !== state.file) {
      const v = await modal({
        title: "上書きの確認",
        body: `<p>scenarios/${esc(existing.file)} は既にあります。上書きしますか？</p>`,
        buttons: [{ label: "キャンセル", value: null }, { label: "上書き", value: "ok", primary: true }],
      });
      if (v !== "ok") return;
      name = existing.file;
    }
    file = name;
  }
  try {
    const r = await api(`/api/scenarios/${encodeURIComponent(file)}`, { method: "PUT", body: state.scenario });
    state.file = r.file;
    state.dirty = false;
    $("#dirty-mark").hidden = true;
    await refreshScenarioList();
    try { localStorage.setItem("lastScenario", r.file); } catch { /* 無視 */ }
    toast(`保存しました: scenarios/${r.file}`);
  } catch (e) { toast(`保存に失敗しました: ${e.message}`); }
}

async function newScenario() {
  if (!(await confirmDiscard())) return;
  setScenario(structuredClone(state.config.defaults), null);
  await refreshScenarioList();
}

// ---------------------------------------------------------------------------
// CSV 取り込み・全削除
// ---------------------------------------------------------------------------
async function importCsv(kind) {
  let rows = null;
  await modal({
    title: `${kind === "targets" ? "目的地" : "補給地点"}を CSV から取り込む`,
    body: `<p>1 行 1 件、<code>名称,緯度,経度[,住所]</code> の形式で貼り付けてください（見出し行は自動で無視）。</p>
      <textarea id="csv-text" rows="10" placeholder="点1,36.2203,139.4971&#10;点2,36.1866,139.5126"></textarea>`,
    buttons: [{ label: "キャンセル", value: null }, { label: "取り込む", value: "ok", primary: true }],
    validate: (_, box) => {
      const out = [];
      const lines = $("#csv-text", box).value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
      for (const [n, line] of lines.entries()) {
        const cols = line.split(/,|\t/).map((c) => c.trim().replace(/^"|"$/g, ""));
        const lat = parseFloat(cols[1]), lon = parseFloat(cols[2]);
        if (isNaN(lat) || isNaN(lon)) { if (n === 0) continue; return `${n + 1} 行目の緯度・経度が読めません: ${line}`; }
        out.push({ name: cols[0], lat, lon, address: cols[3] || "" });
      }
      if (!out.length) return "取り込める行がありません";
      rows = out;
      return null;
    },
  });
  if (!rows) return;
  const items = state.scenario[kind];
  for (const r of rows) {
    const item = { id: nextId(kind === "targets" ? "T" : "C", items), name: r.name, lat: r.lat, lon: r.lon, address: r.address, mapbox_id: null, note: "" };
    if (kind === "targets") item.enabled = true; else { item.group = ""; item.status = "candidate"; }
    items.push(item);
  }
  markDirty();
  renderList(kind);
  renderMarkers();
  fitAll();
  toast(`${rows.length} 件取り込みました`);
}

async function clearAll(kind) {
  if (!state.scenario[kind].length) return;
  const v = await modal({
    title: "全削除",
    body: `<p>${kind === "targets" ? "目的地" : "補給地点"} ${state.scenario[kind].length} 件をすべて削除しますか？（保存するまではファイルは変わりません）</p>`,
    buttons: [{ label: "キャンセル", value: null }, { label: "削除", value: "ok", primary: true }],
  });
  if (v !== "ok") return;
  state.scenario[kind] = [];
  markDirty();
  renderList(kind);
  renderMarkers();
}

// ---------------------------------------------------------------------------
// タブ・全体描画
// ---------------------------------------------------------------------------
function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
  if (name === "results" && state.result && state.result.sensitivity.length > 1) {
    const el = $("#sens-chart");
    if (el) drawSensitivity(el, state.result);   // 非表示中は幅が取れないため描き直す
  }
}

function renderAll() {
  renderList("targets");
  renderList("candidates");
  renderParams();
  renderResults();
  renderMarkers();
  scheduleValidate();
}

function bindUi() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $$(".search").forEach(createSearch);
  bindList("targets");
  bindList("candidates");
  $$("[data-action]").forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.action;
    if (a === "fit") fitAll();
    if (a === "import") importCsv(b.dataset.kind);
    if (a === "clear") clearAll(b.dataset.kind);
  }));
  $("#params-form").addEventListener("input", () => { readParams(); markDirty(); });
  $("#params-form").addEventListener("submit", (e) => e.preventDefault());
  $("#btn-run").addEventListener("click", runSimulation);
  $("#btn-cancel").addEventListener("click", () => { if (state.job) api(`/api/runs/${state.job.id}/cancel`, { method: "POST" }); });
  $("#btn-save").addEventListener("click", () => save(false));
  $("#btn-save-as").addEventListener("click", () => save(true));
  $("#btn-new").addEventListener("click", newScenario);
  $("#scenario-select").addEventListener("change", (e) => { if (e.target.value) openScenario(e.target.value); });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(false); }
  });
  window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
  let rt;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (state.result && !$('[data-panel="results"]').hidden) showTab("results"); }, 200); });
}

async function init() {
  bindUi();
  state.config = await api("/api/config");
  let token = state.config.token;
  if (!state.config.token_configured) token = await askToken();
  state.scenario = structuredClone(state.config.defaults);
  initMap(token);

  let last = null;
  try { last = localStorage.getItem("lastScenario"); } catch { /* 無視 */ }
  const list = await api("/api/scenarios");
  const file = list.find((s) => s.file === last) ? last : list.length ? list[0].file : null;
  if (file) {
    const r = await api(`/api/scenarios/${encodeURIComponent(file)}`);
    setScenario(r.scenario, r.file);
  } else {
    setScenario(state.scenario, null);
  }
  await refreshScenarioList();
}

init().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="banner err" style="margin:8px">起動に失敗しました: ${esc(e.message)}</div>`);
});
