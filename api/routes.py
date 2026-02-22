"""API routes for projects, assets, scenario."""

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from db.models import Asset, Project, Scenario as ScenarioModel
from db.session import get_db
from schemas.scenario import Scenario
from services.gemini import generate_scenario, refine_scenario
from services.media_metadata import get_media_metadata
from services.render_service import RenderBlocked, get_overlay_styles, render_scenario
from services.storage import get_storage
from services.transcriber import transcribe_audio

logger = logging.getLogger(__name__)

router = APIRouter(tags=["api"])

# Allowed formats
VIDEO_EXT = (".mp4", ".mov", ".avi", ".webm")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
ALL_MEDIA_EXT = VIDEO_EXT + IMAGE_EXT
MAX_FILES = 10


class CreateProjectResponse(BaseModel):
    id: str
    name: str


class AssetResponse(BaseModel):
    id: str
    file_key: str
    filename: str
    type: str
    duration_sec: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    user_description: Optional[str] = None
    order_index: int


class ScenarioGenerateRequest(BaseModel):
    global_prompt: str
    asset_descriptions: Optional[dict[str, str]] = None
    reference_links: Optional[list[str]] = None


@router.post("/transcribe-audio")
async def transcribe_audio_endpoint(audio: UploadFile = File(...)):
    """Transcribe voice recording to text (Whisper). Accepts webm, wav, mp3."""
    if not audio.filename and not getattr(audio, "content_type", ""):
        raise HTTPException(400, "Audio file required")
    suffix = ".webm"
    if audio.filename and "." in audio.filename:
        suffix = "." + audio.filename.rsplit(".", 1)[-1].lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        content = await audio.read()
        if len(content) == 0:
            raise HTTPException(400, "Empty audio file")
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(400, "Audio too large (max 10 MB)")
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        text = await asyncio.to_thread(transcribe_audio, tmp_path)
        return {"text": text}
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/projects", response_model=CreateProjectResponse)
async def create_project():
    """Create a new project."""
    with get_db() as db:
        project = Project(name="New Project")
        db.add(project)
        db.commit()
        db.refresh(project)
        return CreateProjectResponse(id=project.id, name=project.name)


@router.post("/projects/{project_id}/assets", response_model=dict)
async def upload_assets(
    project_id: str,
    files: list[UploadFile] = File(...),
):
    """Upload multiple media files to project."""
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Max {MAX_FILES} files allowed")

    storage = get_storage()
    assets_created = []

    with get_db() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(404, "Project not found")

        max_order = (
            db.query(Asset).filter(Asset.project_id == project_id).count()
        )

        for i, file in enumerate(files):
            if not file.filename:
                continue
            ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in ALL_MEDIA_EXT:
                logger.warning("Skipping unsupported file: %s", file.filename)
                continue

            asset_id = str(uuid.uuid4())
            file_key = storage.save_asset(
                project_id, asset_id, file.file, file.filename
            )
            asset_path = storage.get_asset_path(file_key)

            media_type = "video" if ext in VIDEO_EXT else "image"
            meta = get_media_metadata(asset_path)

            asset = Asset(
                id=asset_id,
                project_id=project_id,
                file_key=file_key,
                filename=file.filename,
                type=media_type,
                duration_sec=meta.get("duration_sec"),
                width=meta.get("width"),
                height=meta.get("height"),
                order_index=max_order + i,
            )
            db.add(asset)
            assets_created.append(asset)

        db.commit()
        for a in assets_created:
            db.refresh(a)

        result = [
            AssetResponse(
                id=a.id,
                file_key=a.file_key,
                filename=a.filename,
                type=a.type,
                duration_sec=a.duration_sec,
                width=a.width,
                height=a.height,
                user_description=a.user_description,
                order_index=a.order_index,
            )
            for a in assets_created
        ]

    return {"assets": result}


