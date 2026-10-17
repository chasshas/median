// Review page: the queue, single/grid/compare views, decisions with undo, and the keyboard map
// of ARCHITECTURE.md section 13. A and Shift+A open the archive panel (archive_panel.js). The server renders the views (/review/fragments/*); decisions
// go to the JSON API (/api/review/*). No framework; state lives in this module.

const root = document.getElementById("review");
const stage = document.getElementById("review-stage");
import { checkLastFolder, keptTags, lastFolderId, openArchivePanel } from "./archive_panel.js";

const toasts = document.getElementById("toasts");
const lastFolderLabel = document.getElementById("last-folder");
const LAST_FOLDER_PATH_KEY = "median.review.lastFolderPath";

const PAGE_SIZE = 100;
const GRID_STEP = 200;
const DECIDED_KEY = "median.review.decided";
const MODE_KEY = "median.review.mode";

const state = {
  items: [], // queue items from /api/review/queue, in queue order
  index: 0, // current item (single and compare view) or focused tile (grid view)
  cursor: null,
  more: true,
  loading: null, // promise of the page being loaded
  total: 0, // remaining in the queue (server count, adjusted after each decision)
  mode: "single", // "single" | "grid" | "compare"
  compareWith: null, // the similar file shown in compare view
  similar: [], // similar file ids of the current item, cycled through by C
  selected: new Set(), // selected file ids
  anchor: null, // index of the last plain click, for Shift+click ranges
  gridLimit: GRID_STEP,
  history: [], // undoable decisions: {actionIds, entries: [{item, index}], label}
  decided: 0,
  renderToken: 0,
};
const fragments = new Map(); // url -> Promise<string>, for preloading the next item
const preloaded = new Set();

// --- small helpers ---------------------------------------------------------------------------

function readStorage(storage, key, fallback) {
  try {
    return storage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(storage, key, value) {
  try {
    storage.setItem(key, String(value));
  } catch {
    // Storage can be unavailable (private windows); counters then reset on reload.
  }
}

function isTyping(target) {
  return target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

function current() {
  return state.items[state.index] ?? null;
}

function itemName(item) {
  return item.original_filename || `file ${item.id}`;
}

function describe(entries) {
  return entries.length === 1 ? itemName(entries[0].item) : `${entries.length} files`;
}

function errorText(data, response) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return `${response.status} ${response.statusText}`;
}

async function post(url, body) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) return { ok: false, message: errorText(data, response) };
    return { ok: true, data };
  } catch (error) {
    return { ok: false, message: `Network error: ${error.message}` };
  }
}

function fragment(url, { cache = true } = {}) {
  if (cache && fragments.has(url)) return fragments.get(url);
  const promise = fetch(url).then(async (response) => {
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.text();
  });
  if (cache) {
    fragments.set(url, promise);
    promise.catch(() => fragments.delete(url));
  }
  return promise;
}

const singleUrl = (id) => `/review/fragments/single?file_id=${id}`;

function toast(message, { kind = "info", undo = null } = {}) {
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  const text = document.createElement("span");
  text.textContent = message;
  el.append(text);
  if (undo) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Undo";
    button.addEventListener("click", () => {
      el.remove();
      undoEntry(undo);
    });
    el.append(button);
  }
  toasts.append(el);
  while (toasts.children.length > 4) toasts.firstElementChild.remove();
  setTimeout(() => el.remove(), kind === "error" ? 12000 : 6000);
}

// Compare decisions archive into the panel's last used folder with the kept user tags.
function archiveOptions() {
  return { folder_id: lastFolderId(), add_tags: keptTags() };
}

function showLastFolder(path = null) {
  if (path) writeStorage(localStorage, LAST_FOLDER_PATH_KEY, path);
  const known = lastFolderId() !== null;
  const stored = readStorage(localStorage, LAST_FOLDER_PATH_KEY, null);
  lastFolderLabel.textContent = known && stored ? stored : lastFolderLabel.dataset.inboxPath;
}

function updateCounters() {
  document.getElementById("remaining").textContent = String(state.total);
  document.getElementById("decided").textContent = String(state.decided);
  document.getElementById("selected-count").textContent = String(state.selected.size);
  for (const button of root.querySelectorAll("[data-mode]")) {
    const active = button.dataset.mode === (state.mode === "grid" ? "grid" : "single");
    button.setAttribute("aria-pressed", String(active));
  }
}

// --- the queue -------------------------------------------------------------------------------

