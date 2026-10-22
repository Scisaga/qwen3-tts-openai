---
name: qwen3-tts-openai
description: Install, deploy, connect, and troubleshoot the self-hosted qwen3-tts-openai voice studio, then clone reference voices and generate narration through its REST or MCP interfaces. Use for this project's setup, voice selection, narration jobs, and audio downloads.
---

# Qwen3-TTS OpenAI

Use [Scisaga/qwen3-tts-openai](https://github.com/Scisaga/qwen3-tts-openai) and its existing voice library and durable jobs. The bundled Python 3.10+ helper uses only the standard library; the inference service has its own Docker/Python dependencies.

- For Skill installation, local/SSH deployment, or MCP registration, read [references/setup.md](references/setup.md). Install the whole Skill directory, then use the installed helper, so subsequent work does not depend on a temporary checkout.
- For voice registration, plain text/director/SSML narration, polling, and downloads, read [references/usage.md](references/usage.md). Use connected MCP tools when available; the helper provides REST access without an MCP client.

First identify the intended service root URL. Reuse a matching deployment. Daily narration does not authorize rebuilding images, restarting services, changing GPU allocation, or upgrading an existing checkout.

New inference deployments require Linux x86_64, Docker Compose v2, NVIDIA Container Toolkit, and **one explicitly selected NVIDIA CUDA GPU with FP16 inference**. Check the target checkout's GPU validation, pinned PyTorch/CUDA architecture support, and available memory; see the compatibility note in setup.md. The current checkout still checks for RTX 2080 Ti, so another GPU model needs runtime adaptation and verification before use. For a newer checkout, follow its actual checks rather than treating that GPU model as a permanent requirement. Do not bypass failed checks or substitute CPU inference, another model family, or an experimental backend. Preserve existing `.env`, `models/`, `data/`, client configuration, and other GPU services.

Select a real voice from `list_voices` or `GET /v1/voices`; Base does not accept OpenAI's built-in voice names. Preserve the supplied text and structured controls. Compile before submitting; explain unsupported controls and change `allow_degraded` or `allow_experimental` only when the user has authorized that behavior.

Submission returns a job, not finished audio. Poll the returned `job_id` until `succeeded`, `failed`, or `cancelled`, and download only artifact links returned by that job. On a timeout or an unknown submission result, inspect jobs before retrying; do not silently submit the same narration again. Report job status, local output paths, and any remaining quality checks. Readiness, successful file generation, and human acceptance of voice similarity are separate results.

Keep API tokens in the execution environment (`QWEN_TTS_API_TOKEN`), never in the Skill or a copied prompt. Reference audio is uploaded with REST multipart or the workbench; MCP has no voice-upload tool and does not accept audio as base64 or remote URLs.
