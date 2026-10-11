import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from config import Settings
from storage import digest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PRELOAD_MODEL", "0")


class ControlledBackend:
    """Service-test tone generator, never a runtime selectable backend or Qwen evidence."""
    def __init__(self, *, delay=0, preload_gate=None):
        self.capabilities = {"voice_clone": "supported", "instruction_control": "unsupported", "combined_clone_instruction": "unsupported", "control_granularity": "sentence-or-short-paragraph", "word_alignment": "unsupported"}
        self.health = {"state": "unloaded", "model_id": "controlled-test-tone", "dtype": "test", "attention": "test", "device": "test-only", "capabilities": self.capabilities}
        self.calls, self.preparations = [], []
        self.delay, self.preload_gate = delay, preload_gate
        self.generating = threading.Event()

    def preload(self):
        self.health["state"] = "loading"
        if self.preload_gate:
            self.preload_gate.wait(10)
        self.health["state"] = "ready"

    def stop(self):
        if self.preload_gate:
            self.preload_gate.set()

    def prepare(self, voice):
        self.preparations.append(voice)
        self.health["state"] = "ready"
        return {"model_source": "controlled-test-tone", "key": digest(voice), "mode": voice["mode"]}

    def ensure_generation(self, voice):
        self.health["state"] = "ready"

    def provenance(self):
        return {"model_source": "controlled-test-tone", "revision": "test-v1"}

    def generate(self, unit, generation, language, target):
        self.calls.append({"unit": unit, "generation": generation, "language": language})
        self.generating.set()
        time.sleep(self.delay)
        duration = 0.7 + len(unit["spoken_text"]) * 0.005
        frequency = 300 + int(digest(unit["voice_snapshot"])[0:4], 16) % 200
        audio = np.sin(np.arange(round(duration * 24000)) * 2 * np.pi * frequency / 24000) * 0.12
        sf.write(target, audio, 24000, subtype="PCM_16")
        return {"model_source": "controlled-test-tone", "duration_seconds": duration, "frequency": frequency, "generation_seconds": self.delay}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", hf_home=tmp_path / "models", preload_model=False, min_free_disk_bytes=0, ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg"), ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe"), rubberband_bin=os.getenv("RUBBERBAND_BIN", "rubberband"))


@pytest.fixture
def director():
    return json.loads((ROOT / "examples/narration_director.json").read_text())


@pytest.fixture
def profile():
    return json.loads((ROOT / "examples/voice_profile.json").read_text())


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "sample.wav"
    sf.write(path, np.sin(np.arange(48000) * 2 * np.pi * 440 / 24000) * 0.1, 24000, subtype="PCM_16")
    return path


def wait_done(service, ident, timeout=30):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        result = service.jobs.get(ident)
        if result["state"] in {"succeeded", "failed", "cancelled"}:
            return result
        time.sleep(0.03)
    raise AssertionError(f"Job {ident} did not finish: {service.jobs.get(ident)}")
