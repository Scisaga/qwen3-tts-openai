# 开发与验证

测试分为服务流程验证与真实模型验证。受控测试音频用于检查接口和产物流程，不能证明 Qwen3-TTS 的音色、情绪或音质。

## CPU 测试

Docker 测试镜像提供服务依赖和真实音频工具，不加载 Qwen 模型：

```bash
docker build --target test -t qwen3-tts-openai:test .
docker run --rm --network none qwen3-tts-openai:test
docker run --rm --network none qwen3-tts-openai:test ruff check .
```

原生开发可安装 `requirements-dev.lock`，并提供 FFmpeg、FFprobe 和 Rubber Band CLI：

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/python scripts/check_toolchain.py
.venv/bin/pytest -q
.venv/bin/ruff check .
```

测试覆盖文稿编译、SSML 子集、原文保留、音色条件隔离、缓存、真实音频后处理、持久化任务、HTTP 接口和模型进程超时。

## Skill 验证

```bash
.venv/bin/pytest -q tests/test_skill.py
python3 skills/qwen3-tts-openai/scripts/tts_skill.py --help
```

Skill 测试将完整副本安装到临时客户端目录，并从仓库之外、隔离 Python 导入路径的进程运行。隔离 HTTP 服务覆盖 MCP 初始化与工具发现、接口鉴权、预检、音色准备、配音提交、任务等待及 MP3/WAV 的大小与 SHA-256 校验；同时检查已编辑 Skill 和已有文件的保留、超时任务不被重复提交，以及下载重定向和损坏文件的处理。音频仍由受控测试后端生成，不作为真实 GPU 音质证据。

首次部署按 [Skill 部署指南](../skills/qwen3-tts-openai/references/setup.md) 在兼容目标主机执行，保留部署提交和就绪检查结果；CPU 测试不代替镜像构建、模型下载与实际 GPU 验收。

## 浏览器流程

浏览器测试通过 `tests.ui_server` 启动隔离服务，使用受控测试音调与独立数据目录。需要 Playwright 及 Chromium；运行脚本前先启动测试服务。

```bash
UI_TEST_DATA_DIR="$PWD/test-results/ui-data" \
  .venv/bin/python -m tests.ui_server
```

另开终端运行：

```bash
.venv/bin/python scripts/ui_smoke.py --output test-results/ui
```

该测试检查上传、参考文字编辑、文本与结构化输入、自动预检、重复提交防护、任务进度、音频播放、下载和移动布局。测试服务与生产服务使用不同的数据目录和端口。

`UI_TEST_PROGRESS=1` 可让隔离测试服务逐帧写入受控测试音调并上报已写入帧数，用于检查段内进度与页面刷新。它验证界面与事件传递，不作为模型速度或听感证据。`tests/test_progress.py` 覆盖 RPC 进度与最终回复分离、固定超时、回调失败后的模型进程清理、旧数据库迁移、单调与终态进度，以及单段音频完成前的多次更新。真实 GPU 验证需提交未命中原始音频缓存的任务，观察多次递增的解码步数，并检查最终 MP3/WAV 与 100% 完成状态。

快速复刻使用独立测试数据与受控 ASR HTTP 响应，覆盖真实浏览器 `MediaRecorder` 录音、格式转换、麦克风拒绝授权、离开页面时释放设备、识别失败后的重试、文字确认、音色提取及新句子试听：

```bash
UI_TEST_QUICK_CLONE=1 UI_TEST_DATA_DIR="$PWD/test-results/quick-clone-ui-data" \
  .venv/bin/python -m tests.ui_server
```

另开终端运行 `scripts/quick_clone_ui_smoke.py`。此脚本的 ASR 响应按调用次数模拟一次超时，使用新启动的测试服务；可通过 `--browser-executable` 指定已安装的 Chromium。接口测试同时覆盖音频时长、损坏和静音、草稿过期、鉴权、同一录音重复提交以及满队列时的音色恢复。

两个音色 Tab 的浏览器检查包括键盘切换、已填写内容与录音草稿保留、健康状态刷新后仍保持所选 Tab，以及切走录音页时释放麦克风。内网 HTTPS 另需在访问客户端信任部署证书后验证 `isSecureContext`、实际开始/停止录音和参考音频转换；localhost 的录音测试不能代替这项检查。部署说明见 [内网网页录音](https-recording.md)。

浏览器已信任证书的测试机可以运行以下检查。脚本使用 Chromium 的受控麦克风，创建并删除自己的临时录音草稿，检查转换和回放；不会注册持久音色或提交模型推理。接口鉴权从 `.env` 或环境变量 `API_TOKEN` 读取。`--ca-file` 用于 HTTP 客户端校验证书，浏览器自身也须事先信任同一 CA，脚本不会忽略证书错误：

```bash
.venv/bin/python scripts/https_recording_smoke.py \
  --url https://voice.example.test:8443 --ca-file static/local-ca.crt
