"""Gemini API integration: video analysis and task generation.

Architecture:
  - Python (deterministic) controls B-roll STRUCTURE: how many, where, how long.
  - Gemini (LLM) controls CONTENT: text overlays, stock clip descriptions.
  - Validator enforces rules after plan generation.
"""

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

LONG_VIDEO_THRESHOLD_SEC = 120

# ─── B-roll rules (configurable) ─────────────────────────────────────────────
BROLL_RULES = {
    "max_inserts": 3,        # maximum B-roll inserts per video
    "min_duration": 4.0,     # minimum insert duration (seconds)
    "max_duration": 5.0,     # maximum insert duration (seconds)
    "min_topic_sec": 5.0,    # speaker must discuss topic for at least this long
    "avoid_first": 6.0,      # avoid B-roll in first N seconds
    "avoid_last": 8.0,       # avoid B-roll in last N seconds
    "min_gap": 10.0,         # minimum gap between two inserts
}

# ─── System instruction for Gemini (content only) ─────────────────────────────
CONTENT_SYSTEM_INSTRUCTION = """You are a professional video editor. You receive:
1. A video and its full transcript with timestamps.
2. Pre-computed B-roll slots (exact timestamps where stock footage will be inserted).
3. A user request.

Your job: generate text overlays + stock clip descriptions. Do NOT change the B-roll positions — they are already decided.

TASK ORDER — always follow this exact sequence:
  1. auto_frame_face (if vertical format needed)
  2. trim (only if shortening)
  3. add_text_overlay (key phrases, synced to transcript; 4-8 overlays total)
  4. fetch_stock_video (one per B-roll slot, with specific visual query)
  5. overlay_video (one per B-roll slot, using the pre-decided timestamps)
  6. color_correction (optional final polish)

RULES:
- add_text_overlay params: text, position, font_size, font_color, shadow, background, start_time, end_time ONLY.
- fetch_stock_video params: query, alternative_queries, duration_max, orientation ONLY. MUST have output_id.
- overlay_video params: start_time, end_time, stock_id ONLY.
- Text overlays: strong hook in first 3s, key points synced to speech, CTA near end.
- Stock queries: describe a SPECIFIC visual scene (e.g. "man typing on laptop close-up screen" not "business").
- Do NOT generate more fetch_stock_video / overlay_video tasks than the pre-computed slots.

NEVER mix params between task types. Generate ONLY valid JSON. No markdown."""

# ─── Gemini JSON schema ────────────────────────────────────────────────────────
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
                            "add_subtitles", "add_image_overlay", "overlay_video",
                            "auto_frame_face", "color_correction", "concat", "zoompan",
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
                            "image_path": {"type": "string"},
                            "opacity": {"type": "number"},
                            "target_ratio": {"type": "string"},
                            "brightness": {"type": "number"},
                            "contrast": {"type": "number"},
                            "saturation": {"type": "number"},
                            "clip_paths": {"type": "array", "items": {"type": "string"}},
                            "zoom": {"type": "number"},
                            "duration": {"type": "number"},
                            "query": {"type": "string"},
                            "alternative_queries": {"type": "array", "items": {"type": "string"}},
                            "duration_max": {"type": "integer"},
                            "orientation": {"type": "string"},
                            "stock_id": {"type": "string"},
                            "shadow": {"type": "boolean"},
                            "background": {"type": "string"},
                            "border_color": {"type": "string"},
                            "border_width": {"type": "integer"},
                            "margin": {"type": "integer"},
                            "x": {},
                            "y": {},
                        },
                    },
                },
                "required": ["type", "params"],
            },
        },
    },
    "required": ["scenario_name", "scenario_description", "metadata", "tasks"],
}


# ═══════════════════════════════════════════════════════════════════════════════
# DETERMINISTIC: Python controls B-roll structure
# ═══════════════════════════════════════════════════════════════════════════════

