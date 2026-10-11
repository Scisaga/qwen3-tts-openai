from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GenerationOptions(StrictModel):
    seed: StrictInt = Field(0, ge=0, le=2**32 - 1)
    temperature: float = Field(0.9, gt=0, le=2)
    top_p: float = Field(1.0, gt=0, le=1)
    top_k: StrictInt = Field(50, ge=1, le=1000)
    do_sample: bool = True
    max_new_tokens: StrictInt | None = Field(None, ge=64, le=8192)


class NarrationRequest(StrictModel):
    narration_director: dict[str, Any] | None = None
    voice_profile: dict[str, Any] = Field(default_factory=dict)
    ssml: str | None = None
    text: str | None = None
    voice_id: str | None = None
    voice_mapping: dict[str, str] = Field(default_factory=dict)
    compatibility: Literal["build-narrated-presentation", "generic"] = "build-narrated-presentation"
    language: Literal["Chinese", "English", "Auto", "Japanese", "Korean", "German", "French", "Russian", "Portuguese", "Spanish", "Italian"] = "Chinese"
    allow_degraded: bool = False
    allow_experimental: bool = False
    generation: GenerationOptions = Field(default_factory=GenerationOptions)
    selection: dict[str, list[StrictInt]] | None = None
    speed: float | None = Field(None, ge=0.25, le=4)
    source_documents: dict[Literal["narration_director", "voice_profile"], str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_source(self):
        if sum(x is not None for x in (self.narration_director, self.ssml, self.text)) != 1:
            raise ValueError("Provide exactly one of narration_director, ssml or text")
        if self.speed is not None and self.text is None:
            raise ValueError("speed is for plain text; director/SSML supply their own numeric controls")
        if self.selection is not None:
            if set(self.selection) - {"pages", "segments"}:
                raise ValueError("selection accepts pages and segments (1-based)")
            if not self.selection.get("pages") or any(x <= 0 for xs in self.selection.values() for x in xs):
                raise ValueError("selection needs positive pages and optional positive segment indices")
        for name, raw in self.source_documents.items():
            if json.loads(raw.removeprefix("\ufeff")) != getattr(self, name):
                raise ValueError(f"source_documents.{name} must decode to the same supplied object")
        return self


class SpeechRequest(StrictModel):
    model: str
    input: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    response_format: Literal["mp3", "wav"] = "mp3"
    speed: float = Field(1.0, ge=0.25, le=4)
    instructions: str | None = Field(None, max_length=2000)


class VoiceEdit(StrictModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    ref_text: str | None = Field(None, max_length=4000)
    mode: Literal["icl", "x_vector_only"] | None = None
