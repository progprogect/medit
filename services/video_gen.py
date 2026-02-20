"""AI video generation via Google Veo 2 API."""

import logging
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

VEO_MODEL = "veo-2.0-generate-001"


def generate_video_clip(
    prompt: str,
    dest_path: Path,
    duration_seconds: int = 5,
    aspect_ratio: str = "9:16",
    negative_prompt: str = "text, watermark, logo, subtitles",
) -> Path | None:
    """Generate a short video clip using Google Veo 2.

    Returns Path to the generated video, or None on failure.
    """
    try:
        from google import genai
        from google.genai import types
        from config import get_gemini_api_key

        api_key = get_gemini_api_key()
        client = genai.Client(api_key=api_key)

        logger.info("VideoGen: запускаем Veo для '%s' (%ds, %s)...", prompt[:60], duration_seconds, aspect_ratio)
        t0 = time.time()

        operation = client.models.generate_videos(
            model=VEO_MODEL,
            prompt=prompt,
            config=types.GenerateVideosConfig(
                duration_seconds=min(max(duration_seconds, 5), 8),
                aspect_ratio=aspect_ratio,
                number_of_videos=1,
                negative_prompt=negative_prompt,
                generate_audio=False,
            ),
        )

        # Poll until complete (Veo is async)
        for attempt in range(60):
            time.sleep(5)
            operation = client.operations.get(operation)
            if operation.done:
                break
            if attempt % 6 == 0:
                logger.info("VideoGen: ожидание Veo... %ds", attempt * 5)

        if not operation.done:
            logger.warning("VideoGen: Veo не завершился за 5 минут")
            return None

        response = operation.result
        if not response or not response.generated_videos:
            logger.warning("VideoGen: Veo не вернул видео")
            return None

        video = response.generated_videos[0]
        video_bytes = client.files.download(file=video.video)

        dest_path.write_bytes(video_bytes)
        logger.info("VideoGen: Veo сгенерировал видео за %.0f сек → %s", time.time() - t0, dest_path)
        return dest_path

    except Exception as e:
        logger.error("VideoGen: ошибка Veo: %s", e)
        return None


def is_veo_available() -> bool:
    """Check if Veo API is accessible with current API key."""
    try:
        from google import genai
        from config import get_gemini_api_key
        client = genai.Client(api_key=get_gemini_api_key())
        # Just check the SDK has the method
        return hasattr(client.models, "generate_videos")
    except Exception:
        return False
