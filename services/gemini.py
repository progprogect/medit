"""Gemini API integration: video analysis and task generation."""

import json
import logging
import subprocess
import tempfile
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

GRAPH TASKS: Tasks support optional fields:
  - "output_id": string — name this task's output for use by later tasks via "inputs"
  - "inputs": [string, ...] — use named outputs of previous tasks as input instead of the linear chain

Special registry key: "source" always refers to the ORIGINAL uploaded video.

Use output_id + inputs when you need to:
  1. Cut multiple segments from the original video to later concat them.
  2. Mix original video segments with stock B-roll.
In these cases EVERY trim that cuts from the original MUST use inputs: ["source"] and have an output_id.
fetch_stock_* tasks MUST have an output_id and are automatically placed in the registry (they do NOT change the current chain).
Simple linear tasks (single add_text_overlay, resize, etc.) do NOT need output_id or inputs.

Task types with required params — use EXACTLY these formats:

add_text_overlay — required: text, font_size, font_color; position OR (x, y); optional: start_time, end_time, margin, shadow, background, border_color, border_width
  position values: top_center, bottom_center, top_left, top_right, bottom_left, bottom_right, center
  x, y values: integer pixels, "50%" percentage, or FFmpeg expression like "(w-text_w)/2"
  margin: pixels from edge when using position (default 50)
  shadow: true/false — drop shadow for readability
  background: "dark" (black@0.55), "light" (white@0.55), "none", or custom "black@0.7"
  border_color + border_width: text outline (e.g. "black", 2)
  STYLE GUIDE: For reels/ads use shadow=true + background="dark" for key titles. Use border_color="black"+border_width=2 for subtle outlines. Avoid plain white text on bright backgrounds.
  Examples:
    {"type": "add_text_overlay", "params": {"text": "B2B лидогенерация", "position": "bottom_center", "font_size": 56, "font_color": "white", "shadow": true, "background": "dark", "start_time": 0.0, "end_time": 4.0}}
    {"type": "add_text_overlay", "params": {"text": "Привет!", "x": "(w-text_w)/2", "y": "h*0.15", "font_size": 64, "font_color": "#FFD700", "border_color": "black", "border_width": 3, "start_time": 0.0, "end_time": 2.0}}

trim — required: start, end (seconds).
  If cutting from original for concat: use inputs: ["source"] and set output_id.
  Example single trim: {"type": "trim", "params": {"start": 5.0, "end": 45.0}}
  Example for concat: {"type": "trim", "params": {"start": 5.0, "end": 20.0}, "inputs": ["source"], "output_id": "clip_a"}
  Example with stock: {"type": "trim", "params": {"start": 20.0, "end": 40.0}, "inputs": ["source"], "output_id": "clip_b"}

resize — required: width; optional: height
  Example: {"type": "resize", "params": {"width": 1080, "height": 1920}}

change_speed — required: factor
  Example: {"type": "change_speed", "params": {"factor": 1.25}}

add_subtitles — required: segments (use timestamps from the transcript)
  Example: {"type": "add_subtitles", "params": {"segments": [{"start": 0.0, "end": 3.5, "text": "Привет, я Никита"}]}}

auto_frame_face — required: target_ratio
  Example: {"type": "auto_frame_face", "params": {"target_ratio": "9:16"}}

color_correction — at least one of: brightness, contrast, saturation (-1..1)
  Example: {"type": "color_correction", "params": {"brightness": 0.05, "contrast": 0.1}}

zoompan — required: zoom, duration
  Example: {"type": "zoompan", "params": {"zoom": 1.2, "duration": 3.0}}

concat — preferred: use "inputs" with output_ids; fallback: clip_paths array
  Example with graph: {"type": "concat", "params": {}, "inputs": ["clip_a", "broll_1", "clip_b"]}
  Example legacy: {"type": "concat", "params": {"clip_paths": ["/path/a.mp4"]}}
  Full B-roll example (trim source + fetch stock + concat):
    {"type": "trim", "params": {"start": 0, "end": 15}, "inputs": ["source"], "output_id": "clip_a"}
    {"type": "fetch_stock_video", "params": {"query": "business meeting", "duration_max": 8}, "output_id": "broll_1"}
    {"type": "trim", "params": {"start": 15, "end": 30}, "inputs": ["source"], "output_id": "clip_b"}
    {"type": "concat", "params": {}, "inputs": ["clip_a", "broll_1", "clip_b"]}

