"""Storage abstraction: LocalStorage for local dev, S3Storage for AWS."""

import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol

from config import get_output_dir, get_upload_dir


class Storage(Protocol):
    """Storage interface."""

    def save_upload(self, key: str | None, file: BinaryIO, filename: str) -> str:
        """Save uploaded file, return storage key."""
        ...

    def get_upload_path(self, key: str) -> Path:
        """Get local path to uploaded file."""
        ...

    def save_output(self, key: str | None, source_path: Path) -> str:
        """Save processed file, return storage key."""
        ...

    def get_download_url(self, key: str, is_output: bool = False) -> str:
        """Get URL for downloading file (path for local, presigned for S3)."""
        ...


class LocalStorage:
    """File system storage for local development."""

    def __init__(self) -> None:
        self.upload_dir = get_upload_dir()
        self.output_dir = get_output_dir()

    def save_upload(self, key: str | None, file: BinaryIO, filename: str = "") -> str:
        key = key or str(uuid.uuid4())
        dest_dir = self.upload_dir / key
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(filename).suffix if filename else ".mp4"
        dest_path = dest_dir / f"video{ext}"
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file, f)
        return key

    def _resolve_safe(self, base: Path, key: str) -> Path:
        """Resolve path and ensure it stays inside base (prevents path traversal)."""
        if ".." in key or "/" in key or "\\" in key:
            raise FileNotFoundError("Invalid path")
        path = (base / key).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            raise FileNotFoundError("Invalid path")
        return path

    def get_upload_path(self, key: str) -> Path:
        dir_path = self._resolve_safe(self.upload_dir, key)
        if not dir_path.exists():
            raise FileNotFoundError(f"Upload not found: {key}")
        if dir_path.is_file():
            return dir_path
        for f in dir_path.iterdir():
            if f.suffix in (".mp4", ".mov", ".avi", ".webm"):
                return f
        raise FileNotFoundError(f"No video file in {key}")

    def save_output(self, key: str | None, source_path: Path) -> str:
        key = key or str(uuid.uuid4())
        dest_dir = self.output_dir / key
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / "result.mp4"
        shutil.copy2(source_path, dest_path)
        return key

    def get_download_url(self, key: str, is_output: bool = False) -> str:
        prefix = "outputs" if is_output else "uploads"
        return f"/files/{prefix}/{key}"

    def get_file_path(self, prefix: str, key: str) -> Path:
        """Get path for serving file (GET /files/{prefix}/{key})."""
        if prefix == "uploads":
            dir_path = self._resolve_safe(self.upload_dir, key)
        elif prefix == "outputs":
            dir_path = self._resolve_safe(self.output_dir, key)
        else:
            raise ValueError(f"Invalid prefix: {prefix}")
        if not dir_path.exists():
            raise FileNotFoundError(f"File not found: {prefix}/{key}")
        if dir_path.is_file():
            return dir_path
        for f in dir_path.iterdir():
            if f.suffix in (".mp4", ".mov", ".avi", ".webm"):
                return f
        raise FileNotFoundError(f"No video in {prefix}/{key}")


def get_storage() -> Storage:
    """Get storage instance based on config."""
    from config import get_storage_mode

    if get_storage_mode() == "s3":
        # TODO: return S3Storage()
        raise NotImplementedError("S3 storage not yet implemented")
    return LocalStorage()
