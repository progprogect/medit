# Proposal: Этапы 1–2 (Input → Scenario)

**Scope:** Только Input UI + Scenario UI (Scenes + Timeline). Без рендеринга и merge.  
**Цель:** Получить «идеальный» сценарий, который легко редактировать и сохранять.

---

## 1. Архитектура этапов 1–2

### 1.1 Компоненты Frontend

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  App (SPA)                                                                   │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  CreateScenarioScreen (Этап 1)                                        │  │
│  │  ├── MediaUploader (multi-file, drag-drop, reorder)                    │  │
│  │  ├── MediaAssetList (cards: thumb, name, duration, description input)  │  │
│  │  ├── GlobalPromptInput                                                 │  │
│  │  ├── ReferenceLinkInput (optional)                                    │  │
│  │  └── GenerateScenarioButton                                            │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  ScenarioScreen (Этап 2)                                               │  │
│  │  ├── ScenarioHeader (name, description, total_duration)                 │  │
│  │  ├── ViewTabs [Scenes | Timeline]                                      │  │
│  │  ├── ScenesView                                                        │  │
│  │  │   ├── SceneCard[] (expandable: time, visual, voiceover, overlays,   │  │
│  │  │   │                effects, asset refs)                             │  │
│  │  │   └── SceneEditor (inline or side panel)                             │  │
│  │  └── TimelineView                                                      │  │
│  │      ├── TimelineRuler (time axis)                                     │  │
│  │      ├── LayerTrack[] (Video, Image, Text, Audio, Effects, etc.)        │  │
│  │      ├── SegmentBlock[] (per layer, draggable/resizable)                │  │
│  │      ├── SegmentEditor (properties panel)                              │  │
│  │      └── AddLayerButton / AddSegmentButton                              │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Стек FE:** Оставляем текущий (vanilla JS + HTML + CSS) для MVP. Альтернатива — React/Vue, но для итераций 1–2 vanilla достаточно.

---

### 1.2 API между FE и BE

| Метод | Endpoint | Описание |
|-------|----------|----------|
| `POST` | `/api/projects` | Создать проект (возвращает `project_id`) |
| `POST` | `/api/projects/{id}/assets` | Загрузить медиа (multipart, multiple files). Возвращает `Asset[]` |
| `DELETE` | `/api/projects/{id}/assets/{asset_id}` | Удалить ассет |
| `PATCH` | `/api/projects/{id}/assets/reorder` | Изменить порядок ассетов |
| `POST` | `/api/projects/{id}/scenario/generate` | Запустить генерацию сценария (LLM). Body: `{ global_prompt, asset_descriptions?, reference_links? }` |
| `GET` | `/api/projects/{id}/scenario` | Получить текущий сценарий (или 404 если не сгенерирован) |
| `PUT` | `/api/projects/{id}/scenario` | Сохранить сценарий (полная замена). Body: `Scenario` |
| `PATCH` | `/api/projects/{id}/scenario` | Частичное обновление (опционально, для итерации 3) |

**Альтернатива без проектов (минимальный MVP):**  
Один «текущий» проект в сессии. Тогда:
- `POST /api/upload-multiple` → возвращает `{ assets: Asset[] }`
- `POST /api/scenario/generate` → Body: `{ assets, global_prompt, reference_links? }`, возвращает `Scenario`
- `GET/PUT /api/scenario` — работа с единственным сценарием в сессии (in-memory или в БД по session/project_id)

**Рекомендация:** Начать с «session-based» (один проект в памяти/локальном state), без полноценной сущности Project. Добавить Project в итерации 3 при сохранении/версиях.

---

### 1.3 Где вызываем LLM и как валидируем ответ

**Поток:**
1. FE вызывает `POST /api/scenario/generate` с `{ assets, global_prompt, reference_links? }`
2. BE:
   - Валидирует input (assets существуют, prompt не пустой)
   - Для каждого asset: читает файл, при необходимости извлекает метаданные (ffprobe для видео), транскрибирует (faster-whisper для длинных видео)
   - Собирает контекст для LLM: метаданные медиа, транскрипт, user descriptions, global prompt, reference links
   - Вызывает Gemini с системным промптом + JSON schema (structured output)
   - Получает сырой JSON
