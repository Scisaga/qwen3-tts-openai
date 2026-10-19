<p align="center">
  <img src="static/favicon.svg" width="64" height="64" alt="qwen3-tts-openai 图标">
</p>

# qwen3-tts-openai

**用参考音色，为文字配音。**

一个基于 Qwen3-TTS 的自托管配音工作台。添加一段参考录音，选择音色，输入文稿，就能在自己的 GPU 上生成、试听并下载配音。也可以通过 OpenAI 兼容语音接口、REST API 和 MCP，把本地语音能力接入现有应用。

适合视频旁白、课程讲解、演示文稿配音和文章朗读。模型下载完成后支持离线推理，参考录音、文稿和生成音频保存在自己的存储中。

## 界面预览

### 配音工作台

左侧编辑文稿、选择音色；右侧查看分段、控制参数和执行计划。点击「生成配音」会自动预检，也可以先单独检查文稿。

![配音工作台：文稿编辑与执行计划](docs/images/workbench.png)

| 音色管理 | 生成与试听 |
| --- | --- |
| 在「快速复刻 / 添加音色」两个并列 Tab 中选择添加方式，管理、试听并选择音色。 | 查看任务进度，试听结果，下载 MP3 或 WAV。 |
| ![音色管理界面](docs/images/voices.png) | ![配音结果与音频下载](docs/images/audio-result.png) |

### 快速复刻

录音或上传短录音，自动识别对应文字；回听、确认文字后生成音色。两个添加方式在同一位置切换，已完成的录音草稿与填写内容会保留。

![快速复刻：并列 Tab、参考录音回放与文字确认](docs/images/quick-clone.png)

配音生成过程中，进度会随实际解码步骤更新，页面每半秒刷新，显示处理阶段和已生成的音频长度。模型生成的总长度无法提前确定，因此段内百分比标为估算；产物保存成功后显示 100%。

![生成中的进度与音频长度（受控演示）](docs/images/progress.png)

*截图使用当前界面、演示文稿和受控测试音频，设备与任务标识已隐藏。截图用于展示操作流程，音色效果请在真实模型中试听确认。仓库不附带参考录音，首次使用请添加自己的音色。*

## 主要功能

| 功能 | 能做什么 |
| --- | --- |
| 参考音色克隆 | 上传 WAV，保存为可重复使用的音色；支持完整参考与仅说话人特征两种方式。 |
| 快速复刻（可选） | 在网页录音或上传短录音，自动识别对应文字，确认后保存音色并用新句子试听。 |
| 内网网页录音（可选） | 提供独立 HTTPS 入口、HTTP 页面跳转按钮和公共证书下载；访问电脑信任证书后可使用麦克风。 |
| 多种文稿入口 | 直接粘贴普通文字，或使用导演稿 JSON、SSML 编排段落与页面。 |
| 生成前预检 | 展示实际生成分段、读法和控制参数；不支持的要求会明确报告，降级执行需要确认。 |
| 声音控制 | 调整语速、音高、增益和停顿，并对照原始与处理后的音频。 |
| 试听与导出 | 支持整稿、逐页音频，以及局部试生成；导出 MP3、WAV、时间轴、执行记录和输入原文。 |
| 任务与缓存 | 查看队列、进度和生成历史，取消任务；复用已验证的生成与后处理缓存。 |
| 应用接入 | 提供 OpenAI 兼容语音接口子集、异步 REST API 与 HTTP MCP。 |
| 自托管 | 本地模型与文件存储，SQLite 持久化；网页无需单独构建，模型可预下载后离线运行。 |

## 从文稿到配音

1. **添加音色**：在「音色管理」选择「添加音色」Tab，上传一段有权使用的清晰 WAV 录音，默认支持 1–30 秒。填写与录音一致的文字可使用完整参考；留空则仅提取说话人特征。启用 ASR 后，也可在「快速复刻」Tab 直接录音并自动识别文字。
2. **输入文稿**：选择音色，粘贴要朗读的文字。需要逐页参数或发音规则时，切换到导演稿或 SSML。
3. **生成配音**：点击生成，工作台自动预检并提交任务；若存在问题，先修正文稿或确认计划中列出的降级项。
4. **试听与下载**：确认读法和段落衔接，下载 MP3 或 WAV。多页文稿还可以逐页试听与导出。

