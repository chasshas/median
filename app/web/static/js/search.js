// Search page: query box autocomplete, the filter panel kept in sync with the query box (both
// ways, through /api/search/parse), saved searches, and the library grid from file_grid.js.
import { api, flashAndReload, showFlash, toast } from "./common.js";
import { setupFileGrid } from "./file_grid.js";

const root = document.getElementById("search");
const queryForm = document.getElementById("query-form");
const input = document.getElementById("search-q");
const list = document.getElementById("search-suggest");
const panel = document.getElementById("panel-form");
const errorBox = document.getElementById("search-error");
const warningList = document.getElementById("search-warnings");

function searchUrl(query) {
  const url = new URL("/search", location.origin);
  url.searchParams.set("q", query);
  const sort = document.getElementById("search-sort").value;
  if (sort !== "archived_desc") url.searchParams.set("sort", sort);
  const seed = queryForm.elements.seed?.value;
  if (sort === "random" && seed) url.searchParams.set("seed", seed);
  return url;
}

function showProblems(error, warnings = []) {
  errorBox.hidden = !error;
  errorBox.textContent = error
    ? error.message + (Number.isInteger(error.position) ? ` (at character ${error.position + 1})` : "")
    : "";
  warningList.replaceChildren(
    ...warnings.map((w) => Object.assign(document.createElement("li"), { textContent: w.message })),
  );
}

function errorOf(result) {
  const detail = result.data && result.data.detail;
  return detail && typeof detail === "object" && !Array.isArray(detail)
    ? detail
    : { message: result.message };
}

// --- query box -> panel ----------------------------------------------------------------------

function fillPanel(fields) {
  for (const name of ["folder", "tags_all", "tags_any", "tags_none", "source",
    "collected_from", "collected_to", "archived_from", "archived_to"]) {
    panel.elements[name].value = fields[name];
  }
  panel.elements.exact.checked = Boolean(fields.exact);
  for (const box of panel.querySelectorAll('input[name="type"], input[name="status"]')) {
    box.checked = fields[box.name].includes(box.value);
  }
  const holder = document.getElementById("text-fields");
  const template = holder.lastElementChild;
  const rows = [...fields.text, ""].map((value) => {
    const row = template.cloneNode(true);
    row.querySelector("input").value = value;
    return row;
  });
  holder.replaceChildren(...rows);
}

let parseTimer = null;
let parseSeq = 0;
input.addEventListener("input", () => {
  clearTimeout(parseTimer);
  parseTimer = setTimeout(async () => {
    const mine = ++parseSeq;
    const result = await api("GET", `/api/search/parse?q=${encodeURIComponent(input.value)}`);
    if (mine !== parseSeq) return;
    if (result.ok) {
      fillPanel(result.data.fields);
      showProblems(null, result.data.warnings);
    } else {
      showProblems(errorOf(result));
    }
  }, 300);
});

// --- panel -> query box and URL --------------------------------------------------------------

async function panelChanged() {
  const params = new URLSearchParams(new FormData(panel));
  params.delete("panel");
  params.delete("sort");
  params.delete("seed");
  const result = await api("GET", `/api/search/parse?${params}`);
  if (!result.ok) {
    showProblems(errorOf(result));
    return;
  }
  input.value = result.data.query;
  const url = searchUrl(result.data.query);
  history.replaceState(null, "", url);
  location.assign(url); // run the search
}

panel.addEventListener("change", (event) => {
  if (event.target.matches("input, select")) panelChanged();
});
panel.addEventListener("submit", (event) => {
  event.preventDefault();
  panelChanged();
});

// --- autocomplete ----------------------------------------------------------------------------

// The token around the caret: [start, end) of the whitespace-separated token, quotes respected.
function tokenAt(text, caret) {
  let start = 0;
  let quoted = false;
  for (let i = 0; i < caret; i++) {
    if (text[i] === '"') quoted = !quoted;
    else if (!quoted && /\s/.test(text[i])) start = i + 1;
  }
  let end = caret;
  while (end < text.length && (quoted || !/\s/.test(text[end]))) {
    if (text[end] === '"') quoted = !quoted;
    end++;
  }
  return { start, end, token: text.slice(start, caret) };
}

