"""Тест process с логированием времени каждого этапа."""

import time
from pathlib import Path

from config import get_gemini_api_key
from services.gemini import analyze_and_generate_tasks
from services.executor import run_tasks
from services.storage import get_storage

VIDEO_KEY = "3f92df47-fafd-405c-bb63-418697e2b00e"
PROMPT = "Я хочу наложить текст на видео. Текст должен отражать ключевые мысли из самой речи с видео в виде тезисов"

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def main():
    storage = get_storage()
    
    log("1. Получение пути к видео...")
    t0 = time.time()
    input_path = storage.get_upload_path(VIDEO_KEY)
    log(f"   Готово за {time.time()-t0:.1f}s: {input_path}")
    
    log("2. Gemini: анализ видео и генерация задач...")
    t1 = time.time()
    tasks = analyze_and_generate_tasks(input_path, PROMPT)
    log(f"   Готово за {time.time()-t1:.1f}s. Задач: {len(tasks)}")
    for i, t in enumerate(tasks):
        log(f"   - {i+1}. {t['type']}: {t.get('params', {})}")
    
    log("3. Executor: выполнение задач FFmpeg...")
    t2 = time.time()
    output_path = Path("/tmp/test_result.mp4")
    run_tasks(input_path, tasks, output_path)
    log(f"   Готово за {time.time()-t2:.1f}s")
    
    log("4. Сохранение результата...")
    t3 = time.time()
    output_key = storage.save_output(None, output_path)
    log(f"   Готово за {time.time()-t3:.1f}s. Key: {output_key}")
    
    total = time.time() - t0
    log(f"ИТОГО: {total:.1f}s")
    output_path.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
