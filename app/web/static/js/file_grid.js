// The library grid component (library and search pages): selection of files in the grid, bulk
// actions (move, add tag, remove tag, discard) and the grid keys. Changes go to the JSON API;
// the page then reloads so it shows the new state.
import { api, attachAutocomplete, failures, flashAndReload, isTyping, toast } from "./common.js";

// setupFileGrid(root, {onKey}): root holds the selection bar and the #library-grid list.
// onKey(key, event) handles page-specific keys and returns true when it did.
export function setupFileGrid(root, { onKey = () => false } = {}) {
  const grid = document.getElementById("library-grid");
  const selected = new Set();

  function tiles() {
    return grid ? [...grid.querySelectorAll(".library-tile")] : [];
  }

  function updateSelection() {
    for (const tile of tiles()) {
      const id = Number(tile.dataset.fileId);
      const on = selected.has(id);
      tile.classList.toggle("selected", on);
      const box = tile.querySelector("[data-select-file]");
      if (box) box.checked = on;
    }
    document.getElementById("selected-count").textContent = String(selected.size);
  }

  function toggle(id, on = !selected.has(id)) {
    if (on) selected.add(id);
    else selected.delete(id);
    updateSelection();
  }

  function requireSelection() {
    if (selected.size) return [...selected];
    toast("Select files first (X, or the checkboxes)");
    return null;
  }

  function finish(result, done) {
    if (!result.ok) {
      toast(result.message, { kind: "error" });
      return;
    }
    const problems = failures(result.data);
    const okCount = (result.data.results || []).filter((r) => r.ok).length;
    const message = problems.length ? `${done(okCount)}; ${problems.join("; ")}` : done(okCount);
    flashAndReload(message);
  }

  for (const form of root.querySelectorAll("[data-bulk]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const ids = requireSelection();
      if (!ids) return;
      const kind = form.dataset.bulk;
      if (kind === "move") {
        const select = form.elements.folder_id;
        const path = select.selectedOptions[0]?.textContent || "";
        const result = await api("POST", "/api/library/move", {
          file_ids: ids,
          folder_id: Number(select.value),
        });
        finish(result, (n) => `Moved ${n} file(s) to ${path}`);
        return;
      }
      const tag = form.elements.tag.value.trim();
      if (!tag) {
        toast("Type a tag first");
        form.elements.tag.focus();
        return;
      }
      const result = await api("POST", `/api/library/${kind}`, { file_ids: ids, tags: [tag] });
      const verb = kind === "tags/add" ? "Added" : "Removed";
      finish(result, (n) => `${verb} "${tag}" on ${n} file(s)`);
    });
  }

  root.querySelector("[data-bulk-discard]")?.addEventListener("click", async () => {
    const ids = requireSelection();
    if (!ids) return;
    if (!confirm(`Discard ${ids.length} file(s)? Their originals go to the trash.`)) return;
    const result = await api("POST", "/api/review/discard-archived", { file_ids: ids });
    finish(result, (n) => `Discarded ${n} file(s)`);
  });

  for (const input of root.querySelectorAll("[data-autocomplete]")) attachAutocomplete(input);

  grid?.addEventListener("change", (event) => {
    const box = event.target.closest?.("[data-select-file]");
    if (box) toggle(Number(box.dataset.selectFile), box.checked);
  });

  root.querySelector('[data-select="page"]')?.addEventListener("click", () => {
    for (const tile of tiles()) selected.add(Number(tile.dataset.fileId));
    updateSelection();
  });
  root.querySelector('[data-select="none"]')?.addEventListener("click", () => {
    selected.clear();
    updateSelection();
  });

  function focusedTile() {
    return document.activeElement?.closest?.(".library-tile") || null;
  }

  function moveFocus(delta) {
    const all = tiles();
    if (!all.length) return;
    const at = all.indexOf(focusedTile());
    const next = at < 0 ? (delta > 0 ? 0 : all.length - 1) : Math.min(all.length - 1, Math.max(0, at + delta));
    all[next].querySelector("a")?.focus();
  }

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    if (isTyping(event.target)) {
      if (event.key === "Escape") event.target.blur();
      return;
    }
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const focus = (id) => document.getElementById(id)?.focus();
    let handled = true;
    if (key === "j") moveFocus(1);
    else if (key === "k") moveFocus(-1);
    else if (key === "x") {
      const tile = focusedTile();
      if (tile) toggle(Number(tile.dataset.fileId));
      else handled = false;
    } else if (key === "a" && event.shiftKey) root.querySelector('[data-select="page"]')?.click();
    else if (key === "Escape") {
      selected.clear();
      updateSelection();
    } else if (key === "m") focus("bulk-folder");
    else if (key === "t") focus("bulk-add-tag");
    else handled = onKey(key, event);
    if (handled) event.preventDefault();
  });

  updateSelection();
}
