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


@router.get("/projects/{project_id}/assets", response_model=dict)
async def list_assets(project_id: str):
    """List all assets in project (for preview URLs)."""
    with get_db() as db:
        assets = (
            db.query(Asset)
            .filter(Asset.project_id == project_id)
            .order_by(Asset.order_index)
            .all()
        )
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
            for a in assets
        ]
    return {"assets": result}


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

    from services.scenario_service import ensure_audio_layer

    main_id = asset_dicts[0]["id"] if asset_dicts else None
    scenario = ensure_audio_layer(scenario, main_id)
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

    from services.scenario_service import ensure_audio_layer

    main_id = asset_dicts[0]["id"] if asset_dicts else None
    updated = ensure_audio_layer(updated, main_id)
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
    from services.scenario_service import ensure_audio_layer, ensure_video_layer_matches_scenes

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
        if not asset_ids:
            assets = db.query(Asset).filter(Asset.project_id == project_id).order_by(Asset.order_index).all()
            asset_ids = [a.id for a in assets]
        main_id = asset_ids[0] if asset_ids else None
        scenario = ensure_video_layer_matches_scenes(scenario, main_id)
        return ensure_audio_layer(scenario, main_id)


@router.get("/projects/{project_id}/overlay-styles")
async def get_overlay_styles_endpoint(project_id: str):
    """Get available overlay style presets for render."""
    return {"styles": get_overlay_styles()}


class FetchStockRequest(BaseModel):
    query: Optional[str] = None


@router.post("/projects/{project_id}/scenario/segments/{segment_id}/fetch-stock", response_model=Scenario)
async def fetch_stock_for_segment(
    project_id: str, segment_id: str, body: FetchStockRequest
):
    """Fetch stock video for a B-roll segment. User can find via Pexels or later add AI generation."""
    from services.scenario_service import ensure_audio_layer
    from services.stock import fetch_stock_media

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
        main_asset = next((a for a in assets if a.type == "video"), assets[0] if assets else None)
        if not main_asset:
            raise HTTPException(400, "No video asset in project")
        main_asset_id = main_asset.id
        main_asset_file_key = main_asset.file_key

    video_layer = next((l for l in scenario.layers if l.type == "video"), None)
    if not video_layer:
        raise HTTPException(400, "No video layer")

    segment = next((s for s in video_layer.segments if s.id == segment_id), None)
    if not segment:
        raise HTTPException(404, f"Segment {segment_id} not found")
    if (segment.asset_source or "uploaded") == "uploaded" and (segment.asset_status or "ready") == "ready":
        raise HTTPException(400, "Segment already has video")

    query = body.query
    if not query:
        if (segment.params or {}).get("query"):
            query = segment.params["query"]
        else:
            segment_scene = next((s for s in scenario.scenes if s.id == segment.scene_id), None)
            if segment_scene:
                for gt in getattr(segment_scene, "generation_tasks", []) or []:
                    g = gt if hasattr(gt, "params") else (gt or {})
                    sid = getattr(gt, "segment_id", None) or (g.get("segment_id") if isinstance(g, dict) else None)
                    if sid == segment_id:
                        params = getattr(g, "params", None) or (g.get("params") if isinstance(g, dict) else {}) or {}
                        query = params.get("query", "")
                        break
    if not query:
        raise HTTPException(400, "No search query. Provide query in request body or in segment/scene.")

    storage = get_storage()
    source_path = storage.get_asset_path(main_asset_file_key)
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
        capture_output=True, text=True,
    )
    w, h = 1080, 1920
    if r.returncode == 0:
        lines = r.stdout.strip().splitlines()
        try:
            w, h = int(lines[0]), int(lines[1])
        except (ValueError, IndexError):
            pass
    orientation = "portrait" if h > w else "landscape"
    max_width = min(w, h, 1920) if h > w else min(w, 1920)
    duration = max(5, int(segment.end_sec - segment.start_sec) + 2)

    media_path = fetch_stock_media(
        query=query,
        media_type="video",
        dest_dir=storage.output_dir,
        duration_max=duration,
        orientation=orientation,
        max_width=max_width,
    )
    if not media_path:
        raise HTTPException(502, "Stock search failed. Check PEXELS_API_KEY and try another query.")

    with get_db() as db:
        asset_id = str(uuid.uuid4())
        with open(media_path, "rb") as f:
            file_key = storage.save_asset(
                project_id, asset_id, f, media_path.name
            )
        meta = get_media_metadata(media_path)
        asset = Asset(
            id=asset_id,
            project_id=project_id,
            file_key=file_key,
            filename=media_path.name,
            type="video",
            duration_sec=meta.get("duration_sec"),
            width=meta.get("width"),
            height=meta.get("height"),
            order_index=db.query(Asset).filter(Asset.project_id == project_id).count(),
        )
        db.add(asset)
        db.commit()

        sc = db.query(ScenarioModel).filter(ScenarioModel.project_id == project_id).first()
        scenario = Scenario.model_validate(sc.data)
        video_layer = next((l for l in scenario.layers if l.type == "video"), None)
        seg = next((s for s in video_layer.segments if s.id == segment_id), None)
        if seg:
            seg.asset_id = asset_id
            seg.asset_source = "uploaded"
            seg.asset_status = "ready"
        sc.data = scenario.model_dump()
        sc.version += 1
        db.commit()
        scenario = Scenario.model_validate(sc.data)

    return ensure_audio_layer(scenario, main_asset_id)


