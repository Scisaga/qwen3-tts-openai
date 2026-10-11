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
