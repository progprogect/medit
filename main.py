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

from services.gemini import analyze_and_generate_plan, analyze_and_generate_tasks
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
