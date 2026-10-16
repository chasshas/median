// Keyboard help: "?" toggles the shortcuts dialog defined in base.html.
const dialog = document.getElementById("shortcuts");

function toggle() {
  if (!dialog) return;
  if (dialog.open) dialog.close();
  else dialog.showModal();
}

function isTyping(target) {
  return target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

document.addEventListener("keydown", (event) => {
  if (event.key === "?" && !isTyping(event.target) && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    toggle();
  }
});

for (const button of document.querySelectorAll("[data-shortcuts-open]")) {
  button.addEventListener("click", toggle);
}
