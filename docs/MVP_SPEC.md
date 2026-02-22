# AI Video Editing — Product & Technical Specification (MVP)

**Версия:** 1.0  
**Дата:** 2025-02-13

---

## 1. Обзор MVP Flow

Пайплайн состоит из 4 этапов:

```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  1. Input   │───▶│ 2. Scenario      │───▶│ 3. Render Scenes │───▶│ 4. Merge Scenes │
│  (медиа +   │    │ (LLM → scenes +  │    │ (по сценам)      │    │ (финальное      │
│   prompt)   │    │  timeline layers) │    │                  │    │  видео)         │
└─────────────┘    └──────────────────┘    └─────────────────┘    └─────────────────┘
```

**Выход этапа 2 — сценарий в двух представлениях:**
1. **Scene-based** — список сцен с описаниями
2. **Layer-based** — таймлайн слоёв (CapCut-like), редактируемый пользователем

**Источники элементов таймлайна:**
- **uploaded** — загруженные пользователем медиа
- **suggested** — предложенные системой (B-roll, стоки)
- **generated** — сгенерированные AI (voiceover, music, subtitles, overlays, b-roll, SFX)

---

## 2. User Flow

```
1. Пользователь открывает приложение
2. Добавляет медиа (видео/изображения) — несколько файлов
3. Для каждого медиа (опционально) указывает "что сделать с этим элементом"
4. Добавляет общий prompt/описание результата
5. (Опционально) добавляет reference link (Instagram/YouTube)
6. Нажимает "Сгенерировать сценарий"
7. LLM возвращает сценарий → отображается как сцены + таймлайн
8. Пользователь редактирует сценарий (тайминги, текст, слои, добавляет элементы)
9. Нажимает "Render" → система рендерит сцены
10. Нажимает "Merge" → получает финальное видео
```

---

## 3. UX Screens (Этапы 1–2)

### 3.1 Этап 1 — Input

#### Экран: Добавление медиа

| Элемент | Описание |
|---------|----------|
| **Dropzone / File picker** | Множественный выбор: видео (mp4, mov, avi, webm) и изображения (jpg, png, webp). Drag & drop. |
| **Список загруженных медиа** | Карточки: превью, имя файла, длительность (для видео), размер. Кнопка удалить. |
| **Описание к медиа** | Под каждой карточкой — текстовое поле (опционально): "Что сделать с этим элементом?" (placeholder). |
| **Общий prompt** | Большое текстовое поле: "Опишите желаемый результат" (обязательно для генерации). |
| **Reference link** | Опциональное поле: "Ссылка на референс (Instagram или YouTube)". Кнопка "Добавить". |
| **Кнопка "Сгенерировать сценарий"** | Активна, когда есть ≥1 медиа + общий prompt. |

**Состояния:**
- `idle` — ничего не загружено
- `uploading` — идёт загрузка (прогресс)
- `ready` — медиа загружены, можно генерировать
- `generating` — запрос к LLM (спиннер, "Анализируем…")

#### Валидации (Этап 1)

| Правило | Сообщение |
|---------|-----------|
| Форматы видео | mp4, mov, avi, webm |
| Форматы изображений | jpg, png, webp |
| Макс. размер файла | 500 MB (настраиваемо) |
| Макс. длительность видео | 10 мин (настраиваемо) |
| Макс. кол-во файлов | 10 |
| Общий prompt | мин. 10 символов |
| Reference link | валидный URL Instagram/YouTube |

---

### 3.2 Этап 2 — Scenario Output

#### A) Представление: Список сцен

| Элемент | Описание |
|---------|----------|
| **Заголовок сценария** | Название + краткое описание (от LLM) |
| **Список сцен** | Карточки сцен: номер, time range (0–10 сек), краткое описание визуала |
| **Раскрытие сцены** | По клику: детали (voiceover текст, надписи, эффекты, ассеты) |
| **Индикатор "needs AI"** | Иконка/бейдж на сценах/элементах, требующих генерации |

