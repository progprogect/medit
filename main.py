"""AI Video Editing - FastAPI application."""

import asyncio
import logging
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.routes import router as api_router
from services.gemini import analyze_and_generate_plan, analyze_and_generate_tasks, scan_broll_suggestions
from services.executor import run_tasks
from services.storage import get_storage

logger = logging.getLogger(__name__)
app = FastAPI(title="AI Video Editing")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Avoid 404 when browser requests favicon.ico."""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Return 500 with actual error message for debugging."""
    if isinstance(exc, HTTPException):
        raise exc
    logger.exception("Unhandled exception: %s", exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc) or "Internal Server Error"},
    )


app.include_router(api_router, prefix="/api")

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


class ProcessRequest(BaseModel):
    video_key: str
    prompt: str


class ExecuteRequest(BaseModel):
    video_key: str
    tasks: list[dict]


class ProcessResponse(BaseModel):
    output_key: str
    download_url: str


class AnalyzeResponse(BaseModel):
    scenario_name: str
    scenario_description: str
    metadata: dict
    tasks: list[dict]


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith((".mp4", ".mov", ".avi", ".webm")):
        raise HTTPException(400, "Only video files (mp4, mov, avi, webm) are allowed")
    storage = get_storage()
    video_key = storage.save_upload(None, file.file, file.filename or "video.mp4")
    return {"video_key": video_key}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: ProcessRequest):
    """Generate editing plan (scenario + tasks) for user review."""
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt is required")
    storage = get_storage()
    try:
        input_path = storage.get_upload_path(req.video_key)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    def _analyze():
        return analyze_and_generate_plan(input_path, req.prompt)

    plan = await asyncio.to_thread(_analyze)
    return AnalyzeResponse(**plan)