3. **Валидация ответа LLM:**
   - Парсинг JSON
   - Проверка по JSON Schema (scenario_schema.json)
   - Нормализация: `normalize_llm_scenario(raw)` — исправление типов, дефолтов, переименование полей
   - Бизнес-валидация: `validate_scenario(scenario)` — duration consistency, asset refs существуют, scene_id ↔ segment.scene_id
4. Маппинг в canonical model (см. ниже)
5. Возврат `Scenario` в FE

**Ошибки:**
- LLM вернул невалидный JSON → retry 1 раз, затем 422 с сообщением
- Валидация не прошла → 422 с перечнем ошибок

---

### 1.4 Mapping «LLM output → Canonical Scenario Model»

**Проблема:** LLM может вернуть структуру в «своём» формате (например, tasks как сейчас, или свободный JSON). Нужен чёткий контракт.

**Подход:** LLM возвращает **Scenario** в формате, максимально близком к canonical. В промпте и schema явно задаём:
- `scenes[]` с полями: id, start_sec, end_sec, visual_description, voiceover_text, overlays[], effects, transition, asset_refs[], generation_tasks[]
- `layers[]` с полями: id, type, order, segments[]
- `segments[]` с полями: id, start_sec, end_sec, asset_id, asset_source, asset_status, scene_id, params, generation_task_id

**Маппинг (если LLM вернёт «старый» формат tasks):**
- Отдельная функция `tasks_to_scenario(tasks, metadata, assets)` — преобразует текущий `PlanResponse` (tasks) в `Scenario` (scenes + layers).
- Это позволит не ломать существующий flow и постепенно перейти на новый формат.

**Canonical model:** См. раздел 2.

---

## 2. Data Model

### 2.1 JSON Schema для Scenario

Используем доработанную схему из `docs/scenario_schema.json`. Дополнения:

**AssetRef (в Scene):**
```json
{
  "asset_id": "string",      // id ассета (uploaded) или placeholder (generated)
  "media_id": "string",      // ссылка на загруженный asset.id
  "usage": "string",        // "main" | "broll" | "overlay" | ...
  "generation_params": {}   // если generated: query, type и т.д.
}
```

**Segment.params** — свободный объект в зависимости от типа:
- `text`: `{ text, position, font_size, font_color }`
- `video`/`image`: `{ trim_start?, trim_end? }`
- `audio`: `{ volume? }`

**Полная схема** — в `docs/scenario_schema.json` (уже есть). Добавляем поле `project_id` или `asset_ids` в metadata для связи с загруженными медиа.

### 2.2 Правила консистентности

| Правило | Описание | Валидация |
|---------|----------|-----------|
| **Duration** | `metadata.total_duration_sec` = макс. end_sec среди всех сегментов | При сохранении |
| **Scene coverage** | Сцены не перекрываются (или явно задано overlap) | `scene[i].end_sec <= scene[i+1].start_sec` |
| **Segment-scene link** | `segment.scene_id` ∈ `scenes[].id` или null | При сохранении |
| **Asset refs** | `segment.asset_id` для uploaded/suggested должен существовать в `assets` | При сохранении, warning для generated |
| **Audio continuity** | Рекомендация: один audio segment 0–total_duration, если один источник | Не блокируем, но предупреждаем |
| **Segment order** | В пределах слоя: `segment[i].end_sec <= segment[i+1].start_sec` (если не overlap) | Опционально |

**Валидатор:** `validate_scenario(scenario: Scenario, assets: list[Asset]) -> list[ValidationError]`

---

## 3. Хранение данных

### 3.1 Нужна ли БД сейчас?

**Рекомендация: Да, с итерации 1 (минимально).**

