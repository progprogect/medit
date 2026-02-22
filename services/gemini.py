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
from schemas.scenario import Scenario
from schemas.tasks import PlanResponse

from services.scenario_service import scenario_from_simple_output, tasks_to_scenario, validate_scenario

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
  3. add_text_overlay (ONLY if user explicitly asked for text on screen; otherwise skip)
  4. fetch_stock_video (one per B-roll slot, with specific visual query)
  5. overlay_video (one per B-roll slot, using the pre-decided timestamps)
  6. color_correction (optional final polish)

RULES:
- add_text_overlay params: text, position, font_size, font_color, shadow, background, start_time, end_time ONLY.
- fetch_stock_video params: query, alternative_queries, duration_max, orientation ONLY. MUST have output_id.
- overlay_video params: start_time, end_time, stock_id ONLY.
- Add text overlays ONLY when user explicitly requests text on screen. Otherwise generate ZERO add_text_overlay tasks.
- If user requests content at the end of the video (outro, additional clip, etc.) — use the end slot (last seconds) for fetch_stock_video + overlay_video. Interpret the request to choose the stock query.
- Stock queries: describe a SPECIFIC visual scene.
- Do NOT generate more fetch_stock_video / overlay_video tasks than the pre-computed slots.

NEVER mix params between task types. Generate ONLY valid JSON. No markdown."""

# ─── Simple scenario (no transcription, no ffmpeg) ─────────────────────────────
SCENARIO_SIMPLE_INSTRUCTION = """You are a video editor. You receive a video and a user prompt.
Analyze the video visually and generate a scenario with text overlays.

Return ONLY valid JSON (no markdown) with this structure:
{
  "metadata": {
    "name": "short title",
    "description": "brief description",
    "total_duration_sec": <video duration in seconds>,
    "aspect_ratio": "9:16"
  },
  "scenes": [{
    "id": "scene_0",
    "start_sec": 0,
    "end_sec": <total_duration_sec>,
    "visual_description": "what happens in the video",
    "overlays": [
      {"text": "overlay text", "position": "center|bottom|top", "start_sec": 2.0, "end_sec": 5.0},
      ...
    ]
  }]
}

RULES:
- Add text overlays ONLY when user explicitly requests them. Otherwise return overlays: [].
- If user requests content at the end of the video — add a second scene or describe the stock clip to fetch based on the request.
- position: "center", "bottom", or "top"
- Overlays must not overlap; each 2-4 seconds
- Match overlay content to user prompt when overlays are requested"""

# ─── Refine scenario ────────────────────────────────────────────────────────────
REFINE_INSTRUCTION = """You are a video editor. You receive:
1. A current scenario (JSON with metadata, scenes, layers).
2. A user refinement request.

Your job: apply ONLY the requested changes and return the updated scenario in the exact same JSON structure.

