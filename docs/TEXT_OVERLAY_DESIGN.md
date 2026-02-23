# Текстовые наложения: тезисы vs субтитры

**Цель:** Учитывать при генерации сценария и рендере тип текста (тезисы или субтитры), его формат и расположение.

**Важно:** Тезисы и субтитры могут присутствовать **вместе** в одном сценарии, или по отдельности, или отсутствовать — в зависимости от смысла и промпта пользователя.

---

## 1. Текущее состояние

| Компонент | Сейчас |
|-----------|--------|
| **Overlay (схема)** | `text`, `position` (center/bottom/top), `start_sec`, `end_sec` |
| **LLM (генерация)** | Промпт: overlays с `position: center\|bottom\|top`. Нет явного типа текста |
| **Рендер** | Все overlays → `add_text_overlay` с одним `overlay_style` (minimal, box_dark и т.д.) |
| **Executor** | `add_text_overlay` — drawtext, позиции, font_size 48, стили. `add_subtitles` — SRT, FFmpeg subtitles filter |

**Проблемы:**
- LLM не различает тезисы и субтитры
- Позиция ограничена (center/bottom/top), нет top_center, bottom_center и т.д.
- Нет font_size, font_style на уровне overlay — всё из preset
- Субтитры (add_subtitles) не используются в Create Scenario

---

## 2. Различия: тезисы vs субтитры

| Параметр | Тезисы | Субтитры |
|----------|--------|----------|
| **Содержание** | Ключевые фразы, заголовки, буллеты | Полный текст речи, слово в слово или фразами |
| **Длительность** | 2–5 сек на блок | Синхронно с речью (короткие сегменты) |
| **Размер шрифта** | Крупный (36–72px) | Мелкий (24–36px) |
| **Позиция** | center, top_center, bottom_center | Обычно bottom_center |
| **Стиль** | Box, shadow, outline | Минимальный, читаемость |
| **Источник** | LLM генерирует из контекста | **Определяет LLM** по смыслу или прямому указанию в промпте |

---

### 2.1 Совместное использование

- **Только тезисы** — ключевые фразы, без субтитров
- **Только субтитры** — полный текст речи, без тезисов
- **Тезисы + субтитры** — оба типа одновременно (например: заголовок по центру + субтитры внизу)
- **Ничего** — если пользователь не просил текст на экране

LLM выбирает комбинацию исходя из `global_prompt` и контекста.

### 2.2 Источник субтитров (определяет LLM)

Источник субтитров задаётся **LLM** на основе промпта и контекста:

| Сигнал в промпте | Решение LLM |
|------------------|-------------|
| «субтитры из голоса» | voiceover_text сцен |
| «транскрипция видео» | Whisper по основному видео |
| «текст с экрана» | визуальный текст в кадре (из visual_description) |
| «с субтитрами» (без уточнения) | по контексту: voiceover, если есть; иначе транскрипция |
| «для глухих» / «accessibility» | полная транскрипция речи |

LLM возвращает `subtitle_segments` с таймингами и текстом, выбрав подходящий источник.

---

## 3. Предлагаемая схема

### 3.1 Расширить Overlay

```python
class Overlay(BaseModel):
    text: str = ""
    position: str = "center"  # top_center|bottom_center|center|top_left|...
    start_sec: float = 0
    end_sec: float = 0
    # Новые поля:
    format: str = "thesis"   # "thesis" | "subtitle"
    font_size: Optional[int] = None   # None = из preset
    font_style: Optional[str] = None  # "minimal"|"box_dark"|... или None
```

**format:**
- `thesis` — тезис, ключевая фраза. Рендер: `add_text_overlay`
- `subtitle` — субтитр. Рендер: `add_subtitles` (если несколько сегментов) или `add_text_overlay` с мелкими параметрами

### 3.2 Субтитры: один overlay или сегменты?

**Вариант A:** Overlay с `format="subtitle"` — один блок текста. Для полных субтитров нужен массив overlays (каждый — фраза).

**Вариант B:** Отдельный слой `subtitle` с сегментами `{start_sec, end_sec, text}`. Рендер → один `add_subtitles` task.

**Рекомендация:** Вариант B чище для субтитров (много коротких сегментов). Overlay остаётся для тезисов.

### 3.3 Слой subtitle в Scenario

```python
# В layers добавить тип "subtitle"
Layer(type="subtitle", segments=[
    Segment(start_sec=0.5, end_sec=1.2, params={"text": "Привет!"}),
    Segment(start_sec=1.2, end_sec=2.1, params={"text": "Я расскажу о продукте."}),
    ...
])
```

