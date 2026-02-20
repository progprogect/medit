"""Stock media search and download via Pexels API."""

import logging
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PEXELS_VIDEO_SEARCH = "https://api.pexels.com/videos/search"
PEXELS_IMAGE_SEARCH = "https://api.pexels.com/v1/search"


def _get_api_key() -> str | None:
    from config import get_pexels_api_key
    try:
        return get_pexels_api_key()
    except ValueError:
        return None


def _pexels_request(url: str, params: dict, api_key: str) -> dict:
    import json
    import urllib.parse
    full_url = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(full_url, headers={
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (compatible; AI-VideoEditor/1.0)",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _download_file(url: str, dest: Path) -> Path:
    logger.info("Stock: скачиваем %s -> %s", url, dest)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; AI-VideoEditor/1.0)",
    })
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        while chunk := resp.read(65536):
            f.write(chunk)
    return dest


def fetch_stock_media(
    query: str,
    media_type: str,
    dest_dir: Path,
    duration_max: int = 30,
    orientation: str = "landscape",
    alternatives: list[str] | None = None,
) -> Path | None:
    """Search Pexels and download first suitable result.

    media_type: 'video' or 'image'
    Returns Path to downloaded file, or None if not found / API key missing.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("Stock: PEXELS_API_KEY не задан, пропуск поиска")
        return None

    try:
        if media_type == "video":
            return _fetch_video(query, duration_max, orientation, dest_dir, api_key, alternatives or [])
        else:
            return _fetch_image(query, orientation, dest_dir, api_key, alternatives or [])
    except Exception as e:
        logger.error("Stock: ошибка при поиске '%s': %s", query, e)
        return None


def _fallback_queries(query: str, alternatives: list[str] | None = None) -> list[str]:
    """Build a list of queries from specific to general.
    Prefers Gemini-provided alternatives over auto-generated ones."""
    candidates: list[str] = [query]

    # Use Gemini-provided alternatives first (they're semantically meaningful)
    if alternatives:
        for alt in alternatives:
            if alt and alt.strip() and alt != query:
                candidates.append(alt.strip())

    # Auto-generate fallbacks only if needed
    words = [w for w in query.split() if len(w) > 3]  # skip short/stop words
    if len(words) > 2:
        candidates.append(" ".join(words[:2]))
    if len(words) > 1:
        candidates.append(words[0])

    # Deduplicate while preserving order
    seen: set[str] = set()
    return [q for q in candidates if not (q in seen or seen.add(q))]  # type: ignore[func-returns-value]


def _fetch_video(
    query: str, duration_max: int, orientation: str, dest_dir: Path, api_key: str,
    alternatives: list[str] | None = None,
) -> Path | None:
    pexels_orientation = _map_orientation(orientation)
    videos: list = []
    tried: list[str] = []

    for attempt_query in _fallback_queries(query, alternatives):
        tried.append(attempt_query)
        data = _pexels_request(
            PEXELS_VIDEO_SEARCH,
            {
                "query": attempt_query,
                "per_page": 5,
                "orientation": pexels_orientation,
                "max_duration": duration_max,
            },
            api_key,
        )
        videos = data.get("videos", [])
        if videos:
            if attempt_query != query:
                logger.info("Stock: видео не найдено по '%s', использую упрощённый запрос '%s'",
                            query, attempt_query)
            break

    if not videos:
        logger.warning("Stock: видео не найдено ни по одному из запросов: %s", tried)
        return None

    video = videos[0]
    files = sorted(video.get("video_files", []), key=lambda f: f.get("width", 0))
    if not files:
        logger.warning("Stock: видео '%s' не имеет video_files", query)
        return None
    # Prefer SD/HD file to keep size manageable
    video_url = files[-1]["link"]
    for f in files:
        if f.get("width", 0) <= 1920:
            video_url = f["link"]
            break

    slug = query.replace(" ", "_")[:30]
    dest = dest_dir / f"stock_video_{slug}_{int(time.time())}.mp4"
    return _download_file(video_url, dest)


def _fetch_image(
    query: str, orientation: str, dest_dir: Path, api_key: str,
    alternatives: list[str] | None = None,
) -> Path | None:
    pexels_orientation = _map_orientation(orientation)
    photos: list = []
    tried: list[str] = []

    for attempt_query in _fallback_queries(query, alternatives):
        tried.append(attempt_query)
        data = _pexels_request(
            PEXELS_IMAGE_SEARCH,
            {
                "query": attempt_query,
                "per_page": 5,
                "orientation": pexels_orientation,
            },
            api_key,
        )
        photos = data.get("photos", [])
        if photos:
            if attempt_query != query:
                logger.info("Stock: изображение не найдено по '%s', использую '%s'",
                            query, attempt_query)
            break

    if not photos:
        logger.warning("Stock: изображение не найдено ни по одному из запросов: %s", tried)
        return None

    photo = photos[0]
    image_url = photo["src"].get("large", photo["src"]["original"])
    slug = query.replace(" ", "_")[:30]
    dest = dest_dir / f"stock_image_{slug}_{int(time.time())}.jpg"
    return _download_file(image_url, dest)


def _map_orientation(orientation: str) -> str:
    mapping = {"portrait": "portrait", "square": "square"}
    return mapping.get(orientation, "landscape")