## 快速音色复刻

### 启用与使用

在 `.env` 配置 [qwen3-asr-openai](https://github.com/Scisaga/qwen3-asr-openai) 服务的**根地址**，重启 TTS 服务后即可启用「快速复刻」。留空关闭此入口；需要使用 `BACKEND=qwen-base`。以下地址为示例域名，请替换为自己的部署地址：

```dotenv
ASR_BASE_URL=http://asr.example.test:8001
ASR_TIMEOUT_SECONDS=120
REFERENCE_DRAFT_TTL_SECONDS=600
```

ASR 地址不要追加 `/v1`。识别超时默认 120 秒，可按共享 ASR 服务的排队时间调整。

音色管理页提供两个并列 Tab：

| Tab | 添加方式 |
| --- | --- |
| 快速复刻 | 网页录音或上传短录音，自动识别文字，确认后使用完整参考模式生成音色。 |
| 添加音色 | 上传已有 WAV，手动填写可选参考文字，支持完整参考或仅说话人特征。 |

切换 Tab 会保留已完成的录音草稿、音色名称和文字。正在录音时切走会停止录音并释放麦克风。工作台的「快速复刻」和「添加」按钮分别打开对应 Tab。

1. 在「快速复刻」录制清晰语音，建议 10–20 秒，支持 1–30 秒；也可上传 WAV、WebM、M4A、MP3、OGG、FLAC、MP4 短录音。
2. 服务将录音转换为单声道 PCM WAV，并调用 ASR 的 `POST /v1/audio/transcriptions`。回听录音，确认自动识别的文字与实际内容一致；ASR 可能会归一化数字，请按实际说法修正。
3. 填写音色名称，点击「生成音色」。完整参考条件提取完成后，音色会自动选中。
4. 点击「用新句子试听」检查克隆效果，或点击「使用这个音色」返回工作台配音。

识别失败时录音草稿会保留，可以重新识别或手动填写文字。ASR 故障不影响已有音色和配音服务。

共享 ASR 在处理长录音时，短录音也可能排队。勾选「手动填写文字」可立即跳过识别等待，填写文字后生成音色；识别等待期间切换此选项也会保留同一份录音。接口调用可传 `transcribe=false` 创建草稿，稍后再使用重新识别接口。

录音草稿默认在最后一次识别完成后的 10 分钟过期，并定期清理；已保存的音色独立持久化。重复提交同一草稿与相同名称、文字会返回同一音色。

### 局域网 HTTPS 录音

**网页麦克风需要 HTTPS 或 localhost。** 通过局域网 HTTP 地址访问时，浏览器无法开启麦克风，仍可上传已有录音。项目提供独立 HTTPS 代理，转发到原有 TTS HTTP 服务，证书自动续期。

以下使用示例域名，TTS HTTP 端口为 `8000`、HTTPS 端口为 `8443`，在 `.env` 中配置：

```dotenv
HTTPS_PUBLIC_URL=https://voice.example.test:8443
HTTPS_UPSTREAM=127.0.0.1:8000
HTTPS_LOCAL_CA=1
```

将示例域名替换为访问设备能解析的域名或 IP，端口按自己的部署配置调整。在 Linux 上执行：

```bash
bash scripts/start_https.sh
```

脚本启动 `docker-compose.https.yml` 中的代理，并将公共根证书导出为 `static/local-ca.crt`。随后重启 TTS 服务，使页面读取新的 HTTPS 配置；Docker 部署还需将该证书只读挂载到 `/app/static/local-ca.crt`，详见 [内网网页录音](docs/https-recording.md)。

首次在访问电脑上使用录音时：

1. 在自己的 HTTP 工作台「快速复刻」Tab 点击「下载内网证书」，将其导入电脑的受信任根证书。Windows、macOS、Linux 和 Firefox 的操作见 [证书信任步骤](docs/https-recording.md#在访问电脑上启用录音)。
2. 重新打开浏览器，从工作台点击「打开录音页面」，进入所配置的 HTTPS 地址，点击「开始录音」并允许网站使用麦克风。

配置后，HTTP 页按钮显示「打开录音页面」，点击可直接进入 HTTPS 的快速复刻 Tab；HTTPS 页提供「开始录音」。HTTP 工作台和原有 API 地址继续可用，ASR 仍由 TTS 后端通过内网 HTTP 连接。启用接口鉴权时，需要在 HTTPS 页面重新填写 API Token。

已有受信任域名与 HTTPS 代理时，设置 `HTTPS_PUBLIC_URL` 并保持 `HTTPS_LOCAL_CA=0` 即可。也可以在原生部署中同时配置以下两个文件，让 `server.py` 在当前 `PORT` 直接使用 HTTPS：

```dotenv
TLS_CERTFILE=/path/to/server.crt
TLS_KEYFILE=/path/to/server.key
```

证书需覆盖访问域名或 IP，并受到访问设备信任。Docker 直接启用 TLS 时，还需挂载证书与私钥。独立代理的 CA 和私钥保存在 `.local/https/data`，需要持久保留；这些文件不进入 Git。

## 快速开始

需要 Linux、Docker Compose v2、可工作的 NVIDIA 驱动和 NVIDIA Container Toolkit。**当前推理后端限定单张 NVIDIA RTX 2080 Ti（sm_75），使用 FP16；暂不支持 CPU 推理或其他显卡。**

以下是通用部署示例，可根据自己的环境调整端口与设备。

### 1. 准备配置

获取代码后，在项目目录执行：

```bash
cp .env.example .env
nvidia-smi --query-gpu=index,uuid,name --format=csv
```

编辑 `.env`，选择一张目标 GPU。下面的设备字段需要替换，其他模型参数可沿用示例配置：

```dotenv
GPU_DEVICE=<GPU_UUID_OR_INDEX>
BIND_ADDRESS=127.0.0.1
PORT=8000
HF_HUB_OFFLINE=1
```

### 2. 构建并下载模型

```bash
docker compose config --quiet
docker compose build

docker run --rm --env-file .env -e HF_HOME=/models \
  -v "$PWD/models:/models" qwen3-tts-openai:local \
  python scripts/download_models.py --http --online
```

预下载会获取固定版本的完整模型快照并校验权重，不需要暴露 GPU。`--online` 仅用于这次下载；服务仍按 `.env` 中的离线设置启动。

### 3. 启动工作台

```bash
docker compose up -d --no-build
docker compose logs -f qwen3_tts
```

打开 <http://127.0.0.1:8000/>，等待界面显示模型「已就绪」，然后添加音色。API 文档位于 <http://127.0.0.1:8000/docs>。

`/health` 表示服务存活，`/ready` 在模型与音频工具就绪后返回 200。模型保存在 `models/`，参考录音、任务、缓存与音频保存在 `data/`；两者均不进入 Git。

需要接口鉴权时，可设置 `API_TOKEN`，调用 `/v1/*` 和 `/mcp/` 时携带 `Authorization: Bearer <token>`。更多可调选项见 [.env.example](.env.example)。

### 推理速度

默认使用 SDPA 注意力计算，并在模型进程中启用纯推理模式。CPU 计算线程配置如下；已有 `.env` 的部署需更新这些字段并重启服务：

```dotenv
ATTN_IMPLEMENTATION=sdpa
TORCH_NUM_THREADS=1
TORCH_NUM_INTEROP_THREADS=1
```

两个线程数只控制模型进程内的 CPU 计算，配音任务仍由单个 GPU 模型进程串行处理。可通过 `/health.model` 的 `attention`、`cpu_threads` 和 `hardware` 检查生效值；`ATTN_IMPLEMENTATION=eager` 保留为可显式选择的配置。

Qwen3-TTS 按步骤生成音频，小批量解码会交替执行 CPU 调度与 GPU 计算，GPU 利用率不等同于生成速度。评估配置时应比较同一参考音色、文稿和生成参数下的预热后耗时，并确认任务没有命中原始音频缓存；具体方法见 [开发与验证](docs/testing.md)。

可选启用 **CUDA Graph + 静态 KV 缓存**，将每个音频解码步内的 15 步子模型预测捕获为可重复回放的计算图：

```dotenv
CUDA_GRAPH_ENABLED=1
```

此选项要求 `ATTN_IMPLEMENTATION=sdpa`，兼容 `qwen-base` 和 `qwen-custom` 的预测器接口，默认关闭；真实 GPU 验证当前覆盖 1.7B Base，其他模式需独立验证。无需更换模型权重或安装额外推理库；首次加载会预热、捕获计算图。改变采样参数时会重新捕获，进程最多保留一套图。外层文本、参考音色处理与停止条件继续由固定版本的官方 SDK 执行。

重启后检查 `/ready`，以及 `/health.model.inference_engine=cuda-graph-predictor`、`cuda_graph.captured=true`。生成记录会包含该任务的 `replays_this_call`，并与官方路径的缓存隔离。不同执行路径的采样结果可能不同，应分别检查文字完整性与音色听感。捕获失败会明确报错；设置 `CUDA_GRAPH_ENABLED=0` 并重启可切回官方路径。

## 接入现有应用

### OpenAI 兼容语音接口

先通过网页添加音色，或调用上传接口：

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/voices \
  -F file=@reference.wav \
  -F 'name=我的讲解音色' \
  -F 'ref_text=这里填写录音中实际说出的文字。' \
  -F mode=icl
```

上传返回 `voice_id` 与参考条件提取任务。等待音色就绪后，把下例的 `<voice_id>` 替换为返回的 ID：

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-tts","input":"你好，这是一段本地生成的配音。","voice":"<voice_id>","response_format":"mp3","speed":1}' \
  -o narration.mp3
```

该接口兼容 OpenAI 语音请求结构的子集，支持 MP3、WAV 和语速参数。`voice` 使用本地注册音色 ID，不对应 OpenAI 的预置声音名称；当前不提供流式输出。长稿建议使用异步配音接口。

### REST 与 MCP

| 入口 | 用途 |
| --- | --- |
| `POST /v1/voices` | 上传 WAV 注册音色，或提交 `draft_id`、`name`、确认后的 `ref_text` 注册快速复刻音色。 |
| `POST /v1/voices/references` | 上传短录音并转写，返回 `draft_id`、`ref_text`、`sample_url`、过期时间与识别状态；传 `transcribe=false` 可跳过识别等待。 |
| `POST /v1/voices/references/{draft_id}/transcribe` | 重新识别录音草稿。 |
| `GET /v1/voices/references/{draft_id}/sample` | 回放草稿 WAV，使用同一 API 鉴权。 |
| `DELETE /v1/voices/references/{draft_id}` | 清除临时录音，不影响已保存音色。 |
| `POST /v1/narrations/compile` | 预检文稿并返回执行计划，不调用 GPU。 |
| `POST /v1/narrations` | 提交普通文本、导演稿或 SSML 配音任务。 |
| `GET /v1/jobs/{job_id}` | 查询任务状态与产物下载链接。 |
| `POST /v1/jobs/{job_id}/cancel` | 申请取消任务。 |
| `/mcp/` | MCP Streamable HTTP，提供 `submit_narration`、`get_job`、`list_voices` 工具。 |

导演稿、音色配置和 SSML 示例分别见 [narration_director.json](examples/narration_director.json)、[voice_profile.json](examples/voice_profile.json) 和 [minimal.ssml](examples/minimal.ssml)。完整接口参数以服务自带的 API 文档为准。

## 当前能力范围

- 默认使用 Qwen3-TTS Base 进行参考音色克隆。语速、音高和增益由音频后处理完成；默认 Base 不支持自由表达指令，组合表达后端属于需要显式启用的实验功能。
- 长稿按自然句或短段生成，不提供字级对齐。音色相似度、术语读法和段落衔接请以实际试听为准。
- SSML 支持已实现的标签与数值控制，不是完整的 Azure SSML 实现；未支持的控制会在预检中报告。

## 开源与贡献

欢迎通过 Issue 反馈使用问题，或提交 Pull Request 改进界面、输入协议和模型适配。反馈问题时请提供复现步骤、错误类型与经过脱敏的日志。

开发说明见 [架构与能力范围](docs/architecture.md)和[开发与验证](docs/testing.md)。

项目代码采用 [Apache-2.0](LICENSE) 许可证。Qwen3-TTS 模型权重与第三方组件遵循各自的许可条款，相关说明见 [NOTICE](NOTICE)。
