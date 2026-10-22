from __future__ import annotations

import asyncio
import contextlib
import secrets
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.contracts import NarrationRequest, SpeechRequest, VoiceEdit
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.synthesis_service import SynthesisService

ROOT = Path(__file__).resolve().parents[1]


class RequestGuard:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        path = scope["path"]
        if (path.startswith("/v1/") or path.startswith("/mcp")) and self.settings.api_token:
            auth = headers.get(b"authorization", b"")
            if not secrets.compare_digest(auth, ("Bearer " + self.settings.api_token).encode("utf-8")):
                return await JSONResponse(ServiceError("unauthorized", "Provide Authorization: Bearer API_TOKEN", 401).payload(), 401)(scope, receive, send)
        if path.startswith("/admin/"):
            if not self.settings.admin_token:
                return await JSONResponse(ServiceError("admin_disabled", "ADMIN_TOKEN is not configured; management operations are disabled", 403).payload(), 403)(scope, receive, send)
            if not secrets.compare_digest(headers.get(b"x-admin-token", b""), self.settings.admin_token.encode("utf-8")):
                return await JSONResponse(ServiceError("unauthorized", "Provide x-admin-token", 401).payload(), 401)(scope, receive, send)
        content_length = headers.get(b"content-length")
        try:
            oversized = content_length is not None and int(content_length) > self.settings.max_request_bytes
        except ValueError:
            oversized = True
        if oversized:
            return await JSONResponse(ServiceError("request_too_large", "Request exceeds MAX_REQUEST_BYTES", 413).payload(), 413)(scope, receive, send)
        # Bound chunked bodies before FastAPI's multipart parser can spool an unbounded upload.
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            body, count = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                count += len(chunk)
                if count > self.settings.max_request_bytes:
                    return await JSONResponse(ServiceError("request_too_large", "Request exceeds MAX_REQUEST_BYTES", 413).payload(), 413)(scope, receive, send)
                body.append(chunk)
                if not message.get("more_body", False):
                    break
            data, consumed, original_receive = b"".join(body), False, receive
            async def buffered_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": data, "more_body": False}
                return await original_receive()
            receive = buffered_receive
        return await self.app(scope, receive, send)


