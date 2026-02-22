# Инструкция по тестированию Create Scenario

## Подготовка

1. **Клонируй репозиторий** (если ещё не клонирован):
   ```bash
   git clone https://github.com/progprogect/medit.git
   cd medit
   ```

2. **Создай виртуальное окружение и установи зависимости**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Создай `.env`** в корне проекта:
   ```
   GEMINI_API_KEY=твой_ключ_gemini
   PEXELS_API_KEY=твой_ключ_pexels   # опционально, для стоковых видео
   ```

4. **Примени миграции БД**:
   ```bash
   alembic upgrade head
   ```

## Запуск сервера

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Открой в браузере: **http://127.0.0.1:8000**

---

## Тестирование через UI

1. Перейди на вкладку **Create Scenario**.
2. Перетащи видео (или нажми и выбери файл) в dropzone.
3. Введи **Global prompt** (например: «Сделай короткий сценарий для консультации»).
4. Нажми **Generate scenario**.
5. Подожди 30–90 секунд — Gemini сгенерирует сценарий.
6. Проверь карточки сцен: таймкоды, описание, voiceover, overlays.

---

## Тестирование через API (curl)

### 1. Создать проект
```bash
curl -X POST http://127.0.0.1:8000/api/projects
# Ответ: {"id":"<project_id>","name":"New Project"}
```

### 2. Загрузить видео
```bash
PROJECT_ID="<id_из_шага_1>"
curl -X POST "http://127.0.0.1:8000/api/projects/$PROJECT_ID/assets" \
  -F "files=@путь/к/твоему/видео.mp4"
```

### 3. Сгенерировать сценарий
```bash
curl -X POST "http://127.0.0.1:8000/api/projects/$PROJECT_ID/scenario/generate" \
  -H "Content-Type: application/json" \
  -d '{"global_prompt":"Сделай короткий сценарий для консультации"}' \
  --max-time 120
```

### 4. Получить сценарий
```bash
curl "http://127.0.0.1:8000/api/projects/$PROJECT_ID/scenario"
```

---

## Дополнительно

- **Видео:** Поддерживаются `.mp4`, `.mov`, `.webm`, `.avi`. Для теста можно использовать любое короткое видео.
- **Генерация:** Требуется `GEMINI_API_KEY` в `.env`. Без него `/scenario/generate` вернёт ошибку.
- **БД:** SQLite создаётся в `app.db`. Файл в `.gitignore`, не коммитится.