function loadMore() {
  if (!state.more) return Promise.resolve(false);
  if (state.loading) return state.loading;
  const url = new URL(root.dataset.queueUrl, location.origin);
  url.searchParams.set("limit", String(PAGE_SIZE));
  if (state.cursor) url.searchParams.set("cursor", state.cursor);
  state.loading = (async () => {
    try {
      const response = await fetch(url);
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorText(data, response));
      const known = new Set(state.items.map((i) => i.id));
      for (const item of data.items) if (!known.has(item.id)) state.items.push(item);
      state.cursor = data.next_cursor;
      state.more = Boolean(data.next_cursor);
      state.total = data.total;
      return data.items.length > 0;
    } catch (error) {
      toast(`Loading the queue failed: ${error.message}`, { kind: "error" });
      state.more = false;
      return false;
    } finally {
      state.loading = null;
    }
  })();
  return state.loading;
}

function removeItems(ids) {
  const wanted = new Set(ids);
  const entries = [];
  state.items.forEach((item, index) => {
    if (wanted.has(item.id)) entries.push({ item, index });
  });
  const before = entries.filter((e) => e.index < state.index).length;
  state.items = state.items.filter((item) => !wanted.has(item.id));
  state.index = Math.max(0, state.index - before);
  for (const id of ids) state.selected.delete(id);
  return entries;
}

function restoreItems(entries) {
  const sorted = [...entries].sort((a, b) => a.index - b.index);
  const known = new Set(state.items.map((i) => i.id));
  for (const { item, index } of sorted) {
    if (known.has(item.id)) continue;
    state.items.splice(Math.min(index, state.items.length), 0, item);
  }
  if (sorted.length) state.index = state.items.findIndex((i) => i.id === sorted[0].item.id);
}

// --- rendering -------------------------------------------------------------------------------

function render() {
  updateCounters();
  state.index = Math.max(0, Math.min(state.index, state.items.length - 1));
  if (!state.items.length) {
    if (state.more) {
      loadMore().then(render);
      return;
    }
    state.mode = state.mode === "compare" ? "single" : state.mode;
    stage.innerHTML = '<p class="muted review-empty">Nothing to review. <a href="/upload">Upload files</a>.</p>';
    return;
  }
  if (state.more && state.index >= state.items.length - 5) loadMore().then(updateCounters);
  if (state.mode === "grid") renderGrid();
  else if (state.mode === "compare") renderCompare();
  else renderSingle();
}

async function show(url, options) {
  const token = ++state.renderToken;
  let html;
  try {
    html = await fragment(url, options);
  } catch (error) {
    if (token === state.renderToken) {
      stage.innerHTML = "";
      const p = document.createElement("p");
      p.className = "bad";
      p.textContent = `Loading failed: ${error.message}`;
      stage.append(p);
    }
    return false;
  }
  if (token !== state.renderToken) return false;
  stage.innerHTML = html;
  if (window.htmx) window.htmx.process(stage);
  return true;
}

async function renderSingle() {
  const item = current();
  if (!(await show(singleUrl(item.id)))) return;
  stage.querySelector(".review-single")?.classList.toggle("selected", state.selected.has(item.id));
  preload(state.items[state.index + 1]);
}

function preload(item) {
  if (!item) return;
  fragment(singleUrl(item.id)).catch(() => {});
  if (item.media_kind === "image" && !preloaded.has(item.id)) {
    preloaded.add(item.id);
    const img = new Image();
    img.decoding = "async";
    img.src = item.original_url;
  }
}

async function renderGrid() {
  const items = state.items.slice(0, Math.max(state.gridLimit, state.index + 1));
  const url = `/review/fragments/grid?ids=${items.map((i) => i.id).join(",")}`;
  const token = ++state.renderToken;
  let html;
  try {
    html = await fragment(url, { cache: false });
  } catch (error) {
    toast(`Loading the grid failed: ${error.message}`, { kind: "error" });
    return;
  }
  if (token !== state.renderToken) return;
  stage.innerHTML = `<ul class="grid review-grid">${html}</ul>`;
  if (state.items.length > items.length || state.more) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "grid-more";
    button.textContent = "Show more";
    button.addEventListener("click", async () => {
      state.gridLimit += GRID_STEP;
      if (state.items.length < state.gridLimit) await loadMore();
      render();
    });
    stage.append(button);
  }
  markGrid();
}

function markGrid() {
  const focusedId = current()?.id;
  for (const tile of stage.querySelectorAll(".review-tile")) {
    const id = Number(tile.dataset.fileId);
    tile.classList.toggle("selected", state.selected.has(id));
    tile.classList.toggle("focused", id === focusedId);
    tile.setAttribute("aria-selected", String(state.selected.has(id)));
    if (id === focusedId) tile.scrollIntoView({ block: "nearest" });
  }
  updateCounters();
}

