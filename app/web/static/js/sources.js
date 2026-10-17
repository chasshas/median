// Keys of the sources and job pages: N opens the new-source form, Esc leaves a run's page.
import { isTyping } from "./common.js";

document.addEventListener("keydown", (event) => {
  if (isTyping(event.target) || event.ctrlKey || event.metaKey || event.altKey) return;
  if (document.querySelector("dialog[open]")) return;
  const newAddon = document.getElementById("new-addon");
  if ((event.key === "n" || event.key === "N") && newAddon) {
    event.preventDefault();
    newAddon.form.submit();
  } else if (event.key === "Escape" && document.getElementById("job-live")) {
    event.preventDefault();
    window.location.href = "/jobs";
  }
});
