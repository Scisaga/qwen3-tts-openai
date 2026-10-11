from __future__ import annotations

import multiprocessing
import threading
import time

from config import Settings
from errors import ServiceError
from backends.qwen import capabilities


def model_process(connection, values):
    settings = Settings(**values)
    settings.configure_process()
    from backends.qwen import OfficialQwen
    adapter = OfficialQwen(settings)
    while True:
        try:
            command, arguments = connection.recv()
        except EOFError:
            break
        if command == "stop":
            break
        try:
            if command == "select":
                result = adapter.select(**arguments)
            elif command == "prepare":
                _, result = adapter.prepare(**arguments)
            else:
                result = adapter.generate(**arguments)
            connection.send({"ok": True, "result": result, "status": adapter.status()})
        except Exception as e:
            if isinstance(e, ServiceError):
                error = e.payload()["error"]
                status = e.status
            elif "out of memory" in str(e).lower():
                error = {"type": "gpu_oom", "message": "GPU OOM; shorten the segment/MAX_NEW_TOKENS or wait for GPU memory. No model/device fallback", "details": str(e)[-1500:]}
                status = 507
                import torch
                torch.cuda.empty_cache()
            else:
                error = {"type": "model_failed", "message": str(e)[-2000:], "details": {"exception": type(e).__name__}}
                status = 503
            connection.send({"ok": False, "error": error, "http_status": status, "status": adapter.status()})
    connection.close()


class ModelRuntime:
    """One owned child, called only by the single durable-queue worker.

    The child holds all GPU state. A timeout terminates this child only; the next request
    creates a fresh holder. HTTP remains available during import/download/load/generation.
    """
    def __init__(self, settings: Settings):
        self.settings, self.process, self.connection = settings, None, None
        self.capabilities = capabilities(settings)
        self.health = {"state": "unloaded", "backend": settings.backend, "model_id": settings.model_id, "dtype": settings.dtype, "attention": settings.attn_implementation, "device": settings.device_map, "gpu_selection": settings.gpu_selection(), "capabilities": self.capabilities}
        self._guard = threading.Lock()
        self.conditioning_identity = None

    def _rpc(self, command, arguments, timeout):
        if not self.process or not self.process.is_alive():
            context = multiprocessing.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=model_process, args=(child, self.settings.model_dump()), name="qwen3-tts-model-holder", daemon=True)
            self.process.start()
            child.close()
        self.connection.send((command, arguments))
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if self.connection.poll(0.1):
                try:
                    reply = self.connection.recv()
                except EOFError as e:
                    self.health.update(state="failed", error="Model holder exited unexpectedly")
                    raise ServiceError("model_process_failed", "Model holder exited; inspect process logs", 503) from e
                self.health.update(reply.get("status", {}))
                if self.settings.backend == "qwen-custom" and self.health.get("identity", {}).get("tts_model_size") == "0b6":
                    self.capabilities["instruction_control"] = "unsupported"
                if not reply["ok"]:
                    error = reply["error"]
                    self.health.update(state="failed", error=error)
                    raise ServiceError(error["type"], error["message"], reply["http_status"], error.get("details"))
                self.health.update(state="ready", error=None)
                return reply["result"]
            if not self.process.is_alive():
                self.health.update(state="failed", error="Model holder exited unexpectedly")
                raise ServiceError("model_process_failed", "Model holder exited; inspect process logs", 503)
        self.stop()
        self.health.update(state="failed", error=f"{command} exceeded {timeout:g} seconds")
        raise ServiceError("model_timeout", f"Model {command} timed out. The owned holder was restarted; shorten the input or adjust the relevant timeout", 504)

    def select(self, kind):
        if self.health.get("loaded_kind") != kind or self.health["state"] != "ready":
            self.health["state"] = "loading"
            self._rpc("select", {"kind": kind}, self.settings.model_load_timeout_seconds)

    def preload(self):
        self.select("expression" if self.settings.backend == "qwen-combined-experimental" else "base")

    def provenance(self):
        identity = self.health.get("identity", {})
        stable = {k: v for k, v in identity.items() if k not in {"load_seconds", "loaded_memory_bytes"}}
        return {"backend": self.settings.backend, "identity": stable, "conditioning_identity": self.conditioning_identity, "dtype": self.settings.dtype, "attention": self.settings.attn_implementation, "capabilities": self.capabilities}

    def prepare(self, voice):
        self.select("base")
        return self._rpc("prepare", {"voice": voice}, self.settings.generation_timeout_seconds)

    def ensure_generation(self, voice=None):
        if self.settings.backend == "qwen-combined-experimental":
            # Prepare returns a disk cache entry, so only CustomVoice remains resident for generation.
            prepared = self._rpc("prepare", {"voice": voice}, self.settings.model_load_timeout_seconds + self.settings.generation_timeout_seconds)
            self.conditioning_identity = {"model": prepared["model"], "mode": "x_vector_only"}
            self.select("expression")
        else:
            self.select("base")

    def generate(self, unit, generation, language, target):
        return self._rpc("generate", {"unit": unit, "generation": generation, "language": language, "target": str(target)}, self.settings.generation_timeout_seconds)

    def stop(self):
        with self._guard:
            if self.process:
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=5)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=2)
                self.process.close()
                self.process = None
            if self.connection:
                self.connection.close()
                self.connection = None
            self.health.pop("loaded_kind", None)
            self.health.update(state="unloaded")