async function renderCompare() {
  const item = current();
  const url = `/review/fragments/compare?left=${item.id}&right=${state.compareWith}`;
  await show(url, { cache: false });
}

// --- navigation and selection ----------------------------------------------------------------

async function move(delta) {
  const target = state.index + delta;
  if (target >= state.items.length && state.more) await loadMore();
  if (target < 0 || target >= state.items.length) {
    toast(target < 0 ? "Start of the queue" : "End of the queue");
    return;
  }
  state.index = target;
  if (state.mode === "grid") markGrid();
  else {
    state.mode = "single";
    render();
  }
}

function toggleSelected(item) {
  if (!item) return;
  if (state.selected.has(item.id)) state.selected.delete(item.id);
  else state.selected.add(item.id);
  if (state.mode === "grid") markGrid();
  else {
    stage.querySelector(".review-single")?.classList.toggle("selected", state.selected.has(item.id));
    updateCounters();
  }
}

function setMode(mode) {
  state.mode = mode;
  state.compareWith = null;
  writeStorage(localStorage, MODE_KEY, mode);
  render();
}

// --- decisions -------------------------------------------------------------------------------

function record(entries, actionIds, verb) {
  const entry = { actionIds, entries, label: `${verb} ${describe(entries)}` };
  state.history.push(entry);
  state.decided += entries.length;
  state.total = Math.max(0, state.total - entries.length);
  writeStorage(sessionStorage, DECIDED_KEY, state.decided);
  fragments.clear();
  toast(entry.label, { undo: entry });
  updateCounters();
}

function keepFailed(entries, results) {
  // Puts back the items the server rejected, shows its messages; returns the decided entries.
  const failed = results.filter((r) => !r.ok);
  const failedIds = new Set(failed.map((r) => r.file_id));
  const kept = entries.filter((e) => failedIds.has(e.item.id));
  if (failed.length) {
    restoreItems(kept);
    if (state.mode === "compare") state.mode = "single";
    render();
    toast([...new Set(failed.map((r) => r.error))].join("; "), { kind: "error" });
  }
  return entries.filter((e) => !failedIds.has(e.item.id));
}

async function decide(kind, items) {
  if (!items.length) return;
  let options = null;
  if (kind === "archive") {
    options = await openArchivePanel(items);
    if (!options) return; // cancelled
    showLastFolder(options.folder_path);
  }
  const entries = removeItems(items.map((i) => i.id));
  if (state.mode === "compare") state.mode = "single";
  render(); // move on immediately
  const body = { file_ids: entries.map((e) => e.item.id) };
  if (options) {
    body.folder_id = options.folder_id;
    body.add_tags = options.add_tags;
    body.remove_tags_per_file = options.remove_tags_per_file;
  }
  const result = await post(`/api/review/${kind}`, body);
  if (!result.ok) {
    restoreItems(entries);
    render();
    toast(result.message, { kind: "error" });
    return;
  }
  const done = keepFailed(entries, result.data.results);
  const verb = options ? `Archived into ${options.folder_path}:` : "Discarded";
  if (done.length) record(done, result.data.action_ids, verb);
}

function decideSelection(kind) {
  const items = state.items.filter((i) => state.selected.has(i.id));
  if (!items.length) {
    toast("Nothing selected (X selects)");
    return;
  }
  decide(kind, items);
}

const COMPARE_VERBS = {
  keep_left: "Kept left, discarded right:",
  keep_right: "Kept right, discarded left:",
  keep_both: "Kept both:",
  replace: "Replaced with",
};

async function compareDecide(choice) {
  const button = stage.querySelector(`[data-choice="${choice}"]`);
  if (!button || button.disabled) {
    toast("That choice is not available for this pair");
    return;
  }
  const item = current();
  const otherId = state.compareWith;
  const entries = removeItems([item.id, otherId]);
  state.mode = "single";
  state.compareWith = null;
  render();
  const result = await post("/api/review/compare", {
    file_id: item.id,
    other_id: otherId,
    choice,
    ...archiveOptions(),
  });
  if (!result.ok) {
    restoreItems(entries);
    render();
    toast(result.message, { kind: "error" });
    return;
  }
  record(entries, result.data.action_ids, COMPARE_VERBS[choice]);
}

