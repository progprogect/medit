"""Extract metadata from media files (video, image)."""

import subprocess
from pathlib import Path
from typing import Optional

from PIL import Image


def get_video_metadata(path: Path) -> dict:
    """
    Get video metadata using ffprobe.
    Returns: duration_sec, width, height, codec.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"duration_sec": None, "width": None, "height": None}

    import json

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"duration_sec": None, "width": None, "height": None}

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    width = height = duration_sec = None
    if streams:
        width = streams[0].get("width")
        height = streams[0].get("height")
    if fmt and "duration" in fmt:
        try:
            duration_sec = float(fmt["duration"])
        except (TypeError, ValueError):
            pass

    return {
        "duration_sec": duration_sec,
        "width": width,
        "height": height,
    }


def get_image_metadata(path: Path) -> dict:
    """Get image metadata using Pillow."""
    try:
        with Image.open(path) as img:
            w, h = img.size
            return {"width": w, "height": h, "duration_sec": None}
    except Exception:
        return {"width": None, "height": None, "duration_sec": None}


def get_media_metadata(path: Path) -> dict:
    """Get metadata for video or image."""
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".mov", ".avi", ".webm"):
        return get_video_metadata(path)
    if suffix in (".jpg", ".jpeg", ".png", ".webp"):
        return get_image_metadata(path)
    return {"duration_sec": None, "width": None, "height": None}