let suggestions = [];
let active = -1;
let suggestSeq = 0;
let suggestTimer = null;

function closeSuggestions() {
  suggestions = [];
  active = -1;
  list.hidden = true;
  list.replaceChildren();
  input.removeAttribute("aria-activedescendant");
}

function renderSuggestions() {
  list.replaceChildren(
    ...suggestions.map((s, i) => {
      const li = document.createElement("li");
      li.id = `suggest-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.textContent = s.label;
      li.addEventListener("mousedown", (event) => {
        event.preventDefault();
        accept(i);
      });
      return li;
    }),
  );
  list.hidden = !suggestions.length;
  if (active >= 0) input.setAttribute("aria-activedescendant", `suggest-${active}`);
}

function accept(index) {
  const choice = suggestions[index];
  if (!choice) return;
  const { start, end } = tokenAt(input.value, input.selectionStart ?? input.value.length);
  const after = input.value.slice(end);
  const glue = choice.value.endsWith(":") ? "" : after.startsWith(" ") ? "" : " ";
  input.value = input.value.slice(0, start) + choice.value + glue + after;
  const caret = start + choice.value.length + glue.length;
  input.setSelectionRange(caret, caret);
  closeSuggestions();
  input.dispatchEvent(new Event("input"));
}

input.addEventListener("input", () => {
  clearTimeout(suggestTimer);
  closeSuggestions(); // never offer suggestions for what was typed before
  suggestTimer = setTimeout(async () => {
    const { token } = tokenAt(input.value, input.selectionStart ?? input.value.length);
    const mine = ++suggestSeq;
    if (!token) {
      closeSuggestions();
      return;
    }
    const result = await api("GET", `/api/search/suggest?token=${encodeURIComponent(token)}`);
    if (mine !== suggestSeq || !result.ok) return;
    suggestions = result.data.suggestions;
    active = -1;
    renderSuggestions();
  }, 120);
});

input.addEventListener("keydown", (event) => {
  if (list.hidden || !suggestions.length) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const step = event.key === "ArrowDown" ? 1 : -1;
    active = (active + step + suggestions.length) % suggestions.length;
    renderSuggestions();
    event.preventDefault();
  } else if (event.key === "Enter" && active >= 0) {
    accept(active);
    event.preventDefault();
  } else if (event.key === "Tab") {
    accept(Math.max(active, 0));
    event.preventDefault();
  } else if (event.key === "Escape") {
    closeSuggestions();
    event.stopPropagation();
  }
});
input.addEventListener("blur", () => setTimeout(closeSuggestions, 100));

// --- saved searches --------------------------------------------------------------------------

document.getElementById("save-search").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = event.target.elements.name.value.trim();
  const query = root.dataset.query;
  const result = await api("POST", "/api/saved-searches", { name, query });
  if (result.ok) flashAndReload(`Saved "${result.data.name}"`);
  else toast(errorOf(result).message, { kind: "error" });
});

root.addEventListener("click", async (event) => {
  const rename = event.target.closest?.("[data-saved-rename]");
  const remove = event.target.closest?.("[data-saved-delete]");
  if (!rename && !remove) return;
  const item = event.target.closest("[data-saved-id]");
  const id = item.dataset.savedId;
  const name = item.dataset.savedName;
  if (rename) {
    const next = prompt(`Rename "${name}" to`, name);
    if (!next || next.trim() === name) return;
    const result = await api("PATCH", `/api/saved-searches/${id}`, { name: next });
    if (result.ok) flashAndReload(`Renamed to "${result.data.name}"`);
    else toast(result.message, { kind: "error" });
  } else {
    if (!confirm(`Delete the saved search "${name}"?`)) return;
    const result = await api("DELETE", `/api/saved-searches/${id}`);
    if (result.ok) flashAndReload(`Deleted "${name}"`);
    else toast(result.message, { kind: "error" });
  }
});

// --- keys ------------------------------------------------------------------------------------

setupFileGrid(root, {
  onKey(key) {
    if (key === "/") {
      input.focus();
      input.select();
      return true;
    }
    if (key === "f") {
      const details = document.getElementById("filter-panel");
      details.open = !details.open;
      return true;
    }
    return false;
  },
});
showFlash();
