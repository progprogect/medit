"""Task Executor: runs FFmpeg to apply editing tasks."""

import logging
import platform
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Default font for drawtext - macOS vs Linux
def _get_default_font() -> str:
    if platform.system() == "Darwin":
        return "/System/Library/Fonts/Helvetica.ttc"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _position_to_drawtext(position: str) -> str:
    """Convert position name to FFmpeg drawtext x,y expression."""
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
    """Escape special chars for drawtext filter."""
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _format_srt(segments: list[dict]) -> str:
    """Format segments as SRT."""
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
    return base.parent / f"{prefix}_{hash(str(time.time())) % 100000}{suffix}"


def run_tasks(input_path: Path, tasks: list[dict], output_path: Path) -> Path:
    """
    Execute list of tasks on video. Returns path to output file.
    """
    if not tasks:
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    current_path = input_path
    font = _get_default_font()
    temp_paths: list[Path] = []

    def _cleanup_and_advance(next_path: Path):
        nonlocal current_path
        if current_path != input_path and current_path in temp_paths:
            current_path.unlink(missing_ok=True)
        temp_paths.append(next_path)
        current_path = next_path

    for i, task in enumerate(tasks):
        task_type = task.get("type")
        params = task.get("params", {})
        t0 = time.time()
        logger.info("Executor: задача %d/%d %s...", i + 1, len(tasks), task_type)

        if task_type == "add_text_overlay":
            text = _escape_drawtext(params.get("text", ""))
            position = params.get("position", "top_center")
            font_size = params.get("font_size", 48)
            font_color = params.get("font_color", "white")
            start_time = params.get("start_time")
            end_time = params.get("end_time")

            pos_expr = _position_to_drawtext(position)
            drawtext = f"drawtext=fontfile='{font}':text='{text}':fontsize={font_size}:fontcolor={font_color}:{pos_expr}"
            if start_time is not None:
                drawtext += f":enable='between(t,{start_time},{end_time or 99999})'"

            next_path = _temp_path(output_path, f"step_{task_type}", ".mp4")
            cmd = [
                "ffmpeg", "-y", "-i", str(current_path),
                "-vf", drawtext,
                "-c:a", "copy",
                str(next_path)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: add_text_overlay за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "trim":
            start = params.get("start", 0)
            end = params.get("end")
            next_path = _temp_path(output_path, "step_trim", ".mp4")
            cmd = ["ffmpeg", "-y", "-i", str(current_path), "-ss", str(start)]
            if end is not None:
                cmd.extend(["-to", str(end)])
            cmd.extend(["-c", "copy", str(next_path)])
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: trim за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "resize":
            width = params.get("width", 1280)
            height = params.get("height")
            scale = f"scale={width}:-1" if height is None else f"scale={width}:{height}"
            next_path = _temp_path(output_path, "step_resize", ".mp4")
            cmd = [
                "ffmpeg", "-y", "-i", str(current_path),
                "-vf", scale,
                "-c:a", "copy",
                str(next_path)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: resize за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "change_speed":
            factor = params.get("factor", 1.0)
            next_path = _temp_path(output_path, "step_speed", ".mp4")
            pts = 1.0 / factor
            cmd = [
                "ffmpeg", "-y", "-i", str(current_path),
                "-filter:v", f"setpts={pts}*PTS",
                "-filter:a", f"atempo={min(factor, 2.0)}",
                str(next_path)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: change_speed за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "add_subtitles":
            segments = params.get("segments", [])
            if not segments:
                logger.warning("Executor: add_subtitles без segments, пропуск")
                continue
            srt_content = _format_srt(segments)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False, encoding="utf-8") as f:
                f.write(srt_content)
                srt_path = Path(f.name)
            try:
                next_path = _temp_path(output_path, "step_subtitles", ".mp4")
                srt_str = str(srt_path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
                subtitles_filter = f"subtitles='{srt_str}'"
                cmd = [
                    "ffmpeg", "-y", "-i", str(current_path),
                    "-vf", subtitles_filter,
                    "-c:a", "copy",
                    str(next_path)
                ]
                subprocess.run(cmd, check=True, capture_output=True)
                logger.info("Executor: add_subtitles за %.1f сек", time.time() - t0)
                _cleanup_and_advance(next_path)
            finally:
                srt_path.unlink(missing_ok=True)

        elif task_type == "add_image_overlay":
            image_path = params.get("image_path")
            if not image_path or not Path(image_path).exists():
                logger.warning("Executor: add_image_overlay — image_path не найден, пропуск")
                continue
            position = params.get("position", "bottom_right")
            start_time = params.get("start_time")
            end_time = params.get("end_time")
            opacity = params.get("opacity", 1.0)
            pos_map = {
                "top_left": "10:10", "top_center": "(main_w-overlay_w)/2:10",
                "top_right": "main_w-overlay_w-10:10",
                "bottom_left": "10:main_h-overlay_h-10",
                "bottom_center": "(main_w-overlay_w)/2:main_h-overlay_h-10",
                "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
                "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
            }
            overlay_pos = pos_map.get(position, pos_map["bottom_right"])
            enable_expr = f":enable='between(t,{start_time},{end_time or 99999})'" if start_time is not None else ""
            overlay_filter = f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[img];[0:v][img]overlay={overlay_pos}{enable_expr}[v]"
            next_path = _temp_path(output_path, "step_overlay", ".mp4")
            cmd = [
                "ffmpeg", "-y", "-i", str(current_path), "-i", str(image_path),
                "-filter_complex", overlay_filter, "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
                str(next_path)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: add_image_overlay за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "auto_frame_face":
            target_ratio = params.get("target_ratio", "9:16")
            w_ratio, h_ratio = map(int, target_ratio.split(":"))
            next_path = _temp_path(output_path, "step_face", ".mp4")
            crop_filter = f"crop=ih*{w_ratio}/{h_ratio}:ih:(iw-ih*{w_ratio}/{h_ratio})/2:0"
            cmd = [
                "ffmpeg", "-y", "-i", str(current_path),
                "-vf", crop_filter,
                "-c:a", "copy",
                str(next_path)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: auto_frame_face (center crop) за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "color_correction":
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
            eq_filter = "eq=" + ":".join(eq_parts) if eq_parts else "copy"
            next_path = _temp_path(output_path, "step_color", ".mp4")
            if eq_parts:
                cmd = [
                    "ffmpeg", "-y", "-i", str(current_path),
                    "-vf", eq_filter,
                    "-c:a", "copy",
                    str(next_path)
                ]
            else:
                import shutil
                shutil.copy2(current_path, next_path)
            if eq_parts:
                subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: color_correction за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        elif task_type == "concat":
            clip_paths = params.get("clip_paths", [])
            if not clip_paths:
                logger.warning("Executor: concat без clip_paths, пропуск")
                continue
            valid_paths = [Path(p) for p in clip_paths if Path(p).exists()]
            if not valid_paths:
                logger.warning("Executor: concat — ни один clip_path не найден, пропуск")
                continue
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                for path in valid_paths:
                    f.write(f"file '{path.absolute()}'\n")
                concat_list = Path(f.name)
            try:
                next_path = _temp_path(output_path, "step_concat", ".mp4")
                cmd = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    str(next_path)
                ]
                subprocess.run(cmd, check=True, capture_output=True)
                logger.info("Executor: concat за %.1f сек", time.time() - t0)
                _cleanup_and_advance(next_path)
            finally:
                concat_list.unlink(missing_ok=True)

        elif task_type == "zoompan":
            zoom = params.get("zoom", 1.2)
            duration = params.get("duration", 2.0)
            next_path = _temp_path(output_path, "step_zoompan", ".mp4")
            zoompan_filter = f"zoompan=z='min(zoom+0.0015,{zoom})':d={int(duration * 25)}:s=1280x720"
            cmd = [
                "ffmpeg", "-y", "-i", str(current_path),
                "-vf", zoompan_filter,
                "-c:a", "copy",
                str(next_path)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Executor: zoompan за %.1f сек", time.time() - t0)
            _cleanup_and_advance(next_path)

        else:
            logger.warning("Executor: неизвестный тип задачи %s, пропуск", task_type)

    if current_path != output_path:
        import shutil
        shutil.copy2(current_path, output_path)
        if current_path in temp_paths:
            current_path.unlink(missing_ok=True)
    return output_path
