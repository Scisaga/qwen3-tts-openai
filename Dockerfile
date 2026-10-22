FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS toolchain
ARG DEBIAN_MIRROR=https://deb.debian.org
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg rubberband-cli libsndfile1 sox ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' rubberband ' \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q ' libmp3lame '
WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements/requirements.lock /app/requirements/
RUN --mount=type=cache,target=/root/.cache/pip pip install --index-url "${PIP_INDEX_URL}" --require-hashes -r requirements/requirements.lock

FROM toolchain AS test
COPY requirements/requirements-dev.lock /app/requirements/
RUN --mount=type=cache,target=/root/.cache/pip pip install --index-url "${PIP_INDEX_URL}" --require-hashes -r requirements/requirements-dev.lock
COPY . /app
RUN python scripts/check_toolchain.py
CMD ["pytest", "-q"]

FROM toolchain AS runtime
COPY requirements/requirements-gpu.lock /app/requirements/
RUN --mount=type=cache,target=/root/.cache/pip pip install --index-url "${PIP_INDEX_URL}" --require-hashes -r requirements/requirements-gpu.lock \
    && python -c "import torch; flags=torch._C._cuda_getArchFlags(); print(torch.__version__,torch.version.cuda,flags); assert 'sm_75' in flags" \
    && python -c "from qwen_tts import Qwen3TTSModel; import transformers; print(transformers.__version__)"
COPY . /app
RUN python scripts/check_toolchain.py
ENV HOST=0.0.0.0 PORT=12302 DATA_DIR=/data HF_HOME=/models
EXPOSE 12302
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request,os,ssl; tls=bool(os.getenv('TLS_CERTFILE')); urllib.request.urlopen(('https' if tls else 'http')+'://127.0.0.1:'+os.getenv('PORT','12302')+'/health',context=ssl._create_unverified_context() if tls else None,timeout=3)"
CMD ["python", "-u", "-m", "qwen3_tts_openai"]
