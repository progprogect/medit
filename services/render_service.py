"""Render scenario to final video: full video + B-roll overlays + text overlays."""

import logging
import subprocess
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


def _get_video_size(path: Path) -> tuple[int, int]:
    """Return (width, height) via ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return 1080, 1920
    lines = r.stdout.strip().splitlines()
    try:
        return int(lines[0]), int(lines[1])
    except (ValueError, IndexError):
        return 1080, 1920


def _overlay_to_task_params(
    overlay: Overlay,
    preset: dict[str, Any],
) -> dict[str, Any]:
    """Convert overlay to add_text_overlay task params (absolute times)."""
    start_time = overlay.start_sec
    end_time = overlay.end_sec
    if end_time <= start_time:
        end_time = start_time + 1

    return {
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


def scenario_to_render_tasks(
    scenario: Scenario,
    overlay_style: str,
    source_path: Path,
    broll_registry: dict[str, Path],
    main_asset_id: str | None = None,
) -> list[dict]:
    """
    Build executor tasks: overlay_video for B-roll, add_text_overlay for text.
    Full video as base, audio preserved. B-roll overlays replace picture at slots.
    """
    preset = OVERLAY_PRESETS.get(overlay_style, OVERLAY_PRESETS["minimal"])

    video_layer: Layer | None = next(
        (l for l in scenario.layers if l.type == "video"), None
    )
    if not video_layer or not video_layer.segments:
        raise ValueError("Scenario has no video segments")

    def _is_broll(seg: Segment) -> bool:
        if (seg.asset_source or "uploaded") == "generated" or (seg.asset_status or "ready") == "pending":
            return True
        if (seg.asset_source or "uploaded") == "uploaded" and (seg.asset_status or "ready") == "ready":
            return bool(seg.asset_id and main_asset_id and seg.asset_id != main_asset_id)
        return False

    segments = sorted(video_layer.segments, key=lambda s: s.start_sec)
    tasks: list[dict] = []
    broll_idx = 0

    # 1. overlay_video for each B-roll segment
    for seg in segments:
        if not _is_broll(seg):
            continue
        oid = f"broll_{broll_idx + 1}"
        if oid not in broll_registry:
            logger.warning("Render: B-roll segment %s has no clip (oid=%s), skip", seg.id, oid)
            continue
        tasks.append({
            "type": "overlay_video",
            "params": {
                "start_time": seg.start_sec,
                "end_time": seg.end_sec,
                "stock_id": oid,
            },
        })
        broll_idx += 1

    # 2. add_text_overlay for each overlay (from all scenes)
    for scene in scenario.scenes:
        for ov in scene.overlays if hasattr(scene, "overlays") else scene.get("overlays", []):
            o = Overlay(**ov) if isinstance(ov, dict) else ov
            if not o.text.strip():
                continue
            params = _overlay_to_task_params(o, preset)
            tasks.append({
                "type": "add_text_overlay",
                "params": params,
            })

    return tasks


def render_scenario(
    project_id: str,
    scenario: Scenario,
    assets: list[dict],
    overlay_style: str,
    storage: Storage,
) -> str:
    """
    Render scenario to video.
    Full main video + B-roll overlays (at slots) + text overlays.
    Audio from main video throughout.
    """
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
    w, h = _get_video_size(source_path)
    orientation = "portrait" if h > w else "landscape"
    max_width = min(w, h, 1920) if h > w else min(w, 1920)
    dest_dir = storage.output_dir

    # Build asset lookup
    assets_by_id = {a.get("id"): a for a in assets if a.get("id")}
    main_asset_id = main_asset.get("id")

    # Fetch B-roll for B-roll segments (not main video). Main = asset_id == main_asset_id.
    video_layer = next((l for l in scenario.layers if l.type == "video"), None)
    broll_registry: dict[str, Path] = {}
    broll_idx = 0

    def _is_broll_segment(seg: Segment) -> bool:
        """B-roll slot: generated/pending, or pre-fetched (uploaded+ready but asset_id != main)."""
        if (seg.asset_source or "uploaded") == "generated" or (seg.asset_status or "ready") == "pending":
            return True
        if (seg.asset_source or "uploaded") == "uploaded" and (seg.asset_status or "ready") == "ready":
            return seg.asset_id and seg.asset_id != main_asset_id  # pre-fetched stock
        return False

    def _get_broll_query(seg: Segment) -> str:
        """Resolve stock query from segment.params, scene.generation_tasks, or scene.visual_description."""
        q = (seg.params or {}).get("query", "")
        if q:
            return q
        if not seg.scene_id:
            return "professional b-roll"
        scene = next((s for s in scenario.scenes if s.id == seg.scene_id), None)
        if not scene:
            return "professional b-roll"
        for gt in getattr(scene, "generation_tasks", []) or []:
            g = gt if hasattr(gt, "params") else (gt or {})
            sid = getattr(gt, "segment_id", None) or (g.get("segment_id") if isinstance(g, dict) else None)
            if sid == seg.id:
                params = getattr(g, "params", None) or (g.get("params") if isinstance(g, dict) else {}) or {}
                q = params.get("query", "")
                if q:
                    return q
        # Fallback: use visual_description or generic
        desc = getattr(scene, "visual_description", None) or ""
        if desc and len(desc) > 3:
            return desc[:80]
        return "professional b-roll"

    if video_layer:
        from services.stock import fetch_stock_media

        for seg in sorted(video_layer.segments, key=lambda s: s.start_sec):
            if not _is_broll_segment(seg):
                continue  # Main video segment, not B-roll
            oid = f"broll_{broll_idx + 1}"
            media_path = None
            # Use pre-fetched asset if available
            if seg.asset_status == "ready" and seg.asset_id:
                a = assets_by_id.get(seg.asset_id)
                if a and a.get("file_key"):
                    try:
                        media_path = storage.get_asset_path(a["file_key"])
                    except Exception:
                        pass
            # Otherwise fetch from stock
            if not media_path or not media_path.exists():
                query = _get_broll_query(seg)
                if not query:
                    logger.warning("Render: B-roll segment %s has no query and no asset, skip", seg.id)
                    continue
                duration = max(5, int(seg.end_sec - seg.start_sec) + 2)
                media_path = fetch_stock_media(
                    query=query,
                    media_type="video",
                    dest_dir=dest_dir,
                    duration_max=duration,
                    orientation=orientation,
                    max_width=max_width,
                )
            if media_path and media_path.exists():
                broll_registry[oid] = media_path
                broll_idx += 1
        logger.info("Render: %d B-roll clips (pre-fetched or from stock)", len(broll_registry))

    tasks = scenario_to_render_tasks(
        scenario, overlay_style, source_path, broll_registry, main_asset_id
    )

    initial_registry = {"source": source_path}
    initial_registry.update(broll_registry)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        temp_out = Path(tf.name)
    try:
        logger.info(
            "Render: project=%s tasks=%d (overlay_video=%d add_text_overlay=%d)",
            project_id,
            len(tasks),
            sum(1 for t in tasks if t.get("type") == "overlay_video"),
            sum(1 for t in tasks if t.get("type") == "add_text_overlay"),
        )
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
