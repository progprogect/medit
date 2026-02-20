# AI Video Editing

Система обработки видео на основе текстового промпта. Gemini 2.5 анализирует видео и генерирует JSON с задачами, FFmpeg выполняет их.

## Локальный запуск (MacBook)

```bash
# 1. Установить FFmpeg
brew install ffmpeg

# 2. Создать виртуальное окружение и установить зависимости
python3 -m venv .venv
source .venv/bin/activate  # на Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Создать .env из шаблона (или уже есть с ключом)
cp .env.example .env
# Отредактировать .env, добавить GEMINI_API_KEY

# 4. Запустить
uvicorn main:app --reload
```

Открыть http://localhost:8000

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| GEMINI_API_KEY | API ключ Google Gemini (обязательно) |
| STORAGE_MODE | `local` или `s3` |
| UPLOAD_DIR | Директория загрузок (local) |
| OUTPUT_DIR | Директория результатов (local) |