#### B) Представление: Таймлайн слоёв (CapCut-like)

| Элемент | Описание |
|---------|----------|
| **Дорожки (layers)** | Video, Image, Text/Subtitle, Audio (music, voiceover, SFX), Effects, Overlays, Generated |
| **Сегменты на дорожках** | Прямоугольники с таймингами, прерывистые (несколько клипов на слое) |
| **Визуализация наложений** | Слои друг над другом; видно, что где перекрывается |
| **Редактирование** | Клик по сегменту → панель свойств (тайминги, текст, ассет, параметры эффекта) |
| **Добавление** | Кнопка "+" на слое или "Добавить слой" |
| **Placeholder** | Серый блок с иконкой "⏳" для элементов "needs AI generation" |
| **Статусы** | `ready` (зелёный), `pending` (жёлтый), `error` (красный), `generating` (анимация) |

#### Редактирование слоёв

- **Тайминги**: перетаскивание границ сегмента, ввод start/end в панели
- **Текст**: inline-редактирование или модальное окно
- **Ассеты**: выбор из загруженных / предложенных; для generated — кнопка "Сгенерировать"
- **Порядок слоёв**: drag & drop дорожек
- **Удаление**: кнопка на сегменте или слое

#### Ошибки и статусы

| Статус | Визуал | Действие |
|--------|--------|----------|
| `asset_missing` | Красная обводка | "Ассет не найден" — загрузить или заменить |
| `generation_failed` | Красный бейдж | "Генерация не удалась" — повторить или изменить |
| `validation_error` | Жёлтый | "Некорректные тайминги" — исправить |
| `duration_mismatch` | Предупреждение | "Длительность сцены не совпадает с слоями" |

---

## 4. Entities / Data Model

### 4.1 Input (Этап 1)

```
MediaItem:
  id: string (uuid)
  type: "video" | "image"
  file_key: string          # ключ в storage
  filename: string
  duration_sec?: number     # только для video
  width?: number
  height?: number
  user_description?: string # "что сделать с этим"

InputRequest:
  media: MediaItem[]
  global_prompt: string
  reference_links?: string[]  # ["https://instagram.com/...", "https://youtube.com/..."]
```

### 4.2 Scenario (Этап 2)

См. раздел 7 — JSON Schema.

### 4.3 Asset

```
Asset:
  id: string
  source: "uploaded" | "suggested" | "generated"
  status: "ready" | "pending" | "generating" | "error"
  type: "video" | "image" | "audio" | "text"
  file_key?: string         # когда ready
  generation_task_id?: string  # когда generated, для отслеживания
  metadata?: object
```

---

## 5. API Contracts

### 5.1 Этап 1 → Backend

```
POST /api/upload-multiple
Content-Type: multipart/form-data
Body: files[] (multiple), descriptions[] (optional, per-file)

Response: { media: MediaItem[] }
```

```
POST /api/input/validate
Body: { media_ids: string[], global_prompt: string, reference_links?: string[] }
Response: { valid: boolean, errors?: string[] }
```

### 5.2 Этап 2 — Генерация сценария

```
POST /api/scenario/generate
Body: {
  media: MediaItem[],
  global_prompt: string,
  reference_links?: string[]
}

Response: Scenario (см. JSON Schema)
```

### 5.3 Редактирование сценария

```
PATCH /api/scenario/{scenario_id}
Body: { scenes?: Scene[], layers?: Layer[], version: number }
Response: { scenario: Scenario, version: number }
```

### 5.4 AI Generation (асинхронно)

```
POST /api/generate/{task_type}
Body: { scenario_id, layer_id, segment_id, params }
Response: { task_id: string }

GET /api/generate/{task_id}/status
Response: { status: "pending"|"generating"|"ready"|"error", asset_id?: string }
```