class GenerateVeoRequest(BaseModel):
    prompt: Optional[str] = None


@router.post("/projects/{project_id}/scenario/segments/{segment_id}/generate-veo", response_model=Scenario)
async def generate_veo_for_segment(
    project_id: str, segment_id: str, body: GenerateVeoRequest
):
    """Generate video for a B-roll segment via VEO3. Primary option; stock search is alternative."""
    from services.scenario_service import ensure_audio_layer
    from services.video_gen import generate_video_clip, is_veo_available

    try:
        return await _do_generate_veo(project_id, segment_id, body)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("generate-veo failed for %s/%s", project_id, segment_id)
        raise HTTPException(500, str(e) or "Ошибка генерации VEO")


async def _do_generate_veo(project_id: str, segment_id: str, body: GenerateVeoRequest) -> Scenario:
    from services.scenario_service import ensure_audio_layer
    from services.video_gen import generate_video_clip, is_veo_available

    if not is_veo_available():
        raise HTTPException(503, "VEO3 недоступен. Используйте «Найти в стоке» как альтернативу.")

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
        main_asset = next((a for a in assets if a.type == "video"), assets[0] if assets else None)
        if not main_asset:
            raise HTTPException(400, "No video asset in project")
        main_asset_file_key = main_asset.file_key
        main_asset_id = main_asset.id

    video_layer = next((l for l in scenario.layers if l.type == "video"), None)
    if not video_layer:
        raise HTTPException(400, "No video layer")

    segment = next((s for s in video_layer.segments if s.id == segment_id), None)
    if not segment:
        raise HTTPException(404, f"Segment {segment_id} not found")
    if (segment.asset_source or "uploaded") == "uploaded" and (segment.asset_status or "ready") == "ready":
        raise HTTPException(400, "Segment already has video")

    prompt = body.prompt
    if not prompt:
        prompt = (segment.params or {}).get("query", "")
    if not prompt:
        scene = next((s for s in scenario.scenes if s.id == segment.scene_id), None)
        if scene:
            prompt = (scene.visual_description or "").replace("STOCK CLIP:", "").replace("STOCK:", "").strip()[:200]
    if not prompt:
        raise HTTPException(400, "Provide prompt in request body or ensure segment/scene has query or visual_description.")

    storage = get_storage()
    source_path = storage.get_asset_path(main_asset_file_key)
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
        capture_output=True, text=True,
    )
    h, w = 1920, 1080
    if r.returncode == 0:
        lines = r.stdout.strip().splitlines()
        try:
            w, h = int(lines[0]), int(lines[1])
        except (ValueError, IndexError):
            pass
    aspect_ratio = "9:16" if h > w else "16:9"
    duration = max(5, min(8, int(segment.end_sec - segment.start_sec) + 2))

    veo_prompt = (
        f"Cinematic close-up footage: {prompt}. "
        "Professional video quality, smooth camera movement, no text or watermarks."
    )
    media_path = storage.output_dir / f"ai_video_{segment_id}_{uuid.uuid4().hex[:8]}.mp4"
    result_path = await asyncio.to_thread(
        generate_video_clip,
        veo_prompt,
        media_path,
        duration,
        aspect_ratio,
    )
    if not result_path or not result_path.exists():
        raise HTTPException(502, "VEO3 generation failed. Try «Найти в стоке» as alternative.")

    with get_db() as db:
        asset_id = str(uuid.uuid4())
        with open(result_path, "rb") as f:
            file_key = storage.save_asset(
                project_id, asset_id, f, result_path.name
            )
        meta = get_media_metadata(result_path)
        asset = Asset(
            id=asset_id,
            project_id=project_id,
            file_key=file_key,
            filename=result_path.name,
            type="video",
            duration_sec=meta.get("duration_sec"),
            width=meta.get("width"),
            height=meta.get("height"),
            order_index=db.query(Asset).filter(Asset.project_id == project_id).count(),
        )
        db.add(asset)
        db.commit()

        sc = db.query(ScenarioModel).filter(ScenarioModel.project_id == project_id).first()
        scenario = Scenario.model_validate(sc.data)
        video_layer = next((l for l in scenario.layers if l.type == "video"), None)
        seg = next((s for s in video_layer.segments if s.id == segment_id), None)
        if seg:
            seg.asset_id = asset_id
            seg.asset_source = "uploaded"
            seg.asset_status = "ready"
        sc.data = scenario.model_dump()
        sc.version += 1
        db.commit()
        db.refresh(sc)
        scenario = Scenario.model_validate(sc.data)

    return ensure_audio_layer(scenario, main_asset_id)


