"""Pydantic schemas for Scenario (scenes + layers)."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class AssetRef(BaseModel):
    """Reference to an asset in a scene."""

    asset_id: Optional[str] = None
    media_id: Optional[str] = None
    usage: Optional[str] = None
    generation_params: Optional[dict[str, Any]] = None


class GenerationTaskRef(BaseModel):
    """Reference to an AI generation task."""

    task_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    segment_id: Optional[str] = None


class Overlay(BaseModel):
    """Text overlay in a scene."""

    text: str = ""
    position: str = "center"
    start_sec: float = 0
    end_sec: float = 0


class Scene(BaseModel):
    """Scene in the scenario."""

    id: str
    start_sec: float
    end_sec: float
    visual_description: Optional[str] = None
    voiceover_text: Optional[str] = None
    overlays: list[Overlay] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    transition: Optional[str] = None
    asset_refs: list[AssetRef] = Field(default_factory=list)
    generation_tasks: list[GenerationTaskRef] = Field(default_factory=list)


class Segment(BaseModel):
    """Segment on a layer (timeline block)."""

    id: str
    start_sec: float
    end_sec: float
    asset_id: Optional[str] = None
    asset_source: Optional[str] = None  # uploaded | suggested | generated
    asset_status: Optional[str] = None  # ready | pending | generating | error
    scene_id: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    generation_task_id: Optional[str] = None


class Layer(BaseModel):
    """Layer on the timeline."""

    id: str
    type: str  # video | image | text | subtitle | audio | music | sfx | effects | overlays | generated
    order: int = 0
    segments: list[Segment] = Field(default_factory=list)


class ScenarioMetadata(BaseModel):
    """Scenario metadata."""

    name: str
    description: Optional[str] = None
    total_duration_sec: Optional[float] = None
    aspect_ratio: str = "9:16"
    asset_ids: Optional[list[str]] = None  # link to uploaded assets


class Scenario(BaseModel):
    """Full scenario: scenes + layers."""

    version: int = 1
    metadata: ScenarioMetadata
    scenes: list[Scene] = Field(default_factory=list)
    layers: list[Layer] = Field(default_factory=list)