### 5.5 Рендер и Merge

```
POST /api/render
Body: { scenario_id }
Response: { job_id: string }

GET /api/render/{job_id}/status
Response: { status, progress?, output_key? }

POST /api/merge
Body: { scenario_id, render_job_id }
Response: { output_key, download_url }
```

---

## 6. Technical Architecture (High-Level)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Frontend (SPA)                                 │
│  - Input screen (multi-upload, descriptions, prompt, reference)          │
│  - Scenario screen (scenes list + timeline layers, editing)              │
│  - Render/Merge controls                                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           FastAPI Backend                                │
│  - /api/upload-multiple, /api/input/validate                             │
│  - /api/scenario/generate → LLM (Gemini)                                 │
│  - /api/scenario/{id} (PATCH) — versioned storage                        │
│  - /api/generate/* — async AI tasks (voiceover, music, etc.)             │
│  - /api/render, /api/merge → Executor (FFmpeg)                           │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────┐         ┌─────────────────┐         ┌─────────────────┐
│ Gemini (LLM) │         │ Storage (files)  │         │ FFmpeg Executor  │
│ - analyze    │         │ - uploads        │         │ - scene render   │
│ - scenario   │         │ - outputs       │         │ - merge          │
│ - suggest    │         │ - generated     │         │                  │
└───────────────┘         └─────────────────┘         └─────────────────┘
```

---

## 7. Best Practices

| Практика | Реализация |
|----------|-------------|
| **Версионирование сценария** | Каждое изменение — `version++`. Хранить `ScenarioVersion` с diff или полным snapshot. |
| **Идемпотентность рендера** | Render по `scenario_id` + `version`. Кэш: если сценарий не менялся — вернуть существующий output. |
| **Валидация** | Перед render: duration consistency, asset refs exist, no overlapping conflicts. |
| **Хранение ассетов** | `uploads/`, `outputs/`, `generated/` — раздельные префиксы. TTL для temp (опционально). |
| **Retries** | LLM: 2 retry с exponential backoff. FFmpeg: 1 retry. Generation tasks: 3 retry. |
| **Placeholder replacement** | При `generation_finished` — обновить `asset_id` в segment, пересчитать timeline. |

---

## 8. Детальная реализация этапов 1–2

### 8.1 Этап 1 — UX (детально)

#### Экран добавления медиа

```
┌─────────────────────────────────────────────────────────────────┐
│  Добавьте медиа                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Перетащите сюда видео или изображения                       │ │
│  │  или нажмите для выбора (до 10 файлов)                       │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Загруженные (3):                                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                           │
│  │ [thumb]  │ │ [thumb]  │ │ [thumb]  │                           │
│  │ clip.mp4 │ │ img.png  │ │ intro.mp4│                           │
│  │ 0:45     │ │ —       │ │ 0:12     │  [×]                       │
│  │ Что с ним?│ │ Что с ним?│ │ Что с ним?│                       │
│  │ [______] │ │ [______] │ │ [______] │                           │
│  └──────────┘ └──────────┘ └──────────┘                           │
│                                                                  │
│  Опишите желаемый результат *                                    │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Сделай рекламное видео с тезисами и B-roll вставками         │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Референс (опционально)                                          │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ https://www.instagram.com/reel/...                            │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  [ Сгенерировать сценарий ]                                      │
└─────────────────────────────────────────────────────────────────┘
```

### 8.2 Этап 2 — UX (детально)

#### Список сцен

```
┌─────────────────────────────────────────────────────────────────┐
│  Сценарий: B2B LeadGen Promo                                     │
│  Рекламное видео с тезисами и B-roll                             │
│                                                                  │
│  Сцены:                                                          │
│  ▼ Сцена 1 (0:00 – 0:10)  [⏳ 1 AI]                              │
│     Визуал: спикер в кадре, приветствие                           │
│     Голос: "Привет! Я Никита..."                                  │
│     Надписи: "Привет! Я Никита!" (center, 0–1.5)                  │
│     Ассеты: clip.mp4 (uploaded)                                   │
│                                                                  │
│  ▶ Сцена 2 (0:10 – 0:25)  [✓]                                    │
│     ...                                                           │
└─────────────────────────────────────────────────────────────────┘
```

#### Таймлайн слоёв

```
┌─────────────────────────────────────────────────────────────────┐
│  Timeline                                    | 0  5  10  15  20  │
├─────────────────────────────────────────────────────────────────┤
│  Video 1    [======clip.mp4======][==B-roll==][====clip.mp4====]  │
│  Video 2    [                    ][stock_1   ][                  ]│
│  Text       [==Привет==][====Помогаю B2B====][...]               │
│  Audio      [===============original audio======================]  │
│  Music      [          ][⏳ generating][    ]                     │
└─────────────────────────────────────────────────────────────────┘
```

### 8.3 Business Logic — LLM Output

**Структура плана от LLM:**

- LLM получает: список медиа с описаниями, global_prompt, reference_links (если есть)
- LLM возвращает: **Scenario** (см. JSON Schema ниже)
- Ограничения: макс. N сцен, макс. M слоёв, длительность ≤ суммы исходных медиа (или заданная)

**Преобразование LLM → Canonical Timeline:**

1. Парсинг JSON ответа
2. Валидация: обязательные поля, типы, тайминги в диапазоне
3. Нормализация: объединение дубликатов, исправление overlaps
4. Маппинг сцен → слои: каждая сцена ссылается на segment_ids в layers
5. Разрешение asset refs: uploaded → file_key, suggested/generated → placeholder

### 8.4 Хранение Timeline

- **Версионирование**: `ScenarioVersion { id, scenario_id, version, data: Scenario, created_at }`
- **Diff/patch**: для MVP — полный snapshot. Позже — JSON Patch (RFC 6902) для экономии места.
- **Валидация**: при каждом PATCH — `validate_scenario(data)` перед сохранением.

### 8.5 AI Generation Tasks

```
GenerationTask:
  id: string
  type: "voiceover" | "music" | "subtitles" | "overlay" | "broll" | "sfx"
  params: object  # зависит от типа
  status: "pending" | "generating" | "ready" | "error"
  asset_id?: string  # когда ready
  target: { scenario_id, layer_id, segment_id }
```

**Placeholder replacement:** при `status=ready` — обновить segment.asset_id, перерисовать timeline.

### 8.6 Динамические типы слоёв

- Реестр типов: `LAYER_TYPES = ["video", "image", "text", "audio", "effects", "overlays", "generated"]`
- Каждый тип — свой рендерер и валидатор
- Расширяемость: новый тип = новый entry в реестре + UI компонент

---

## 9. JSON Schema для Scenario

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Scenario",
  "type": "object",
  "required": ["version", "scenes", "layers", "metadata"],
  "properties": {
    "version": { "type": "integer", "minimum": 1 },
    "metadata": {
      "type": "object",
      "properties": {
        "name": { "type": "string" },
        "description": { "type": "string" },
        "total_duration_sec": { "type": "number" },
        "aspect_ratio": { "type": "string" }
      }
    },
    "scenes": {
      "type": "array",
      "items": { "$ref": "#/definitions/Scene" }
    },
    "layers": {
      "type": "array",
      "items": { "$ref": "#/definitions/Layer" }
    }
  },
  "definitions": {
    "Scene": {
      "type": "object",
      "required": ["id", "start_sec", "end_sec"],
      "properties": {
        "id": { "type": "string" },
        "start_sec": { "type": "number" },
        "end_sec": { "type": "number" },
        "visual_description": { "type": "string" },
        "voiceover_text": { "type": "string" },
        "overlays": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "text": { "type": "string" },
              "position": { "type": "string" },
              "start_sec": { "type": "number" },
              "end_sec": { "type": "number" }
            }
          }
        },
        "effects": { "type": "array", "items": { "type": "string" } },
        "transition": { "type": "string" },
        "asset_refs": {
          "type": "array",
          "items": { "$ref": "#/definitions/AssetRef" }
        },
        "generation_tasks": {
          "type": "array",
          "items": { "$ref": "#/definitions/GenerationTaskRef" }
        }
      }
    },
    "Layer": {
      "type": "object",
      "required": ["id", "type", "segments"],
      "properties": {
        "id": { "type": "string" },
        "type": {
          "type": "string",
          "enum": ["video", "image", "text", "subtitle", "audio", "music", "sfx", "effects", "overlays", "generated"]
        },
        "order": { "type": "integer" },
        "segments": {
          "type": "array",
          "items": { "$ref": "#/definitions/Segment" }
        }
      }
    },
    "Segment": {
      "type": "object",
      "required": ["id", "start_sec", "end_sec"],
      "properties": {
        "id": { "type": "string" },
        "start_sec": { "type": "number" },
        "end_sec": { "type": "number" },
        "asset_id": { "type": "string" },
        "asset_source": { "type": "string", "enum": ["uploaded", "suggested", "generated"] },
        "asset_status": { "type": "string", "enum": ["ready", "pending", "generating", "error"] },
        "scene_id": { "type": "string" },
        "params": { "type": "object" },
        "generation_task_id": { "type": "string" }
      }
    },
    "AssetRef": {
      "type": "object",
      "properties": {
        "asset_id": { "type": "string" },
        "media_id": { "type": "string" },
        "usage": { "type": "string" }
      }
    },
    "GenerationTaskRef": {
      "type": "object",
      "properties": {
        "task_type": { "type": "string" },
        "params": { "type": "object" },
        "segment_id": { "type": "string" }
      }
    }
  }
}
```

---

## 10. Пример JSON сценария

```json
{
  "version": 1,
  "metadata": {
    "name": "B2B LeadGen Promo",
    "description": "Рекламное видео с тезисами и B-roll",
    "total_duration_sec": 59,
    "aspect_ratio": "9:16"
  },
  "scenes": [
    {
      "id": "scene_1",
      "start_sec": 0,
      "end_sec": 10,
      "visual_description": "Спикер в кадре, приветствие",
      "voiceover_text": "Привет! Я Никита, помогаю B2B компаниям настраивать лидогенерацию.",
      "overlays": [
        { "text": "Привет! Я Никита!", "position": "center", "start_sec": 0, "end_sec": 1.5 }
      ],
      "effects": [],
      "transition": "cut",
      "asset_refs": [{ "asset_id": "media_1", "media_id": "clip_1", "usage": "main" }],
      "generation_tasks": []
    },
    {
      "id": "scene_2",
      "start_sec": 10,
      "end_sec": 15,
      "visual_description": "B-roll: человек за ноутбуком",
      "voiceover_text": null,
      "overlays": [],
      "effects": [],
      "transition": "cut",
      "asset_refs": [{ "asset_id": "broll_1", "media_id": null, "usage": "broll" }],
      "generation_tasks": [
        { "task_type": "fetch_stock_video", "params": { "query": "person typing laptop close-up" }, "segment_id": "seg_broll_1" }
      ]
    }
  ],
  "layers": [
    {
      "id": "layer_video_1",
      "type": "video",
      "order": 0,
      "segments": [
        {
          "id": "seg_main_1",
          "start_sec": 0,
          "end_sec": 10,
          "asset_id": "media_1",
          "asset_source": "uploaded",
          "asset_status": "ready",
          "scene_id": "scene_1",
          "params": {}
        },
        {
          "id": "seg_broll_1",
          "start_sec": 10,
          "end_sec": 15,
          "asset_id": null,
          "asset_source": "generated",
          "asset_status": "pending",
          "scene_id": "scene_2",
          "params": {},
          "generation_task_id": "gen_1"
        },
        {
          "id": "seg_main_2",
          "start_sec": 15,
          "end_sec": 59,
          "asset_id": "media_1",
          "asset_source": "uploaded",
          "asset_status": "ready",
          "scene_id": "scene_3",
          "params": {}
        }
      ]
    },
    {
      "id": "layer_text_1",
      "type": "text",
      "order": 1,
      "segments": [
        {
          "id": "seg_text_1",
          "start_sec": 0,
          "end_sec": 1.5,
          "asset_id": null,
          "asset_source": "generated",
          "asset_status": "ready",
          "scene_id": "scene_1",
          "params": { "text": "Привет! Я Никита!", "position": "center", "font_size": 60 }
        }
      ]
    },
    {
      "id": "layer_audio_1",
      "type": "audio",
      "order": 2,
      "segments": [
        {
          "id": "seg_audio_main",
          "start_sec": 0,
          "end_sec": 59,
          "asset_id": "media_1",
          "asset_source": "uploaded",
          "asset_status": "ready",
          "scene_id": null,
          "params": {}
        }
      ]
    }
  ]
}
```

---

## 11. Правила: сцены ↔ слои

| Правило | Описание |
|---------|----------|
| **Mapping** | `Segment.scene_id` ссылается на `Scene.id`. Сцена может охватывать несколько сегментов в разных слоях. |
| **Обязательные поля** | Scene: id, start_sec, end_sec. Layer: id, type, segments. Segment: id, start_sec, end_sec. |
| **Duration consistency** | Сумма длительностей сегментов на video layer = total_duration_sec. Сцены не должны перекрываться (или явно определено overlap). |
| **Audio** | Рекомендуется один непрерывный сегмент audio (0–total_duration), если источник один. Не дробить без необходимости. |
| **Placeholders** | `asset_status: "pending"` или `"generating"` + `generation_task_id` — элемент ждёт AI. |

---

## 12. Placeholders и асинхронная генерация

- **Placeholder**: segment с `asset_status != "ready"` и опционально `generation_task_id`
- **Замена**: при завершении генерации — webhook или polling обновляет segment.asset_id, asset_status = "ready"
- **UI**: placeholder отображается как серый блок с иконкой; при ready — подставляется превью/длительность

---

## 13. Global Audio

- **Правило**: если один источник аудио (например, основное видео) — один сегмент на audio layer от 0 до конца
- **Склейки**: только между video-элементами; аудио не режем
- **Исключение**: если пользователь явно добавил музыку/SFX — отдельные сегменты на своих слоях

---

## 14. Следующий шаг — Рекомендация

### Минимальный MVP (сделать прямо сейчас)

1. **Минимальный UX этапа 1:**
   - Один файл видео (как сейчас) + общий prompt
   - Опционально: поле "описание к медиа" (одно, т.к. один файл)
   - Без reference link в MVP

2. **Минимальная Schema:**
   - Упрощённый Scenario: `metadata` + `layers` (без scenes в MVP — scenes как view поверх layers)
   - 3 типа слоёв: `video`, `text`, `audio`
   - Segment: start_sec, end_sec, asset_id, params (для text)

3. **Минимальный Renderer:**
   - Текущий executor (tasks) → маппинг layers → tasks
   - Один проход: trim + overlay + text → output
   - Без scene-by-scene в MVP (один render pass)

### Как масштабировать

1. **Этап 1:** Мульти-upload, описания per-media, reference link
2. **Этап 2:** Полная scene-based view, CapCut-like timeline UI (библиотека или custom)
3. **Этап 3:** Scene-by-scene render, merge с переходами
4. **Этап 4:** Async AI generation (voiceover, music), placeholders, замена
5. **Этап 5:** Версионирование, diff/patch, collaborative editing

---

*Конец документа*
