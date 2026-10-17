// File page: title, description and folder editing; tag chips (remove, add with autocomplete,
// restore removed ones); prev/next within the library context the page was opened from.
import { api, attachAutocomplete, flashAndReload, isTyping, showFlash, toast } from "./common.js";

const page = document.getElementById("file-page");
const fileId = Number(page.dataset.fileId);
const nav = document.querySelector(".file-nav");

// --- details ---------------------------------------------------------------------------------

const details = page.querySelector("[data-file-details]");
details?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(details);
  const body = { title: data.get("title"), description: data.get("description") };
  if (data.has("folder_id")) body.folder_id = Number(data.get("folder_id"));
  const result = await api("PATCH", `/api/files/${fileId}`, body);
  if (result.ok) flashAndReload("Saved");
  else toast(result.message, { kind: "error" });
});

// --- tags ------------------------------------------------------------------------------------

function editor() {
  return document.getElementById("file-tag-editor");
}

async function refreshTags(focusInput = false) {
  try {
    const response = await fetch(`/files/${fileId}/fragments/tags`);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    editor().outerHTML = await response.text();
  } catch (error) {
    toast(`Reloading the tags failed: ${error.message}`, { kind: "error" });
    return;
  }
  setupTags();
  if (focusInput) document.getElementById("file-tag-input")?.focus();
}

async function change(method, url, body, focusInput) {
  const result = await api(method, url, body);
  if (!result.ok) {
    toast(result.message, { kind: "error" });
    return;
  }
  await refreshTags(focusInput);
}

function setupTags() {
  const box = editor();
  if (!box) return;
  const input = box.querySelector("[data-autocomplete]");
  if (input) attachAutocomplete(input);
  box.querySelector("[data-tag-add]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = input.value.trim();
    if (name) change("POST", `/api/files/${fileId}/tags`, { name }, true);
  });
  box.addEventListener("click", (event) => {
    const remove = event.target.closest?.("[data-tag-remove]");
    if (remove) {
      change("DELETE", `/api/files/${fileId}/tags/${remove.dataset.tagRemove}`, undefined, false);
      return;
    }
    const restore = event.target.closest?.("[data-tag-restore]");
    if (restore) {
      const url = `/api/files/${fileId}/tags/${restore.dataset.tagRestore}/restore`;
      change("POST", url, undefined, false);
    }
  });
}

// --- keyboard --------------------------------------------------------------------------------

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
  if (document.querySelector("dialog[open]") || event.target instanceof HTMLMediaElement) return;
  if (isTyping(event.target)) {
    if (event.key === "Escape") event.target.blur();
    return;
  }
  const key = event.key.toLowerCase();
  let handled = true;
  if (key === "j" && nav?.dataset.nextUrl) location.assign(nav.dataset.nextUrl);
  else if (key === "k" && nav?.dataset.prevUrl) location.assign(nav.dataset.prevUrl);
  else if (key === "t") document.getElementById("file-tag-input")?.focus();
  else if (key === "e") details?.elements.title?.focus();
  else handled = false;
  if (handled) event.preventDefault();
});

setupTags();
showFlash();
