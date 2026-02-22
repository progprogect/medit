"""Render scenario to final video: trim, overlay, concat."""

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from schemas.scenario import Layer, Overlay, Scenario, Segment

from services.executor import run_tasks
from services.storage import Storage

logger = logging.getLogger(__name__)


class RenderBlocked(Exception):
    """Render cannot proceed: some segments need video (generate or upload)."""

    def __init__(self, message: str, segment_ids: list[str] | None = None):
        super().__init__(message)
        self.segment_ids = segment_ids or []


OVERLAY_PRESETS: dict[str, dict[str, Any]] = {
    "minimal": {
        "font_color": "white",
        "shadow": True,
        "background": None,
        "border_width": 0,
        "border_color": None,
        "font_size": 48,
    },
    "box_dark": {
        "font_color": "white",
        "shadow": True,
        "background": "dark",
        "border_width": 0,
        "border_color": None,
        "font_size": 48,
    },
    "box_light": {
        "font_color": "black",
        "shadow": False,
        "background": "light",
        "border_width": 0,
        "border_color": None,
        "font_size": 48,
    },
    "outline": {
        "font_color": "white",
        "shadow": False,
        "background": None,
        "border_width": 2,
        "border_color": "black",
        "font_size": 48,
    },
    "bold_center": {
        "font_color": "white",
        "shadow": True,
        "background": None,
        "border_width": 0,
        "border_color": None,
        "font_size": 72,
    },
}


def get_overlay_styles() -> list[dict[str, str]]:
    """Return list of overlay style presets for API."""
    labels = {
        "minimal": "Минимальный",
        "box_dark": "Тёмный фон",
        "box_light": "Светлый фон",
        "outline": "Контур",
        "bold_center": "Крупный по центру",
    }
    return [{"id": k, "label": labels.get(k, k)} for k in OVERLAY_PRESETS]


def _get_scene_by_id(scenario: Scenario, scene_id: str | None):
    """Get scene by id (Scene model or dict)."""
    if not scene_id:
        return None
    for s in scenario.scenes:
        sid = s.id if hasattr(s, "id") else s.get("id")
        if sid == scene_id:
            return s
    return None


def _overlays_for_segment(
    scenario: Scenario, segment: Segment
) -> list[Overlay]:
    """Get overlays from scene that fall within segment time range."""
    scene = _get_scene_by_id(scenario, segment.scene_id)
    if not scene:
        return []
    overlays_raw = scene.overlays if hasattr(scene, "overlays") else scene.get("overlays", [])
    result = []
    for o in overlays_raw:
        ov = Overlay(**o) if isinstance(o, dict) else o
        # Overlay must overlap with segment
        if ov.end_sec <= segment.start_sec or ov.start_sec >= segment.end_sec:
            continue
        result.append(ov)
    return result


def _overlay_to_task_params(
    overlay: Overlay,
    segment_start: float,
    preset: dict[str, Any],
) -> dict[str, Any]:
    """Convert overlay to add_text_overlay task params with preset style."""
    # Times relative to segment start (trimmed clip)
    start_time = max(0, overlay.start_sec - segment_start)
    end_time = overlay.end_sec - segment_start
    if end_time <= start_time:
        end_time = start_time + 1

    params = {
        "text": overlay.text,
        "position": overlay.position or "center",
        "start_time": start_time,
        "end_time": end_time,
        "font_size": preset.get("font_size", 48),
        "font_color": preset.get("font_color", "white"),
        "shadow": preset.get("shadow", False),
        "background": preset.get("background"),
        "border_width": preset.get("border_width", 0),
        "border_color": preset.get("border_color"),
    }
    return params


def scenario_to_render_tasks(
    scenario: Scenario,
    overlay_style: str,
    source_path: Path,
) -> tuple[list[dict], dict[str, Path]]:
    """
    Build executor tasks for rendering scenario.
    Returns (tasks, initial_registry).
    Raises RenderBlocked if any segment is not ready.
    """
    preset = OVERLAY_PRESETS.get(overlay_style, OVERLAY_PRESETS["minimal"])

    video_layer: Layer | None = next(
        (l for l in scenario.layers if l.type == "video"), None
    )
    if not video_layer or not video_layer.segments:
        raise ValueError("Scenario has no video segments")

    segments = sorted(video_layer.segments, key=lambda s: s.start_sec)
    blocked: list[str] = []

    for seg in segments:
        status = seg.asset_status or "ready"
        source = seg.asset_source or "uploaded"
        # MVP: only uploaded segments
        if status != "ready" or source != "uploaded":
            blocked.append(seg.id)

    if blocked:
        raise RenderBlocked(
            f"Segments need video: {', '.join(blocked)}. Generate or upload first.",
            segment_ids=blocked,
        )

    tasks: list[dict] = []
    segment_output_ids: list[str] = []

    for i, seg in enumerate(segments):
        seg_out_id = f"seg_{i}"
        segment_output_ids.append(seg_out_id)

        # 1. Trim
        trim_task = {
            "type": "trim",
            "params": {"start": seg.start_sec, "end": seg.end_sec},
            "output_id": seg_out_id,
            "inputs": ["source"],
        }
        tasks.append(trim_task)

        # 2. Overlays (chain on trim output)
        overlays = _overlays_for_segment(scenario, seg)
        prev_id = seg_out_id
        for ov in overlays:
            if not ov.text.strip():
                continue
            ov_params = _overlay_to_task_params(ov, seg.start_sec, preset)
            overlay_task = {
                "type": "add_text_overlay",
                "params": ov_params,
                "output_id": seg_out_id,
                "inputs": [prev_id],
            }
            tasks.append(overlay_task)
            prev_id = seg_out_id

    # 3. Concat all segments
    concat_task = {
        "type": "concat",
        "params": {},
        "inputs": segment_output_ids,
    }
    tasks.append(concat_task)

    initial_registry = {"source": source_path}
    return tasks, initial_registry


def render_scenario(
    project_id: str,
    scenario: Scenario,
    assets: list[dict],
    overlay_style: str,
    storage: Storage,
) -> str:
    """
    Render scenario to video. Returns output_key for download.
    """
    # Main asset: first video (same as scenario generation)
    main_asset = None
    for a in assets:
        if a.get("type") == "video":
            main_asset = a
            break
    if not main_asset:
        main_asset = assets[0] if assets else None
    if not main_asset or not main_asset.get("file_key"):
        raise ValueError("No video asset for rendering")

    source_path = storage.get_asset_path(main_asset["file_key"])

    tasks, initial_registry = scenario_to_render_tasks(
        scenario, overlay_style, source_path
    )

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        temp_out = Path(tf.name)
    try:
        logger.info("Render: %d tasks for project %s", len(tasks), project_id)
        run_tasks(
            input_path=source_path,
            tasks=tasks,
            output_path=temp_out,
            initial_registry=initial_registry,
        )
        output_key = f"{project_id}/{uuid.uuid4()}"
        saved_key = storage.save_output(output_key, temp_out)
        return saved_key
    finally:
        temp_out.unlink(missing_ok=True)