**Почему:**
- Нужно хранить проекты, ассеты, сценарии, версии — без БД всё развалится при перезагрузке
- SQLite — нулевая настройка, один файл, легко бэкапить
- Ранний ввод БД упрощает миграцию на AWS (сразу пишем через абстракцию)

**Альтернатива «без БД»:** Хранить в localStorage (FE) или в JSON-файлах на диске. Минусы: нет нормальных запросов, сложно версионировать, при multi-user не сработает.

### 3.2 Какую БД

**SQLite** для локальной разработки.

**Почему не Postgres сразу:**
- Postgres требует установки/контейнера
- SQLite достаточно для MVP, один разработчик
- Миграция SQLite → Postgres тривиальна при использовании SQLAlchemy (смена connection string)

**Орм/миграции:** SQLAlchemy + Alembic.

- SQLAlchemy — стандарт в Python, хорошая абстракция
- Alembic — миграции, версионирование схемы
- При переходе на Postgres: те же модели, другой dialect

### 3.3 Таблицы

```
projects
  id (uuid, PK)
  name (text)
  created_at, updated_at

assets
  id (uuid, PK)
  project_id (FK → projects)
  file_key (text)           -- ключ в storage: uploads/{project_id}/{asset_id}/file
  filename (text)
  type (video|image)
  duration_sec (real, nullable)
  width, height (int, nullable)
  user_description (text, nullable)
  order_index (int)          -- для reorder
  created_at

scenarios
  id (uuid, PK)
  project_id (FK → projects, unique)  -- один сценарий на проект
  data (jsonb / json)        -- полный Scenario
  version (int)              -- для optimistic locking
  status (draft|saved)
  created_at, updated_at

scenario_versions
  id (uuid, PK)
  scenario_id (FK → scenarios)
  version (int)
  data (jsonb / json)
  created_at

generation_jobs (для итерации 3, placeholders)
  id (uuid, PK)
  scenario_id (FK)
  segment_id (text)
  task_type (text)
  status (pending|running|done|error)
  result_asset_id (uuid, nullable)
  created_at, updated_at
```

**SQLite:** `json` вместо `jsonb`. При миграции на Postgres — `jsonb`.

### 3.4 Хранение медиа

**Локально (сейчас):**
- Путь: `uploads/{project_id}/{asset_id}/{filename}`
- Пример: `uploads/proj-123/asset-456/clip.mp4`
- Storage interface расширяем: `save_asset(project_id, asset_id, file, filename) -> file_key`

**На AWS (позже):**
- S3: `uploads/{project_id}/{asset_id}/{filename}`
- Presigned URLs для скачивания
- Текущий `LocalStorage` и будущий `S3Storage` реализуют один интерфейс (уже есть `Storage` protocol)

### 3.5 Миграции

**Alembic:**
- `alembic init`
- Первая миграция: создание `projects`, `assets`, `scenarios`, `scenario_versions`
- Команды: `alembic upgrade head`, `alembic revision -m "add X"`

**Переносимость на AWS:**
- Модели SQLAlchemy — без привязки к SQLite
- В config: `DATABASE_URL=sqlite:///./app.db` (локально) → `postgresql://...` (AWS)
- Медиа: `STORAGE_MODE=local` → `s3`

---

## 4. Пошаговый план работ (итерации)

### Итерация 1: Минимальный Input UI + генерация + Scenes view

**Цель:** Пользователь загружает медиа, вводит prompt, получает сценарий, видит его как список сцен.

**Задачи:**
1. **BE:**
   - Добавить SQLite + SQLAlchemy + Alembic
   - Таблицы: `projects`, `assets`, `scenarios`
   - `POST /api/projects` — создать проект
   - `POST /api/projects/{id}/assets` — multi-upload (несколько файлов)
   - Расширить Storage: сохранение по `project_id/asset_id`
   - `POST /api/projects/{id}/scenario/generate` — вызов LLM, маппинг в Scenario, сохранение
   - `GET /api/projects/{id}/scenario` — вернуть сценарий
   - Адаптировать/создать промпт Gemini для вывода в формате Scenario (scenes + layers)

