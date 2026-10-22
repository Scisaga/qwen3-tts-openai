# 安装、部署与接入

Skill 客户端只需要 Python 3.10+，不需要安装 PyTorch、Qwen SDK 或本项目的 Python 依赖。推理服务部署在 Linux x86_64 GPU 主机；macOS、Windows 客户端通过已有 HTTP(S) 入口或 SSH 转发连接它。

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

`doctor` 检查项目身份、`/ready`、真实音色列表访问；`--mcp` 额外完成 MCP 初始化与工具发现，不提交推理任务。helper 直接连接指定服务，不继承 shell 的 HTTP 代理。省略 `--mcp` 可诊断未启用 MCP 的 REST 服务。返回码非零时先处理鉴权、模型加载或音频工具故障。已有服务能够接入时，不改 `.env`、模型、镜像或启动方式。

## 首次部署

仅在用户要求安装部署且目标上确实没有对应服务时执行。以下是部署流程，由智能体根据检查结果执行；安装脚本的 `install` 命令只安装 Skill。

1. 在目标主机检查 `uname -sm`、`docker version`、`docker compose version`、Docker 访问权限和 NVIDIA Container Toolkit。缺失依赖按目标发行版的官方说明安装；保留现有驱动及其他 GPU 服务。
2. 使用以下命令查看设备和占用；选择用户明确指定的一张 **NVIDIA GPU**，检查计算架构、可用显存与当前构建是否兼容。仅有一张兼容卡且用户已授权使用时可选择它。多卡目标不明确时先获取选择；不得选择 `all`、`auto` 或多张卡。

   ```bash
   nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used --format=csv
   ```

   当前仓库默认单卡 FP16 推理，`backends/qwen.py` 的 `verify_device()` 仍检查 RTX 2080 Ti 与 `sm_75`；镜像和探测脚本也检查 PyTorch 构建包含 `sm_75`。其他型号需要完成运行时适配、构建匹配和实机验证，当前代码不能直接启动它们。部署更新版本时以该 checkout 的实际检查和验证记录为准，不将当前验证型号写成长期产品边界。设备检查失败时报告具体原因，不跳过检查或切换产品、后端。

3. 在用户指定的目录取得仓库。未指定时可选 `$HOME/services/qwen3-tts-openai`；先检查目录是否存在。已有 checkout 不执行自动 pull、reset 或清理。全新安装可执行：

   ```bash
   git clone https://github.com/Scisaga/qwen3-tts-openai.git "$HOME/services/qwen3-tts-openai"
   cd "$HOME/services/qwen3-tts-openai"
   git rev-parse HEAD
   ```

   阅读该 checkout 的 README、`.env.example`、Compose 和 Dockerfile，以实际代码为准，记录部署提交。固定版本需求使用用户选定的 tag 或 commit。

4. **仅在 `.env` 不存在时**复制 `.env.example` 并设置仅当前用户可读写。已有文件按现有部署诊断，不整体覆盖。设置：

   ```dotenv
   GPU_DEVICE=<明确选中的单卡 UUID 或索引>
   BIND_ADDRESS=127.0.0.1
   PORT=8000
   BACKEND=qwen-base
   DTYPE=float16
   HF_HUB_OFFLINE=1
   ENABLE_MCP=1
   ```

   保留仓库锁定的模型和 revision。实际端口可调整，先检查冲突；服务入口与客户端配置同步使用该端口。需要鉴权时将生成的随机 Token 直接写入 `.env` 的 `API_TOKEN`，不打印到对话。ASR、HTTPS、CUDA Graph 和组合表达后端按用户需要单独配置。

5. 在部署目录构建镜像、预下载并校验完整模型快照，然后启动：

   ```bash
   docker compose config --quiet
   docker compose build
   docker run --rm --env-file .env -e HF_HOME=/models \
     -v "$PWD/models:/models" qwen3-tts-openai:local \
     python scripts/download_models.py --http --online
   docker compose up -d --no-build
   docker compose ps
   ```

   下载不需要 GPU，`--online` 只作用于本次下载，服务仍使用离线配置。模型在 `models/`，参考录音、任务和产物在 `data/`。失败后保留目录和下载缓存，根据失败步骤续跑；不执行 `down -v`、删除数据或反复重建。

6. 有界等待 `/ready`，模型首次加载可参考 `.env` 的 `MODEL_LOAD_TIMEOUT_SECONDS`。使用已安装 helper 的 `doctor --mcp` 检查服务身份、模型、音频工具、鉴权与 MCP。未就绪时检查 `docker compose logs --tail=100 qwen3_tts` 并脱敏反馈；`/health` 返回 200 仅代表存活。

7. 返回实际工作台入口、API 文档 `/docs`、MCP 地址 `/mcp/`、安装位置和部署提交。真实配音验收需要用户提供参考录音或选择已有音色；不能用测试音调代替模型听感验证。

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
