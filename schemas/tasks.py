"""Pydantic models for task JSON from Gemini."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AddTextOverlayParams(BaseModel):
    """Params for add_text_overlay task."""

    text: str
    position: str = "top_center"
    font_size: int = 48
    font_color: str = "white"
    start_time: float | None = None
    end_time: float | None = None


class TrimParams(BaseModel):
    """Params for trim task."""

    start: float
    end: float


class ResizeParams(BaseModel):
    """Params for resize task."""

    width: int
    height: int | None = None


class ChangeSpeedParams(BaseModel):
    """Params for change_speed task."""

    factor: float


TaskType = Literal[
    "add_text_overlay",
    "trim",
    "resize",
    "change_speed",
    "add_subtitles",
    "add_image_overlay",
    "auto_frame_face",
    "color_correction",
    "concat",
    "zoompan",
]


class Task(BaseModel):
    """Single task from Gemini output."""

    type: TaskType
    params: dict[str, Any]


class PlanResponse(BaseModel):
    """Full plan from Gemini: scenario + metadata + tasks."""

    scenario_name: str
    scenario_description: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    tasks: list[Task] = Field(default_factory=list)


class TasksResponse(BaseModel):
    """Legacy: list of tasks only (for backward compatibility)."""

    tasks: list[Task] = Field(default_factory=list)
