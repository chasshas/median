// Upload page: drag and drop, one multipart POST per selection, then htmx polls the batch.
const form = document.getElementById("upload-form");
const input = document.getElementById("upload-files");
const dropzone = document.getElementById("dropzone");
const selection = document.getElementById("upload-selection");
const progress = document.getElementById("upload-progress");
const message = document.getElementById("upload-message");
const batches = document.getElementById("upload-batches");
const submit = document.getElementById("upload-submit");

function describe() {
  const n = input.files.length;
  selection.textContent = n ? `${n} file${n === 1 ? "" : "s"} selected` : "No files selected";
}

for (const name of ["dragenter", "dragover"]) {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
}
for (const name of ["dragleave", "drop"]) {
  dropzone.addEventListener(name, () => dropzone.classList.remove("dragging"));
}
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer?.files.length) {
    input.files = event.dataTransfer.files;
    describe();
  }
});
input.addEventListener("change", describe);

function showBatch(batchId) {
  const el = document.createElement("div");
  el.setAttribute("hx-get", `/api/uploads/${batchId}`);
  el.setAttribute("hx-trigger", "load");
  el.setAttribute("hx-swap", "outerHTML");
  batches.prepend(el);
  window.htmx.process(el);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!input.files.length) {
    message.textContent = "Choose at least one file.";
    return;
  }
  const data = new FormData(form);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", form.action);
  xhr.responseType = "json";
  xhr.upload.addEventListener("progress", (e) => {
    if (e.lengthComputable) progress.value = Math.round((e.loaded / e.total) * 100);
  });
  xhr.addEventListener("load", () => {
    submit.disabled = false;
    progress.hidden = true;
    if (xhr.status === 202 && xhr.response?.batch_id) {
      message.textContent = `Uploaded ${xhr.response.files} file(s).`;
      form.reset();
      describe();
      showBatch(xhr.response.batch_id);
    } else {
      message.textContent = `Upload failed: ${xhr.response?.detail ?? xhr.status}`;
    }
  });
  xhr.addEventListener("error", () => {
    submit.disabled = false;
    progress.hidden = true;
    message.textContent = "Upload failed: network error.";
  });
  submit.disabled = true;
  progress.hidden = false;
  progress.value = 0;
  message.textContent = "Uploading…";
  xhr.send(data);
});

document.addEventListener("keydown", (event) => {
  const t = event.target;
  const typing = t instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName);
  if (!typing && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "u") {
    event.preventDefault();
    input.click();
  }
});