async function undoEntry(entry) {
  if (!entry) {
    toast("Nothing to undo");
    return;
  }
  const result = await post("/api/review/undo", { action_ids: entry.actionIds });
  if (!result.ok) {
    toast(result.message, { kind: "error" });
    return;
  }
  state.history = state.history.filter((e) => e !== entry);
  const results = result.data.results;
  const restored = new Set(results.filter((r) => r.ok).map((r) => r.file_id));
  const failed = results.filter((r) => !r.ok);
  if (failed.length) toast([...new Set(failed.map((r) => r.error))].join("; "), { kind: "error" });
  if (!restored.size) return;
  const back = entry.entries.filter((e) => restored.has(e.item.id));
  restoreItems(back);
  state.decided = Math.max(0, state.decided - back.length);
  state.total += back.length;
  writeStorage(sessionStorage, DECIDED_KEY, state.decided);
  fragments.clear();
  if (state.mode === "compare") state.mode = "single";
  render();
  toast(`Undone: ${entry.label}`);
}

// --- compare ---------------------------------------------------------------------------------

function similarIds(item) {
  const strip = stage.querySelector(".review-single");
  if (strip && Number(strip.dataset.fileId) === item.id) {
    return (strip.dataset.similarIds || "").split(",").filter(Boolean).map(Number);
  }
  return [...item.similar.kept, ...item.similar.discarded];
}

function compare(otherId = null) {
  const item = current();
  if (!item) return;
  const ids = state.mode === "compare" && state.similar.length ? state.similar : similarIds(item);
  if (!ids.length) {
    toast("No similar files to compare with");
    return;
  }
  let next = otherId;
  if (next === null) {
    const at = ids.indexOf(state.compareWith);
    next = state.mode === "compare" ? ids[(at + 1) % ids.length] : ids[0];
  }
  state.similar = ids;
  state.mode = "compare";
  state.compareWith = next;
  render();
}

// --- input -----------------------------------------------------------------------------------

const COMPARE_KEYS = { 1: "keep_left", 2: "keep_right", b: "keep_both", r: "replace" };

function onKey(key, shift, fromMedia) {
  const arrow = key.startsWith("Arrow");
  if (arrow && fromMedia) return false; // let the player seek
  if (state.mode === "compare") {
    if (key in COMPARE_KEYS) {
      compareDecide(COMPARE_KEYS[key]);
      return true;
    }
    if (key === "Escape") {
      setMode("single");
      return true;
    }
  }
  switch (key) {
    case "j":
    case "ArrowRight":
    case "ArrowDown":
    case "s":
      move(1);
      return true;
    case "k":
    case "ArrowLeft":
    case "ArrowUp":
      move(-1);
      return true;
    case "a":
      if (shift) decideSelection("archive");
      else if (current()) decide("archive", [current()]);
      return true;
    case "d":
      if (shift) decideSelection("discard");
      else if (current()) decide("discard", [current()]);
      return true;
    case "x":
      toggleSelected(current());
      return true;
    case "c":
      compare();
      return true;
    case "u":
      undoEntry(state.history.at(-1));
      return true;
    case "g":
      setMode(state.mode === "grid" ? "single" : "grid");
      return true;
    case "Enter":
      if (state.mode !== "grid") return false;
      setMode("single");
      return true;
    default:
      return false;
  }
}

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
  if (isTyping(event.target) || document.querySelector("dialog[open]")) return;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (onKey(key, event.shiftKey, event.target instanceof HTMLMediaElement)) event.preventDefault();
});

stage.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const choice = target.closest("[data-choice]");
  if (choice) {
    compareDecide(choice.dataset.choice);
    return;
  }
  if (target.closest("[data-compare-close]")) {
    setMode("single");
    return;
  }
  const pick = target.closest("[data-compare-id]");
  if (pick) {
    compare(Number(pick.dataset.compareId));
    return;
  }
  const tile = target.closest(".review-tile");
  if (!tile || target.closest("a")) return;
  const index = state.items.findIndex((i) => i.id === Number(tile.dataset.fileId));
  if (index < 0) return;
  if (event.shiftKey && state.anchor !== null) {
    const [from, to] = [Math.min(state.anchor, index), Math.max(state.anchor, index)];
    for (const item of state.items.slice(from, to + 1)) state.selected.add(item.id);
  } else {
    toggleSelected(state.items[index]);
    state.anchor = index;
  }
  state.index = index;
  markGrid();
});

stage.addEventListener("dblclick", (event) => {
  const tile = event.target instanceof Element ? event.target.closest(".review-tile") : null;
  if (!tile) return;
  state.index = Math.max(0, state.items.findIndex((i) => i.id === Number(tile.dataset.fileId)));
  setMode("single");
});

for (const button of root.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", () => setMode(button.dataset.mode));
}

// --- start -----------------------------------------------------------------------------------

state.decided = Number(readStorage(sessionStorage, DECIDED_KEY, "0")) || 0;
showLastFolder();
checkLastFolder().then((path) => showLastFolder(path));
state.mode = readStorage(localStorage, MODE_KEY, "single") === "grid" ? "grid" : "single";
loadMore().then(render);
