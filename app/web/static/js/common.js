// Helpers shared by the library, file, tags and archive panel modules: JSON API calls, toasts,
// tag autocomplete on <input list=...> and a one-shot message carried across a reload.

const FLASH_KEY = "median.flash";

export function isTyping(target) {
  return target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

export function errorText(data, response) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return `${response.status} ${response.statusText}`;
}

// api("POST", url, body) -> {ok, status, data, message}; never throws.
export async function api(method, url, body = undefined) {
  try {
    const options = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      return { ok: false, status: response.status, data, message: errorText(data, response) };
    }
    return { ok: true, status: response.status, data, message: "" };
  } catch (error) {
    return { ok: false, status: 0, data: null, message: `Network error: ${error.message}` };
  }
}

// Messages of the files a bulk call rejected (results with ok false), deduplicated.
export function failures(data) {
  const results = (data && data.results) || [];
  return [...new Set(results.filter((r) => !r.ok).map((r) => r.error))];
}

export function toast(message, { kind = "info" } = {}) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  box.append(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  setTimeout(() => el.remove(), kind === "error" ? 12000 : 5000);
}

export function flashAndReload(message, url = null) {
  try {
    sessionStorage.setItem(FLASH_KEY, message);
  } catch {
    // Without storage the message is simply not shown after the reload.
  }
  if (url) location.assign(url);
  else location.reload();
}

export function showFlash() {
  let message = null;
  try {
    message = sessionStorage.getItem(FLASH_KEY);
    sessionStorage.removeItem(FLASH_KEY);
  } catch {
    return;
  }
  if (message) toast(message);
}

// Fill the input's <datalist> with tags that start with what is typed (most used first).
export function attachAutocomplete(input) {
  const list = input.list;
  if (!list || input.dataset.autocompleteReady) return;
  input.dataset.autocompleteReady = "1";
  let timer = null;
  let seq = 0;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const prefix = input.value;
    if (!prefix.trim()) {
      list.replaceChildren();
      return;
    }
    timer = setTimeout(async () => {
      const mine = ++seq;
      const url = `/api/tags/autocomplete?limit=10&prefix=${encodeURIComponent(prefix)}`;
      const result = await api("GET", url);
      if (mine !== seq || !result.ok) return;
      list.replaceChildren(
        ...result.data.tags.map((tag) => {
          const option = document.createElement("option");
          option.value = tag.name;
          option.label = `${tag.name} (${tag.count})`;
          return option;
        }),
      );
    }, 120);
  });
}
