const videoGrid = document.querySelector("[data-video-api]");
const makeSelect = document.querySelector("#make-select");
const modelSelect = document.querySelector("#model-select");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderVideo(video) {
  const title = escapeHtml(video.title);
  const description = escapeHtml(video.description);
  const watchUrl = escapeHtml(video.watch_url);
  const embedUrl = escapeHtml(video.embed_url);
  const scope = escapeHtml([video.make || "All makes", video.model || ""].join(" ").trim());

  return `
    <article class="video-card">
      <div class="video-frame">
        <iframe src="${embedUrl}" title="${title}" loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
      </div>
      <div class="video-copy">
        <h3>${title}</h3>
        <p>${description}</p>
        <span>${scope}</span>
        <a href="${watchUrl}" target="_blank" rel="noopener">Open on YouTube</a>
      </div>
    </article>
  `;
}

async function hydrateVideos() {
  if (!videoGrid) return;
  const endpoint = videoGrid.dataset.videoApi;
  if (!endpoint) return;

  try {
    const response = await fetch(endpoint, { headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const videos = await response.json();
    if (!Array.isArray(videos) || videos.length === 0) return;
    videoGrid.innerHTML = videos.map(renderVideo).join("");
  } catch {
    // Server-rendered cards stay visible if the API is unavailable.
  }
}

hydrateVideos();

async function hydrateModels() {
  if (!makeSelect || !modelSelect) return;
  const make = makeSelect.value;
  const selected = modelSelect.dataset.selected || "";

  modelSelect.innerHTML = '<option value="">Select model...</option>';
  if (!make) return;

  try {
    const response = await fetch(`/api/models?make=${encodeURIComponent(make)}`, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    const models = await response.json();
    if (!Array.isArray(models)) return;
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      option.selected = model === selected;
      modelSelect.append(option);
    }
  } catch {
    // Server-rendered options stay available if the API is unavailable.
  }
}

if (makeSelect && modelSelect) {
  makeSelect.addEventListener("change", () => {
    modelSelect.dataset.selected = "";
    hydrateModels();
  });
}

function setButtonBusy(form) {
  const button = form.querySelector("button[type='submit'], button:not([type])");
  if (!button) return;
  button.dataset.originalText = button.textContent;
  button.textContent = button.dataset.busyText || "Working...";
  button.disabled = true;
  form.classList.add("is-submitting");
}

function clearButtonBusy(form) {
  const button = form.querySelector("button[type='submit'], button:not([type])");
  if (!button) return;
  button.textContent = button.dataset.originalText || "Submit";
  button.disabled = false;
  form.classList.remove("is-submitting");
}

function setupUploadProgress() {
  const form = document.querySelector("form[action='/admin/upload']");
  if (!form) return;
  const input = form.querySelector("input[type='file']");
  const status = form.querySelector("[data-upload-status]");
  const meter = form.querySelector("[data-upload-meter]");
  const message = form.querySelector("[data-upload-message]");
  if (!input || !status || !meter || !message) return;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const files = Array.from(input.files || []);
    if (!files.length) {
      message.textContent = "Choose at least one PDF.";
      status.hidden = false;
      return;
    }

    const formData = new FormData(form);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");

    status.hidden = false;
    meter.style.width = "0%";
    message.textContent = `Uploading ${files.length} PDF file${files.length === 1 ? "" : "s"}...`;
    setButtonBusy(form);

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.max(1, Math.round((event.loaded / event.total) * 100));
      meter.style.width = `${percent}%`;
      message.textContent = `Uploading files: ${percent}%`;
    });

    xhr.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(xhr.responseText || "{}");
      } catch {
        payload = {};
      }
      if (xhr.status >= 200 && xhr.status < 300 && payload.ok) {
        meter.style.width = "100%";
        message.textContent = payload.message || "Upload complete. OCR job started.";
        window.setTimeout(() => {
          window.location.href = `/admin?message=${encodeURIComponent(message.textContent)}`;
        }, 900);
      } else {
        message.textContent = payload.message || `Upload failed with status ${xhr.status}.`;
        clearButtonBusy(form);
      }
    });

    xhr.addEventListener("error", () => {
      message.textContent = "Upload failed before reaching the server. Try fewer files or smaller batches.";
      clearButtonBusy(form);
    });

    xhr.send(formData);
  });
}

setupUploadProgress();

function setupReportCopyDeterrents() {
  const report = document.querySelector(".report");
  if (!report) return;

  report.addEventListener("contextmenu", (event) => {
    event.preventDefault();
  });

  report.addEventListener("dragstart", (event) => {
    event.preventDefault();
  });

  document.addEventListener("keydown", (event) => {
    const key = event.key.toLowerCase();
    const blocked = (event.ctrlKey || event.metaKey) && ["p", "s", "u", "c"].includes(key);
    if (blocked && report.contains(document.activeElement)) {
      event.preventDefault();
    }
    if ((event.ctrlKey || event.metaKey) && key === "p") {
      event.preventDefault();
    }
  });
}

setupReportCopyDeterrents();

for (const form of document.querySelectorAll("form")) {
  if (form.action.endsWith("/admin/upload")) continue;
  form.addEventListener("submit", () => {
    setButtonBusy(form);
  });
}
