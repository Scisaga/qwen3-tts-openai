# 音色、配音与产物

先确认服务根地址。所有 helper 的 `--url`、`--token-env`、`--timeout` 是全局参数，放在子命令前。默认 URL 是 `QWEN_TTS_URL` 或 `http://127.0.0.1:8000`；Token 从 `QWEN_TTS_API_TOKEN` 读取。自定义变量可使用 `--token-env API_TOKEN`。

## 选择或添加音色

```bash
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py voices
```

按实际 `voice_id`、名称、参考模式与 `conditioning_state` 选择音色；同名有歧义时先确定目标。Base 使用注册音色，不使用 `alloy` 等 OpenAI 预置名。只有显式配置为 CustomVoice 的服务接受音色列表返回的 `preset:<speaker>`。

没有音色时，在工作台添加用户提供的、有权使用的清晰参考录音，或用 REST 上传 WAV。默认时长范围 1–30 秒；实际限制以服务配置为准。完整参考 `icl` 的 `ref_text` 必须对应实际录音；没有文字时使用 `x_vector_only`，不要伪造转写。

以下 Bash 示例在已设置 `QWEN_TTS_API_TOKEN` 的环境中运行，不在提示词中填写真实值；未启用鉴权时省略鉴权头：

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/voices \
  -H "Authorization: Bearer $QWEN_TTS_API_TOKEN" \
  -F file=@reference.wav \
  -F 'name=我的讲解音色' \
  -F 'ref_text=参考录音里实际说出的文字。' \
  -F mode=icl
```

保存返回的 `voice_id` 和 `preparation_job_id`。等待准备任务 `succeeded`，重新查询音色，确认 `conditioning_state=ready` 后提交配音。队列失败时音色可能已经保存，先查列表，不盲目重复上传。

快速复刻是可选流程：需要服务使用 `qwen-base` 并配置 ASR 服务根地址 `ASR_BASE_URL`（不带 `/v1`）。`POST /v1/voices/references` 上传短录音得到 `draft_id` 和转写；用户回听并确认文字后，再向 `POST /v1/voices` 提交 `draft_id`、`name`、`ref_text` 和 `mode=icl`。ASR 故障时可使用 `transcribe=false` 和用户确认的文字，保留同一份草稿。未要求快速复刻时无需部署 ASR。

## 普通文字配音

将以下示例的音色 ID 替换为查询返回的真实值，保存为 `request.json`。正文保留原文、空白和换行；`speed` 只适用于普通文字。

```json
{
  "text": "你好，欢迎使用本地配音工作台。",
  "voice_id": "<列表返回的 voice_id>",
  "language": "Chinese",
  "compatibility": "generic",
  "speed": 1
}
```

```bash
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py compile --request request.json
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py submit --request request.json
```

`compile` 不调用 GPU。检查 `valid`、分段、读法、控制方式和 issues；`submit` 会再次预检，通过后仅提交一次。不要为跳过预检静默增加 `allow_degraded=true`。Base 不支持自由表达指令，语速、音高和增益通过音频后处理实现。

保存提交响应中的 `job_id`，以下 `<job_id>` 均替换为它：

```bash
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py job '<job_id>' --wait --wait-seconds 600
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py download '<job_id>' --artifact full-mp3 --output narration.mp3
python3 ~/.agents/skills/qwen3-tts-openai/scripts/tts_skill.py download '<job_id>' --artifact full-wav --output narration.wav
```

只有 `succeeded` 表示生成完成；失败和取消时检查 `error`。等待超时返回码为 3，并保留服务器任务；用同一 ID 继续查询。提交响应丢失时先用 `jobs` 检查已有任务，核对创建时间、工作台记录与原始输入，再决定是否重试。

下载 helper 只使用该任务返回的产物链接，拒绝重定向，校验 `bytes` 和 SHA-256，不覆盖已存在的本地文件。音频、时间轴、执行记录和输入原文均从实际 `artifacts` 中选择，不猜文件路径或制造下载 URL。目录需提前创建。

## 导演稿与 SSML

`text`、`narration_director`、`ssml` 必须且只能提供一个。导演稿使用用户的完整对象和 `voice_profile`；SSML 使用 `voice_mapping` 将其中的音色名称映射到实际 ID。不要为“修复”预检而改写原文或丢弃控制字段。需要保留 JSON 原始格式时，将原始字符串附在 `source_documents.narration_director` 或 `source_documents.voice_profile` 中，其解析结果必须与对应对象相同。

具体输入结构从部署 checkout 的 `examples/narration_director.json`、`examples/voice_profile.json`、`examples/minimal.ssml`、`contracts.py` 或在线 `/docs` 获取，不凭名称补造字段。实例不支持的表达控制、逐字对齐和 SSML 标签不能承诺实现。长稿使用异步任务；局部试听使用服务支持的 `selection.pages` 和 `selection.segments`，下标从 1 开始。

## 使用 MCP

先发现连接中的真实工具 schema，当前服务只提供：

| 工具 | 用途 |
| --- | --- |
| `list_voices` | 查询音色和准备状态 |
| `submit_narration` | 提交与 REST 相同的 `NarrationRequest`，位于 `request` 参数内 |
| `get_job` | 使用 `job_id` 查询进度和产物 |

MCP 没有预检或参考录音上传工具；先用 REST helper 预检、工作台或 REST 上传，再调用 MCP。一次普通文字调用的参数形状为：

```json
{
  "name": "submit_narration",
  "arguments": {
    "request": {
      "text": "你好，欢迎使用本地配音工作台。",
      "voice_id": "<列表返回的 voice_id>",
      "language": "Chinese",
      "compatibility": "generic"
    }
  }
}
```

通过 MCP 已提交后，不再运行 REST `submit`。`get_job` 与 REST helper 可查询同一任务；下载相对链接时使用同一服务根地址和 API Token。

## 常见故障

| 现象 | 检查与处理 |
| --- | --- |
| `/health` 在线但 `/ready` 为 503 | 检查 `model.state`、`audio.state`、模型快照、GPU 和容器日志；不要重复提交 |
| HTTP 401 | 检查当前进程是否获得正确 Token 环境变量；不要输出其值 |
| REST 正常而 MCP 失败 | 检查 `ENABLE_MCP=1`、结尾 `/mcp/`、客户端网络和鉴权；`doctor --mcp` 完成工具发现 |
| 音色仍在准备或失败 | 查询 `preparation_job_id` 和音色准备错误，解决原因后显式重试准备，保留已存音色 |
| 预检报告不支持的控制 | 解释具体 issue；只有用户接受后才启用相应降级，不启用实验后端作为修复 |
| 等待或提交超时 | 保留任务 ID，查询 `job` 或 `jobs`，不自动重发配音 |
| 下载哈希或大小不符 | helper 删除本次残留文件；检查服务存储与原任务，不把损坏文件当成成功 |
| 内网 HTTP 不能录音 | 使用已有可信 HTTPS、localhost 或上传录音；HTTPS 代理配置见部署仓库文档 |

交付时给出最终状态、音色与语言、实际 MP3/WAV 路径和已知问题。模型就绪与产物校验不能证明音色相似度、长稿完整性或实际听感；只描述已执行的验证。
