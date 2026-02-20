"""AI Video Editing - FastAPI application."""

import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.gemini import analyze_and_generate_plan, analyze_and_generate_tasks, scan_broll_suggestions
from services.executor import run_tasks
from services.storage import get_storage

app = FastAPI(title="AI Video Editing")

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
        from services.stock import fetch_stock_media
        from services.executor import run_tasks

        pre_registry: dict[str, Path] = {}
        overlay_tasks: list[dict] = []
        dest_dir = storage.output_dir

        for i, slot in enumerate(enabled_slots):
            oid = f"broll_{i + 1}"
            media_path = fetch_stock_media(
                query=slot.query,
                media_type="video",
                dest_dir=dest_dir,
                duration_max=max(10, int(slot.duration) + 5),
                orientation="landscape",
                alternatives=slot.alternative_queries,
            )
            if media_path is None:
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
            raise RuntimeError("No stock clips could be found for any slot")

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
    return FileResponse(path, media_type="video/mp4")
