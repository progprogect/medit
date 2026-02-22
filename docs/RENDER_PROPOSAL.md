# Предложение: Рендеринг сценария в видео

## Цель

На основе готового сценария и таймлайна:
1. Сгенерировать или получить видеоклипы для каждого сегмента
2. Наложить надписи с выбранным пользователем стилем
3. Объединить всё в финальное видео

---

## 1. Стили надписей (для FFmpeg)

### Расширение схемы Overlay

Текущий `Overlay`: `text`, `position`, `start_sec`, `end_sec` — без стиля.

**Вариант A: Preset-стили (рекомендуется)**

Пользователь выбирает один preset для всех надписей в проекте:

| Preset ID | Описание | FFmpeg-параметры |
|-----------|----------|------------------|
| `minimal` | Белый текст, лёгкая тень | font_color=white, shadow=1 |
| `box_dark` | Тёмный полупрозрачный фон | box=1, boxcolor=black@0.55 |
| `box_light` | Светлый фон | box=1, boxcolor=white@0.55 |
| `outline` | Контур | borderw=2, bordercolor=black |
| `bold_center` | Крупный, тень, по центру | font_size=72, shadow=1 |

**Вариант B: Явные параметры**

Расширить `Overlay`:
```python
class OverlayStyle(BaseModel):
    font_size: int = 48
    font_color: str = "white"
    shadow: bool = True
    background: str | None = None  # "dark" | "light" | "none"
    border_width: int = 0
    border_color: str | None = None
```

**Рекомендация:** Вариант A — preset на уровне рендера. Пользователь выбирает в UI один стиль, он применяется ко всем overlays. Executor уже поддерживает эти параметры в `add_text_overlay`.

---

## 2. Источники видеосегментов

| asset_source | asset_status | Действие |
|--------------|--------------|----------|
| uploaded | ready | Trim из исходного видео |
| generated | ready | Использовать сгенерированный файл (Veo или stock) |
| generated | pending | **Пользователь выбирает:** Сгенерировать (Veo) или Загрузить |
| suggested | ready | Stock clip уже скачан |

---

## 3. Бизнес-логика рендеринга

```
render_scenario(scenario, assets, overlay_style_preset, segment_resolutions):
  # segment_resolutions: { seg_id: path | "pending" }
  # Для pending сегментов — ошибка или пропуск (пользователь должен сначала resolve)
  
  clips = []
  for seg in video_layer.segments (ordered by start_sec):
    if seg not in segment_resolutions or segment_resolutions[seg] == "pending":
      raise RenderBlocked(f"Segment {seg.id} needs video: generate or upload")
    
    input_path = segment_resolutions[seg]
    # 1. Trim
    trimmed = trim(input_path, seg.start_sec, seg.end_sec)
    
    # 2. Overlays (from scene overlays in this segment's time range)
    overlays = get_overlays_for_segment(scene, seg)
    for ov in overlays:
      params = overlay_to_task_params(ov, overlay_style_preset)
      trimmed = add_text_overlay(trimmed, params)
    
    clips.append(trimmed)
  
  # 3. Concat
  final = concat(clips)
  return final
```

**Важно:** Сегменты с `asset_status=pending` блокируют рендер. Пользователь должен:
- Нажать «Сгенерировать» → Veo создаёт клип → segment обновляется
- Или «Загрузить» → загружает файл → segment обновляется

---

## 4. API

| Метод | Endpoint | Описание |
|-------|---------|----------|
| GET | `/api/projects/{id}/overlay-styles` | Список preset-стилей надписей |
| POST | `/api/projects/{id}/scenario/segments/{seg_id}/generate` | Генерация клипа через Veo (для segment с asset_source=generated) |
| POST | `/api/projects/{id}/scenario/segments/{seg_id}/upload` | Загрузка видео вместо генерации |
| POST | `/api/projects/{id}/scenario/render` | Рендер всего сценария. Body: `{ overlay_style: string }` |

### POST /scenario/render

- Проверяет, что все video segments имеют готовое видео (asset_status=ready или resolution через upload)
- Возвращает: `{ job_id, status }` — асинхронно, или сразу `{ output_url }` если быстрый рендер

---

## 5. UI Flow

### Экран «Подготовка к рендеру» (после таймлайна)

1. **Стиль надписей** — dropdown: Минимальный / Тёмный фон / Светлый фон / Контур
2. **Сегменты** — список с состоянием:
   - ✅ Готов (uploaded/generated ready)
   - ⏳ Ожидает — кнопки [Сгенерировать] [Загрузить]
3. **Кнопка «Сгенерировать видео»** — активна, когда все сегменты готовы

### Альтернатива: inline в таймлайне

- На каждом сегменте с pending — иконка + dropdown: Сгенерировать / Загрузить
- Глобальный выбор стиля надписей в шапке
- Кнопка «Рендер» внизу

---

## 6. Технические детали

### Маппинг preset → executor params

```python
OVERLAY_PRESETS = {
    "minimal": {"font_color": "white", "shadow": True, "background": None},
    "box_dark": {"font_color": "white", "shadow": True, "background": "dark"},
    "box_light": {"font_color": "black", "shadow": False, "background": "light"},
    "outline": {"font_color": "white", "border_width": 2, "border_color": "black"},
}
```

### Хранение сгенерированных/загруженных клипов

- `outputs/{project_id}/segments/{seg_id}.mp4` — для generated/uploaded
- В segment: `asset_id` → ссылка на file_key в storage (для generated — новый asset в outputs)

### Порядок слоёв при рендере

1. Video layer — основной видеопоток (trim + concat сегментов)
2. Text layer — overlays накладываются на каждый segment до concat (проще) или одним проходом (сложнее)

**Рекомендация:** накладывать overlays на каждый segment до concat — executor уже умеет add_text_overlay в цепочке.

---

## 7. Этапы реализации

### Этап 1 (MVP)
- [ ] Расширить Overlay / добавить overlay_style_preset в render request
- [ ] Реализовать `scenario_to_render_pipeline` — trim → overlay → concat для uploaded-only
- [ ] API `POST /scenario/render` с overlay_style
- [ ] UI: выбор стиля + кнопка «Рендер» (только для сценариев без pending segments)

### Этап 2
- [ ] API generate/upload для pending segments
- [ ] UI: кнопки Сгенерировать/Загрузить на сегментах
- [ ] Интеграция Veo для generated segments

### Этап 3
- [ ] Асинхронный рендер (job queue)
- [ ] Прогресс, превью

---

## 8. Схема данных (дополнения)

### Segment (при upload)
- `asset_id` — id загруженного файла (новый asset в project или в outputs)
- `asset_source` = "uploaded" (если user upload) или "generated" (если Veo)
- `asset_status` = "ready"

### RenderJob (опционально, для async)
- id, project_id, status, output_file_key, created_at
