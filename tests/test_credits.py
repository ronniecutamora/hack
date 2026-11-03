"""Every locked Python package remains listed with the third-party credits."""

import json
import re
import tomllib
from pathlib import Path


def test_locked_packages_are_credited():
    root = Path(__file__).resolve().parent.parent
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    readme = (root / "README.md").read_text(encoding="utf-8")
    credits = readme.split("### Third-party code and assets", 1)[1].split("## Repo structure", 1)[0]
    missing = [
        package["name"]
        for package in lock["package"]
        if package["name"] != "hackathon-app"
        and not re.search(rf"(?<![\w-]){re.escape(package['name'])}(?![\w-])", credits, re.I)
    ]
    assert not missing, f"Missing package credits: {missing}"


def test_video_results_and_credits_match_the_recorded_clips():
    root = Path(__file__).resolve().parent.parent
    results = json.loads((root / "data/eval/results.json").read_text(encoding="utf-8"))
    readme = (root / "README.md").read_text(encoding="utf-8")
    credits = readme.split("### Third-party code and assets", 1)[1].split("## Repo structure", 1)[0]
    for clip in results["video_reports"]["clips"]:
        rows = [
            line for line in readme.splitlines() if line.startswith("|") and clip["author"] in line
        ]
        assert len(rows) >= 2  # evaluation table and third-party credits
        spark = clip["spark_frame_floodwater"]
        sam = clip["sam_frame_floodwater"]
        figures = (
            f"| {sum(spark)}/{len(spark)} | {sum(sam)}/{len(sam)} | "
            f"{clip['latency_seconds']:.2f} s | ${clip['cost_usd']:.6f} |"
        )
        assert any(figures in row for row in rows)
        assert clip["author"] in credits and clip["license"] in credits
