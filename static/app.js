// ─── State ────────────────────────────────────────────────────────────────────
let videoKey = null;
let selectedMaxInserts = 3;
let currentSuggestions = [];
let currentProjectId = null;
let createAssets = [];
let currentScenario = null;

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const scanHint      = document.getElementById("scanHint");
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

// Create Scenario tab
const legacyUploadSection = document.getElementById("legacyUploadSection");
const createDropzone = document.getElementById("createDropzone");
const createFileInput = document.getElementById("createFileInput");
const createDropzoneText = document.getElementById("createDropzoneText");
const assetList = document.getElementById("assetList");
const globalPrompt = document.getElementById("globalPrompt");
const referenceLink = document.getElementById("referenceLink");
const generateScenarioBtn = document.getElementById("generateScenarioBtn");
const createProgress = document.getElementById("createProgress");
const createScenarioScreen = document.getElementById("createScenarioScreen");
const scenarioScreen = document.getElementById("scenarioScreen");
const scenarioHeader = document.getElementById("scenarioHeader");
const scenesView = document.getElementById("scenesView");
const timelineView = document.getElementById("timelineView");
const viewTabs = document.querySelectorAll(".view-tab");

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
  if (scanHint) scanHint.classList.toggle("hidden", hasVideo);
  analyzeBtn.disabled = !(hasVideo && prompt.value.trim());
}

// ─── Tab switching ─────────────────────────────────────────────────────────────
tabBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    tabBtns.forEach(b => b.classList.remove("active"));
    tabContents.forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (legacyUploadSection) {
      legacyUploadSection.style.display = btn.dataset.tab === "create" ? "none" : "block";
    }
  });
});

if (legacyUploadSection) {
  legacyUploadSection.style.display = "none";
}