RULES:
- Preserve metadata.total_duration_sec, metadata.aspect_ratio, asset_ids.
- Preserve scene and segment IDs where possible.
- Apply only what the user asked (e.g. add overlays, change text, remove B-roll, shorten).
- Return valid JSON. No markdown."""

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

def _validate_and_fix_slots(
    raw_slots: list[dict],
    video_duration: float,
    rules: dict,
) -> list[dict]:
    """Validate and fix B-roll slots from LLM output.
    Enforces: valid range, min duration, max duration, min gap between slots.
    """
    target_dur = (rules["min_duration"] + rules["max_duration"]) / 2
    avoid_end = video_duration - rules["avoid_last"]
    valid_slots = []

    for slot in sorted(raw_slots, key=lambda s: s["start"]):
        start = slot["start"]
        end = slot.get("end") or round(start + target_dur, 1)

        # Enforce valid range
        start = max(start, rules["avoid_first"])
        end = min(end, avoid_end)

        # Enforce duration bounds
        dur = end - start
        if dur < rules["min_duration"]:
            end = round(start + rules["min_duration"], 1)
        elif dur > rules["max_duration"]:
            end = round(start + rules["max_duration"], 1)

        # Skip if out of range
        if start >= avoid_end or end > avoid_end:
            continue

        # Enforce minimum gap from previous slot
        if valid_slots and start < valid_slots[-1]["end"] + rules["min_gap"]:
            start = round(valid_slots[-1]["end"] + rules["min_gap"], 1)
            end = round(start + target_dur, 1)
            if end > avoid_end:
                continue

        slot = dict(slot)
        slot["start"] = round(start, 1)
        slot["end"] = round(end, 1)
        valid_slots.append(slot)

    return valid_slots[:rules["max_inserts"]]


def find_broll_slots(
    segments: list[dict],
    video_duration: float,
    rules: dict | None = None,
) -> list[dict]:
    """Deterministic fallback: equal-section split when semantic search fails."""
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

    section_len = (valid_end - valid_start) / max_n
    slots: list[dict] = []

    for i in range(max_n):
        section_start = valid_start + i * section_len
        section_end = section_start + section_len
        boundaries = [s["end"] for s in segments if section_start <= s["end"] <= section_end]
        mid = (section_start + section_end) / 2
        snap = min(boundaries, key=lambda b: abs(b - mid)) if boundaries else mid

        insert_start = round(snap, 1)
        insert_end = round(insert_start + target_dur, 1)

        if slots and insert_start < slots[-1]["end"] + min_gap:
            insert_start = round(slots[-1]["end"] + min_gap, 1)
            insert_end = round(insert_start + target_dur, 1)
        if insert_end > avoid_end:
            break

        context_segs = [s for s in segments if s["start"] >= insert_start - 3 and s["end"] <= insert_end + 5]
        context_text = " ".join(s["text"] for s in context_segs).strip()[:300]
        slots.append({"start": insert_start, "end": insert_end, "context_text": context_text})

    logger.info("B-roll slots (fallback): %s", [(s["start"], s["end"]) for s in slots])
    return slots


def find_broll_slots_semantic(
    segments: list[dict],
    video_duration: float,
    client,
    model: str,
    rules: dict,
) -> list[dict]:
    """Semantic B-roll slot selection: Gemini picks the BEST moments, Python enforces rules.

    Gemini analyzes what is being discussed and selects moments where:
    - A specific topic is discussed for 5+ seconds continuously
    - The content has a clear visual concept for stock footage
    - The moment benefits from visual reinforcement
    Python then validates timing constraints.
    """
    target_dur = (rules["min_duration"] + rules["max_duration"]) / 2

    prompt = f"""You are a video editor. Analyze this transcript and find the {rules['max_inserts']} BEST moments to insert B-roll stock footage.

TRANSCRIPT (with exact timestamps):
{json.dumps(segments, ensure_ascii=False, indent=2)}

Video duration: {video_duration:.1f} seconds

SELECTION CRITERIA:
1. Speaker discusses a SPECIFIC topic continuously for at least {rules['min_topic_sec']} seconds
2. The content has a CLEAR VISUAL CONCEPT that stock footage can illustrate
3. NOT in the first {rules['avoid_first']}s or last {rules['avoid_last']}s of the video
4. Moments should be at least {rules['min_gap']}s apart
5. Choose the MOST IMPACTFUL and visually illustratable moments

SCORING — prefer moments where:
- Speaker describes a specific action or tool (e.g. "email automation", "AI messaging")
- The topic is sustained (5+ seconds of related speech)
- A visual would ENHANCE understanding, not just decorate
- Avoid generic moments or simple greetings

For each chosen moment, return:
- start: the exact second to BEGIN showing the B-roll overlay (from transcript timestamps)
- topic: brief topic label
- context_text: the relevant speech text at this moment
- query: a SPECIFIC stock footage search query describing the visual scene
- alternative_queries: 2-3 fallback queries