def create_app(settings=None, backend=None):
    settings = settings or Settings.from_env()
    service = SynthesisService(settings, backend)
    mcp = None
    if settings.enable_mcp:
        from qwen3_tts_openai.mcp_server import build_mcp
        mcp = build_mcp(service)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(service.start)
        async def maintain_references():
            while True:
                await asyncio.to_thread(service.references.prune)
                if service.quick_clone_status()["enabled"]:
                    await asyncio.to_thread(service.asr.probe)
                await asyncio.sleep(30)
        maintenance = asyncio.create_task(maintain_references())
        async with AsyncExitStack() as stack:
            if mcp:
                await stack.enter_async_context(mcp.session_manager.run())
            try:
                yield
            finally:
                maintenance.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintenance
                await asyncio.to_thread(service.close)

    app = FastAPI(title="Qwen3-TTS 本地配音", version="0.1.0", lifespan=lifespan, description="Local official Qwen3-TTS plus explicit FFmpeg controls. One GPU holder, persistent bounded queue. Speech compatibility: mp3/wav, no streaming; registered voice IDs or explicit presets only.")
    app.state.service = service
    app.add_middleware(RequestGuard, settings=settings)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.mount("/examples", StaticFiles(directory=ROOT / "examples"), name="examples")
    if mcp:
        app.mount("/mcp", mcp.streamable_http_app(), name="mcp")

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return JSONResponse(error.payload(), error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        details = [{k: row[k] for k in ("loc", "type", "msg") if k in row} for row in error.errors()]
        return JSONResponse(ServiceError("invalid_input", "Request validation failed", 422, details).payload(), 422)

    @app.get("/", include_in_schema=False)
    @app.get("/studio", include_in_schema=False)
    @app.get("/voices", include_in_schema=False)
    @app.get("/jobs", include_in_schema=False)
    @app.get("/integrations/mcp", include_in_schema=False)
    @app.get("/runtime", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/health")
    def health():
        return service.health()

    @app.get("/ready")
    def ready():
        return JSONResponse(service.health(), 200 if service.ready() else 503)

    @app.post("/v1/voices", status_code=202)
    async def register_voice(file: UploadFile | None = File(None), name: str = Form(...), ref_text: str = Form(""),
                             mode: str | None = Form(None), draft_id: str | None = Form(None),
                             voice_id: str | None = Form(None)):
        if (file is None) == (draft_id is None):
            if file:
                await file.close()
            raise ServiceError("invalid_reference_source", "Provide exactly one WAV file or reference draft_id", 422)
        if draft_id is not None:
            return await asyncio.to_thread(service.register_reference, draft_id, name, ref_text, mode, voice_id)
        if not (file.filename or "").lower().endswith(".wav"):
            await file.close()
            raise ServiceError("invalid_audio", "Upload a .wav reference file", 422)
        with tempfile.TemporaryDirectory(dir=settings.data_dir) as temp:
            path, count = Path(temp) / "upload.wav", 0
            try:
                with path.open("wb") as out:
                    while chunk := await file.read(256 * 1024):
                        count += len(chunk)
                        if count > settings.max_upload_bytes:
                            raise ServiceError("upload_too_large", "Reference exceeds MAX_UPLOAD_BYTES", 413)
                        out.write(chunk)
                return await asyncio.to_thread(service.register_voice, path, name, ref_text, mode, voice_id)
            finally:
                await file.close()

    @app.post("/v1/voices/references", status_code=201)
    async def create_reference(file: UploadFile = File(...), language: str | None = Form(None), transcribe: bool = Form(True)):
        try:
            service.require_quick_clone()
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in {".wav", ".webm", ".m4a", ".mp4", ".mp3", ".ogg", ".flac"}:
                raise ServiceError("invalid_audio", "请选择 WAV、WebM、M4A、MP3、OGG、FLAC 或 MP4 录音。", 422)
            with tempfile.TemporaryDirectory(dir=settings.data_dir) as temp:
                path, count = Path(temp) / ("recording" + suffix), 0
                with path.open("wb") as out:
                    while chunk := await file.read(256 * 1024):
                        count += len(chunk)
                        if count > settings.max_upload_bytes:
                            raise ServiceError("upload_too_large", "录音超过上传大小限制。", 413)
                        out.write(chunk)
                return await asyncio.to_thread(service.create_reference, path, language, transcribe)
        finally:
            await file.close()

    @app.post("/v1/voices/references/{draft_id}/transcribe")
    async def transcribe_reference(draft_id: str, language: str | None = Form(None)):
        return await asyncio.to_thread(service.transcribe_reference, draft_id, language)

    @app.get("/v1/voices/references/{draft_id}/sample")
    def reference_sample(draft_id: str):
        service.require_quick_clone()
        return FileResponse(service.references.sample(draft_id), media_type="audio/wav")

    @app.delete("/v1/voices/references/{draft_id}", status_code=204)
    def delete_reference(draft_id: str):
        service.require_quick_clone()
        service.references.delete(draft_id)

    @app.get("/v1/voices")
    def voices():
        return {"voices": service.list_voices()}

    @app.patch("/v1/voices/{voice_id}")
    def edit_voice(voice_id: str, request: VoiceEdit):
        return service.edit_voice(voice_id, request.model_dump(exclude_unset=True))

    @app.post("/v1/voices/{voice_id}/prepare", status_code=202)
    def prepare_voice(voice_id: str):
        return service.prepare_voice(voice_id)

    @app.get("/v1/voices/{voice_id}/sample")
    def voice_sample(voice_id: str):
        return FileResponse(service.voices.path(voice_id, original=True), media_type="audio/wav")

    @app.post("/v1/narrations/compile")
    def compile(request: NarrationRequest):
        return service.compile(request)

    @app.post("/v1/narrations", status_code=202)
    async def narrate(request: NarrationRequest, http: Request):
        raw = (await http.body()).decode("utf-8")
        return await asyncio.to_thread(service.submit, request, raw)

    @app.get("/v1/jobs")
    def jobs():
        return {"jobs": [service.public_job(job) for job in service.jobs.list()]}

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str):
        return service.get_job(job_id)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return service.cancel(job_id)

    @app.get("/v1/jobs/{job_id}/artifacts/{artifact_id}")
    def artifact(job_id: str, artifact_id: str):
        path, media = service.artifact(job_id, artifact_id)
        return FileResponse(path, media_type=media, filename=path.name)

    @app.post("/v1/audio/speech")
    async def speech(request: SpeechRequest, http: Request):
        path, media = await service.speech(request, (await http.body()).decode("utf-8"))
        return FileResponse(path, media_type=media, filename=path.name)

    @app.post("/admin/model/preload", status_code=202)
    def preload():
        # Enqueued on the same holder; never race an active generation or load a second model.
        job = service.jobs.submit("model_preload", {})
        service.wake.set()
        return service.public_job(job)

    return app


app = create_app()