@router.delete("/projects/{project_id}/assets/{asset_id}")
async def delete_asset(project_id: str, asset_id: str):
    """Delete asset from project."""
    with get_db() as db:
        asset = (
            db.query(Asset)
            .filter(Asset.project_id == project_id, Asset.id == asset_id)
            .first()
        )
        if not asset:
            raise HTTPException(404, "Asset not found")
        file_key = asset.file_key
        db.delete(asset)
        db.commit()

    storage = get_storage()
    try:
        storage.delete_asset(file_key)
    except FileNotFoundError:
        pass
    return {"ok": True}


@router.patch("/projects/{project_id}/assets/reorder")
async def reorder_assets(
    project_id: str,
    body: dict,
):
    """Reorder assets. Body: { asset_ids: string[] }"""
    asset_ids = body.get("asset_ids", [])
    if not asset_ids:
        raise HTTPException(400, "asset_ids required")

    with get_db() as db:
        assets = (
            db.query(Asset)
            .filter(Asset.project_id == project_id, Asset.id.in_(asset_ids))
            .all()
        )
        if len(assets) != len(asset_ids):
            raise HTTPException(404, "Some assets not found")
        for i, aid in enumerate(asset_ids):
            a = next(x for x in assets if x.id == aid)
            a.order_index = i
        db.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/scenario/generate", response_model=Scenario)
async def scenario_generate(project_id: str, body: ScenarioGenerateRequest):
    """Generate scenario from assets and prompt."""
    if not body.global_prompt.strip():
        raise HTTPException(400, "global_prompt is required")

    with get_db() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(404, "Project not found")
        assets = (
            db.query(Asset)
            .filter(Asset.project_id == project_id)
            .order_by(Asset.order_index)
            .all()
        )
        if not assets:
            raise HTTPException(400, "No assets in project")
        asset_dicts = [
            {
                "id": a.id,
                "file_key": a.file_key,
                "type": a.type,
                "duration_sec": a.duration_sec,
                "user_description": (body.asset_descriptions or {}).get(a.id) or a.user_description,
            }
            for a in assets
        ]

    def _generate():
        storage = get_storage()
        return generate_scenario(
            assets=asset_dicts,
            global_prompt=body.global_prompt,
            reference_links=body.reference_links,
            storage=storage,
        )

    try:
        scenario = await asyncio.to_thread(_generate)
    except ValueError as e:
        raise HTTPException(400, str(e))

    scenario_dict = scenario.model_dump()
    scenario_dict["metadata"] = scenario_dict.get("metadata", {})
    scenario_dict["metadata"]["asset_ids"] = [ad["id"] for ad in asset_dicts]

    with get_db() as db:
        existing = (
            db.query(ScenarioModel)
            .filter(ScenarioModel.project_id == project_id)
            .first()
        )
        if existing:
            existing.data = scenario_dict
            existing.version += 1
            existing.status = "draft"
            db.commit()
        else:
            sc = ScenarioModel(
                project_id=project_id,
                data=scenario_dict,
                version=1,
                status="draft",
            )
            db.add(sc)
            db.commit()

    return scenario


class RefineScenarioRequest(BaseModel):
    refinement_prompt: str


