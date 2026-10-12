"""Optional real-media check; no generated assets, remote APIs or platform calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                    reason="real-media smoke test requires ffmpeg and ffprobe")
def test_real_image_assembly_reuses_verified_shots(tmp_path):
    script = Path(__file__).resolve().parents[1] / "skills/builtin/auto-short-video/scripts/assemble.py"
    project = tmp_path / "outputs" / "checkpoint-render"
    project.mkdir(parents=True)
    images = [project / "first.ppm", project / "second.ppm"]
    for image, color in zip(images, [bytes([255, 0, 0]), bytes([0, 0, 255])]):
        image.write_bytes(b"P6\n32 32\n255\n" + color * (32 * 32))
    storyboard = project / "storyboard.json"
    storyboard.write_text(json.dumps({"size": "160x160", "image_motion": "static", "shots": [
        {"image": str(image), "duration": 0.2} for image in images]}), encoding="utf-8")
    final = project / "final.mp4"
    env = {**os.environ, "EASEL_ROOT": str(tmp_path), "PYTHONUTF8": "1"}
    command = [sys.executable, str(script), "assemble", "--resume",
               "--storyboard", str(storyboard), "-o", str(final)]
    first = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert first.returncode == 0, first.stderr
    manifest = project / ".final.mp4.checkpoints" / "checkpoints.json"
    before = manifest.read_bytes()
    second = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert second.returncode == 0, second.stderr
    assert second.stderr.count("已核验并复用分镜") == 2
    assert manifest.read_bytes() == before
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(final)], capture_output=True, text=True, timeout=10)
    assert probe.returncode == 0, probe.stderr
    assert 0.3 <= float(json.loads(probe.stdout)["format"]["duration"]) <= 0.6
