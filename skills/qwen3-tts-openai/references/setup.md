# 安装、部署与接入

Skill 客户端使用 Python 3.10+ 和标准库。服务可在 Windows x64 原生部署，也可在 Linux x86_64 原生部署或使用 Docker Compose。客户端可通过 HTTP(S) 或 SSH 转发连接已有服务。

## 安装 Skill

取得 [项目源码](https://github.com/Scisaga/qwen3-tts-openai) 后，在源码根目录以当前客户端用户运行：

```bash
python3 skills/qwen3-tts-openai/scripts/tts_skill.py install --client codex
```

`--client claude` 安装到 Claude Code，`--client both` 同时安装。默认位置是 `~/.agents/skills/qwen3-tts-openai` 和 `~/.claude/skills/qwen3-tts-openai`。可用 `--skills-dir <目录>` 指定一个客户端的技能父目录。安装复制整个 Skill，后续不依赖源码路径；相同内容重复安装会复用，不同内容的同名目录会停止，更新前先将旧副本移到技能目录之外。

Windows PowerShell 使用 `py -3`：

```powershell
py -3 .\skills\qwen3-tts-openai\scripts\tts_skill.py install --client codex
py -3 "$HOME\.agents\skills\qwen3-tts-openai\scripts\tts_skill.py" --help
```

下文命令使用已安装的 Codex 副本。Claude Code 用户将 `.agents` 替换为 `.claude`；其他安装位置相应替换。若客户端没有发现新 Skill，重新打开会话。Windows、WSL 和 SSH 服务端的用户目录分别独立；在运行智能体的客户端安装 Skill。

## 优先接入已有服务

确认用户指定的服务根地址、目标主机与部署目录。没有给出地址时，检查当前项目的已有部署配置和启动方式；只读取所需字段，不输出 `.env`、Token 或完整容器环境。不要把一个探测失败的地址当作重新部署的依据。

鉴权通过环境变量 `QWEN_TTS_API_TOKEN` 读取，由用户的密钥管理方式提供，值对应服务的 `API_TOKEN`。默认地址是 `http://127.0.0.1:8000`；可使用 `QWEN_TTS_URL` 或全局 `--url` 指定实际地址。服务未启用鉴权时无需 Token。

```bash
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py \
  --url http://127.0.0.1:8000 doctor --mcp
```

```powershell
py -3 "$HOME\.agents\skills\qwen3-tts-openai\scripts\tts_skill.py" --url http://127.0.0.1:8000 doctor --mcp
```

`doctor` 检查项目身份、`/ready`、真实音色列表访问；`--mcp` 额外完成 MCP 初始化与工具发现，不提交推理任务。helper 直接连接指定服务，不继承 shell 的 HTTP 代理。省略 `--mcp` 可诊断未启用 MCP 的 REST 服务。返回码非零时先处理鉴权、模型加载或音频工具故障。已有服务能够接入时，不改 `.env`、模型、镜像或启动方式。

## 首次部署

用户要求部署且目标主机上没有可复用的服务时，优先使用已有 checkout。需要新 checkout 时，可在用户选定的目录执行 `git clone https://github.com/Scisaga/qwen3-tts-openai.git`。阅读该 checkout 的 README 和 `.env.example`，记录 `git rev-parse HEAD`。固定版本的部署使用用户选定的 tag 或 commit。

在目标主机查看 GPU，核对可用显存和构建支持。只有一张兼容卡时可选择它。多卡目标不明确时先请用户选择。

```text
nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used --format=csv
```

当前 `verify_device()` 检查 RTX 2080 Ti（`sm_75`）和 PyTorch 构建的架构支持。启动前以实际 checkout 的设备检查为准。检查失败时报告具体原因。

在已有 `.env` 中按需修改相关字段。新部署从 `.env.example` 创建配置，填写选定 GPU 的索引或 UUID。以下是本机部署的设置。

```dotenv
GPU_DEVICE=<GPU_UUID_OR_INDEX>
HOST=127.0.0.1
BIND_ADDRESS=127.0.0.1
PORT=8000
BACKEND=qwen-base
DTYPE=float16
HF_HUB_OFFLINE=1
ENABLE_MCP=1
```

保留仓库锁定的模型和 revision。检查端口冲突，并在服务和客户端配置中使用同一地址。需要鉴权时，将随机 Token 写入 `.env` 的 `API_TOKEN`，由客户端从 `QWEN_TTS_API_TOKEN` 读取。模型缓存保存在 `models/`，音色、任务和产物保存在 `data/`。

### Windows 原生部署

在 PowerShell 按目标 checkout 的 README「Windows 原生启动」执行。检查 x64 Python 3.12、NVIDIA Windows 驱动和音频工具，使用 `requirements/requirements-gpu-windows.lock` 安装服务依赖。运行音频工具检查，预下载并校验模型，再从仓库根目录执行 `python -m qwen3_tts_openai`。使用虚拟环境中的 Python 并查看启动终端的日志。

### Linux 部署

使用原生部署时，按目标 checkout 的 README「原生启动」准备音频工具，使用 `requirements/requirements-gpu.lock` 安装依赖，并从仓库根目录执行同一模块入口。

使用 Docker Compose 时，检查 Docker Compose v2、Docker 访问权限和 NVIDIA Container Toolkit。在部署目录构建镜像、预下载模型并启动服务。

```bash
docker compose config --quiet
docker compose build
docker run --rm --env-file .env -e HF_HOME=/models \
  -v "$PWD/models:/models" qwen3-tts-openai:local \
  python scripts/download_models.py --http --online
docker compose up -d --no-build
docker compose ps
```

### 检查就绪并接入

参考 `.env` 的 `MODEL_LOAD_TIMEOUT_SECONDS` 等待 `/ready`。使用已安装 helper 的 `doctor --mcp` 检查模型、音频工具、鉴权和 MCP。原生部署查看启动终端日志，Compose 部署查看 `docker compose logs --tail=100 qwen3_tts`。

返回实际工作台入口、API 文档 `/docs`、MCP 地址 `/mcp/`、Skill 安装位置和部署提交。实际配音验收需要用户提供参考录音或选择已有音色。

## SSH 目标

在用户已配置的 SSH 主机（例如 `tts-host`）上执行前述 Linux 部署流程。沿用 SSH 密钥和已核实的主机记录，使用 `BatchMode=yes` 与 `StrictHostKeyChecking=yes`。无需将 Skill 运行客户端改成 root。

服务保持监听服务器回环地址。客户端可开一个前台转发，把本地 `18000` 转到服务器实际服务端口：

```bash
ssh -N -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18000:127.0.0.1:8000 tts-host
```

保持该终端开启，客户端用 `http://127.0.0.1:18000` 运行 `doctor`、打开工作台和配置 MCP；Ctrl+C 只关闭转发。实际远端端口按部署配置替换，若本地端口占用则选空闲端口并同步配置。已有 HTTPS 入口时直接复用它。局域网网页录音的证书与代理配置见服务仓库的 `docs/https-recording.md`，普通 HTTP 远端页面不能启用麦克风。

## MCP 接入

REST helper 本身不依赖 MCP。需要 MCP 时，先运行 `doctor --mcp`，再用当前客户端的 CLI 检查同名配置；相同则复用，不同则报告具体地址冲突，保留其他服务器配置。

Codex 示例：

```bash
codex mcp add qwen3-tts-openai --url http://127.0.0.1:8000/mcp/ \
  --bearer-token-env-var QWEN_TTS_API_TOKEN
codex mcp list
```

没有启用鉴权时省略 `--bearer-token-env-var`。启用鉴权时，启动客户端的环境必须提供该变量；只有当前 shell 设置成功不代表已经运行的 IDE 拥有它。

Claude Code 未启用鉴权的示例：

```bash
claude mcp add --transport http --scope user qwen3-tts-openai http://127.0.0.1:8000/mcp/
claude mcp list
```

启用鉴权时，可在项目 `.mcp.json` 的 `mcpServers` 中**合并**以下条目，使用 Claude Code 支持的环境变量引用；不要覆盖已有条目或将实际 Token 写入文件：

```json
{
  "qwen3-tts-openai": {
    "type": "http",
    "url": "http://127.0.0.1:8000/mcp/",
    "headers": {"Authorization": "Bearer ${QWEN_TTS_API_TOKEN}"}
  }
}
```

上面是 `mcpServers` 的内容，不是完整配置文件。已有用户级同名条目时先核对作用域，避免它遮蔽项目配置。连接 SSH 转发或远端 HTTPS 时替换 URL。

配置保存、HTTP/MCP 探测通过和当前会话已加载工具分别判断。在当前会话发现 `list_voices`、`submit_narration`、`get_job` 后再使用；必要时重开会话。该服务使用 Bearer Token，不运行 OAuth 登录。

客户端依据：[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Claude Code MCP](https://code.claude.com/docs/en/mcp)。