class RenderScenarioRequest(BaseModel):
    overlay_style: str = "minimal"
    render_mode: str = "C"  # A: LLM→tasks, B: LLM+валидация, C: Python+проверка


class RenderScenarioResponse(BaseModel):
    output_key: str
    download_url: str


@router.post("/projects/{project_id}/scenario/render", response_model=RenderScenarioResponse)
async def scenario_render(project_id: str, body: RenderScenarioRequest):
    """Render scenario to video: full main video + B-roll overlays + text overlays."""
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
            render_mode=body.render_mode or "C",
            storage=storage,
        )

    try:
        output_key = await asyncio.to_thread(_render)
    except RenderBlocked as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("Render failed")
        raise HTTPException(500, str(e) or "Ошибка рендеринга")

    download_url = storage.get_download_url(output_key, is_output=True)
    return RenderScenarioResponse(output_key=output_key, download_url=download_url)


@router.put("/projects/{project_id}/scenario", response_model=Scenario)
async def save_scenario(project_id: str, body: Scenario):
    """Save scenario (full replace)."""
    from services.scenario_service import ensure_audio_layer

    with get_db() as db:
        assets = db.query(Asset).filter(Asset.project_id == project_id).all()
        asset_ids = [a.id for a in assets]
        main_id = asset_ids[0] if asset_ids else None
        scenario = ensure_audio_layer(body, main_id)
        sc = (
            db.query(ScenarioModel)
            .filter(ScenarioModel.project_id == project_id)
            .first()
        )
        if not sc:
            raise HTTPException(404, "Scenario not found")
        sc.data = scenario.model_dump()
        sc.version += 1
        sc.status = "saved"
        db.commit()
        db.refresh(sc)
    return Scenario.model_validate(sc.data)
