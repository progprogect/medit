const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const dropzoneText = document.getElementById("dropzoneText");
const fileInfo = document.getElementById("fileInfo");
const promptInput = document.getElementById("prompt");
const analyzeBtn = document.getElementById("analyzeBtn");
const planSection = document.getElementById("planSection");
const planInfo = document.getElementById("planInfo");
const tasksEditorWrap = document.getElementById("tasksEditorWrap");
const tasksEditor = document.getElementById("tasksEditor");
const renderBtn = document.getElementById("renderBtn");
const resultSection = document.getElementById("resultSection");
const statusEl = document.getElementById("status");
const preview = document.getElementById("preview");
const previewVideo = document.getElementById("previewVideo");
const downloadBtn = document.getElementById("downloadBtn");
const errorEl = document.getElementById("error");

let videoKey = null;

function hideError() {
  errorEl.classList.add("hidden");
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.classList.remove("hidden");
}

function updateAnalyzeButton() {
  analyzeBtn.disabled = !(videoKey && promptInput.value.trim());
}

// Drag and drop
dropzone.addEventListener("click", () => fileInput.click());

dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("dragover");
});

dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  const file = e.dataTransfer.files[0];
  if (file && file.type.startsWith("video/")) {
    handleFile(file);
  } else {
    showError("Выберите видеофайл (mp4, mov, avi, webm)");
  }
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) handleFile(file);
});

async function handleFile(file) {
  hideError();
  const formData = new FormData();
  formData.append("file", file);

  try {
    analyzeBtn.disabled = true;
    dropzoneText.textContent = "Загрузка…";
    const res = await fetch("/upload", {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    videoKey = data.video_key;
    fileInfo.textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB) ✓`;
    fileInfo.classList.remove("hidden");
    dropzone.classList.add("has-file");
    dropzoneText.textContent = "Файл загружен";
    updateAnalyzeButton();
  } catch (err) {
    showError(err.message || "Ошибка загрузки");
    dropzoneText.textContent = "Перетащите файл сюда или нажмите для выбора";
  } finally {
    analyzeBtn.disabled = false;
    updateAnalyzeButton();
  }
}

analyzeBtn.addEventListener("click", async () => {
  if (!videoKey || !promptInput.value.trim()) return;
  hideError();
  resultSection.classList.add("hidden");
  planSection.classList.remove("hidden");
  planInfo.innerHTML = "<p class='loading'>Анализ… может занять 1–2 минуты</p>";
  tasksEditorWrap.style.display = "none";
  renderBtn.style.display = "none";
  analyzeBtn.disabled = true;

  try {
    const res = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_key: videoKey,
        prompt: promptInput.value.trim(),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    planInfo.innerHTML = `
      <p><strong>Сценарий:</strong> ${escapeHtml(data.scenario_name)}</p>
      <p><strong>Описание:</strong> ${escapeHtml(data.scenario_description)}</p>
      ${data.metadata && Object.keys(data.metadata).length ? `<p><strong>Метаданные:</strong> <pre class="metadata-pre">${escapeHtml(JSON.stringify(data.metadata, null, 2))}</pre></p>` : ""}
    `;
    tasksEditor.value = JSON.stringify(data.tasks, null, 2);
    tasksEditorWrap.style.display = "block";
    renderBtn.style.display = "inline-block";
  } catch (err) {
    showError(err.message || "Ошибка анализа");
  } finally {
    analyzeBtn.disabled = false;
    updateAnalyzeButton();
  }
});

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

renderBtn.addEventListener("click", async () => {
  if (!videoKey) return;
  hideError();
  let tasks;
  try {
    tasks = JSON.parse(tasksEditor.value);
  } catch (e) {
    showError("Некорректный JSON в задачах. Проверьте синтаксис.");
    return;
  }
  if (!Array.isArray(tasks)) {
    showError("Задачи должны быть массивом.");
    return;
  }

  resultSection.classList.remove("hidden");
  statusEl.textContent = "Рендеринг… может занять несколько минут";
  statusEl.classList.add("loading");
  preview.classList.add("hidden");
  downloadBtn.classList.add("hidden");
  renderBtn.disabled = true;

  try {
    const res = await fetch("/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_key: videoKey,
        tasks,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    statusEl.textContent = "Готово ✓";
    statusEl.classList.remove("loading");
    const baseUrl = window.location.origin;
    const downloadUrl = data.download_url.startsWith("/") ? baseUrl + data.download_url : data.download_url;
    previewVideo.src = downloadUrl;
    preview.classList.remove("hidden");
    downloadBtn.href = downloadUrl;
    downloadBtn.download = "result.mp4";
    downloadBtn.classList.remove("hidden");
  } catch (err) {
    statusEl.textContent = "Ошибка";
    statusEl.classList.remove("loading");
    showError(err.message || "Ошибка рендеринга");
  } finally {
    renderBtn.disabled = false;
  }
});

promptInput.addEventListener("input", updateAnalyzeButton);
