// Tags page: instant filter, rename (confirming a merge into an existing tag) and delete
// (confirming when the tag is in use).
import { api, flashAndReload, isTyping, showFlash, toast } from "./common.js";

const filter = document.getElementById("tag-filter");
const table = document.getElementById("tags-table");
const shown = document.getElementById("tag-shown");

function applyFilter() {
  if (!table) return;
  const needle = filter.value.trim().toLowerCase().replace(/\s+/g, "_");
  let count = 0;
  for (const row of table.tBodies[0].rows) {
    const match = !needle || row.dataset.tagName.includes(needle);
    row.hidden = !match;
    if (match) count += 1;
  }
  shown.textContent = String(count);
}

filter?.addEventListener("input", applyFilter);
filter?.closest("form")?.addEventListener("submit", (event) => {
  if (table) event.preventDefault(); // filtering is instant; the form is the no-JS fallback
});

async function rename(row, name) {
  const url = `/api/tags/${row.dataset.tagId}/rename`;
  let result = await api("POST", url, { name });
  if (!result.ok && result.status === 409 && result.data?.merge_into) {
    if (!confirm(`${result.message}. Merge?`)) return;
    result = await api("POST", url, { name, merge: true });
  }
  if (!result.ok) {
    toast(result.message, { kind: "error" });
    return;
  }
  const { tag, merged_from: mergedFrom } = result.data;
  flashAndReload(
    mergedFrom ? `Merged "${mergedFrom}" into "${tag.name}" (${tag.count} files)` : `Renamed to "${tag.name}"`,
  );
}

async function remove(row) {
  const count = Number(row.dataset.count) || 0;
  const name = row.dataset.tagName;
  const question = count
    ? `Delete "${name}"? It is used by ${count} file(s); it will be taken off all of them.`
    : `Delete "${name}"?`;
  if (!confirm(question)) return;
  const result = await api("DELETE", `/api/tags/${row.dataset.tagId}${count ? "?force=true" : ""}`);
  if (result.ok) flashAndReload(`Deleted "${name}"`);
  else toast(result.message, { kind: "error" });
}

table?.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-tag-rename]");
  if (!form) return;
  event.preventDefault();
  const row = form.closest("tr");
  const name = form.elements.name.value.trim();
  if (name && name !== row.dataset.tagName) rename(row, name);
});

table?.addEventListener("click", (event) => {
  const button = event.target.closest?.("[data-tag-delete]");
  if (button) remove(button.closest("tr"));
});

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || document.querySelector("dialog[open]")) return;
  if (event.key === "Escape" && event.target === filter) {
    filter.value = "";
    applyFilter();
    filter.blur();
    return;
  }
  if (event.key === "/" && !isTyping(event.target)) {
    event.preventDefault();
    filter?.focus();
  }
});

if (filter?.value) applyFilter();
showFlash();