2. **FE:**
   - Новый экран «Create Scenario» (отдельная страница или вкладка)
   - MediaUploader: multi-file, drag-drop, список карточек (name, duration, thumb, description)
   - GlobalPromptInput, ReferenceLinkInput (optional)
   - Кнопка «Generate scenario»
   - После генерации — переход к ScenarioScreen
   - ScenesView: список сцен (time range, visual_description, voiceover, overlays, asset refs)
   - Без редактирования в итерации 1 (только просмотр)

**Результат:** Полный flow Input → Generate → Scenes view. Сценарий сохраняется в БД.

---

### Итерация 2: Timeline view + редактирование

**Цель:** Таймлайн слоёв (CapCut-like) + возможность редактировать сцены и сегменты.

**Задачи:**
1. **FE:**
   - ViewTabs: Scenes | Timeline
   - TimelineView: ruler, layer tracks, segment blocks
   - Визуализация сегментов по start_sec/end_sec
   - SegmentEditor: панель свойств (start, end, text, asset)
   - Редактирование в ScenesView: тайминги, тексты, asset refs
   - `PUT /api/projects/{id}/scenario` — сохранение изменений

2. **BE:**
   - `PUT /api/projects/{id}/scenario` — валидация + сохранение
   - `validate_scenario()` перед сохранением

3. **UX:**
   - Drag для изменения порядка слоёв
   - Resize сегментов (start/end)
   - Добавление нового сегмента/слоя (базовое)

**Результат:** Два представления, редактирование, сохранение.

---

### Итерация 3: Сохранение/версии + placeholders + улучшения UX

**Цель:** Версионирование, черновик/сохранено, placeholders для AI-generated, полировка.

**Задачи:**
1. **Версионирование:**
   - `scenario_versions` — при каждом «Save» создаём новую версию
   - UI: «Сохранить», «История версий» (опционально)
   - `status: draft | saved`

2. **Placeholders:**
   - Визуал для `asset_status: pending | generating`
   - Таблица `generation_jobs` (заготовка под будущую генерацию)
   - Иконки/бейджи «needs AI» на сценах и сегментах

3. **UX:**
   - Reorder ассетов в Input
   - Удаление ассетов
   - Улучшение Timeline (zoom, scroll, snap to grid)
   - Добавление нового слоя/сегмента (полноценное)

**Результат:** Готовый сценарий с версиями, placeholders, удобное редактирование.

---

## 5. Сводка решений

| Вопрос | Решение |
|--------|---------|
| БД | SQLite + SQLAlchemy + Alembic |
| Миграция на AWS | Смена DATABASE_URL и STORAGE_MODE |
| Медиа локально | `uploads/{project_id}/{asset_id}/` |
| Медиа на AWS | S3, тот же путь в ключе |
| Проекты | Один проект на «сессию» в итерации 1; полноценные projects в БД |
| LLM output | Structured output в формате Scenario (scenes + layers) |
| Маппинг | Промпт + schema задают формат; при необходимости — `tasks_to_scenario()` для обратной совместимости |
| Валидация | JSON Schema + `validate_scenario()` с бизнес-правилами |

---

## 6. Файловая структура (предложение)

```
/
├── alembic/
│   ├── versions/
│   └── env.py
├── db/
│   ├── models.py      # SQLAlchemy models
│   └── session.py     # get_db, engine
├── schemas/
│   ├── tasks.py       # (существующий)
│   └── scenario.py    # Pydantic Scenario, Scene, Layer, Segment
├── services/
│   ├── gemini.py      # + generate_scenario()
│   ├── scenario.py    # validate_scenario, normalize_llm_scenario
│   └── ...
├── api/
│   ├── projects.py    # routes для projects, assets, scenario
│   └── ...
├── main.py
└── docs/
    ├── MVP_SPEC.md
    ├── PROPOSAL_STAGES_1_2.md
    └── scenario_schema.json
```

---

*После утверждения proposal — приступаем к реализации по итерациям.*