def find_broll_slots(
    segments: list[dict],
    video_duration: float,
    rules: dict | None = None,
) -> list[dict]:
    """Compute optimal B-roll insertion points deterministically from transcript.

    Strategy:
    - Split valid range into equal sections (one per allowed insert).
    - In each section, snap to the nearest sentence boundary.
    - Return {start, end, context_text} for each slot.
    """
    rules = rules or BROLL_RULES
    max_n = rules["max_inserts"]
    target_dur = (rules["min_duration"] + rules["max_duration"]) / 2
    avoid_start = rules["avoid_first"]
    avoid_end = video_duration - rules["avoid_last"]
    min_gap = rules["min_gap"]

    valid_start = avoid_start
    valid_end = avoid_end - target_dur

    if valid_end <= valid_start or not segments:
        return []

    # Divide valid range into max_n sections and find one slot per section
    section_len = (valid_end - valid_start) / max_n
    slots: list[dict] = []

    for i in range(max_n):
        section_start = valid_start + i * section_len
        section_end = section_start + section_len

        # Find segment boundaries within this section
        boundaries = [
            s["end"] for s in segments
            if section_start <= s["end"] <= section_end
        ]

        if boundaries:
            # Pick the boundary closest to the middle of the section
            mid = (section_start + section_end) / 2
            snap = min(boundaries, key=lambda b: abs(b - mid))
        else:
            snap = (section_start + section_end) / 2

        insert_start = round(snap, 1)
        insert_end = round(insert_start + target_dur, 1)

        # Avoid overlap with previous slot
        if slots and insert_start < slots[-1]["end"] + min_gap:
            insert_start = round(slots[-1]["end"] + min_gap, 1)
            insert_end = round(insert_start + target_dur, 1)

        if insert_end > avoid_end:
            break

        # Gather transcript context around this slot for stock query generation
        context_segs = [
            s for s in segments
            if s["start"] >= insert_start - 3 and s["end"] <= insert_end + 5
        ]
        context_text = " ".join(s["text"] for s in context_segs).strip()[:300]

        slots.append({
            "start": insert_start,
            "end": insert_end,
            "context_text": context_text,
        })

    logger.info("B-roll slots: %s", [(s["start"], s["end"]) for s in slots])
    return slots


def validate_and_fix_plan(tasks: list[dict], rules: dict | None = None) -> list[dict]:
    """Post-process plan to enforce B-roll rules deterministically.

    Enforces:
    - fetch_stock_video has output_id
    - overlay_video has correct duration (min/max)
    - Number of overlays does not exceed max_inserts
    - Fetch tasks without matching overlay are removed
    """
    rules = rules or BROLL_RULES

    # Ensure all fetch_stock tasks have output_ids
    stock_counter = 0
    for task in tasks:
        if task["type"] in ("fetch_stock_video", "fetch_stock_image") and not task.get("output_id"):
            stock_counter += 1
            task["output_id"] = f"stock_{stock_counter}"

    # Fix overlay durations
    for task in tasks:
        if task["type"] != "overlay_video":
            continue
        p = task["params"]
        start = p.get("start_time", 0)
        end = p.get("end_time", start + rules["min_duration"])
        dur = end - start
        if dur < rules["min_duration"]:
            p["end_time"] = round(start + rules["min_duration"], 1)
        elif dur > rules["max_duration"]:
            p["end_time"] = round(start + rules["max_duration"], 1)

    # Limit number of overlays to max_inserts
    overlay_indices = [i for i, t in enumerate(tasks) if t["type"] == "overlay_video"]
    if len(overlay_indices) > rules["max_inserts"]:
        excess_indices = set(overlay_indices[rules["max_inserts"]:])
        # Find stock_ids used by excess overlays
        excess_stock_ids = {tasks[i]["params"].get("stock_id") for i in excess_indices}
        # Remove excess overlays and their fetch tasks
        tasks = [
            t for j, t in enumerate(tasks)
            if j not in excess_indices
            and not (t["type"] == "fetch_stock_video" and t.get("output_id") in excess_stock_ids)
        ]
        logger.info("Validator: обрезано до %d B-roll вставок", rules["max_inserts"])

    logger.info("Validator: итого задач %d, overlay %d",
                len(tasks), sum(1 for t in tasks if t["type"] == "overlay_video"))
    return tasks


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini utilities
# ═══════════════════════════════════════════════════════════════════════════════

