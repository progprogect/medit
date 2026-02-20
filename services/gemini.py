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

LONG_VIDEO_THRESHOLD_SEC = 120  # 2 minutes — использовать Whisper + низкий FPS

SYSTEM_INSTRUCTION = """You are a video editing assistant. Given a video (and optionally a transcript) and a user's text request, you generate a JSON plan: scenario_name, scenario_description, metadata, and a list of editing tasks.

Available task types:
- add_text_overlay: Overlay text. Params: text, position (top_center, bottom_center, top_left, top_right, bottom_left, bottom_right, center), font_size, font_color, start_time (optional), end_time (optional)
- trim: Cut video. Params: start (seconds), end (seconds)
- resize: Change dimensions. Params: width, height (optional)
- change_speed: Playback speed. Params: factor (0.5 = half, 2.0 = double)
- add_subtitles: Burn-in subtitles. Params: segments (array of {start, end, text})
- add_image_overlay: Overlay image. Params: image_path or image_data, position, start_time (optional), end_time (optional), opacity (optional)
- auto_frame_face: Crop to follow face (vertical format). Params: target_ratio (e.g. "9:16")
- color_correction: Adjust colors. Params: brightness (optional), contrast (optional), saturation (optional)
- concat: Concatenate clips. Params: clip_paths (array of paths)
- zoompan: Zoom/pan effect. Params: zoom, duration, x (optional), y (optional)

SELF-CHECK: Before returning, verify that your tasks match the user's request and content best practices (hooks, retention, clear structure). Ensure timestamps are logical. If something is off, adjust the plan.

Generate ONLY valid JSON. No markdown. Tasks execute in order."""

PLAN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario_name": {"type": "string"},
        "scenario_description": {"type": "string"},
        "metadata": {"type": "object"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "add_text_overlay", "trim", "resize", "change_speed",
                            "add_subtitles", "add_image_overlay", "auto_frame_face",
                            "color_correction", "concat", "zoompan",
                        ],
                    },
                    "params": {"type": "object"},
                },
                "required": ["type", "params"],
            },
        },
    },
    "required": ["scenario_name", "scenario_description", "metadata", "tasks"],
}


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

    if is_long:
        t0 = time.time()
        logger.info("Gemini: длинное видео (%.0f сек), транскрипция Whisper...", duration_sec)
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

    prompt_parts = [
        f"User request: {user_prompt}\n\n"
        "Return JSON with scenario_name, scenario_description, metadata (e.g. timestamps for YouTube), and tasks array. "
        "Each task has type and params. Execute tasks in order.",
    ]
    if transcript_text:
        prompt_parts.insert(
            0,
            f"""Transcript of the video (for context — use this to find hooks and key moments):
---
{transcript_text[:15000]}
"""
        )
        if transcript_segments:
            prompt_parts.append(
                f"\n\nAvailable subtitle segments (start, end, text): {json.dumps(transcript_segments[:100])}"
            )

    prompt = "\n".join(prompt_parts)

    t_gen = time.time()
    logger.info("Gemini: вызов generate_content...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[video_part, prompt],
        config={
            "system_instruction": SYSTEM_INSTRUCTION,
            "response_mime_type": "application/json",
            "response_schema": PLAN_JSON_SCHEMA,
        },
    )

    logger.info("Gemini: generate_content занял %.1f сек", time.time() - t_gen)
    if not response.text:
        raise ValueError("Empty response from Gemini")

    data = json.loads(response.text)
    validated = PlanResponse.model_validate(data)
    return {
        "scenario_name": validated.scenario_name,
        "scenario_description": validated.scenario_description,
        "metadata": validated.metadata,
        "tasks": [{"type": t.type, "params": t.params} for t in validated.tasks],
    }


def analyze_and_generate_tasks(video_path: Path, user_prompt: str) -> list[dict]:
    """
    Legacy: analyze and return tasks only (for backward compatibility).
    """
    plan = analyze_and_generate_plan(video_path, user_prompt)
    return plan["tasks"]
