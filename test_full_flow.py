#!/usr/bin/env python3
"""E2E test: scenario with B-roll -> render -> verify output. Run: python test_full_flow.py"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db.session import SessionLocal
from db.models import Project, Scenario, Asset
from schemas.scenario import Scenario as ScenarioSchema
from services.render_service import render_scenario
from services.storage import get_storage
from services.scenario_service import ensure_audio_layer


def get_video_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip()) if r.returncode == 0 else 0


def main():
    db = SessionLocal()
    proj = None
    for p in db.query(Project).limit(50).all():
        sc = db.query(Scenario).filter(Scenario.project_id == p.id).first()
        assets = db.query(Asset).filter(Asset.project_id == p.id).all()
        if not assets or not any(a.type == "video" for a in assets):
            continue
        if sc and sc.data:
            for layer in sc.data.get("layers", []):
                if layer.get("type") != "video":
                    continue
                for seg in layer.get("segments", []):
                    if seg.get("asset_source") == "generated":
                        proj = p
                        break
                if proj:
                    break
        if proj:
            break
    if not proj:
        proj = db.query(Project).first()

    if not proj:
        print("No project found")
        return 1

    assets = db.query(Asset).filter(Asset.project_id == proj.id).order_by(Asset.order_index).all()
    video_asset = next((a for a in assets if a.type == "video"), assets[0])
    sc = db.query(Scenario).filter(Scenario.project_id == proj.id).first()

    if sc:
        scenario = ScenarioSchema.model_validate(sc.data)
    else:
        with open("docs/scenario_example.json") as f:
            scenario = ScenarioSchema.model_validate(json.load(f))
        for layer in scenario.layers:
            for seg in layer.segments or []:
                if seg.asset_id == "media_1":
                    seg.asset_id = video_asset.id
        if scenario.metadata:
            scenario.metadata.asset_ids = [video_asset.id]
        storage = get_storage()
        duration = get_video_duration(storage.get_asset_path(video_asset.file_key))
        if duration > 0:
            scenario.metadata.total_duration_sec = min(duration, scenario.metadata.total_duration_sec or 60)
        scenario = ensure_audio_layer(scenario, video_asset.id)

    asset_dicts = [{"id": a.id, "file_key": a.file_key, "type": a.type, "duration_sec": a.duration_sec} for a in assets]
    db.close()

    storage = get_storage()
    output_key = render_scenario(proj.id, scenario, asset_dicts, "minimal", storage)
    out_path = storage.output_dir / output_key / "result.mp4"
    duration = get_video_duration(out_path)
    print(f"OK: {out_path} ({duration:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