Return JSON array of exactly {rules['max_inserts']} best moments:
[{{"start": 19.7, "topic": "email automation setup", "context_text": "...", "query": "person configuring email automation on laptop screen", "alternative_queries": ["email marketing software setup", "business automation tool"]}}]"""

    try:
        t0 = time.time()
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config={"response_mime_type": "application/json"},
        )
        logger.info("BrollScan: семантический поиск моментов %.1f сек", time.time() - t0)
        raw = json.loads(response.text)
        if not isinstance(raw, list):
            raw = []
    except Exception as e:
        logger.warning("BrollScan: семантический поиск не удался (%s), используем fallback", e)
        return find_broll_slots(segments, video_duration, rules)

    if not raw:
        logger.warning("BrollScan: Gemini не вернул моменты, используем fallback")
        return find_broll_slots(segments, video_duration, rules)

    # Normalize: add end time based on target duration
    for slot in raw:
        if "start" not in slot:
            continue
        slot["end"] = round(float(slot["start"]) + target_dur, 1)

    # Validate and enforce rules
    validated = _validate_and_fix_slots(raw, video_duration, rules)

    # Fallback if validation removed too many
    if len(validated) < 1:
        logger.warning("BrollScan: все моменты не прошли валидацию, используем fallback")
        return find_broll_slots(segments, video_duration, rules)

    logger.info("BrollScan: семантические слоты: %s",
                [(s["start"], s["end"], s.get("topic", "")) for s in validated])
    return validated


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

    # Add optional end slot — LLM will use it only when user requests content at the end
    end_start = max(0, duration_sec - 6)
    end_slot = {"start": end_start, "end": duration_sec, "context_text": user_prompt[:200]}
    broll_slots = list(broll_slots)[: rules["max_inserts"] - 1] + [end_slot]
    rules = dict(rules)
    rules["max_inserts"] = len(broll_slots)

    # Step 4: Upload video and generate plan
    video_part = _upload_video(client, video_path, is_long)

    # Build prompt with pre-computed B-roll positions as constraints
    slots_json = json.dumps(broll_slots, ensure_ascii=False, indent=2)
    broll_constraint = ""
    if broll_slots:
        broll_constraint = (
            f"\n\nB-ROLL SLOTS (use these timestamps):\n{slots_json}\n"
            f"Generate fetch_stock_video + overlay_video for each slot that fits the user request. "
            f"The last slot is for the end of the video — use it ONLY if user requests content at the end. "
            f"Use context_text and user request to decide the stock query for each slot.\n"
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

    # Fallback: if we have broll_slots but Gemini returned no overlay_video, synthesize from context
    overlay_count = sum(1 for t in tasks if t["type"] == "overlay_video")
    if broll_slots and overlay_count == 0:
        logger.info("Gemini не вернул overlay_video — синтезируем из слотов")
        for i, slot in enumerate(broll_slots):
            out_id = f"stock_{i + 1}"
            query = (slot.get("context_text") or "professional b-roll")[:80]
            tasks.append({"type": "fetch_stock_video", "output_id": out_id, "params": {"query": query}})
            tasks.append({"type": "overlay_video", "params": {"start_time": slot["start"], "end_time": slot["end"], "stock_id": out_id}})
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


def scan_broll_suggestions(
    video_path: Path,
    max_inserts: int = 3,
    model: str = "gemini-2.5-flash",
) -> list[dict]:
    """Scan video and return B-roll insertion suggestions.

    Returns list of:
    {start, end, context_text, query, alternative_queries}
    """
    from services.transcriber import transcribe

    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)

    rules = dict(BROLL_RULES)
    rules["max_inserts"] = max_inserts

    duration_sec = _get_video_duration(video_path)

    t0 = time.time()
    logger.info("BrollScan: транскрипция...")
    transcript_text, transcript_segments, _ = transcribe(video_path)
    logger.info("BrollScan: транскрипция %.1f сек", time.time() - t0)

    # Semantic slot selection: Gemini finds best moments, Python validates constraints
    slots = find_broll_slots_semantic(transcript_segments, duration_sec, client, model, rules)
    if not slots:
        return []

    result = []
    for slot in slots:
        result.append({
            "start": slot["start"],
            "end": slot["end"],
            "duration": round(slot["end"] - slot["start"], 1),
            "context_text": slot.get("context_text", ""),
            "query": slot.get("query", slot.get("topic", slot.get("context_text", "")[:60])),
            "alternative_queries": slot.get("alternative_queries", []),
        })

    logger.info("BrollScan: %d предложений готово", len(result))
    return result


def generate_scenario(
    assets: list[dict],
    global_prompt: str,
    reference_links: list[str] | None = None,
    storage=None,
) -> Scenario:
    """
    Generate Scenario (scenes + layers) from assets and prompt.

    Uses analyze_and_generate_plan (transcription + Gemini) when possible.
    Fallback to simple flow (no transcription) if ffmpeg audio extraction fails.
    Result: scenario only, no video rendering.
    """
    if not assets:
        raise ValueError("At least one asset required")
    if not global_prompt.strip():
        raise ValueError("global_prompt is required")

    if storage is None:
        from services.storage import get_storage
        storage = get_storage()

    # Find first video asset
    video_asset = None
    for a in assets:
        if a.get("type") == "video":
            video_asset = a
            break
    if not video_asset:
        raise ValueError("At least one video asset required for scenario generation")

    file_key = video_asset.get("file_key")
    if not file_key:
        raise ValueError("Asset missing file_key")
    video_path = storage.get_asset_path(file_key)
    total_duration = video_asset.get("duration_sec") or _get_video_duration(video_path)
    if total_duration <= 0:
        total_duration = _get_video_duration(video_path)

    main_asset_id = video_asset.get("id", "")

    # Try full flow with transcription first
    try:
        plan_dict = analyze_and_generate_plan(video_path, global_prompt)
        tasks = plan_dict.get("tasks", [])

        plan = PlanResponse(
            scenario_name=plan_dict["scenario_name"],
            scenario_description=plan_dict["scenario_description"],
            metadata=plan_dict.get("metadata", {}),
            tasks=tasks,
        )
        scenario = tasks_to_scenario(plan, assets, total_duration)
    except (subprocess.CalledProcessError, OSError) as e:
        # ffmpeg failed (e.g. audio extraction for MOV/codec issues)
        logger.warning("Transcription/ffmpeg failed (%s), fallback to simple flow", e)
        scenario = _generate_scenario_simple(video_path, main_asset_id, total_duration, global_prompt)

    # Validate
    errors = validate_scenario(scenario, assets)
    if errors:
        for e in errors:
            logger.warning("Scenario validation: %s", e.message)

    return scenario


def _generate_scenario_simple(
    video_path: Path, main_asset_id: str, total_duration: float, global_prompt: str
) -> Scenario:
    """Fallback: Gemini Vision only, no transcription."""
    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)
    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    is_long = file_size_mb > 20
    video_part = _upload_video(client, video_path, is_long)
    prompt = (
        f"Video duration: {total_duration:.1f} seconds. "
        f"User request: {global_prompt}\n\nReturn the scenario JSON as specified."
    )
    logger.info("Gemini: generate_scenario (fallback, no transcription)...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[video_part, prompt],
        config={
            "system_instruction": SCENARIO_SIMPLE_INSTRUCTION,
            "response_mime_type": "application/json",
        },
    )
    if not response.text:
        raise ValueError("Empty response from Gemini")
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        logger.error("Gemini: JSON parse error: %s", e)
        raise ValueError(f"Cannot parse Gemini response: {e}")
    return scenario_from_simple_output(data, main_asset_id, total_duration)


def refine_scenario(
    scenario: Scenario,
    refinement_prompt: str,
    assets: list[dict],
) -> Scenario:
    """
    Refine existing scenario based on user prompt. No video reload.
    """
    if not refinement_prompt.strip():
        raise ValueError("refinement_prompt is required")

    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)

    scenario_json = json.dumps(scenario.model_dump(), ensure_ascii=False, indent=2)
    prompt = (
        f"CURRENT SCENARIO:\n{scenario_json}\n\n"
        f"USER REFINEMENT REQUEST: {refinement_prompt}\n\n"
        "Return the updated scenario JSON."
    )

    logger.info("Gemini: refine_scenario...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "system_instruction": REFINE_INSTRUCTION,
            "response_mime_type": "application/json",
        },
    )

    if not response.text:
        raise ValueError("Empty response from Gemini")

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        logger.error("Gemini: JSON parse error: %s", e)
        raise ValueError(f"Cannot parse Gemini response: {e}")

    from services.scenario_service import (
        ensure_video_layer_matches_scenes,
        normalize_llm_scenario,
        scenario_from_simple_output,
    )

    if "scenes" in data and "metadata" in data:
        if "layers" in data and data["layers"]:
            scenario = normalize_llm_scenario(data)
        else:
            main_asset_id = ""
            if assets:
                main_asset_id = assets[0].get("id", "") if isinstance(assets[0], dict) else getattr(assets[0], "id", "")
            duration = scenario.metadata.total_duration_sec or 60
            scenario = scenario_from_simple_output(data, main_asset_id, duration)
    else:
        raise ValueError("Invalid scenario structure from Gemini")

    asset_ids = [a.get("id") if isinstance(a, dict) else getattr(a, "id", None) for a in assets if a]
    asset_ids = [x for x in asset_ids if x]
    if asset_ids and scenario.metadata:
        scenario.metadata.asset_ids = asset_ids

    scenario = ensure_video_layer_matches_scenes(scenario, asset_ids[0] if asset_ids else None)

    errors = validate_scenario(scenario, assets)
    for e in errors:
        logger.warning("Scenario validation: %s", e.message)

    return scenario
