// ─── State ────────────────────────────────────────────────────────────────────
let videoKey = null;
let selectedMaxInserts = 3;
let currentSuggestions = [];

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const dropzone      = document.getElementById("dropzone");
const fileInput     = document.getElementById("fileInput");
const dropzoneText  = document.getElementById("dropzoneText");
const fileInfo      = document.getElementById("fileInfo");
const errorEl       = document.getElementById("error");

// Tabs
const tabBtns = document.querySelectorAll(".tab-btn");
const tabContents = document.querySelectorAll(".tab-content");

// B-Roll tab
const scanBtn       = document.getElementById("scanBtn");
const countBtns     = document.querySelectorAll(".count-btn");
const scanProgress  = document.getElementById("scanProgress");
const brollResults  = document.getElementById("brollResults");
const slotCards     = document.getElementById("slotCards");
const applyBtn      = document.getElementById("applyBtn");
const rescanBtn     = document.getElementById("rescanBtn");
const brollOutput   = document.getElementById("brollOutput");
const brollStatus   = document.getElementById("brollStatus");
const brollPreview  = document.getElementById("brollPreview");
const brollVideo    = document.getElementById("brollVideo");
const brollDownload = document.getElementById("brollDownload");
const brollNewBtn   = document.getElementById("brollNewBtn");

// Editor tab
const analyzeBtn    = document.getElementById("analyzeBtn");
const prompt        = document.getElementById("prompt");
const planSection   = document.getElementById("planSection");
const planInfo      = document.getElementById("planInfo");
const tasksEditorWrap = document.getElementById("tasksEditorWrap");
const tasksEditor   = document.getElementById("tasksEditor");
const renderBtn     = document.getElementById("renderBtn");
const resultSection = document.getElementById("resultSection");
const statusEl      = document.getElementById("status");
const preview       = document.getElementById("preview");
const previewVideo  = document.getElementById("previewVideo");
const downloadBtn   = document.getElementById("downloadBtn");

// ─── Utilities ────────────────────────────────────────────────────────────────
function showError(msg) {
  errorEl.textContent = msg;
  errorEl.classList.remove("hidden");
  setTimeout(() => errorEl.classList.add("hidden"), 8000);
}

function hideError() {
  errorEl.classList.add("hidden");
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

function updateButtons() {
  const hasVideo = !!videoKey;
  scanBtn.disabled = !hasVideo;
  analyzeBtn.disabled = !(hasVideo && prompt.value.trim());
}

// ─── Tab switching ─────────────────────────────────────────────────────────────
tabBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    tabBtns.forEach(b => b.classList.remove("active"));
    tabContents.forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

// ─── Upload ───────────────────────────────────────────────────────────────────
dropzone.addEventListener("click", () => fileInput.click());

dropzone.addEventListener("dragover", e => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});

dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));

dropzone.addEventListener("drop", e => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  const file = e.dataTransfer.files[0];
  if (file && file.type.startsWith("video/")) handleFile(file);
  else showError("Выберите видеофайл (mp4, mov, avi, webm)");
});

fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) handleFile(fileInput.files[0]);
});

async function handleFile(file) {
  hideError();
  const formData = new FormData();
  formData.append("file", file);

  dropzoneText.textContent = "Загрузка…";
  scanBtn.disabled = true;
  analyzeBtn.disabled = true;

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    videoKey = data.video_key;
    fileInfo.textContent = `${file.name}  (${(file.size / 1024 / 1024).toFixed(1)} MB) ✓`;
    fileInfo.classList.remove("hidden");
    dropzone.classList.add("has-file");
    dropzoneText.textContent = "Файл загружен";
  } catch (err) {
    showError(err.message || "Ошибка загрузки");
    dropzoneText.textContent = "Перетащите файл сюда или нажмите для выбора";
  }

  updateButtons();
}

// ─── Count selector ───────────────────────────────────────────────────────────
countBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    countBtns.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    selectedMaxInserts = parseInt(btn.dataset.count);
  });
});

// ─── B-Roll: Scan ─────────────────────────────────────────────────────────────
scanBtn.addEventListener("click", startScan);
rescanBtn.addEventListener("click", startScan);