fetch_stock_video — find and download a stock video clip. Required: query. Optional: duration_max (sec), orientation (landscape/portrait/square).
  MUST have output_id so the clip can be used in later tasks.
  Example: {"type": "fetch_stock_video", "params": {"query": "business meeting", "duration_max": 10, "orientation": "landscape"}, "output_id": "broll_1"}

fetch_stock_image — find and download a stock image. Required: query. Optional: orientation.
  MUST have output_id.
  Example: {"type": "fetch_stock_image", "params": {"query": "data chart blue", "orientation": "landscape"}, "output_id": "graph_img"}

SELF-CHECK: Before returning — verify every task has non-empty params. Verify timestamps are logical. Remove any task with empty params.

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
                            "fetch_stock_video", "fetch_stock_image",
                        ],
                    },
                    "output_id": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "params": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "position": {"type": "string"},
                            "font_size": {"type": "integer"},
                            "font_color": {"type": "string"},
                            "start_time": {"type": "number"},
                            "end_time": {"type": "number"},
                            "start": {"type": "number"},
                            "end": {"type": "number"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "factor": {"type": "number"},
                            "segments": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "start": {"type": "number"},
                                        "end": {"type": "number"},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["start", "end", "text"],
                                },
                            },
                            # add_text_overlay extra styling
                            "margin": {"type": "integer"},
                            "x": {},
                            "y": {},
                            "shadow": {"type": "boolean"},
                            "background": {"type": "string"},
                            "border_color": {"type": "string"},
                            "border_width": {"type": "integer"},
                            # add_image_overlay
                            "image_path": {"type": "string"},
                            "opacity": {"type": "number"},
                            "target_ratio": {"type": "string"},
                            "brightness": {"type": "number"},
                            "contrast": {"type": "number"},
                            "saturation": {"type": "number"},
                            "clip_paths": {"type": "array", "items": {"type": "string"}},
                            "zoom": {"type": "number"},
                            "duration": {"type": "number"},
                            # fetch_stock_*
                            "query": {"type": "string"},
                            "duration_max": {"type": "integer"},
                            "orientation": {"type": "string"},
                        },
                    },
                },
                "required": ["type", "params"],
            },
        },
    },
    "required": ["scenario_name", "scenario_description", "metadata", "tasks"],
}


def detect_burned_subtitles(video_path: Path) -> bool:
    """Check if video has burned-in (hardcoded) subtitles by sampling frames with Gemini Vision."""
    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)

    # Extract 4 frames evenly spaced through the video
    duration = _get_video_duration(video_path)
    if duration <= 0:
        return False

    frame_times = [duration * t for t in (0.1, 0.33, 0.66, 0.9)]
    frame_parts = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, ts in enumerate(frame_times):
            frame_path = Path(tmpdir) / f"frame_{idx}.jpg"
            result = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
                 "-vframes", "1", "-q:v", "3", str(frame_path)],
                capture_output=True,
            )
            if result.returncode == 0 and frame_path.exists():
                frame_data = frame_path.read_bytes()
                frame_parts.append(
                    types.Part.from_bytes(data=frame_data, mime_type="image/jpeg")
                )

    if not frame_parts:
        return False

    prompt = (
        "Look at these video frames. Do you see any burned-in (hardcoded) subtitles or captions "
        "as text overlaid on the video picture itself (not UI elements, not titles, "
        "but subtitle text at the bottom or sides of the frame)? "
        "Answer with a single word: YES or NO."
    )
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[*frame_parts, prompt],
        )
        answer = (response.text or "").strip().upper()
        result = "YES" in answer
        logger.info("detect_burned_subtitles: ответ Gemini='%s' → %s", answer, result)
        return result
    except Exception as e:
        logger.warning("detect_burned_subtitles: ошибка %s, считаем False", e)
        return False


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


