"""Audio transcription using faster-whisper."""

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _extract_audio(video_path: Path, output_path: Path) -> Path:
    """Extract audio from video using FFmpeg (-vn)."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def _format_srt(segments: list[dict]) -> str:
    """Format segments as SRT."""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = seg["start"]
        end = seg["end"]
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start_srt = _sec_to_srt(start)
        end_srt = _sec_to_srt(end)
        lines.append(f"{i}\n{start_srt} --> {end_srt}\n{text}\n")
    return "\n".join(lines)


def _sec_to_srt(sec: float) -> str:
    """Convert seconds to SRT timestamp HH:MM:SS,mmm."""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ensure_wav_16k(audio_path: Path, output_path: Path) -> Path:
    """Convert audio to WAV 16kHz mono for Whisper. Handles webm, wav, mp3, etc."""
    cmd = [
        "ffmpeg", "-y", "-i", str(audio_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def transcribe_audio(audio_path: Path, model_size: str = "base") -> str:
    """
    Transcribe audio from file (webm, wav, mp3, etc.). Returns plain text.
    Used for voice prompt input.
    """
    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = Path(tmpdir) / "audio.wav"
        _ensure_wav_16k(audio_path, wav_path)
        logger.info("Transcriber: голосовое аудио, Whisper (model=%s)...", model_size)
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments_raw, _ = model.transcribe(str(wav_path))
        plain_parts = [s.text or "" for s in segments_raw]
        plain_text = " ".join(plain_parts).strip()
    logger.info("Transcriber: голосовой ввод готов")
    return plain_text


def transcribe(video_path: Path, model_size: str = "base") -> tuple[str, list[dict], str]:
    """
    Transcribe audio from video. Returns (plain_text, segments, srt_content).
    segments: [{"start": float, "end": float, "text": str}, ...]
    """
    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / "audio.wav"
        _extract_audio(video_path, audio_path)
        logger.info("Transcriber: аудио извлечено, запуск Whisper (model=%s)...", model_size)
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments_raw, _ = model.transcribe(str(audio_path))
        segments = []
        plain_parts = []
        for s in segments_raw:
            text = s.text or ""
            seg = {"start": s.start, "end": s.end, "text": text}
            segments.append(seg)
            plain_parts.append(text)
        plain_text = " ".join(plain_parts).strip()
        srt_content = _format_srt(segments)
    logger.info("Transcriber: готово, сегментов: %d", len(segments))
    return plain_text, segments, srt_content
