// The review page's archive panel (A / Shift+A): pick a folder (searchable tree, 5 recent
// folders, last used folder preselected unless the files' crawl source has a default folder,
// inline "new subfolder"), drop crawler tags of a single
// file, add user tags with autocomplete, optionally keep those user tags for the next files.
// openArchivePanel(items) resolves to {folder_id, folder_path, add_tags, remove_tags_per_file}
// on Enter / Archive, or null on Esc / Cancel.
import { api, attachAutocomplete, toast } from "./common.js";

const LAST_FOLDER_KEY = "median.review.lastFolder";
const KEEP_KEY = "median.review.keepTags";
const KEPT_TAGS_KEY = "median.review.keptTags";

const dialog = document.getElementById("archive-panel");
const form = document.getElementById("archive-form");
const subject = document.getElementById("archive-panel-subject");
const search = document.getElementById("folder-search");
const picker = document.getElementById("folder-picker");
const recentBox = document.getElementById("recent-folders");
const newFolder = document.getElementById("new-subfolder");
const crawlerBlock = document.getElementById("crawler-tags-block");
const crawlerList = document.getElementById("crawler-tags");
const bulkNote = document.getElementById("bulk-tags-note");
const userList = document.getElementById("user-tags");
const tagInput = document.getElementById("user-tag-input");
const keepBox = document.getElementById("keep-user-tags");

const panel = {
  folders: [], // [{id, name, path, depth, count, is_inbox}] in tree order
  userTags: [],
  removed: new Set(), // crawler tag names to remove from the single file
  resolve: null,
};

function read(storage, key, fallback) {
  try {
    const value = storage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function write(storage, key, value) {
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage unavailable: the panel just forgets between files.
  }
}

export function lastFolderId() {
  return read(localStorage, LAST_FOLDER_KEY, null);
}

// Forget the last used folder if it was deleted meanwhile (for example in the library).
export async function checkLastFolder() {
  const id = lastFolderId();
  if (id === null) return;
  const result = await api("GET", `/api/folders/${id}`);
  if (result.status === 404) write(localStorage, LAST_FOLDER_KEY, null);
  else if (result.ok) return result.data.path;
  return null;
}

// User tags kept for the next files (session only), or [].
export function keptTags() {
  return read(sessionStorage, KEEP_KEY, false) ? read(sessionStorage, KEPT_TAGS_KEY, []) : [];
}

function flatten(nodes, out = []) {
  for (const node of nodes) {
    out.push({
      id: node.id,
      name: node.name,
      path: node.path,
      depth: node.path.split(" / ").length - 1,
      count: node.direct_count,
      is_inbox: node.is_inbox,
    });
    flatten(node.children, out);
  }
  return out;
}

function selectedFolder() {
  const checked = picker.querySelector("input:checked");
  return checked ? panel.folders.find((f) => f.id === Number(checked.value)) : null;
}

function select(id, { scroll = true } = {}) {
  const input = picker.querySelector(`input[value="${id}"]`);
  if (!input) return;
  input.checked = true;
  if (scroll) input.closest("li").scrollIntoView({ block: "nearest" });
  for (const button of recentBox.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.folderId) === id));
  }
}

function renderFolders() {
  picker.replaceChildren(
    ...panel.folders.map((folder) => {
      const li = document.createElement("li");
      li.style.setProperty("--depth", String(folder.depth));
      li.dataset.path = folder.path.toLowerCase();
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "panel-folder";
      radio.value = String(folder.id);
      const name = document.createElement("span");
      name.className = "folder-name";
      name.textContent = folder.name;
      name.dataset.name = folder.name;
      name.dataset.path = folder.path;
      const count = document.createElement("span");
      count.className = "muted";
      count.textContent = ` ${folder.count}`;
      label.append(radio, " ", name, count);
      li.append(label);
      return li;
    }),
  );
}

function filterFolders() {
  const needle = search.value.trim().toLowerCase();
  let firstVisible = null;
  for (const li of picker.children) {
    const visible = !needle || li.dataset.path.includes(needle);
    li.hidden = !visible;
    li.classList.toggle("searching", Boolean(needle));
    const name = li.querySelector(".folder-name");
    name.textContent = needle ? name.dataset.path : name.dataset.name;
    if (visible && !firstVisible) firstVisible = li;
  }
  const checked = picker.querySelector("input:checked");
  if (firstVisible && (!checked || checked.closest("li").hidden)) {
    select(Number(firstVisible.querySelector("input").value));
  }
}

function renderRecent(recent) {
  recentBox.replaceChildren();
  if (!recent.length) return;
  const label = document.createElement("span");
  label.className = "muted";
  label.textContent = "Recent:";
  recentBox.append(label);
  for (const folder of recent) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.folderId = String(folder.id);
    button.textContent = folder.path;
    button.addEventListener("click", () => {
      search.value = "";
      filterFolders();
      select(folder.id);
    });
    recentBox.append(button);
  }
}

function chip(name, { onRemove, removed = false, kind }) {
  const li = document.createElement("li");
  li.className = `tag chip tag-${kind}${removed ? " removed" : ""}`;
  li.dataset.tag = name;
  const text = document.createElement("span");
  text.textContent = name;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "chip-remove";
  button.textContent = removed ? "↺" : "×";
  button.title = removed ? `Keep ${name}` : `Remove ${name}`;
  button.setAttribute("aria-label", button.title);
  button.addEventListener("click", onRemove);
  li.append(text, button);
  return li;
}

function renderUserTags() {
  userList.replaceChildren(
    ...panel.userTags.map((name) =>
      chip(name, {
        kind: "user",
        onRemove: () => {
          panel.userTags = panel.userTags.filter((t) => t !== name);
          renderUserTags();
          tagInput.focus();
        },
      }),
    ),
  );
}

