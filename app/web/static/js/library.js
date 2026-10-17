// Library page: folder actions (create, rename, move, delete); the grid, its selection and bulk
// actions come from file_grid.js. Changes go to the JSON API; the page then reloads so the tree
// counts and the grid show the new state.
import { api, flashAndReload, showFlash, toast } from "./common.js";
import { setupFileGrid } from "./file_grid.js";

const root = document.getElementById("library");

function folderLibraryUrl(folderId) {
  const url = new URL(location.href);
  url.searchParams.delete("page");
  if (folderId) url.searchParams.set("folder", String(folderId));
  else url.searchParams.delete("folder");
  return url.toString();
}

// --- folder actions --------------------------------------------------------------------------

const currentFolder = Number(root.dataset.folderId) || null;

async function folderAction(form) {
  const kind = form.dataset.folderAction;
  const data = new FormData(form);
  let result;
  if (kind === "create") {
    const parent = Number(data.get("parent_id")) || null;
    result = await api("POST", "/api/folders", { name: data.get("name"), parent_id: parent });
    if (result.ok) flashAndReload(`Created ${result.data.path}`, folderLibraryUrl(result.data.id));
  } else if (kind === "rename") {
    result = await api("PATCH", `/api/folders/${currentFolder}`, { name: data.get("name") });
    if (result.ok) flashAndReload(`Renamed to ${result.data.path}`);
  } else if (kind === "move") {
    const parent = Number(data.get("parent_id")) || null;
    result = await api("PATCH", `/api/folders/${currentFolder}`, { parent_id: parent });
    if (result.ok) flashAndReload(`Moved to ${result.data.path}`);
  } else if (kind === "delete") {
    const moveContents = data.get("move_contents_to_parent") === "true";
    const count = Number(form.dataset.subtreeCount) || 0;
    const what = count || form.dataset.hasChildren === "1"
      ? ` Its files and subfolders (${count} file(s) in all) move to the parent.`
      : "";
    if (!confirm(`Delete "${form.dataset.path}"?${moveContents ? what : ""}`)) return;
    const query = moveContents ? "?move_contents_to_parent=true" : "";
    const folder = await api("GET", `/api/folders/${currentFolder}`);
    result = await api("DELETE", `/api/folders/${currentFolder}${query}`);
    if (result.ok) {
      const parent = folder.ok ? folder.data.parent_id : null;
      flashAndReload(`Deleted ${form.dataset.path}`, folderLibraryUrl(parent));
    }
  }
  if (result && !result.ok) toast(result.message, { kind: "error" });
}

for (const form of root.querySelectorAll("[data-folder-action]")) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    folderAction(form);
  });
}

setupFileGrid(root, {
  onKey(key) {
    if (key !== "n") return false;
    document.getElementById("new-folder-name")?.focus();
    return true;
  },
});
showFlash();
