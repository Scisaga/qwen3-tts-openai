#!/usr/bin/env python3
"""Install the portable Skill and operate qwen3-tts-openai with Python 3.10+ stdlib."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SKILL_NAME = "qwen3-tts-openai"
TERMINAL = {"succeeded", "failed", "cancelled"}
IGNORED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}


def package_files(root):
    return {
        p.relative_to(root): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not (set(p.relative_to(root).parts) & IGNORED)
        and p.suffix not in {".pyc", ".pyo"}
    }


def install(client, skills_dir=None):
    source = Path(__file__).resolve().parents[1]
    clients = ("codex", "claude") if client == "both" else (client,)
    if skills_dir and len(clients) != 1:
        raise ValueError("--skills-dir requires a single --client")
    targets = []
    contents = package_files(source)
    for name in clients:
        parent = Path(skills_dir).expanduser() if skills_dir else Path.home() / {
            "codex": ".agents/skills", "claude": ".claude/skills",
        }[name]
        target = parent / SKILL_NAME
        if target.is_symlink():
            raise ValueError(f"Existing Skill is a symlink; inspect it before replacing: {target}")
        if target.exists() and (not target.is_dir() or package_files(target) != contents):
            raise ValueError(f"Existing Skill differs; archive it outside the skills directory first: {target}")
        targets.append((name, target))
    installed = []
    for name, target in targets:
        unchanged = target.exists()
        if not unchanged:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".tts-install-", dir=target.parent) as staging:
                staged = Path(staging) / SKILL_NAME
                shutil.copytree(source, staged, ignore=shutil.ignore_patterns(*IGNORED, "*.pyc", "*.pyo"))
                staged.rename(target)
        installed.append({"client": name, "path": str(target), "status": "unchanged" if unchanged else "installed"})
    return {"skills": installed, "next": "Use the installed SKILL.md; start a new session if the Skill is not visible."}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, token="", timeout=30):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("--url must be the service root, e.g. http://127.0.0.1:8000, without credentials or /v1")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        # Artifact downloads must never forward a bearer token to another location.
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def open(self, path, body=None, headers=None, allowed_errors=()):
        parsed = urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc:
            raise ValueError("Only relative API paths on the selected service are accepted")
        request_headers = {"Accept": "application/json"}
        if self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = Request(self.url + path, data=data, headers=request_headers)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            if error.code in allowed_errors:
                return error
            detail = error.read(8192).decode("utf-8", errors="replace")
            raise ValueError(f"HTTP {error.code} at {path}: {detail}") from None

    def json(self, path, body=None, headers=None, allowed_errors=()):
        with self.open(path, body, headers, allowed_errors) as response:
            return json.load(response)

    def doctor(self, mcp=False):
        health = self.json("/health")
        if health.get("service") != SKILL_NAME:
            raise ValueError("The target is not a qwen3-tts-openai service; no changes were made")
        ready = self.json("/ready", allowed_errors=(503,))
        voices = self.json("/v1/voices")
        result = {"service": health["service"], "inference_ready": ready.get("inference_ready", False),
                  "api_accessible": True, "voice_count": len(voices["voices"]),
                  "model": ready.get("model"), "audio": ready.get("audio")}
        if mcp:
            endpoint = "/mcp/"
            headers = {"Accept": "application/json, text/event-stream"}
            initialized = self.json(endpoint, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "qwen3-tts-skill", "version": "1.0.0"}},
            }, headers)
            # This project's FastMCP server uses stateless HTTP and JSON responses.
            headers["MCP-Protocol-Version"] = initialized["result"]["protocolVersion"]
            with self.open(endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers):
                pass
            listed = self.json(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
            names = sorted(tool["name"] for tool in listed["result"]["tools"])
            if not {"list_voices", "submit_narration", "get_job"}.issubset(names):
                raise ValueError("MCP did not expose the expected narration tools")
            result["mcp_tools"] = names
        return result

    def job(self, job_id):
        return self.json("/v1/jobs/" + quote(job_id, safe=""))

    def wait_job(self, job_id, seconds, interval):
        deadline = time.monotonic() + seconds
        while True:
            job = self.job(job_id)
            if job["status"] in TERMINAL:
                return job, 0 if job["status"] == "succeeded" else 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**job, "wait_timed_out": True,
                        "next": "Query this job_id again. The server job was not cancelled or resubmitted."}, 3
            time.sleep(min(interval, remaining))

    def download(self, job_id, artifact_id, output):
        job = self.job(job_id)
        artifact = next((item for item in job["artifacts"] if item["artifact_id"] == artifact_id), None)
        if artifact is None:
            raise ValueError("Artifact is absent; inspect the job status and its returned artifacts")
        expected_path = "/v1/jobs/" + quote(job_id, safe="") + "/artifacts/" + quote(artifact_id, safe="")
        if artifact["url"] != expected_path:
            raise ValueError("Unexpected artifact URL; refusing to send credentials or download it")
        target = Path(output).expanduser()
        digest, size = hashlib.sha256(), 0
        # Exclusive creation preserves an existing local file.
        out = target.open("xb")
        try:
            with out:
                with self.open(artifact["url"], headers={"Accept": artifact["media_type"]}) as response:
                    while chunk := response.read(256 * 1024):
                        size += len(chunk)
                        if size > artifact["bytes"]:
                            raise ValueError("Downloaded artifact exceeds its declared size")
                        digest.update(chunk)
                        out.write(chunk)
                if size != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
                    raise ValueError("Artifact size or SHA-256 verification failed")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {"job_id": job_id, "artifact_id": artifact_id, "path": str(target.resolve()),
                "bytes": size, "sha256": digest.hexdigest()}


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--url", default=os.getenv("QWEN_TTS_URL", "http://127.0.0.1:8000"))
    root.add_argument("--token-env", default="QWEN_TTS_API_TOKEN", help="Environment variable holding the API token")
    root.add_argument("--timeout", type=positive, default=30, help="Per-request timeout in seconds")
    commands = root.add_subparsers(dest="command", required=True)
    installer = commands.add_parser("install", help="Copy this Skill to the current user's client directory")
    installer.add_argument("--client", choices=("codex", "claude", "both"), required=True)
    installer.add_argument("--skills-dir", type=Path, help="Override the skills parent for a single client")
    doctor = commands.add_parser("doctor", help="Check identity, readiness, and API authentication without inference")
    doctor.add_argument("--mcp", action="store_true", help="Also initialize MCP and list its actual tools")
    commands.add_parser("voices", help="List registered voices and their conditioning states")
    commands.add_parser("jobs", help="Inspect existing jobs before retrying an interrupted submission")
    for name in ("compile", "submit"):
        command = commands.add_parser(name, help="Preflight narration" if name == "compile" else "Preflight and submit once")
        command.add_argument("--request", required=True, type=Path, help="NarrationRequest JSON file; preserves all fields")
    job = commands.add_parser("job", help="Inspect or wait for an existing job")
    job.add_argument("job_id")
    job.add_argument("--wait", action="store_true")
    job.add_argument("--wait-seconds", type=positive, default=600)
    job.add_argument("--poll-interval", type=positive, default=2)
    download = commands.add_parser("download", help="Download a returned artifact and verify size and SHA-256")
    download.add_argument("job_id")
    download.add_argument("--artifact", default="full-mp3")
    download.add_argument("--output", required=True, type=Path)
    return root


def run(args):
    if args.command == "install":
        return install(args.client, args.skills_dir), 0
    client = Client(args.url, os.getenv(args.token_env, ""), args.timeout)
    if args.command == "doctor":
        result = client.doctor(args.mcp)
        return result, 0 if result["inference_ready"] else 1
    if args.command == "voices":
        return client.json("/v1/voices"), 0
    if args.command == "jobs":
        return client.json("/v1/jobs"), 0
    if args.command in {"compile", "submit"}:
        request = json.loads(args.request.read_text(encoding="utf-8-sig"))
        if not isinstance(request, dict):
            raise ValueError("The request file must contain a JSON object")
        plan = client.json("/v1/narrations/compile", request)
        if args.command == "compile" or not plan["valid"]:
            return plan, 0 if plan["valid"] else 2
        # No retry: an interrupted response does not mean the server rejected the job.
        return client.json("/v1/narrations", request), 0
    if args.command == "job":
        if args.wait:
            return client.wait_job(args.job_id, args.wait_seconds, args.poll_interval)
        job = client.job(args.job_id)
        return job, 1 if job["status"] in {"failed", "cancelled"} else 0
    return client.download(args.job_id, args.artifact, args.output), 0


def main():
    args = parser().parse_args()
    try:
        result, code = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except (OSError, URLError, ValueError, KeyError, TypeError) as error:
        message = str(error)
        token = os.getenv(args.token_env, "")
        if token:
            message = message.replace(token, "[redacted]")
        print(json.dumps({"error": message, "next": "Inspect the failure; submissions are never automatically retried."},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