@app.post("/execute", response_model=ProcessResponse)
async def execute(req: ExecuteRequest):
    """Execute tasks on video (after user review/edit)."""
    storage = get_storage()
    try:
        input_path = storage.get_upload_path(req.video_key)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    def _execute():
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            temp_path = Path(tf.name)
        try:
            run_tasks(input_path, req.tasks, temp_path)
            return storage.save_output(None, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    try:
        output_key = await asyncio.to_thread(_execute)
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    download_url = storage.get_download_url(output_key, is_output=True)
    return ProcessResponse(output_key=output_key, download_url=download_url)


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt is required")
    storage = get_storage()
    try:
        input_path = storage.get_upload_path(req.video_key)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    # Запускаем в thread pool — Gemini и FFmpeg блокируют event loop
    def _process():
        tasks = analyze_and_generate_tasks(input_path, req.prompt)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            temp_path = Path(tf.name)
        try:
            run_tasks(input_path, tasks, temp_path)
            return storage.save_output(None, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    output_key = await asyncio.to_thread(_process)

    download_url = storage.get_download_url(output_key, is_output=True)
    return ProcessResponse(output_key=output_key, download_url=download_url)


class BrollScanRequest(BaseModel):
    video_key: str
    max_inserts: int = 3


class BrollSlot(BaseModel):
    start: float
    end: float
    duration: float
    context_text: str
    query: str
    alternative_queries: list[str] = []
    enabled: bool = True
    mode: str = "stock"  # "stock" or "ai"


class BrollApplyRequest(BaseModel):
    video_key: str
    slots: list[BrollSlot]


@app.post("/broll-scan")
async def broll_scan(req: BrollScanRequest):
    """Scan video and return B-roll insertion suggestions."""
    storage = get_storage()
    try:
        input_path = storage.get_upload_path(req.video_key)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    max_inserts = max(1, min(req.max_inserts, 6))

    def _scan():
        return scan_broll_suggestions(input_path, max_inserts=max_inserts)

    suggestions = await asyncio.to_thread(_scan)
    return {"suggestions": suggestions}


@app.post("/broll-apply", response_model=ProcessResponse)
async def broll_apply(req: BrollApplyRequest):
    """Download stock clips and overlay them on the video."""
    storage = get_storage()
    try:
        input_path = storage.get_upload_path(req.video_key)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    enabled_slots = [s for s in req.slots if s.enabled]
    if not enabled_slots:
        raise HTTPException(400, "No enabled slots to apply")

    def _apply():
        import subprocess as _sp
        from services.stock import fetch_stock_media
        from services.video_gen import generate_video_clip
        from services.executor import run_tasks

        # Get source video dimensions for quality matching
        r = _sp.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of",
             "default=noprint_wrappers=1:nokey=1", str(input_path)],
            capture_output=True, text=True,
        )
        lines = r.stdout.strip().splitlines()
        src_w = int(lines[0]) if len(lines) >= 1 else 1080
        src_h = int(lines[1]) if len(lines) >= 2 else 1920
        # Pexels max_width: match source if portrait, otherwise 1920
        max_width = min(src_w, src_h) if src_h > src_w else src_w  # shorter side for portrait
        max_width = min(max_width, 1920)
        orientation = "portrait" if src_h > src_w else "landscape"

        pre_registry: dict[str, Path] = {}
        overlay_tasks: list[dict] = []
        dest_dir = storage.output_dir

        for i, slot in enumerate(enabled_slots):
            oid = f"broll_{i + 1}"
            media_path: Path | None = None

            if slot.mode == "ai":
                # Build a cinematic Veo prompt from the query
                veo_prompt = (
                    f"Cinematic close-up footage: {slot.query}. "
                    f"Professional video quality, smooth camera movement, no text or watermarks."
                )
                veo_path = dest_dir / f"ai_video_{oid}_{int(time.time())}.mp4"
                media_path = generate_video_clip(
                    prompt=veo_prompt,
                    dest_path=veo_path,
                    duration_seconds=max(5, int(slot.duration) + 1),
                    aspect_ratio="9:16" if src_h > src_w else "16:9",
                )
                if media_path is None:
                    # Fallback to stock if AI generation fails
                    logger.warning("Veo не удался для '%s', пробуем сток", slot.query)
                    media_path = fetch_stock_media(
                        query=slot.query, media_type="video", dest_dir=dest_dir,
                        duration_max=max(10, int(slot.duration) + 5),
                        orientation=orientation, alternatives=slot.alternative_queries,
                        max_width=max_width,
                    )
            else:
                media_path = fetch_stock_media(
                    query=slot.query, media_type="video", dest_dir=dest_dir,
                    duration_max=max(10, int(slot.duration) + 5),
                    orientation=orientation, alternatives=slot.alternative_queries,
                    max_width=max_width,
                )

            if media_path is None:
                logger.warning("Не удалось получить клип для слота %d, пропуск", i + 1)
                continue

            pre_registry[oid] = media_path
            overlay_tasks.append({
                "type": "overlay_video",
                "params": {
                    "start_time": slot.start,
                    "end_time": slot.end,
                    "stock_id": oid,
                },
            })

        if not overlay_tasks:
            raise RuntimeError("No clips could be found for any enabled slot")

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            temp_path = Path(tf.name)
        try:
            run_tasks(input_path, overlay_tasks, temp_path, initial_registry=pre_registry)
            return storage.save_output(None, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    try:
        output_key = await asyncio.to_thread(_apply)
    except RuntimeError as e:
        raise HTTPException(422, str(e))

    download_url = storage.get_download_url(output_key, is_output=True)
    return ProcessResponse(output_key=output_key, download_url=download_url)


@app.get("/capabilities")
async def capabilities():
    """Return available features (e.g. whether AI video gen is available)."""
    from services.video_gen import is_veo_available
    return {"ai_video_generation": is_veo_available()}


@app.get("/files/{prefix}/{key:path}")
async def serve_file(prefix: str, key: str):
    if prefix not in ("uploads", "outputs"):
        raise HTTPException(400, "Invalid prefix")
    storage = get_storage()
    if not hasattr(storage, "get_file_path"):
        raise HTTPException(500, "Storage does not support file serving")
    try:
        path = storage.get_file_path(prefix, key)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    suffix = path.suffix.lower()
    media_type = (
        "image/jpeg" if suffix in (".jpg", ".jpeg") else
        "image/png" if suffix == ".png" else
        "image/webp" if suffix == ".webp" else
        "video/mp4"
    )
    return FileResponse(path, media_type=media_type)