// ─── Create Scenario ───────────────────────────────────────────────────────────
async function ensureProject() {
  if (currentProjectId) return currentProjectId;
  const res = await fetch("/api/projects", { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to create project");
  }
  const data = await res.json();
  currentProjectId = data.id;
  return currentProjectId;
}

createDropzone.addEventListener("click", () => createFileInput.click());

createDropzone.addEventListener("dragover", e => {
  e.preventDefault();
  createDropzone.classList.add("dragover");
});

createDropzone.addEventListener("dragleave", () => createDropzone.classList.remove("dragover"));

createDropzone.addEventListener("drop", async (e) => {
  e.preventDefault();
  createDropzone.classList.remove("dragover");
  const files = Array.from(e.dataTransfer.files).filter(f => {
    const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
    return [".mp4", ".mov", ".avi", ".webm", ".jpg", ".jpeg", ".png", ".webp"].includes(ext);
  });
  if (files.length) await uploadCreateAssets(files);
});

createFileInput.addEventListener("change", async () => {
  const files = Array.from(createFileInput.files || []);
  if (files.length) await uploadCreateAssets(files);
  createFileInput.value = "";
});

async function uploadCreateAssets(files) {
  if (files.length > 10) {
    showError("Максимум 10 файлов");
    files = files.slice(0, 10);
  }
  hideError();
  const projectId = await ensureProject();
  const formData = new FormData();
  files.forEach(f => formData.append("files", f));

  createDropzoneText.textContent = "Загрузка…";
  generateScenarioBtn.disabled = true;

  try {
    const res = await fetch(`/api/projects/${projectId}/assets`, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    createAssets = createAssets.concat(data.assets || []);
    renderAssetList();
    createDropzoneText.textContent = "Перетащите видео или изображения сюда (до 10 файлов)";
  } catch (err) {
    showError(err.message || "Ошибка загрузки");
    createDropzoneText.textContent = "Перетащите видео или изображения сюда (до 10 файлов)";
  }
  updateCreateButtons();
}

function renderAssetList() {
  assetList.innerHTML = "";
  createAssets.forEach((a, i) => {
    const card = document.createElement("div");
    card.className = "asset-card";
    const thumbUrl = a.type === "video"
      ? `/files/uploads/${a.file_key}`
      : `/files/uploads/${a.file_key}`;
    const ext = (a.filename || "").split(".").pop() || "";
    const isVideo = ["mp4", "mov", "avi", "webm"].includes(ext.toLowerCase());
    const thumb = isVideo
      ? `<video class="asset-thumb" src="${thumbUrl}" muted preload="metadata"></video>`
      : `<img class="asset-thumb" src="${thumbUrl}" alt="">`;
    card.innerHTML = `
      ${thumb}
      <div class="asset-info">
        <div class="asset-name">${escapeHtml(a.filename)}</div>
        <div class="asset-meta">${a.type === "video" && a.duration_sec ? formatTime(a.duration_sec) : "Изображение"}</div>
        <input type="text" class="asset-description" data-id="${a.id}" placeholder="Что сделать с этим элементом?" value="${escapeHtml(a.user_description || "")}">
      </div>
    `;
    assetList.appendChild(card);
  });

  assetList.querySelectorAll(".asset-description").forEach(input => {
    input.addEventListener("input", () => {
      const a = createAssets.find(x => x.id === input.dataset.id);
      if (a) a.user_description = input.value;
    });
  });
}

function updateCreateButtons() {
  const hasAssets = createAssets.length > 0;
  const hasPrompt = globalPrompt && globalPrompt.value.trim().length >= 10;
  const hasVideo = createAssets.some(a => a.type === "video");
  generateScenarioBtn.disabled = !(hasAssets && hasPrompt && hasVideo);
}

globalPrompt.addEventListener("input", updateCreateButtons);

generateScenarioBtn.addEventListener("click", async () => {
  if (!currentProjectId || createAssets.length === 0 || !globalPrompt.value.trim()) return;
  const hasVideo = createAssets.some(a => a.type === "video");
  if (!hasVideo) {
    showError("Нужно хотя бы одно видео");
    return;
  }

  hideError();
  createScenarioScreen.classList.add("hidden");
  createProgress.classList.remove("hidden");
  generateScenarioBtn.disabled = true;

  const assetDescriptions = {};
  createAssets.forEach(a => {
    if (a.user_description) assetDescriptions[a.id] = a.user_description;
  });

  const refLinks = referenceLink && referenceLink.value.trim()
    ? [referenceLink.value.trim()]
    : undefined;

  try {
    const res = await fetch(`/api/projects/${currentProjectId}/scenario/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        global_prompt: globalPrompt.value.trim(),
        asset_descriptions: Object.keys(assetDescriptions).length ? assetDescriptions : undefined,
        reference_links: refLinks,
      }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    currentScenario = await res.json();

    createProgress.classList.add("hidden");
    scenarioScreen.classList.remove("hidden");
    renderScenarioHeader(currentScenario);
    renderScenesView(currentScenario);
  } catch (err) {
    createProgress.classList.add("hidden");
    createScenarioScreen.classList.remove("hidden");
    showError(err.message || "Ошибка генерации сценария");
  } finally {
    generateScenarioBtn.disabled = false;
    updateCreateButtons();
  }
});

function renderScenarioHeader(scenario) {
  const m = scenario.metadata || {};
  scenarioHeader.innerHTML = `
    <h3>${escapeHtml(m.name || "Сценарий")}</h3>
    ${m.description ? `<p class="desc">${escapeHtml(m.description)}</p>` : ""}
    ${m.total_duration_sec ? `<p class="duration">Длительность: ${formatTime(m.total_duration_sec)}</p>` : ""}
  `;
}

function renderScenesView(scenario) {
  const scenes = scenario.scenes || [];
  scenesView.innerHTML = "";
  scenes.forEach((scene, i) => {
    const card = document.createElement("div");
    card.className = "scene-card";
    const hasGen = (scene.generation_tasks || []).length > 0;
    card.innerHTML = `
      <div class="scene-card-header">
        <span>Сцена ${i + 1}</span>
        <span class="scene-time">${formatTime(scene.start_sec)} – ${formatTime(scene.end_sec)}</span>
        ${hasGen ? '<span class="scene-badge">needs AI</span>' : ""}
      </div>
      <div class="scene-card-body">
        ${scene.visual_description ? `<div class="scene-visual">${escapeHtml(scene.visual_description)}</div>` : ""}
        ${scene.voiceover_text ? `<div class="scene-voiceover">${escapeHtml(scene.voiceover_text)}</div>` : ""}
        ${(scene.overlays || []).length ? `
          <div class="scene-overlays">
            <h4>Надписи:</h4>
            <ul>
              ${scene.overlays.map(o => `<li>${escapeHtml(o.text || "")}</li>`).join("")}
            </ul>
          </div>
        ` : ""}
      </div>
    `;
    card.querySelector(".scene-card-header").addEventListener("click", () => {
      card.classList.toggle("expanded");
    });
    scenesView.appendChild(card);
  });
}

viewTabs.forEach(btn => {
  btn.addEventListener("click", () => {
    viewTabs.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const isScenes = btn.dataset.view === "scenes";
    scenesView.classList.toggle("hidden", !isScenes);
    timelineView.classList.toggle("hidden", isScenes);
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

let veoAvailable = false;

// Check AI video generation availability
fetch("/capabilities").then(r => r.json()).then(data => {
  veoAvailable = !!data.ai_video_generation;
}).catch(() => { veoAvailable = false; });

function renderSlotCards(suggestions) {
  slotCards.innerHTML = "";
  suggestions.forEach((slot, i) => {
    const card = document.createElement("div");
    card.className = "slot-card";
    const modeToggle = veoAvailable ? `
      <div class="slot-mode-row">
        <span class="slot-mode-label">Источник:</span>
        <div class="mode-toggle">
          <button class="mode-btn active" data-index="${i}" data-mode="stock">🔍 Сток</button>
          <button class="mode-btn" data-index="${i}" data-mode="ai">🤖 AI-генерация</button>
        </div>
      </div>` : "";

    card.innerHTML = `
      <div class="slot-card-header">
        <label class="slot-check-label">
          <input type="checkbox" class="slot-check" data-index="${i}" checked>
          <span class="slot-num">Вставка ${i + 1}</span>
        </label>
        <span class="slot-time">${formatTime(slot.start)} – ${formatTime(slot.end)} · ${slot.duration}s</span>
      </div>
      <div class="slot-context">${escapeHtml(slot.context_text)}</div>
      ${modeToggle}
      <div class="slot-query-row">
        <span class="slot-query-label" id="query-label-${i}">🎬 Запрос для поиска:</span>
        <input type="text" class="slot-query-input" data-index="${i}" value="${escapeHtml(slot.query)}">
      </div>
    `;
    slotCards.appendChild(card);
  });

  // Sync query edits
  slotCards.querySelectorAll(".slot-query-input").forEach(input => {
    input.addEventListener("input", () => {
      currentSuggestions[parseInt(input.dataset.index)].query = input.value;
    });
  });

  // Mode toggle (stock vs AI)
  slotCards.querySelectorAll(".mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.index);
      const mode = btn.dataset.mode;
      const group = slotCards.querySelectorAll(`.mode-btn[data-index="${idx}"]`);
      group.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentSuggestions[idx].mode = mode;
      const label = document.getElementById(`query-label-${idx}`);
      if (label) {
        label.textContent = mode === "ai" ? "🤖 Промпт для AI:" : "🎬 Запрос для поиска:";
      }
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
      mode: currentSuggestions[i].mode || "stock",
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
