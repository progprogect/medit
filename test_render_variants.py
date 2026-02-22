#!/usr/bin/env python3
"""E2E test: full flow via HTTP API — scenario gen → 1 B-roll → render A/B/C."""

import sys
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
TEST_VIDEO = Path(__file__).parent / "Consultations.mp4"


def main():
    if not TEST_VIDEO.exists():
        print(f"Test video not found: {TEST_VIDEO}")
        return 1

    print("1. Creating project...")
    r = requests.post(f"{BASE}/api/projects", timeout=10)
    r.raise_for_status()
    proj = r.json()
    project_id = proj["id"]
    print(f"   Project: {project_id}")

    print("2. Uploading video...")
    with open(TEST_VIDEO, "rb") as f:
        r = requests.post(
            f"{BASE}/api/projects/{project_id}/assets",
            files=[("files", (TEST_VIDEO.name, f, "video/mp4"))],
            timeout=60,
        )
    r.raise_for_status()
    assets = r.json().get("assets", [])
    if not assets:
        print("   No assets returned")
        return 1
    print(f"   Assets: {len(assets)}")

    print("3. Generating scenario (max 1 B-roll insert)...")
    r = requests.post(
        f"{BASE}/api/projects/{project_id}/scenario/generate",
        json={
            "global_prompt": "Сделай короткий сценарий с максимум одной B-roll вставкой. Одна вставка стокового видео в середине."
        },
        timeout=180,
    )
    r.raise_for_status()
    scenario = r.json()
    print(f"   Scenario: {scenario.get('metadata', {}).get('name', '?')}")

    # Find all segments with asset_source=generated that need stock
    video_layer = next((l for l in scenario.get("layers", []) if l.get("type") == "video"), None)
    if not video_layer:
        print("   No video layer")
        return 1

    pending_segs = [
        s
        for s in video_layer.get("segments", [])
        if (s.get("asset_source") or "uploaded") == "generated"
        and (s.get("asset_status") or "ready") != "ready"
    ]

    if pending_segs:
        print(f"4. Fetching stock for {len(pending_segs)} segment(s)...")
        for pending_seg in pending_segs:
            query = (pending_seg.get("params") or {}).get("query") or "person typing laptop"
            r = requests.post(
                f"{BASE}/api/projects/{project_id}/scenario/segments/{pending_seg['id']}/fetch-stock",
                json={"query": query},
                timeout=60,
            )
            if r.status_code != 200:
                print(f"   FAIL fetch-stock {pending_seg['id']}: {r.status_code} {r.text[:500]}")
                return 1
            scenario = r.json()
        print("   Stock fetched for all")
    else:
        print("4. No pending B-roll segments")
        has_pending = any(
            (s.get("asset_source") or "uploaded") == "generated"
            and (s.get("asset_status") or "ready") != "ready"
            for s in video_layer.get("segments", [])
        )
        if has_pending:
            print("   Cannot render: pending segments remain")
            return 1

    outputs = []
    for mode in ("A", "B", "C"):
        print(f"5. Rendering ({mode})...")
        r = requests.post(
            f"{BASE}/api/projects/{project_id}/scenario/render",
            json={"overlay_style": "minimal", "render_mode": mode},
            timeout=300,
        )
        if r.status_code != 200:
            print(f"   FAIL: {r.status_code} {r.text[:200]}")
            return 1
        data = r.json()
        output_key = data.get("output_key")
        download_url = data.get("download_url")
        out_path = Path(__file__).parent / "outputs" / output_key / "result.mp4"
        outputs.append((mode, out_path, download_url))
        print(f"   OK: {output_key}")

    print("\n--- Results ---")
    for mode, path, url in outputs:
        exists = path.exists() if path else False
        print(f"  {mode}: {path} (exists={exists})")
        print(f"       URL: {BASE}{url}")

    print("\nИтоговые видео в outputs/")
    print(f"  {outputs[0][1].parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
