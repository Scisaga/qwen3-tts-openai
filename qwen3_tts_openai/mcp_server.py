"""Thin Streamable HTTP MCP facade, sharing the durable service with REST/UI."""
import asyncio
import json

from mcp.server.fastmcp import FastMCP

from qwen3_tts_openai.contracts import NarrationRequest


def build_mcp(service):
    mcp = FastMCP("Qwen3-TTS", stateless_http=True, json_response=True)
    mcp.settings.streamable_http_path = "/"

    @mcp.tool()
    async def submit_narration(request: dict) -> dict:
        """Submit director/SSML/plain-text narration with registered voice_id; returns job links."""
        return await asyncio.to_thread(service.submit, NarrationRequest.model_validate(request))

    @mcp.tool()
    async def get_job(job_id: str) -> dict:
        """Query status and audio artifact links; audio is never embedded as base64."""
        return await asyncio.to_thread(service.get_job, job_id)

    @mcp.tool()
    async def list_voices() -> dict:
        """List registered reference voices or the configured backend's explicit presets."""
        return {"voices": await asyncio.to_thread(service.list_voices)}

    @mcp.resource("qwen3tts://health")
    async def health() -> str:
        return json.dumps(await asyncio.to_thread(service.health), ensure_ascii=False)

    @mcp.resource("qwen3tts://usage")
    def usage() -> str:
        return "Submit narration_director + voice_profile + voice_id without rewriting the director. Compile first using POST /v1/narrations/compile. Poll get_job. Fetch returned relative artifact URLs with the same API bearer token. Base expression requirements need explicit allow_degraded. Experimental combination needs allow_experimental. Upload audio through /v1/voices, not MCP/base64/remote URLs. Limits and capabilities are reported by /health."

    return mcp