```

## 真实模型验证

安装 GPU 依赖，完成模型预下载并选择目标设备后，可先执行只读探测：

```bash
.venv/bin/pip install --require-hashes -r requirements-gpu.lock
.venv/bin/python scripts/probe_gpu.py --output test-results/gpu-probe.json
```

服务启动后，使用两段有权使用的参考录音验证真实 API 流程：

```bash
.venv/bin/python scripts/gpu_smoke.py \
  --base-url http://127.0.0.1:8000 \
  --sample-a reference-a.wav --ref-text-a '录音 A 的实际文字' \
  --sample-b reference-b.wav --ref-text-b '录音 B 的实际文字' \
  --output test-results/gpu-smoke
```

该脚本会注册测试音色并提交真实配音任务，应使用隔离部署。组合表达另由 `scripts/experiment_combined.py` 验证，需要单独运行并保留人工试听结论。

模型就绪、文件生成、音频可播放与听感接受分别判断。不要把 CPU 测试、页面截图或一次短句生成当作性能、长稿质量或音色相似度的结论。

比较推理速度时，固定模型版本、参考录音、参考文字、文稿、语言、随机种子和采样参数。每个配置先预热，再进行多次生成，记录中位耗时、音频时长、`decoder_steps` 和 RTF（生成耗时 / 音频时长）。同时检查 `manifest.json` 中每个单元的 `raw_cache_hit=false` 和 `generation_measurement_source=this-job`，避免把缓存命中当作模型加速。切换注意力实现可能改变采样的数值结果，需要同时检查音频长度与实际听感。

在隔离部署中分别设置 `ATTN_IMPLEMENTATION=eager`、`ATTN_IMPLEMENTATION=sdpa`，重启后通过 `/health.model` 确认配置和实际线程数。复用同一张 GPU 时，只运行一个模型持有进程，并在生产队列空闲时测试；结果受参考音频长度、文稿、共享 GPU 负载与 CPU 调度影响，不将短句结果推广为所有任务的固定加速比例。

CUDA Graph 验证需固定 SDPA，分别设置 `CUDA_GRAPH_ENABLED=0/1`，比较预热后的短句和长段生成。启用时检查 `model.cuda_graph.captured`，并检查每个生成单元的 `generation.cuda_graph.replays_this_call > 0`；仅配置了环境变量不能证明实际回放。记录捕获时间、解码步数、音频长度、RTF 与 GPU 峰值显存，同时保留 MP3/WAV 产物。

还需验证：子模型静态缓存与官方动态缓存的贪心预测一致；同一路径、同种子的重复音频哈希一致；切换音色或采样参数后再恢复原参数，结果仍可复现；首次捕获和热图回放使用相同种子时结果一致。通过 ASR 辅助检查生成文字是否完整，音色和停顿仍需人工回听。捕获失败、输入或 SDK 参数不兼容时应明确失败，不接受静默回退；关闭图后不得复用图路径生成的原始音频缓存。

快速复刻的真实链路通过 `scripts/quick_clone_smoke.py` 验证。在配置了真实 `ASR_BASE_URL` 的隔离部署中运行：

```bash
.venv/bin/python scripts/quick_clone_smoke.py \
  --base-url http://127.0.0.1:8000 --sample reference.wav \
  --output test-results/quick-clone-live
```

脚本会注册音色、等待 ICL 参考条件就绪，并生成一句新文字的 MP3 与 WAV；支持 `--text`、`--language` 和 `--timeout`。鉴权 Token 从环境变量 `API_TOKEN` 读取。所有测试数据与报告保留在忽略目录中，实际音色相似度需要人工试听。

共享 ASR 繁忙时，可先独立完成真实转写，再通过 `--asr-proof` 复用验证结果；证明文件包含 `real_asr_verified=true`、`audio_sha256`、`ref_text` 和 `elapsed_seconds`。脚本严格检查原 WAV 与归一化 WAV 均匹配该哈希，并在报告中标明 `verified-prior-response`，避免在 TTS 隔离验证期间再次等待 ASR 队列。

## 公开测试结果

测试输出留在被忽略的 `test-results/` 中。分享结果前移除服务地址、设备 UUID、绝对路径、Token、真实音色与任务标识；使用演示截图与脱敏摘要描述发现。公开文档和测试使用 `example.test` 示例域名，实际部署地址只写入被忽略的 `.env`。

发布前运行 `python3 scripts/check_public_content.py --staged` 检查暂存内容；CI 同时检查所有受版本控制的文件，阻止内网地址、设备 UUID、主机路径及私有配置进入公开仓库。截图还需要逐张检查画面内容。