@router.post("/projects/{project_id}/scenario/refine", response_model=Scenario)
async def scenario_refine(project_id: str, body: RefineScenarioRequest):
    """Refine existing scenario with a new prompt."""
    if not body.refinement_prompt.strip():
        raise HTTPException(400, "refinement_prompt is required")

    with get_db() as db:
        sc = db.query(ScenarioModel).filter(ScenarioModel.project_id == project_id).first()
        if not sc:
            raise HTTPException(404, "Scenario not found")
        scenario = Scenario.model_validate(sc.data)

        assets = (
            db.query(Asset)
            .filter(Asset.project_id == project_id)
            .order_by(Asset.order_index)
            .all()
        )
        asset_dicts = [
            {"id": a.id, "file_key": a.file_key, "type": a.type, "duration_sec": a.duration_sec}
            for a in assets
        ]

    def _refine():
        return refine_scenario(scenario, body.refinement_prompt.strip(), asset_dicts)

    try:
        updated = await asyncio.to_thread(_refine)
    except ValueError as e:
        raise HTTPException(400, str(e))

    scenario_dict = updated.model_dump()
    scenario_dict["metadata"] = scenario_dict.get("metadata", {})
    scenario_dict["metadata"]["asset_ids"] = [a["id"] for a in asset_dicts]

    with get_db() as db:
        existing = db.query(ScenarioModel).filter(ScenarioModel.project_id == project_id).first()
        if existing:
            existing.data = scenario_dict
            existing.version += 1
            existing.status = "draft"
            db.commit()

    return updated


@router.get("/projects/{project_id}/scenario", response_model=Scenario)
async def get_scenario(project_id: str):
    """Get scenario for project."""
    from services.scenario_service import ensure_video_layer_matches_scenes

    with get_db() as db:
        sc = (
            db.query(ScenarioModel)
            .filter(ScenarioModel.project_id == project_id)
            .first()
        )
        if not sc:
            raise HTTPException(404, "Scenario not found")
        scenario = Scenario.model_validate(sc.data)
        asset_ids = (scenario.metadata.asset_ids or []) if scenario.metadata else []
        main_id = asset_ids[0] if asset_ids else None
        return ensure_video_layer_matches_scenes(scenario, main_id)


@router.get("/projects/{project_id}/overlay-styles")
async def get_overlay_styles_endpoint(project_id: str):
    """Get available overlay style presets for render."""
    return {"styles": get_overlay_styles()}


class RenderScenarioRequest(BaseModel):
    overlay_style: str = "minimal"


class RenderScenarioResponse(BaseModel):
    output_key: str
    download_url: str


@router.post("/projects/{project_id}/scenario/render", response_model=RenderScenarioResponse)
async def scenario_render(project_id: str, body: RenderScenarioRequest):
    """Render scenario to video. MVP: only uploaded segments."""
    from services.scenario_service import scenario_for_render

    with get_db() as db:
        sc = db.query(ScenarioModel).filter(ScenarioModel.project_id == project_id).first()
        if not sc:
            raise HTTPException(404, "Scenario not found")
        scenario = Scenario.model_validate(sc.data)
        assets = (
            db.query(Asset)
            .filter(Asset.project_id == project_id)
            .order_by(Asset.order_index)
            .all()
        )
        if not assets:
            raise HTTPException(400, "No assets in project")
        main_id = next((a.id for a in assets if a.type == "video"), assets[0].id)
        scenario = scenario_for_render(scenario, main_id)
        asset_dicts = [
            {"id": a.id, "file_key": a.file_key, "type": a.type, "duration_sec": a.duration_sec}
            for a in assets
        ]
    storage = get_storage()

    def _render():
        return render_scenario(
            project_id=project_id,
            scenario=scenario,
            assets=asset_dicts,
            overlay_style=body.overlay_style or "minimal",
            storage=storage,
        )

    try:
        output_key = await asyncio.to_thread(_render)
    except RenderBlocked as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    download_url = storage.get_download_url(output_key, is_output=True)
    return RenderScenarioResponse(output_key=output_key, download_url=download_url)


@router.put("/projects/{project_id}/scenario", response_model=Scenario)
async def save_scenario(project_id: str, body: Scenario):
    """Save scenario (full replace)."""
    with get_db() as db:
        sc = (
            db.query(ScenarioModel)
            .filter(ScenarioModel.project_id == project_id)
            .first()
        )
        if not sc:
            raise HTTPException(404, "Scenario not found")
        sc.data = body.model_dump()
        sc.version += 1
        sc.status = "saved"
        db.commit()
        db.refresh(sc)
    return Scenario.model_validate(sc.data)