def detect_burned_subtitles(video_path: Path) -> bool:
    """Check if video has burned-in subtitles by sampling frames with Gemini Vision."""
    duration = _get_video_duration(video_path)
    if duration <= 0:
        return False

    frame_times = [duration * t for t in (0.15, 0.5, 0.85)]
    frame_parts = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, ts in enumerate(frame_times):
            frame_path = Path(tmpdir) / f"frame_{idx}.jpg"
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
                 "-vframes", "1", "-q:v", "3", str(frame_path)],
                capture_output=True,
            )
            if r.returncode == 0 and frame_path.exists():
                frame_parts.append(
                    types.Part.from_bytes(data=frame_path.read_bytes(), mime_type="image/jpeg")
                )

    if not frame_parts:
        return False

    try:
        api_key = get_gemini_api_key()
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                *frame_parts,
                "Do you see burned-in subtitles overlaid on the video frames? Answer YES or NO.",
            ],
        )
        answer = (response.text or "").strip().upper()
        result_val = "YES" in answer
        logger.info("detect_burned_subtitles: %s → %s", answer, result_val)
        return result_val
    except Exception as e:
        logger.warning("detect_burned_subtitles: ошибка %s, считаем False", e)
        return False


def _get_video_duration(video_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _parse_time(v) -> float | None:
    """Parse timestamp: float → float, '01:23' → 83.0, None → None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            pass
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_task(task: dict) -> dict:
    """Normalize a single task dict from Gemini's free-form output."""
    # Normalize key: type might be 'task_type', 'action', 'step', etc.
    for key in ("task_type", "action", "step", "operation"):
        if key in task and "type" not in task:
            task["type"] = task.pop(key)
            break

    # Normalize output_id / id
    if "id" in task and "output_id" not in task:
        task["output_id"] = task.pop("id")

    params = task.get("params", task.get("parameters", task.get("config", {})))
    if not isinstance(params, dict):
        params = {}
    task["params"] = params

    # Normalize timestamp strings in params
    for key in ("start_time", "end_time", "start", "end"):
        if key in params:
            parsed = _parse_time(params[key])
            if parsed is not None:
                params[key] = parsed

    return task


def _normalize_gemini_response(data: dict) -> dict:
    """Normalize Gemini's free-form JSON to match PlanResponse schema."""
    # Normalize tasks list key
    tasks_raw = data.get("tasks", data.get("task_list", data.get("steps", [])))
    if not isinstance(tasks_raw, list):
        tasks_raw = []
    data["tasks"] = [_normalize_task(t) for t in tasks_raw if isinstance(t, dict)]

    # Ensure required top-level fields
    if "scenario_name" not in data:
        data["scenario_name"] = data.get("name", data.get("title", "Video Plan"))
    if "scenario_description" not in data:
        data["scenario_description"] = data.get("description", data.get("summary", ""))
    if "metadata" not in data:
        data["metadata"] = {}

    return data


def _upload_video(client, video_path: Path, is_long: bool) -> types.Part:
    """Upload video to Files API or inline."""
    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    if file_size_mb > 20:
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
                logger.info("Gemini: ожидание... %ds (state=%s)", i * 2, state)
            time.sleep(2)
        fps = 0.1 if is_long else 0.5
        return types.Part(
            file_data=types.FileData(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
            video_metadata=types.VideoMetadata(fps=fps),
        )
    else:
        return types.Part.from_bytes(data=video_path.read_bytes(), mime_type="video/mp4")


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_and_generate_plan(
    video_path: Path,
    user_prompt: str,
    model: str = "gemini-2.5-flash",
    broll_rules: dict | None = None,
) -> dict:
    """Generate editing plan using hybrid architecture.

    Flow:
    1. Transcribe audio (Whisper).
    2. Detect burned-in subtitles (Gemini Vision).
    3. Compute B-roll slots deterministically from transcript (Python).
    4. Upload video, call Gemini with pre-computed slots as constraints.
    5. Validate and fix plan (Python).
    """
    from services.transcriber import transcribe

    rules = broll_rules or BROLL_RULES
    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)

    duration_sec = _get_video_duration(video_path)
    is_long = duration_sec > LONG_VIDEO_THRESHOLD_SEC

    # Step 1: Transcribe
    t0 = time.time()
    logger.info("Gemini: транскрипция Whisper (%.0f сек)...", duration_sec)
    transcript_text, transcript_segments, _ = transcribe(video_path)
    logger.info("Gemini: транскрипция %.1f сек", time.time() - t0)

    # Step 2: Detect burned subtitles
    t0 = time.time()
    has_burned_subs = detect_burned_subtitles(video_path)
    logger.info("Gemini: burned-in субтитры = %s (%.1f сек)", has_burned_subs, time.time() - t0)

    # Step 3: Compute B-roll slots (Python, deterministic)
    broll_slots = find_broll_slots(transcript_segments, duration_sec, rules)

    # Step 4: Upload video and generate plan
    video_part = _upload_video(client, video_path, is_long)

    # Build prompt with pre-computed B-roll positions as constraints
    slots_json = json.dumps(broll_slots, ensure_ascii=False, indent=2)
    broll_constraint = ""
    if broll_slots:
        broll_constraint = (
            f"\n\nPRE-COMPUTED B-ROLL SLOTS (DO NOT CHANGE THESE TIMESTAMPS):\n{slots_json}\n"
            f"Generate exactly {len(broll_slots)} fetch_stock_video + overlay_video task pairs, "
            f"using these exact start_time/end_time values. Use context_text to decide the stock query.\n"
        )

    segments_for_prompt = transcript_segments[:200] if transcript_segments else []
    transcript_block = (
        f"VIDEO TRANSCRIPT:\n---\n{transcript_text[:15000]}\n---\n"
        f"Segments with timestamps: {json.dumps(segments_for_prompt)}\n"
        if transcript_text else ""
    )

    burned_note = (
        "NOTE: Video has burned-in subtitles — do NOT add add_subtitles task.\n"
        if has_burned_subs else ""
    )

    prompt = "".join([
        transcript_block,
        broll_constraint,
        burned_note,
        f"\nUser request: {user_prompt}\n\n"
        "Return JSON: scenario_name, scenario_description, metadata, tasks. "
        "EVERY task must have fully filled params.",
    ])

    t_gen = time.time()
    logger.info("Gemini: вызов generate_content (model=%s)...", model)
    response = client.models.generate_content(
        model=model,
        contents=[video_part, prompt],
        config={
            "system_instruction": CONTENT_SYSTEM_INSTRUCTION,
            "response_mime_type": "application/json",
        },
    )
    logger.info("Gemini: generate_content %.1f сек", time.time() - t_gen)

    if not response.text:
        raise ValueError("Empty response from Gemini")

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        logger.error("Gemini: JSON parse error: %s\nResponse: %s", e, response.text[:500])
        raise ValueError(f"Cannot parse Gemini response as JSON: {e}")

    # Normalize response: Gemini sometimes uses task_type/action/step instead of type,
    # and string timestamps instead of floats.
    data = _normalize_gemini_response(data)
    validated = PlanResponse.model_validate(data)

    tasks = []
    for t in validated.tasks:
        if not t.params and t.inputs is None:
            continue
        task_dict: dict = {"type": t.type, "params": t.params}
        if t.output_id is not None:
            task_dict["output_id"] = t.output_id
        if t.inputs is not None:
            task_dict["inputs"] = t.inputs
        tasks.append(task_dict)

    # Step 5: Validate and fix (Python, deterministic)
    tasks = validate_and_fix_plan(tasks, rules)

    return {
        "scenario_name": validated.scenario_name,
        "scenario_description": validated.scenario_description,
        "metadata": validated.metadata,
        "tasks": tasks,
    }


def analyze_and_generate_tasks(video_path: Path, user_prompt: str) -> list[dict]:
    """Legacy: analyze and return tasks only."""
    plan = analyze_and_generate_plan(video_path, user_prompt)
    return plan["tasks"]