function renderCrawlerTags(names) {
  crawlerList.replaceChildren(
    ...names.map((name) =>
      chip(name, {
        kind: "crawler",
        removed: panel.removed.has(name),
        onRemove: () => {
          if (panel.removed.has(name)) panel.removed.delete(name);
          else panel.removed.add(name);
          renderCrawlerTags(names);
        },
      }),
    ),
  );
}

function addTypedTags() {
  const typed = tagInput.value.split(",").map((t) => t.trim()).filter(Boolean);
  for (const name of typed) if (!panel.userTags.includes(name)) panel.userTags.push(name);
  tagInput.value = "";
  renderUserTags();
  return typed.length > 0;
}

async function loadFolders(selectId = null) {
  const [tree, recent] = await Promise.all([
    api("GET", "/api/folders"),
    api("GET", "/api/folders/recent?limit=5"),
  ]);
  if (!tree.ok) throw new Error(tree.message);
  panel.folders = flatten(tree.data.folders);
  renderFolders();
  renderRecent(recent.ok ? recent.data.folders : []);
  const known = (id) => id !== null && panel.folders.some((f) => f.id === id);
  const recentFirst = recent.ok && recent.data.folders.length ? recent.data.folders[0].id : null;
  const inbox = panel.folders.find((f) => f.is_inbox)?.id ?? panel.folders[0]?.id;
  const wanted = [selectId, lastFolderId(), recentFirst].find(known) ?? inbox;
  search.value = "";
  filterFolders();
  select(wanted);
}

async function createSubfolder() {
  const name = newFolder.value.trim();
  if (!name) return;
  const parent = selectedFolder();
  const result = await api("POST", "/api/folders", { name, parent_id: parent ? parent.id : null });
  if (!result.ok) {
    toast(result.message, { kind: "error" });
    return;
  }
  newFolder.value = "";
  await loadFolders(result.data.id);
  toast(`Created ${result.data.path}`);
}

function close(value) {
  const resolve = panel.resolve;
  panel.resolve = null;
  if (dialog.open) dialog.close();
  if (resolve) resolve(value);
}

function confirmPanel(items) {
  addTypedTags();
  const folder = selectedFolder();
  if (!folder) {
    toast("Choose a folder", { kind: "error" });
    return;
  }
  write(localStorage, LAST_FOLDER_KEY, folder.id);
  write(sessionStorage, KEEP_KEY, keepBox.checked);
  write(sessionStorage, KEPT_TAGS_KEY, keepBox.checked ? panel.userTags : []);
  const removeTags = {};
  if (items.length === 1 && panel.removed.size) removeTags[items[0].id] = [...panel.removed];
  close({
    folder_id: folder.id,
    folder_path: folder.path,
    add_tags: [...panel.userTags],
    remove_tags_per_file: removeTags,
  });
}

let currentItems = [];

form.addEventListener("submit", (event) => {
  event.preventDefault();
  confirmPanel(currentItems);
});
form.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.isComposing) return;
  if (event.target === tagInput && tagInput.value.trim()) {
    event.preventDefault();
    addTypedTags();
  } else if (event.target === newFolder) {
    event.preventDefault();
    createSubfolder();
  } else if (!(event.target instanceof HTMLButtonElement)) {
    event.preventDefault();
    confirmPanel(currentItems);
  }
});
tagInput.addEventListener("input", () => {
  if (tagInput.value.includes(",")) addTypedTags();
});
search.addEventListener("input", filterFolders);
search.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  const visible = [...picker.children].filter((li) => !li.hidden);
  const at = visible.findIndex((li) => li.querySelector("input").checked);
  const next = visible[Math.max(0, Math.min(visible.length - 1, at + (event.key === "ArrowDown" ? 1 : -1)))];
  if (next) select(Number(next.querySelector("input").value));
});
document.getElementById("create-subfolder").addEventListener("click", createSubfolder);
document.getElementById("archive-cancel").addEventListener("click", () => close(null));
dialog.addEventListener("cancel", (event) => {
  event.preventDefault(); // Esc: close through close() so the promise resolves
  close(null);
});
attachAutocomplete(tagInput);

// The crawl source's default folder, when every item came with the same one.
export function suggestedFolder(items) {
  const ids = new Set(items.map((item) => item.default_folder_id ?? null));
  return ids.size === 1 ? [...ids][0] : null;
}

export async function openArchivePanel(items) {
  if (!items.length || panel.resolve) return null;
  currentItems = items;
  const single = items.length === 1;
  const name = single ? items[0].original_filename || `file ${items[0].id}` : `${items.length} files`;
  subject.textContent = single ? name : `${name} (folder and added tags apply to every file)`;
  panel.removed = new Set();
  panel.userTags = keptTags();
  keepBox.checked = read(sessionStorage, KEEP_KEY, false);
  tagInput.value = "";
  newFolder.value = "";
  renderUserTags();
  crawlerBlock.hidden = !single;
  bulkNote.hidden = single;
  crawlerList.replaceChildren();
  const promise = new Promise((resolve) => {
    panel.resolve = resolve;
  });
  const sourceFolder = suggestedFolder(items);
  dialog.showModal();
  search.focus();
  try {
    const [, tags] = await Promise.all([
      loadFolders(sourceFolder),
      single ? api("GET", `/api/files/${items[0].id}/tags`) : Promise.resolve(null),
    ]);
    if (tags && tags.ok) {
      renderCrawlerTags(tags.data.active.filter((t) => t.origin === "crawler").map((t) => t.name));
    }
  } catch (error) {
    toast(`Loading folders failed: ${error.message}`, { kind: "error" });
  }
  return promise;
}
