"""Task Executor: runs FFmpeg to apply editing tasks.

Supports a graph-based task model:
  - Each task may declare `output_id` to name its output for later reference.
  - Each task may declare `inputs` (list of output_id strings) to use named
    results of previous tasks as input instead of the default linear chain.
  - Without output_id / inputs the behaviour is identical to the old linear chain.
"""

import logging
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_default_font() -> str:
    if platform.system() == "Darwin":
        return "/System/Library/Fonts/Helvetica.ttc"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _position_to_drawtext(position: str) -> str:
    positions = {
        "top_center": "x=(w-text_w)/2:y=20",
        "bottom_center": "x=(w-text_w)/2:y=h-th-20",
        "top_left": "x=20:y=20",
        "top_right": "x=w-text_w-20:y=20",
        "bottom_left": "x=20:y=h-th-20",
        "bottom_right": "x=w-text_w-20:y=h-th-20",
        "center": "x=(w-text_w)/2:y=(h-text_h)/2",
    }
    return positions.get(position, positions["top_center"])


def _escape_drawtext(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _format_srt(segments: list[dict]) -> str:
    def _sec_to_srt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int((sec % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        start = seg.get("start", 0)
        end = seg.get("end", start + 1)
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"{i}\n{_sec_to_srt(start)} --> {_sec_to_srt(end)}\n{text}\n")
    return "\n".join(lines)


def _temp_path(base: Path, prefix: str, suffix: str) -> Path:
    return base.parent / f"{prefix}_{abs(hash(str(time.time()))) % 100000}{suffix}"


def run_tasks(input_path: Path, tasks: list[dict], output_path: Path) -> Path:
    """Execute list of tasks on video using a graph-based registry.

    Each task dict may contain:
      type, params         — as before
      output_id (optional) — name to store this task's output in registry
      inputs (optional)    — list of output_id names to use as inputs;
                             first element is used as current_path,
                             remaining are available for multi-input tasks (concat)
    """
    if not tasks:
        shutil.copy2(input_path, output_path)
        return output_path

    font = _get_default_font()
    temp_paths: list[Path] = []
    # Maps output_id -> Path
    registry: dict[str, Path] = {}
    # Default linear chain pointer
    current_path: Path = input_path

    def _register(task: dict, result_path: Path) -> None:
        """Store result in registry and advance current_path."""
        nonlocal current_path
        output_id = task.get("output_id")
        if output_id:
            registry[output_id] = result_path
        current_path = result_path

    def _resolve_input(task: dict) -> Path:
        """Resolve primary input for a task."""
        inputs = task.get("inputs")
        if inputs:
            first = inputs[0]
            if first not in registry:
                raise ValueError(f"Task {task.get('type')}: input '{first}' not found in registry. "
                                 f"Available: {list(registry.keys())}")
            return registry[first]
        return current_path

    def _resolve_inputs_list(task: dict) -> list[Path]:
        """Resolve all inputs (for concat)."""
        inputs = task.get("inputs")
        if inputs:
            paths = []
            for name in inputs:
                if name not in registry:
                    raise ValueError(f"concat: input '{name}' not found in registry.")
                paths.append(registry[name])
            return paths
        return [current_path]

    for i, task in enumerate(tasks):
        task_type = task.get("type")
        params = task.get("params", {})
        t0 = time.time()
        logger.info("Executor: задача %d/%d %s (output_id=%s, inputs=%s)...",
                    i + 1, len(tasks), task_type, task.get("output_id"), task.get("inputs"))

        if task_type == "add_text_overlay":
            src = _resolve_input(task)
            text = _escape_drawtext(params.get("text", ""))
            position = params.get("position", "top_center")
            font_size = params.get("font_size", 48)
            font_color = params.get("font_color", "white")
            start_time = params.get("start_time")
            end_time = params.get("end_time")

            pos_expr = _position_to_drawtext(position)
            drawtext = (
                f"drawtext=fontfile='{font}':text='{text}':"
                f"fontsize={font_size}:fontcolor={font_color}:{pos_expr}"
            )
            if start_time is not None:
                drawtext += f":enable='between(t,{start_time},{end_time or 99999})'"

            out = _temp_path(output_path, "step_text", ".mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-vf", drawtext, "-c:a", "copy", str(out)],
                check=True, capture_output=True,
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: add_text_overlay за %.1f сек", time.time() - t0)

        elif task_type == "trim":
            src = _resolve_input(task)
            start = params.get("start", 0)
            end = params.get("end")
            out = _temp_path(output_path, "step_trim", ".mp4")
            cmd = ["ffmpeg", "-y", "-i", str(src), "-ss", str(start)]
            if end is not None:
                cmd.extend(["-to", str(end)])
            cmd.extend(["-c", "copy", str(out)])
            subprocess.run(cmd, check=True, capture_output=True)
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: trim за %.1f сек", time.time() - t0)

        elif task_type == "resize":
            src = _resolve_input(task)
            width = params.get("width", 1280)
            height = params.get("height")
            scale = f"scale={width}:-1" if height is None else f"scale={width}:{height}"
            out = _temp_path(output_path, "step_resize", ".mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-vf", scale, "-c:a", "copy", str(out)],
                check=True, capture_output=True,
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: resize за %.1f сек", time.time() - t0)

        elif task_type == "change_speed":
            src = _resolve_input(task)
            factor = params.get("factor", 1.0)
            pts = 1.0 / factor
            out = _temp_path(output_path, "step_speed", ".mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-filter:v", f"setpts={pts}*PTS",
                 "-filter:a", f"atempo={min(factor, 2.0)}",
                 str(out)],
                check=True, capture_output=True,
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: change_speed за %.1f сек", time.time() - t0)

        elif task_type == "add_subtitles":
            src = _resolve_input(task)
            segments = params.get("segments", [])
            if not segments:
                logger.warning("Executor: add_subtitles без segments, пропуск")
                continue
            srt_content = _format_srt(segments)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".srt", delete=False, encoding="utf-8"
            ) as f:
                f.write(srt_content)
                srt_path = Path(f.name)
            try:
                out = _temp_path(output_path, "step_subtitles", ".mp4")
                srt_str = str(srt_path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(src),
                     "-vf", f"subtitles='{srt_str}'",
                     "-c:a", "copy", str(out)],
                    check=True, capture_output=True,
                )
                temp_paths.append(out)
                _register(task, out)
                logger.info("Executor: add_subtitles за %.1f сек", time.time() - t0)
            finally:
                srt_path.unlink(missing_ok=True)

        elif task_type == "add_image_overlay":
            src = _resolve_input(task)
            image_path = params.get("image_path")
            if not image_path or not Path(image_path).exists():
                logger.warning("Executor: add_image_overlay — image_path не найден, пропуск")
                continue
            position = params.get("position", "bottom_right")
            start_time = params.get("start_time")
            end_time = params.get("end_time")
            opacity = params.get("opacity", 1.0)
            pos_map = {
                "top_left": "10:10",
                "top_center": "(main_w-overlay_w)/2:10",
                "top_right": "main_w-overlay_w-10:10",
                "bottom_left": "10:main_h-overlay_h-10",
                "bottom_center": "(main_w-overlay_w)/2:main_h-overlay_h-10",
                "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
                "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
            }
            overlay_pos = pos_map.get(position, pos_map["bottom_right"])
            enable_expr = (
                f":enable='between(t,{start_time},{end_time or 99999})'"
                if start_time is not None else ""
            )
            overlay_filter = (
                f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[img];"
                f"[0:v][img]overlay={overlay_pos}{enable_expr}[v]"
            )
            out = _temp_path(output_path, "step_imgoverlay", ".mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-i", str(image_path),
                 "-filter_complex", overlay_filter,
                 "-map", "[v]", "-map", "0:a?", "-c:a", "copy", str(out)],
                check=True, capture_output=True,
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: add_image_overlay за %.1f сек", time.time() - t0)

        elif task_type == "auto_frame_face":
            src = _resolve_input(task)
            target_ratio = params.get("target_ratio", "9:16")
            w_ratio, h_ratio = map(int, target_ratio.split(":"))
            out = _temp_path(output_path, "step_face", ".mp4")
            crop_filter = f"crop=ih*{w_ratio}/{h_ratio}:ih:(iw-ih*{w_ratio}/{h_ratio})/2:0"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-vf", crop_filter, "-c:a", "copy", str(out)],
                check=True, capture_output=True,
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: auto_frame_face (center crop) за %.1f сек", time.time() - t0)

        elif task_type == "color_correction":
            src = _resolve_input(task)
            brightness = params.get("brightness", 0)
            contrast = params.get("contrast", 0)
            saturation = params.get("saturation", 0)
            eq_parts = []
            if brightness != 0:
                eq_parts.append(f"brightness={brightness}")
            if contrast != 0:
                eq_parts.append(f"contrast={contrast}")
            if saturation != 0:
                eq_parts.append(f"saturation={1 + saturation}")
            out = _temp_path(output_path, "step_color", ".mp4")
            if eq_parts:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(src),
                     "-vf", "eq=" + ":".join(eq_parts),
                     "-c:a", "copy", str(out)],
                    check=True, capture_output=True,
                )
            else:
                shutil.copy2(src, out)
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: color_correction за %.1f сек", time.time() - t0)

        elif task_type == "concat":
            # Prefer inputs from registry; fall back to legacy clip_paths param
            if task.get("inputs"):
                clip_paths_resolved = _resolve_inputs_list(task)
            else:
                raw_paths = params.get("clip_paths", [])
                clip_paths_resolved = [Path(p) for p in raw_paths if Path(p).exists()]

            if not clip_paths_resolved:
                logger.warning("Executor: concat — нет clip_paths/inputs, пропуск")
                continue

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                for p in clip_paths_resolved:
                    f.write(f"file '{p.absolute()}'\n")
                concat_list = Path(f.name)
            try:
                out = _temp_path(output_path, "step_concat", ".mp4")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", str(concat_list), "-c", "copy", str(out)],
                    check=True, capture_output=True,
                )
                temp_paths.append(out)
                _register(task, out)
                logger.info("Executor: concat за %.1f сек", time.time() - t0)
            finally:
                concat_list.unlink(missing_ok=True)

        elif task_type == "zoompan":
            src = _resolve_input(task)
            zoom = params.get("zoom", 1.2)
            duration = params.get("duration", 2.0)
            out = _temp_path(output_path, "step_zoompan", ".mp4")
            zoompan_filter = (
                f"zoompan=z='min(zoom+0.0015,{zoom})':d={int(duration * 25)}:s=1280x720"
            )
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-vf", zoompan_filter, "-c:a", "copy", str(out)],
                check=True, capture_output=True,
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: zoompan за %.1f сек", time.time() - t0)

        elif task_type in ("fetch_stock_video", "fetch_stock_image"):
            from services.stock import fetch_stock_media
            query = params.get("query", "")
            if not query:
                logger.warning("Executor: %s без query, пропуск", task_type)
                continue
            media_type = "video" if task_type == "fetch_stock_video" else "image"
            duration_max = params.get("duration_max", 30)
            orientation = params.get("orientation", "landscape")
            dest_dir = output_path.parent

            media_path = fetch_stock_media(
                query=query,
                media_type=media_type,
                duration_max=duration_max,
                orientation=orientation,
                dest_dir=dest_dir,
            )
            if media_path is None:
                logger.warning("Executor: %s — ничего не найдено по запросу '%s', пропуск",
                               task_type, query)
                continue
            temp_paths.append(media_path)
            _register(task, media_path)
            logger.info("Executor: %s скачано за %.1f сек: %s",
                        task_type, time.time() - t0, media_path)

        else:
            logger.warning("Executor: неизвестный тип задачи %s, пропуск", task_type)

    # Copy final current_path to output_path, then clean up all temp files
    if current_path != output_path:
        shutil.copy2(current_path, output_path)
    for p in temp_paths:
        if p != output_path:
            p.unlink(missing_ok=True)
    return output_path
