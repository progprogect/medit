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
        # Arial supports Cyrillic; Helvetica.ttc does not
        arial = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
        if arial.exists():
            return str(arial)
        return "/System/Library/Fonts/Helvetica.ttc"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _position_to_drawtext(position: str, margin: int = 50) -> str:
    """Convert named position to FFmpeg drawtext x/y with a sensible margin."""
    positions = {
        "top_center":    f"x=(w-text_w)/2:y={margin}",
        "bottom_center": f"x=(w-text_w)/2:y=h-th-{margin}",
        "top_left":      f"x={margin}:y={margin}",
        "top_right":     f"x=w-text_w-{margin}:y={margin}",
        "bottom_left":   f"x={margin}:y=h-th-{margin}",
        "bottom_right":  f"x=w-text_w-{margin}:y=h-th-{margin}",
        "center":        "x=(w-text_w)/2:y=(h-text_h)/2",
    }
    return positions.get(position, positions["bottom_center"])


def _escape_drawtext(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _ffmpeg_run(cmd: list[str], task_name: str) -> None:
    """Run FFmpeg command. On failure, log stderr and re-raise with a clear message."""
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.error("Executor: FFmpeg ошибка в '%s' (exit %d):\n%s",
                     task_name, e.returncode, stderr[-3000:])
        raise RuntimeError(
            f"FFmpeg failed in '{task_name}' (exit {e.returncode}). "
            f"See logs for details."
        ) from e


def _build_drawtext(
    font: str,
    text: str,
    font_size: int,
    font_color: str,
    pos_expr: str,
    start_time: float | None,
    end_time: float | None,
    background: str | None,
    shadow: bool,
    border_color: str | None,
    border_width: int,
) -> str:
    """Assemble FFmpeg drawtext filter string with optional styling."""
    escaped = _escape_drawtext(text)
    parts = [
        f"drawtext=fontfile='{font}'",
        f"text='{escaped}'",
        f"fontsize={font_size}",
        f"fontcolor={font_color}",
        pos_expr,
    ]

    if shadow:
        parts += ["shadowx=2", "shadowy=2", "shadowcolor=black@0.8"]

    if border_color and border_width:
        parts += [f"borderw={border_width}", f"bordercolor={border_color}"]

    if background and background != "none":
        if background == "dark":
            box_color = "black@0.55"
        elif background == "light":
            box_color = "white@0.55"
        else:
            box_color = background  # e.g. "black@0.7" or "#000000@0.5"
        parts += ["box=1", f"boxcolor={box_color}", "boxborderw=12"]

    if start_time is not None:
        parts.append(f"enable='between(t,{start_time},{end_time or 99999})'")

    return ":".join(parts)


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
    # Maps output_id -> Path. "source" always points to the original input.
    registry: dict[str, Path] = {"source": input_path}
    # Default linear chain pointer
    current_path: Path = input_path

    def _register(task: dict, result_path: Path, advance_chain: bool = True) -> None:
        """Store result in registry and optionally advance the linear current_path."""
        nonlocal current_path
        output_id = task.get("output_id")
        if output_id:
            registry[output_id] = result_path
        if advance_chain:
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
            text = params.get("text", "")
            font_size = params.get("font_size", 48)
            font_color = params.get("font_color", "white")
            start_time = params.get("start_time")
            end_time = params.get("end_time")
            shadow = bool(params.get("shadow", False))
            background = params.get("background")  # "dark", "light", "none", or "color@opacity"
            border_color = params.get("border_color")
            border_width = int(params.get("border_width", 0))
            margin = int(params.get("margin", 50))

            # Custom x/y take precedence over named position
            if "x" in params and "y" in params:
                x_val = params["x"]
                y_val = params["y"]
                # Support percentage strings like "50%", plain int pixels, or FFmpeg expressions
                def _resolve_coord(v, dim_expr: str) -> str:
                    if isinstance(v, str) and v.endswith("%"):
                        pct = float(v[:-1]) / 100.0
                        return f"({dim_expr}*{pct:.4f})"
                    return str(v)
                pos_expr = f"x={_resolve_coord(x_val, 'w')}:y={_resolve_coord(y_val, 'h')}"
            else:
                position = params.get("position", "bottom_center")
                pos_expr = _position_to_drawtext(position, margin)

            drawtext = _build_drawtext(
                font=font,
                text=text,
                font_size=font_size,
                font_color=font_color,
                pos_expr=pos_expr,
                start_time=start_time,
                end_time=end_time,
                background=background,
                shadow=shadow,
                border_color=border_color,
                border_width=border_width,
            )

            out = _temp_path(output_path, "step_text", ".mp4")
            _ffmpeg_run(
                ["ffmpeg", "-y", "-i", str(src), "-vf", drawtext, "-c:a", "copy", str(out)],
                "add_text_overlay",
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: add_text_overlay за %.1f сек", time.time() - t0)

        elif task_type == "trim":
            src = _resolve_input(task)
            start = params.get("start", 0)
            end = params.get("end")
            out = _temp_path(output_path, "step_trim", ".mp4")
            # -ss before -i = input seeking (fast, avoids exit 234 when start > file duration)
            cmd = ["ffmpeg", "-y", "-ss", str(start)]
            if end is not None:
                cmd.extend(["-to", str(end)])
            cmd.extend(["-i", str(src), "-c", "copy", str(out)])
            _ffmpeg_run(cmd, "trim")
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: trim за %.1f сек", time.time() - t0)

        elif task_type == "resize":
            src = _resolve_input(task)
            width = params.get("width", 1280)
            height = params.get("height")
            scale = f"scale={width}:-1" if height is None else f"scale={width}:{height}"
            out = _temp_path(output_path, "step_resize", ".mp4")
            _ffmpeg_run(
                ["ffmpeg", "-y", "-i", str(src), "-vf", scale, "-c:a", "copy", str(out)],
                "resize",
            )
            temp_paths.append(out)
            _register(task, out)
            logger.info("Executor: resize за %.1f сек", time.time() - t0)

        elif task_type == "change_speed":
            src = _resolve_input(task)
            factor = params.get("factor", 1.0)
            pts = 1.0 / factor
            out = _temp_path(output_path, "step_speed", ".mp4")
            _ffmpeg_run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-filter:v", f"setpts={pts}*PTS",
                 "-filter:a", f"atempo={min(factor, 2.0)}",
                 str(out)],
                "change_speed",
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
                _ffmpeg_run(
                    ["ffmpeg", "-y", "-i", str(src),
                     "-vf", f"subtitles='{srt_str}'",
                     "-c:a", "copy", str(out)],
                    "add_subtitles",
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
            _ffmpeg_run(
                ["ffmpeg", "-y", "-i", str(src), "-i", str(image_path),
                 "-filter_complex", overlay_filter,
                 "-map", "[v]", "-map", "0:a?", "-c:a", "copy", str(out)],
                "add_image_overlay",
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
            _ffmpeg_run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-vf", crop_filter, "-c:a", "copy", str(out)],
                "auto_frame_face",
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
                _ffmpeg_run(
                    ["ffmpeg", "-y", "-i", str(src),
                     "-vf", "eq=" + ":".join(eq_parts),
                     "-c:a", "copy", str(out)],
                    "color_correction",
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
                _ffmpeg_run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", str(concat_list), "-c", "copy", str(out)],
                    "concat",
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
            _ffmpeg_run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-vf", zoompan_filter, "-c:a", "copy", str(out)],
                "zoompan",
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
            # fetch_stock tasks only put media into the registry for later use;
            # they must NOT override current_path so linear chain stays on the main video
            _register(task, media_path, advance_chain=False)
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
