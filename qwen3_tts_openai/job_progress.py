"""Decoder-backed progress with an explicitly estimated generation fraction."""
from __future__ import annotations

import re

CODEC_FRAMES_PER_SECOND = 12.5  # Pinned tokenizer: 1920 samples/frame at 24kHz.


def speech_weight(text):
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    words = len(re.findall(r"[a-zA-Z0-9]+", text))
    return max(1.0, cjk / 5.0 + words / 2.5)


class NarrationProgress:
    def __init__(self, store, ident, units):
        self.store, self.ident = store, ident
        self.weights = [speech_weight(unit["spoken_text"]) for unit in units]
        self.total = sum(self.weights)

    def update(self, index, event):
        detail = {"stage": event["stage"], "unit_index": index + 1, "units_total": len(self.weights)}
        fraction = 0.0
        if event["stage"] == "synthesizing":
            steps = max(0, event["decoder_steps"] - 1)
            estimated_steps = max(12.5, self.weights[index] * CODEC_FRAMES_PER_SECOND)
            fraction = .95 * steps / (steps + .2 * estimated_steps)
            detail.update(decoder_steps=event["decoder_steps"],
                          generated_audio_seconds=round(steps / CODEC_FRAMES_PER_SECOND, 2), estimated=True)
        elif event["stage"] == "postprocessing":
            fraction = .95
        elif event["stage"] == "unit_complete":
            fraction = 1.0
        value = .02 + .86 * (sum(self.weights[:index]) + self.weights[index] * fraction) / self.total
        self.store.progress(self.ident, value, detail)
