from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)
    model_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    model_revision: str = "fd4b254389122332181a7c3db7f27e918eec64e3"
    expression_model_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    expression_model_revision: str = "0c0e3051f131929182e2c023b9537f8b1c68adfe"
    backend: Literal["qwen-base", "qwen-custom", "qwen-combined-experimental"] = "qwen-base"
    device_map: str = "cuda:0"
    dtype: Literal["float16"] = "float16"
    attn_implementation: Literal["eager", "sdpa"] = "sdpa"
    torch_num_threads: int = Field(1, ge=1, le=64)
    torch_num_interop_threads: int = Field(1, ge=1, le=64)
    cuda_graph_enabled: bool = False
    gpu_device: str = ""
    gpu_host_device: str = ""
    hf_home: Path = Path("models")
    hf_hub_offline: bool = False
    preload_model: bool = True
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = Field(12302, ge=1, le=65535)
    api_token: str = ""
    admin_token: str = ""
    queue_limit: int = Field(8, ge=1, le=100)
    max_upload_bytes: int = Field(20 * 1024 * 1024, ge=1024)
    max_request_bytes: int = Field(22 * 1024 * 1024, ge=1024)
    max_reference_seconds: float = Field(30, ge=1, le=300)
    max_text_chars: int = Field(30000, ge=1)
    max_unit_chars: int = Field(240, ge=8, le=2000)
    max_output_bytes: int = Field(512 * 1024 * 1024, ge=1024)
    max_audio_seconds: float = Field(3600, gt=0)
    min_free_disk_bytes: int = Field(256 * 1024 * 1024, ge=0)
    job_timeout_seconds: float = Field(3600, gt=0)
    generation_timeout_seconds: float = Field(180, gt=0)
    model_load_timeout_seconds: float = Field(900, gt=0)
    sync_timeout_seconds: float = Field(180, gt=0)
    ffmpeg_timeout_seconds: float = Field(120, gt=0)
    ffmpeg_threads: int = Field(2, ge=1, le=32)
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    rubberband_bin: str = "rubberband"
    work_sample_rate: Literal[16000, 22050, 24000, 32000, 44100, 48000] = 24000
    mp3_bitrate: Literal["128k", "160k", "192k", "256k"] = "128k"
    max_new_tokens: int = Field(2048, ge=64, le=8192)
    enable_mcp: bool = True
    asr_base_url: str = ""
    asr_timeout_seconds: float = Field(120, gt=0, le=600)
    reference_draft_ttl_seconds: int = Field(600, ge=60, le=86400)
    tls_certfile: str = ""
    tls_keyfile: str = ""
    https_public_url: str = ""
    https_local_ca: bool = False

    @field_validator("https_public_url")
    @classmethod
    def https_url(cls, value):
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path):
            raise ValueError("HTTPS_PUBLIC_URL must be an HTTPS service root without credentials, path, query or fragment")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError("HTTPS_PUBLIC_URL has an invalid port") from exc
        return value

    @field_validator("asr_base_url")
    @classmethod
    def asr_url(cls, value):
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path):
            raise ValueError("ASR_BASE_URL must be an HTTP(S) service root, without /v1, credentials, query or fragment")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError("ASR_BASE_URL has an invalid port") from exc
        return value

    @model_validator(mode="after")
    def device_rules(self):
        if self.cuda_graph_enabled and (self.attn_implementation != "sdpa" or self.backend == "qwen-combined-experimental"):
            raise ValueError("CUDA_GRAPH_ENABLED requires SDPA and qwen-base or qwen-custom")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("Configure TLS_CERTFILE and TLS_KEYFILE together, or leave both empty")
        if self.device_map != "cuda:0":
            raise ValueError("DEVICE_MAP must be cuda:0; isolate one RTX 2080 Ti using GPU_DEVICE")
        for name in ("gpu_device", "gpu_host_device"):
            value = getattr(self, name)
            if "," in value or value.lower() in {"all", "auto", "none"}:
                raise ValueError(f"{name.upper()} must be a single GPU UUID or explicit index")
        if self.max_request_bytes < self.max_upload_bytes:
            raise ValueError("MAX_REQUEST_BYTES must cover MAX_UPLOAD_BYTES plus multipart overhead")
        if self.work_sample_rate <= 24000 and self.mp3_bitrate in {"192k", "256k"}:
            raise ValueError("MP3_BITRATE must be 128k or 160k at WORK_SAMPLE_RATE <= 24000; use 32000/44100/48000 for higher bitrates")
        return self

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        values = {name: os.environ[name.upper()] for name in cls.model_fields if name.upper() in os.environ}
        if "work_sample_rate" in values:
            values["work_sample_rate"] = int(values["work_sample_rate"])
        return cls(**values)

    def configure_process(self):
        # Called before any torch import. Never pick a device from the host automatically.
        if self.gpu_device:
            os.environ["CUDA_VISIBLE_DEVICES"] = self.gpu_device
        os.environ["HF_HOME"] = str(self.hf_home.resolve())
        if self.hf_hub_offline:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"

    def gpu_selection(self, driver_path: Path = Path("/proc/driver/nvidia/gpus")):
        """Describe configuration without initializing CUDA or claiming inference readiness.

        Compose retains the host selector separately because its sole exposed card is
        selected as visible device 0 inside the container. Kernel metadata is optional.
        """
        requested = self.gpu_host_device or self.gpu_device
        result = {"host_selector": requested, "host_index": None, "host_uuid": None,
                  "host_name": None, "index_source": None, "visible_selector": self.gpu_device,
                  "logical_device": self.device_map, "isolated": bool(self.gpu_device)}
        if requested.isdecimal():
            result.update(host_index=int(requested), index_source="configured-index")
        if requested.startswith("GPU-"):
            result["host_uuid"] = requested
        try:
            for path in driver_path.glob("*/information"):
                fields = dict(line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)
                fields = {key.strip(): value.strip() for key, value in fields.items()}
                if fields.get("GPU UUID") != requested:
                    continue
                minor = fields.get("Device Minor", "")
                result.update(host_name=fields.get("Model"), host_uuid=fields.get("GPU UUID"))
                if minor.isdecimal():
                    result.update(host_index=int(minor), index_source="kernel-device-minor")
                break
        except OSError:
            pass  # Containers may not expose kernel metadata; never guess a host index.
        return result
