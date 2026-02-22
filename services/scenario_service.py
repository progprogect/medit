"""Scenario validation, normalization, and conversion."""

import logging
from typing import Any, Optional

from schemas.scenario import (
    AssetRef,
    GenerationTaskRef,
    Layer,
    Overlay,
    Scenario,
    ScenarioMetadata,
    Scene,
    Segment,
)
from schemas.tasks import PlanResponse

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Scenario validation error."""

    def __init__(self, message: str, field: Optional[str] = None):
        self.message = message
        self.field = field
        super().__init__(message)


def _asset_id(a: Any) -> str:
    """Get asset id from model or dict."""
    return getattr(a, "id", None) or (a.get("id") if isinstance(a, dict) else "")


def validate_scenario(
    scenario: Scenario, assets: Optional[list[Any]] = None
) -> list[ValidationError]:
    """
    Validate scenario consistency.
    Returns list of ValidationError (empty if valid).
    """
    errors: list[ValidationError] = []
    asset_ids = {_asset_id(a) for a in (assets or []) if _asset_id(a)}

    if not scenario.scenes and not scenario.layers:
        errors.append(ValidationError("Scenario must have at least one scene or layer"))

    scene_ids = {s.id for s in scenario.scenes}

    for scene in scenario.scenes:
        if scene.start_sec >= scene.end_sec:
            errors.append(
                ValidationError(
                    f"Scene {scene.id}: start_sec must be < end_sec",
                    field=f"scenes.{scene.id}",
                )
            )

    for i, scene in enumerate(sorted(scenario.scenes, key=lambda s: s.start_sec)):
        if i > 0 and scenario.scenes[i - 1].end_sec > scene.start_sec:
            errors.append(
                ValidationError(
                    f"Scenes overlap: {scenario.scenes[i - 1].id} and {scene.id}"
                )
            )

    for layer in scenario.layers:
        for seg in layer.segments:
            if seg.scene_id and seg.scene_id not in scene_ids:
                errors.append(
                    ValidationError(
                        f"Segment {seg.id}: scene_id '{seg.scene_id}' not found"
                    )
                )
            if (
                seg.asset_source == "uploaded"
                and seg.asset_id
                and seg.asset_id not in asset_ids
            ):
                errors.append(
                    ValidationError(
                        f"Segment {seg.id}: asset_id '{seg.asset_id}' not in project assets"
                    )
                )

    max_end = 0
    for layer in scenario.layers:
        for seg in layer.segments:
            if seg.end_sec > max_end:
                max_end = seg.end_sec

    if scenario.metadata.total_duration_sec is not None:
        if abs(scenario.metadata.total_duration_sec - max_end) > 0.1:
            errors.append(
                ValidationError(
                    f"metadata.total_duration_sec ({scenario.metadata.total_duration_sec}) "
                    f"does not match max segment end ({max_end})"
                )
            )

    return errors


def normalize_llm_scenario(raw: dict) -> Scenario:
    """Normalize raw LLM output to Scenario model."""
    metadata = raw.get("metadata", {})
    if isinstance(metadata, dict):
        meta = ScenarioMetadata(
            name=metadata.get("name", "Untitled"),
            description=metadata.get("description"),
            total_duration_sec=metadata.get("total_duration_sec"),
            aspect_ratio=metadata.get("aspect_ratio", "9:16"),
        )
    else:
        meta = ScenarioMetadata(name="Untitled")

    scenes = []
    for s in raw.get("scenes", []):
        overlays = [
            Overlay(
                text=o.get("text", ""),
                position=o.get("position", "center"),
                start_sec=float(o.get("start_sec", 0)),
                end_sec=float(o.get("end_sec", 0)),
            )
            for o in s.get("overlays", [])
        ]
        asset_refs = [
            AssetRef(
                asset_id=r.get("asset_id"),
                media_id=r.get("media_id"),
                usage=r.get("usage"),
            )
            for r in s.get("asset_refs", [])
        ]
        gen_tasks = [
            GenerationTaskRef(
                task_type=t.get("task_type", ""),
                params=t.get("params", {}),
                segment_id=t.get("segment_id"),
            )
            for t in s.get("generation_tasks", [])
        ]
        scenes.append(
            Scene(
                id=str(s.get("id", f"scene_{len(scenes)}")),
                start_sec=float(s.get("start_sec", 0)),
                end_sec=float(s.get("end_sec", 0)),
                visual_description=s.get("visual_description"),
                voiceover_text=s.get("voiceover_text"),
                overlays=overlays,
                effects=s.get("effects", []),
                transition=s.get("transition"),
                asset_refs=asset_refs,
                generation_tasks=gen_tasks,
            )
        )

    layers = []
    for layer in raw.get("layers", []):
        segments = []
        for seg in layer.get("segments", []):
            segments.append(
                Segment(
                    id=str(seg.get("id", f"seg_{len(segments)}")),
                    start_sec=float(seg.get("start_sec", 0)),
                    end_sec=float(seg.get("end_sec", 0)),
                    asset_id=seg.get("asset_id"),
                    asset_source=seg.get("asset_source"),
                    asset_status=seg.get("asset_status", "ready"),
                    scene_id=seg.get("scene_id"),
                    params=seg.get("params", {}),
                    generation_task_id=seg.get("generation_task_id"),
                )
            )
        layers.append(
            Layer(
                id=str(layer.get("id", f"layer_{len(layers)}")),
                type=layer.get("type", "video"),
                order=layer.get("order", len(layers)),
                segments=segments,
            )
        )

    return Scenario(
        version=int(raw.get("version", 1)),
        metadata=meta,
        scenes=scenes,
        layers=layers,
    )


def is_tasks_format(raw: dict) -> bool:
    """Check if raw is in old tasks format (PlanResponse)."""
    return "tasks" in raw and "scenario_name" in raw


def ensure_video_layer_matches_scenes(scenario: Scenario, main_asset_id: str | None = None) -> Scenario:
    """
    Ensure video layer has one segment per scene (timeline shows separate parts).
    Fixes scenarios where video was one combined segment.
    """
    scenes = sorted(scenario.scenes, key=lambda s: s.start_sec)
    if len(scenes) <= 1:
        return scenario

    video_layer = next((l for l in scenario.layers if l.type == "video"), None)
    if not video_layer:
        return scenario

    # If we already have one segment per scene, skip
    if len(video_layer.segments) >= len(scenes):
        return scenario

    main_id = main_asset_id
    if not main_id and video_layer.segments:
        main_id = video_layer.segments[0].asset_id

    new_segments = []
    for i, scene in enumerate(scenes):
        new_segments.append(
            Segment(
                id=f"seg_video_{i}",
                start_sec=scene.start_sec,
                end_sec=scene.end_sec,
                asset_id=main_id,
                asset_source="uploaded",
                asset_status="ready",
                scene_id=scene.id,
                params={},
            )
        )

    new_layers = []
    for layer in scenario.layers:
        if layer.type == "video":
            new_layers.append(Layer(id=layer.id, type=layer.type, order=layer.order, segments=new_segments))
        else:
            new_layers.append(layer)

    return Scenario(
        version=scenario.version,
        metadata=scenario.metadata,
        scenes=scenario.scenes,
        layers=new_layers,
    )


def scenario_from_simple_output(
    raw: dict, main_asset_id: str, duration: float
) -> Scenario:
    """
    Build full Scenario from simple LLM output (metadata + scenes with overlays).
    No transcription, no ffmpeg. Used for Create Scenario MVP.
    Video layer: one segment per scene (timeline shows separate parts).
    """
    meta_raw = raw.get("metadata", {}) or {}
    meta = ScenarioMetadata(
        name=meta_raw.get("name", "Untitled"),
        description=meta_raw.get("description"),
        total_duration_sec=meta_raw.get("total_duration_sec") or duration,
        aspect_ratio=meta_raw.get("aspect_ratio", "9:16"),
    )
    scenes_raw = raw.get("scenes", [])
    if not scenes_raw:
        scenes_raw = [{
            "id": "scene_0",
            "start_sec": 0,
            "end_sec": duration,
            "visual_description": meta.description or "",
            "overlays": [],
        }]
    scenes = []
    text_segments = []
    video_segments = []
    for i, s in enumerate(scenes_raw):
        scene_id = str(s.get("id", f"scene_{i}"))
        start_sec = float(s.get("start_sec", 0))
        end_sec = float(s.get("end_sec", duration))
        overlays = [
            Overlay(
                text=o.get("text", ""),
                position=o.get("position", "center"),
                start_sec=float(o.get("start_sec", 0)),
                end_sec=float(o.get("end_sec", 0)),
            )
            for o in s.get("overlays", [])
        ]
        for o in overlays:
            text_segments.append(
                Segment(
                    id=f"seg_text_{len(text_segments)}",
                    start_sec=o.start_sec,
                    end_sec=o.end_sec,
                    asset_id=None,
                    asset_source="generated",
                    asset_status="ready",
                    scene_id=scene_id,
                    params={
                        "text": o.text,
                        "position": o.position,
                        "font_size": 10,
                        "font_color": "#FFFFFF",
                    },
                )
            )
        scenes.append(
            Scene(
                id=scene_id,
                start_sec=start_sec,
                end_sec=end_sec,
                visual_description=s.get("visual_description"),
                voiceover_text=s.get("voiceover_text"),
                overlays=overlays,
                effects=s.get("effects", []),
                transition=s.get("transition", "cut"),
                asset_refs=[AssetRef(asset_id=main_asset_id, usage="main")],
                generation_tasks=[],
            )
        )
        asset_source = s.get("asset_source", "uploaded")
        video_segments.append(
            Segment(
                id=f"seg_video_{i}",
                start_sec=start_sec,
                end_sec=end_sec,
                asset_id=main_asset_id if asset_source == "uploaded" else None,
                asset_source=asset_source,
                asset_status="ready" if asset_source == "uploaded" else "pending",
                scene_id=scene_id,
                params={"query": s.get("stock_query", "")} if asset_source != "uploaded" else {},
            )
        )
    layers = [
        Layer(
            id="layer_video_1",
            type="video",
            order=0,
            segments=video_segments,
        ),
        Layer(id="layer_text_1", type="text", order=1, segments=text_segments),
        Layer(
            id="layer_audio_1",
            type="audio",
            order=2,
            segments=[
                Segment(
                    id="seg_audio_main",
                    start_sec=0,
                    end_sec=duration,
                    asset_id=main_asset_id,
                    asset_source="uploaded",
                    asset_status="ready",
                    scene_id=None,
                    params={},
                )
            ],
        ),
    ]
    return Scenario(version=1, metadata=meta, scenes=scenes, layers=layers)


def tasks_to_scenario(
    plan: PlanResponse,
    assets: list[Any],
    total_duration: float,
) -> Scenario:
    """
    Convert PlanResponse (tasks) to Scenario (scenes + layers).
    Handles add_text_overlay and overlay_video (B-roll) tasks.
    """
    asset_ids = [_asset_id(a) for a in assets if _asset_id(a)]
    main_asset_id = asset_ids[0] if asset_ids else "source"

    overlay_tasks = [t for t in plan.tasks if t.type == "add_text_overlay"]
    broll_tasks = [t for t in plan.tasks if t.type == "overlay_video"]

    # Fetch stock tasks for B-roll (to get query for generation_task_id)
    fetch_stock = {t.output_id: t for t in plan.tasks if t.type == "fetch_stock_video" and t.output_id}

    text_segments = []
    for i, t in enumerate(overlay_tasks):
        p = t.params
        start = p.get("start_time") or p.get("start") or 0
        end = p.get("end_time") or p.get("end") or start + 2
        text_segments.append(
            Segment(
                id=f"seg_text_{i}",
                start_sec=float(start),
                end_sec=float(end),
                asset_id=None,
                asset_source="generated",
                asset_status="ready",
                scene_id="scene_0",
                params={
                    "text": p.get("text", ""),
                    "position": p.get("position", "center"),
                    "font_size": p.get("font_size", 48),
                    "font_color": p.get("font_color", "white"),
                },
            )
        )

    overlays = [
        Overlay(
            text=t.params.get("text", ""),
            position=t.params.get("position", "center"),
            start_sec=float(t.params.get("start_time", 0) or 0),
            end_sec=float(t.params.get("end_time", 2) or 2),
        )
        for t in overlay_tasks
    ]

    # Build video layer: main segments + B-roll segments
    video_segments: list[Segment] = []
    broll_ranges = [
        (float(t.params.get("start_time", 0) or 0), float(t.params.get("end_time", 0) or 0), t)
        for t in sorted(broll_tasks, key=lambda x: float(x.params.get("start_time", 0) or 0))
    ]
    broll_ranges = [(s, e, t) for s, e, t in broll_ranges if e > s]

    pos = 0.0
    seg_idx = 0
    for bstart, bend, btask in broll_ranges:
        if bstart > pos:
            video_segments.append(
                Segment(
                    id=f"seg_main_{seg_idx}",
                    start_sec=pos,
                    end_sec=bstart,
                    asset_id=main_asset_id,
                    asset_source="uploaded",
                    asset_status="ready",
                    scene_id=f"scene_{len(video_segments)}",
                    params={},
                )
            )
            seg_idx += 1
        stock_id = btask.params.get("stock_id") or btask.params.get("output_id")
        gen_task = fetch_stock.get(stock_id) if stock_id else None
        video_segments.append(
            Segment(
                id=f"seg_broll_{len(video_segments)}",
                start_sec=bstart,
                end_sec=bend,
                asset_id=None,
                asset_source="generated",
                asset_status="pending",
                scene_id=f"scene_{len(video_segments)}",
                params={"query": gen_task.params.get("query", "") if gen_task else ""},
                generation_task_id=stock_id,
            )
        )
        pos = bend
    if pos < total_duration:
        video_segments.append(
            Segment(
                id=f"seg_main_{seg_idx}",
                start_sec=pos,
                end_sec=total_duration,
                asset_id=main_asset_id,
                asset_source="uploaded",
                asset_status="ready",
                scene_id=f"scene_{len(video_segments)}",
                params={},
            )
        )

    if not video_segments:
        video_segments = [
            Segment(
                id="seg_main",
                start_sec=0,
                end_sec=total_duration,
                asset_id=main_asset_id,
                asset_source="uploaded",
                asset_status="ready",
                scene_id="scene_0",
                params={},
            )
        ]

    # One scene per video segment — timeline shows separate parts
    scenes = []
    for i, seg in enumerate(video_segments):
        scene_id = f"scene_{i}"
        scenes.append(
            Scene(
                id=scene_id,
                start_sec=seg.start_sec,
                end_sec=seg.end_sec,
                visual_description=plan.scenario_description or "" if i == 0 else "",
                voiceover_text=None,
                overlays=[o for o in overlays if seg.start_sec <= o.start_sec < seg.end_sec],
                effects=[],
                transition="cut",
                asset_refs=[AssetRef(asset_id=seg.asset_id or main_asset_id, usage="main")],
                generation_tasks=[],
            )
        )

    layers = [
        Layer(id="layer_video_1", type="video", order=0, segments=video_segments),
        Layer(id="layer_text_1", type="text", order=1, segments=text_segments),
        Layer(
            id="layer_audio_1",
            type="audio",
            order=2,
            segments=[
                Segment(
                    id="seg_audio_main",
                    start_sec=0,
                    end_sec=total_duration,
                    asset_id=main_asset_id,
                    asset_source="uploaded",
                    asset_status="ready",
                    scene_id=None,
                    params={},
                )
            ],
        ),
    ]

    return Scenario(
        version=1,
        metadata=ScenarioMetadata(
            name=plan.scenario_name,
            description=plan.scenario_description,
            total_duration_sec=total_duration,
            aspect_ratio=plan.metadata.get("aspect_ratio", "9:16"),
        ),
        scenes=scenes,
        layers=layers,
    )
