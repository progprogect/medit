"""Gemini API integration: video analysis and task generation."""

import json
import logging
import subprocess
import time
from pathlib import Path

from google import genai
from google.genai import types

from config import get_gemini_api_key
from schemas.tasks import PlanResponse

logger = logging.getLogger(__name__)

LONG_VIDEO_THRESHOLD_SEC = 120  # 2 minutes — меньше кадров для анализа

SYSTEM_INSTRUCTION = """You are a video editing assistant. Given a video, a transcript, and a user's text request, you generate a JSON plan: scenario_name, scenario_description, metadata, and a list of editing tasks.

CRITICAL RULE: Every task MUST have fully populated params. NEVER return empty params {}.

Task types with required params — use EXACTLY these formats:

add_text_overlay — required: text, position, font_size, font_color; optional: start_time, end_time
  Example: {"type": "add_text_overlay", "params": {"text": "B2B лидогенерация — это реально", "position": "bottom_center", "font_size": 48, "font_color": "white", "start_time": 0.0, "end_time": 4.0}}

trim — required: start, end (seconds)
  Example: {"type": "trim", "params": {"start": 5.0, "end": 45.0}}

resize — required: width; optional: height (omit to keep aspect ratio)
  Example: {"type": "resize", "params": {"width": 1080, "height": 1920}}

change_speed — required: factor (0.5 = slow 2x, 2.0 = fast 2x)
  Example: {"type": "change_speed", "params": {"factor": 1.25}}

add_subtitles — required: segments (array of objects with start, end, text — use timestamps from the transcript)
  Example: {"type": "add_subtitles", "params": {"segments": [{"start": 0.0, "end": 3.5, "text": "Привет, я Никита"}, {"start": 3.5, "end": 7.0, "text": "Помогаю B2B компаниям"}]}}

auto_frame_face — required: target_ratio
  Example: {"type": "auto_frame_face", "params": {"target_ratio": "9:16"}}

color_correction — at least one of: brightness (-1..1), contrast (-1..1), saturation (-1..1)
  Example: {"type": "color_correction", "params": {"brightness": 0.05, "contrast": 0.1, "saturation": 0.1}}

zoompan — required: zoom, duration
  Example: {"type": "zoompan", "params": {"zoom": 1.2, "duration": 3.0}}

concat — required: clip_paths (array of absolute file paths)
  Example: {"type": "concat", "params": {"clip_paths": ["/path/a.mp4", "/path/b.mp4"]}}

SELF-CHECK: Before returning — verify every task has non-empty params with all required fields filled. Verify timestamps are logical and match the transcript. Remove any task with empty params.

Generate ONLY valid JSON. No markdown. Tasks execute in order."""

# Схема не используется — response_schema ограничивает Gemini и приводит к пустым params.
# Вместо этого используем response_mime_type=application/json + детальный системный промпт.


def _get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def analyze_and_generate_plan(video_path: Path, user_prompt: str) -> dict:
    """
    Analyze video with Gemini and generate editing plan (scenario + metadata + tasks).
    For long videos: transcribe audio first, then send transcript + low-FPS video.
    Returns dict: {scenario_name, scenario_description, metadata, tasks}.
    """
    from services.transcriber import transcribe

    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)

    duration_sec = _get_video_duration(video_path)
    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    use_files_api = file_size_mb > 20
    is_long = duration_sec > LONG_VIDEO_THRESHOLD_SEC

    transcript_text = ""
    transcript_segments: list[dict] = []

    t0 = time.time()
    logger.info("Gemini: транскрипция Whisper (%.0f сек видео)...", duration_sec)
    transcript_text, transcript_segments, _ = transcribe(video_path)
    logger.info("Gemini: транскрипция заняла %.1f сек", time.time() - t0)

    if use_files_api:
        t0 = time.time()
        logger.info("Gemini: загрузка видео в Files API...")
        uploaded = client.files.upload(file=str(video_path))
        logger.info("Gemini: загрузка заняла %.1f сек", time.time() - t0)
        for i in range(60):
            f = client.files.get(name=uploaded.name)
            state = getattr(f, "state", None)
            if state in ("ACTIVE", "STATE_ACTIVE"):
                logger.info("Gemini: файл готов за %.1f сек (state=%s)", time.time() - t0, state)
                break
            if i % 5 == 0 and i > 0:
                logger.info("Gemini: ожидание обработки... %ds (state=%s)", i * 2, state)
            time.sleep(2)
        fps = 0.1 if is_long else 0.5
        video_meta = types.VideoMetadata(fps=fps)
        video_part = types.Part(
            file_data=types.FileData(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
            video_metadata=video_meta,
        )
    else:
        video_bytes = video_path.read_bytes()
        video_part = types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")

    segments_for_prompt = transcript_segments[:200] if transcript_segments else []
    transcript_block = (
        f"VIDEO TRANSCRIPT (use timestamps for all time-based params):\n---\n{transcript_text[:15000]}\n---\n"
        f"Segments with exact timestamps: {json.dumps(segments_for_prompt)}\n\n"
        if transcript_text else ""
    )

    prompt_parts = [
        transcript_block,
        f"User request: {user_prompt}\n\n"
        "Return JSON with scenario_name, scenario_description, metadata (e.g. YouTube timestamps), and tasks array. "
        "EVERY task must have fully filled params — never empty {}.",
    ]

    prompt = "".join(prompt_parts)

    t_gen = time.time()
    logger.info("Gemini: вызов generate_content...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[video_part, prompt],
        config={
            "system_instruction": SYSTEM_INSTRUCTION,
            "response_mime_type": "application/json",
        },
    )

    logger.info("Gemini: generate_content занял %.1f сек", time.time() - t_gen)
    if not response.text:
        raise ValueError("Empty response from Gemini")

    data = json.loads(response.text)
    validated = PlanResponse.model_validate(data)
    tasks = []
    for t in validated.tasks:
        if not t.params:
            logger.warning("Gemini: задача %s вернула пустые params, пропускаем", t.type)
            continue
        tasks.append({"type": t.type, "params": t.params})
    if not tasks:
        logger.warning("Gemini: все задачи имели пустые params — возможно, нужно переформулировать промпт")
    return {
        "scenario_name": validated.scenario_name,
        "scenario_description": validated.scenario_description,
        "metadata": validated.metadata,
        "tasks": tasks,
    }


def analyze_and_generate_tasks(video_path: Path, user_prompt: str) -> list[dict]:
    """
    Legacy: analyze and return tasks only (for backward compatibility).
    """
    plan = analyze_and_generate_plan(video_path, user_prompt)
    return plan["tasks"]