Рендер: если есть слой subtitle → один `add_subtitles` task с `params.segments = [{start, end, text}]`.

---

## 4. Изменения по компонентам

### 4.1 LLM (генерация сценария)

**SCENARIO_SIMPLE_INSTRUCTION / generate_scenario:**
- Добавить в промпт:
  - «Текст на экране: 1) тезисы (ключевые фразы, 2–4 сек) — overlays с format="thesis"; 2) субтитры (полный текст речи) — слой subtitle с сегментами по voiceover_text».
  - «Тезисы и субтитры могут быть вместе, по отдельности или отсутствовать — в зависимости от смысла и промпта пользователя».
  - «Источник субтитров определяй LLM: по прямому указанию в промпте («из голоса», «транскрипция видео») или по смыслу (voiceover_text, транскрипция основного видео и т.д.)».

**Правила:**
- Прямое указание: «с субтитрами» → subtitle layer; «тезисы» / «ключевые моменты» → overlays format="thesis"
- По смыслу: «для соцсетей» → тезисы; «доступность» / «accessibility» → субтитры
- Комбинация: «тезисы и субтитры» → оба; «без текста» → ни overlays, ни subtitle

**Структура ответа LLM:**
```json
{
  "scenes": [{
    "overlays": [{"text": "...", "position": "top_center", "format": "thesis", "start_sec": 2, "end_sec": 5}]
  }],
  "subtitle_segments": [{"start": 0.5, "end": 1.2, "text": "Привет!"}, ...]
}
```
Или subtitle_segments привязать к сценам.

### 4.2 Схемы (schemas)

- **Overlay:** добавить `format: str = "thesis"`, опционально `font_size`, `font_style`
- **Layer:** тип `subtitle` уже в enum
- **Segment:** для subtitle layer — `params: {text}` + start_sec, end_sec

### 4.3 scenario_service (нормализация)

- `scenario_from_simple_output`: парсить `subtitle_segments` из LLM, создавать слой `subtitle` с сегментами
- `tasks_to_scenario`: если в плане есть add_subtitles — создавать subtitle layer

### 4.4 render_service

- `scenario_to_render_tasks`: 
  - для overlay с `format="subtitle"` (если оставим в overlay) → add_subtitles
  - для слоя `subtitle` → один add_subtitles task, собрать segments из всех segment'ов слоя
  - для overlay с `format="thesis"` → add_text_overlay (как сейчас)
- `_overlay_to_task_params`: учитывать overlay.font_size, overlay.font_style (если заданы), иначе preset

### 4.5 Executor

- `add_text_overlay`: уже поддерживает position, font_size, font_color
- `add_subtitles`: уже есть, формат `params.segments = [{start, end, text}]`

### 4.6 UI (overlay_style select)

- Сейчас один overlay_style на весь рендер
- Можно: оставить глобальный стиль для тезисов; для субтитров — фиксированный «минимальный» или отдельная опция «стиль субтитров»

---

## 5. План реализации (по шагам)

### Шаг 1: Схема и данные
- Расширить Overlay: `format`, опционально `font_size`, `font_style`
- Добавить поддержку слоя `subtitle` в scenario_from_simple_output (если LLM вернёт subtitle_segments)

### Шаг 2: LLM-промпты
- Обновить SCENARIO_SIMPLE_INSTRUCTION: описать thesis vs subtitle, position (полный список)
- Передавать в промпт пожелание пользователя (из global_prompt): «субтитры» / «тезисы» / «ключевые фразы»

### Шаг 3: Рендер
- В scenario_to_render_tasks: обрабатывать слой subtitle → add_subtitles
- В _overlay_to_task_params: overlay.format, overlay.font_size, overlay.font_style
- Для overlay_style: применять к thesis; для subtitle — отдельный минимальный стиль

### Шаг 4: scenario_to_llm_tasks (режимы A/B)
- Добавить в промпт SCENARIO_TO_TASKS_INSTRUCTION: add_subtitles для subtitle layer, add_text_overlay для thesis overlays

### Шаг 5: UI (опционально)
- В карточке сцены показывать тип overlay (тезис/субтитр)
- Выбор «стиль субтитров» при рендере (если нужно)

---

## 6. Вопросы для согласования

1. **Субтитры:** слой subtitle (вариант B) или overlays с format="subtitle" (вариант A)?
2. **Позиции:** расширить до полного списка (top_center, bottom_center, top_left, …) — уже есть в executor, нужно прокинуть в LLM и схему?
3. **Приоритет:** сначала тезисы (расширить Overlay) или сразу субтитры (слой subtitle)?

**Источник субтитров:** решается LLM по смыслу или прямому указанию в промпте (согласовано).