async function startScan() {
  if (!videoKey) return;
  hideError();

  // Reset UI
  brollResults.classList.add("hidden");
  brollOutput.classList.add("hidden");
  scanProgress.classList.remove("hidden");
  setProgressStep("transcribe");

  // Fake step progression while waiting
  const stepTimer = setTimeout(() => setProgressStep("analyze"), 10000);
  const stepTimer2 = setTimeout(() => setProgressStep("queries"), 30000);

  try {
    const res = await fetch("/broll-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_key: videoKey, max_inserts: selectedMaxInserts }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    currentSuggestions = data.suggestions || [];

    clearTimeout(stepTimer);
    clearTimeout(stepTimer2);
    scanProgress.classList.add("hidden");

    if (currentSuggestions.length === 0) {
      showError("Не удалось найти подходящие места для вставок");
      return;
    }

    renderSlotCards(currentSuggestions);
    brollResults.classList.remove("hidden");
  } catch (err) {
    clearTimeout(stepTimer);
    clearTimeout(stepTimer2);
    scanProgress.classList.add("hidden");
    showError(err.message || "Ошибка при сканировании");
  }
}

function setProgressStep(step) {
  document.querySelectorAll(".progress-step").forEach(el => el.classList.remove("active", "done"));
  const steps = ["transcribe", "analyze", "queries"];
  const idx = steps.indexOf(step);
  steps.forEach((s, i) => {
    const el = document.getElementById(`step-${s}`);
    if (i < idx) el.classList.add("done");
    else if (i === idx) el.classList.add("active");
  });
}

function renderSlotCards(suggestions) {
  slotCards.innerHTML = "";
  suggestions.forEach((slot, i) => {
    const card = document.createElement("div");
    card.className = "slot-card";
    card.innerHTML = `
      <div class="slot-card-header">
        <label class="slot-check-label">
          <input type="checkbox" class="slot-check" data-index="${i}" checked>
          <span class="slot-num">Вставка ${i + 1}</span>
        </label>
        <span class="slot-time">${formatTime(slot.start)} – ${formatTime(slot.end)} · ${slot.duration}s</span>
      </div>
      <div class="slot-context">${escapeHtml(slot.context_text)}</div>
      <div class="slot-query-row">
        <span class="slot-query-label">🎬 Поиск в стоке:</span>
        <input type="text" class="slot-query-input" data-index="${i}" value="${escapeHtml(slot.query)}">
      </div>
    `;
    slotCards.appendChild(card);
  });

  // Sync edits back to currentSuggestions
  slotCards.querySelectorAll(".slot-query-input").forEach(input => {
    input.addEventListener("input", () => {
      currentSuggestions[parseInt(input.dataset.index)].query = input.value;
    });
  });
}

// ─── B-Roll: Apply ────────────────────────────────────────────────────────────
applyBtn.addEventListener("click", async () => {
  if (!videoKey || currentSuggestions.length === 0) return;
  hideError();

  // Build slots payload
  const slots = currentSuggestions.map((s, i) => {
    const checkbox = slotCards.querySelector(`.slot-check[data-index="${i}"]`);
    const queryInput = slotCards.querySelector(`.slot-query-input[data-index="${i}"]`);
    return {
      start: s.start,
      end: s.end,
      duration: s.duration,
      context_text: s.context_text,
      query: queryInput ? queryInput.value : s.query,
      alternative_queries: s.alternative_queries || [],
      enabled: checkbox ? checkbox.checked : true,
    };
  });

  const enabledCount = slots.filter(s => s.enabled).length;
  if (enabledCount === 0) {
    showError("Выберите хотя бы одну вставку");
    return;
  }

  applyBtn.disabled = true;
  applyBtn.textContent = `Скачиваю клипы и рендерю… (${enabledCount} вставки)`;

  try {
    const res = await fetch("/broll-apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_key: videoKey, slots }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();

    brollResults.classList.add("hidden");
    brollOutput.classList.remove("hidden");
    brollStatus.textContent = `Готово! ${enabledCount} вставки добавлены.`;

    const url = (data.download_url.startsWith("/") ? window.location.origin : "") + data.download_url;
    brollVideo.src = url;
    brollPreview.classList.remove("hidden");
    brollDownload.href = url;
    brollDownload.download = "with_broll.mp4";
    brollDownload.classList.remove("hidden");
  } catch (err) {
    showError(err.message || "Ошибка при применении вставок");
  } finally {
    applyBtn.disabled = false;
    applyBtn.textContent = "Применить вставки";
  }
});

brollNewBtn.addEventListener("click", () => {
  brollOutput.classList.add("hidden");
  brollResults.classList.remove("hidden");
});

// ─── Editor tab ───────────────────────────────────────────────────────────────
prompt.addEventListener("input", updateButtons);

analyzeBtn.addEventListener("click", async () => {
  if (!videoKey || !prompt.value.trim()) return;
  hideError();
  planSection.classList.add("hidden");
  resultSection.classList.add("hidden");
  analyzeBtn.disabled = true;
  analyzeBtn.textContent = "Анализирую…";

  try {
    const res = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_key: videoKey, prompt: prompt.value.trim() }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();

    planInfo.innerHTML = `
      <p><strong>Сценарий:</strong> ${escapeHtml(data.scenario_name)}</p>
      <p><strong>Описание:</strong> ${escapeHtml(data.scenario_description)}</p>
    `;
    tasksEditor.value = JSON.stringify(data.tasks, null, 2);
    tasksEditorWrap.style.display = "block";
    planSection.classList.remove("hidden");
  } catch (err) {
    showError(err.message || "Ошибка анализа");
  } finally {
    analyzeBtn.disabled = false;
    analyzeBtn.textContent = "Анализировать";
    updateButtons();
  }
});

renderBtn.addEventListener("click", async () => {
  if (!videoKey) return;
  let tasks;
  try {
    tasks = JSON.parse(tasksEditor.value);
  } catch {
    showError("Некорректный JSON в задачах");
    return;
  }
  if (!Array.isArray(tasks)) { showError("Задачи должны быть массивом"); return; }

  hideError();
  resultSection.classList.remove("hidden");
  statusEl.textContent = "Рендеринг…";
  statusEl.classList.add("loading");
  preview.classList.add("hidden");
  downloadBtn.classList.add("hidden");
  renderBtn.disabled = true;

  try {
    const res = await fetch("/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_key: videoKey, tasks }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();

    statusEl.textContent = "Готово ✓";
    statusEl.classList.remove("loading");
    const url = (data.download_url.startsWith("/") ? window.location.origin : "") + data.download_url;
    previewVideo.src = url;
    preview.classList.remove("hidden");
    downloadBtn.href = url;
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