def _repair_broll_plan(tasks: list[dict]) -> list[dict]:
    """
    Fix B-roll plans where Gemini forgets output_ids and concat inputs.

    Rules:
    - fetch_stock_video → assign output_id, add to concat sequence (video only!)
    - fetch_stock_image → assign output_id, do NOT add to concat (images go as overlays)
    - trim tasks in B-roll plan → force inputs=['source'], assign output_id, add to concat sequence
    - concat → fill inputs from sequence in plan order
    - post-processing tasks (add_text_overlay, color_correction, resize, etc.) stay AFTER concat
    """
    has_fetch_video = any(t["type"] == "fetch_stock_video" for t in tasks)
    if not has_fetch_video:
        # No video B-roll → nothing to repair for concat
        # Still assign output_ids to fetch_stock_image for overlay use
        for i, task in enumerate(tasks):
            if task["type"] == "fetch_stock_image" and not task.get("output_id"):
                tasks[i] = dict(task)
                tasks[i]["output_id"] = f"stock_img_{i}"
        return tasks

    ASSEMBLY_TYPES = {"trim", "fetch_stock_video", "fetch_stock_image"}
    POST_TYPES = {"add_text_overlay", "add_subtitles", "color_correction", "zoompan",
                  "add_image_overlay", "resize", "auto_frame_face", "change_speed"}

    assembly: list[dict] = []
    post_processing: list[dict] = []
    concat_task: dict | None = None

    stock_counter = 0
    clip_counter = 0
    sequence: list[str] = []  # concat sequence (video clips only)

    for task in tasks:
        task = dict(task)
        t_type = task["type"]

        if t_type == "fetch_stock_video":
            if not task.get("output_id"):
                stock_counter += 1
                task["output_id"] = f"stock_{stock_counter}"
                logger.info("Repair: auto output_id='%s' для fetch_stock_video", task["output_id"])
            sequence.append(task["output_id"])
            assembly.append(task)

        elif t_type == "fetch_stock_image":
            if not task.get("output_id"):
                stock_counter += 1
                task["output_id"] = f"stock_img_{stock_counter}"
                logger.info("Repair: auto output_id='%s' для fetch_stock_image", task["output_id"])
            # Images go to assembly but NOT to sequence (can't concat video+image)
            assembly.append(task)

        elif t_type == "trim":
            # Force all trims in a B-roll plan to read from original source
            if not task.get("inputs"):
                task["inputs"] = ["source"]
            if not task.get("output_id"):
                clip_counter += 1
                task["output_id"] = f"clip_{clip_counter}"
                logger.info("Repair: auto output_id='%s', inputs=['source'] для trim", task["output_id"])
            sequence.append(task["output_id"])
            assembly.append(task)

        elif t_type == "concat":
            concat_task = task  # capture, will rebuild below

        elif t_type in POST_TYPES:
            post_processing.append(task)

        # Other unknown types — keep in post_processing
        else:
            post_processing.append(task)

    if not sequence:
        return tasks  # nothing useful found

    # Re-interleave: alternate source clips and stock clips properly
    clip_ids = [s for s in sequence if s.startswith("clip_")]
    stock_ids = [s for s in sequence if s.startswith("stock_") and not s.startswith("stock_img_")]
    # Alternate: clip, stock, clip, stock... then append remaining stock
    interleaved: list[str] = []
    for i, clip_id in enumerate(clip_ids):
        interleaved.append(clip_id)
        if i < len(stock_ids):
            interleaved.append(stock_ids[i])
    # Any remaining stock clips that didn't pair with a source clip
    interleaved.extend(stock_ids[len(clip_ids):])
    if not interleaved:
        interleaved = sequence  # fallback to original order

    # Build concat with the properly interleaved sequence
    if concat_task is None:
        concat_task = {"type": "concat", "params": {}}
    concat_task["inputs"] = interleaved
    concat_task["params"] = {}
    logger.info("Repair: concat inputs = %s", interleaved)

    # Final plan: assembly → concat → post-processing
    repaired = assembly + [concat_task] + post_processing
    return repaired


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

    t0 = time.time()
    logger.info("Gemini: проверка наличия burned-in субтитров...")
    has_burned_subs = detect_burned_subtitles(video_path)
    logger.info("Gemini: burned-in субтитры = %s (%.1f сек)", has_burned_subs, time.time() - t0)

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

    burned_subs_note = (
        "NOTE: This video already has burned-in (hardcoded) subtitles visible on the frames. "
        "Do NOT add an add_subtitles task.\n\n"
        if has_burned_subs else ""
    )

    prompt_parts = [
        transcript_block,
        burned_subs_note,
        f"User request: {user_prompt}\n\n"
        "Return JSON with scenario_name, scenario_description, metadata (e.g. YouTube timestamps), and tasks array. "
        "EVERY task must have fully filled params — never empty {}.",
    ]

    prompt = "".join(p for p in prompt_parts if p)

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
    tasks = []
    for t in validated.tasks:
        if not t.params and t.inputs is None:
            logger.warning("Gemini: задача %s вернула пустые params и нет inputs, пропускаем", t.type)
            continue
        task_dict: dict = {"type": t.type, "params": t.params}
        if t.output_id is not None:
            task_dict["output_id"] = t.output_id
        if t.inputs is not None:
            task_dict["inputs"] = t.inputs
        tasks.append(task_dict)
    if not tasks:
        logger.warning("Gemini: все задачи имели пустые params — возможно, нужно переформулировать промпт")

    tasks = _repair_broll_plan(tasks)
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
