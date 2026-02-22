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


def tasks_to_scenario(
    plan: PlanResponse,
    assets: list[Any],
    total_duration: float,
) -> Scenario:
    """
    Convert PlanResponse (tasks) to Scenario (scenes + layers).
    Fallback when LLM returns old format.
    """
    asset_ids = [_asset_id(a) for a in assets if _asset_id(a)]
    main_asset_id = asset_ids[0] if asset_ids else "source"

    overlay_tasks = [t for t in plan.tasks if t.type == "add_text_overlay"]

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

    scene = Scene(
        id="scene_0",
        start_sec=0,
        end_sec=total_duration,
        visual_description=plan.scenario_description or "",
        voiceover_text=None,
        overlays=overlays,
        effects=[],
        transition="cut",
        asset_refs=[AssetRef(asset_id=main_asset_id, usage="main")],
        generation_tasks=[],
    )

    layers = [
        Layer(
            id="layer_video_1",
            type="video",
            order=0,
            segments=[
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
            ],
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
        scenes=[scene],
        layers=layers,
    )
