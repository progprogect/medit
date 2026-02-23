#!/usr/bin/env python3
"""E2E test: thesis overlays + subtitle layer → render. No B-roll, no LLM."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas.scenario import (
    Layer,
    Overlay,
    Scenario,
    ScenarioMetadata,
    Scene,
    Segment,
)
from services.render_service import render_scenario
from services.storage import get_storage


def main():
    storage = get_storage()
    uploads = storage.upload_dir
    file_key = None
    asset_id = None
    for proj_dir in uploads.iterdir():
        if not proj_dir.is_dir():
            continue
        for asset_dir in proj_dir.iterdir():
            if not asset_dir.is_dir():
                continue
            for f in asset_dir.glob("*.mp4"):
                file_key = f"{proj_dir.name}/{asset_dir.name}/{f.name}"
                asset_id = asset_dir.name
                break
            if file_key:
                break
        if file_key:
            break
    if not file_key:
        print("No video found in uploads")
        return 1

    video_path = storage.get_asset_path(file_key)
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    duration = float(r.stdout.strip()) if r.returncode == 0 else 30.0

    # Build scenario: main video only, thesis overlays, subtitle segments
    scenario = Scenario(
        version=1,
        metadata=ScenarioMetadata(
            name="Test thesis+subtitle",
            total_duration_sec=duration,
            aspect_ratio="9:16",
            asset_ids=[asset_id],
        ),
        scenes=[
            Scene(
                id="scene_0",
                start_sec=0,
                end_sec=duration,
                visual_description="Test",
                overlays=[
                    Overlay(text="Тезис 1", position="top_center", start_sec=1, end_sec=4, format="thesis"),
                    Overlay(text="Тезис 2", position="bottom_center", start_sec=duration/2, end_sec=duration/2+3, format="thesis"),
                ],
            )
        ],
        layers=[
            Layer(
                id="layer_video_1",
                type="video",
                order=0,
                segments=[
                    Segment(
                        id="seg_main",
                        start_sec=0,
                        end_sec=duration,
                        asset_id=asset_id,
                        asset_source="uploaded",
                        asset_status="ready",
                        scene_id="scene_0",
                        params={},
                    )
                ],
            ),
            Layer(
                id="layer_text_1",
                type="text",
                order=1,
                segments=[
                    Segment(id="t1", start_sec=1, end_sec=4, params={"text": "Тезис 1", "position": "top_center"}),
                    Segment(id="t2", start_sec=duration/2, end_sec=duration/2+3, params={"text": "Тезис 2", "position": "bottom_center"}),
                ],
            ),
            Layer(
                id="layer_subtitle_1",
                type="subtitle",
                order=2,
                segments=[
                    Segment(id="s1", start_sec=0.5, end_sec=2.5, params={"text": "Субтитр первый"}),
                    Segment(id="s2", start_sec=3, end_sec=5, params={"text": "Субтитр второй"}),
                ],
            ),
            Layer(
                id="layer_audio_1",
                type="audio",
                order=3,
                segments=[
                    Segment(id="a1", start_sec=0, end_sec=duration, asset_id=asset_id, asset_source="uploaded", params={}),
                ],
            ),
        ],
    )

    assets = [{"id": asset_id, "file_key": file_key, "type": "video", "duration_sec": duration}]
    project_id = "test_thesis_sub"
    output_key = render_scenario(
        project_id=project_id,
        scenario=scenario,
        assets=assets,
        overlay_style="minimal",
        storage=storage,
        render_mode="C",
    )
    out_path = storage.output_dir / output_key / "result.mp4"
    print(f"OK: {out_path}")
    return 0 if out_path.exists() else 1


if __name__ == "__main__":
    sys.exit(main())
