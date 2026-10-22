"use strict";

window.McpPanel = (() => {
  let checked = false, checking = false, generation = 0;
  const toolLabels = {
    list_voices:["查询音色", "列出已保存音色或当前后端的预设音色，确认音色 ID 和准备状态。"],
    submit_narration:["提交配音", "接收普通文字、导演稿或 SSML，返回持久化任务编号；提交成功不等于音频已生成。"],
    get_job:["查询任务与产物", "查询同一任务的状态、进度与产物下载链接，不在 MCP 响应中传输音频。"],
  };
  const endpoint = () => new URL("/mcp/", location.origin).href;

  function refreshContext() {
    if ($("mcp-endpoint").value !== endpoint()) $("mcp-endpoint").value = endpoint();
    const auth = state.health?.api_auth_enabled;
    $("mcp-auth-state").textContent = auth === true ? "Bearer Token" : auth === false ? "未启用" : "尚未读取";
    $("mcp-auth-help").textContent = auth === true ? "连接检查使用左下方填写的 API Token；智能体客户端也需配置相同的鉴权凭据。" : auth === false ? "当前服务未启用接口鉴权，无需填写 Token。" : "服务鉴权状态尚未读取，请确认服务在线后重试。";
    const server = {type:"http", url:endpoint()};
    if (auth !== false) server.headers = {Authorization:"Bearer <API_TOKEN>"};
    const config = JSON.stringify({mcpServers:{"qwen3-tts-openai":server}}, null, 2);
    if ($("mcp-config").textContent !== config) $("mcp-config").textContent = config;
    $("mcp-config-help").textContent = auth === false ? "示例使用当前访问地址；服务未启用鉴权，因此不包含请求头。" : "将 <API_TOKEN> 替换为客户端安全提供的凭据。示例与复制内容不会带入你填写的真实 Token。";
  }

  async function rpc(method, params, id, protocol) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const headers = {Accept:"application/json, text/event-stream"};
      if (protocol) headers["MCP-Protocol-Version"] = protocol;
      const body = {jsonrpc:"2.0", method};
      if (id !== undefined) body.id = id;
      if (params !== undefined) body.params = params;
      const response = await api("/mcp/", {method:"POST", headers, body:JSON.stringify(body), signal:controller.signal, cache:"no-store", redirect:"error"});
      if (id === undefined) { await response.text(); return null; }
      const reply = await response.json();
      if (reply.error) throw new Error("MCP 返回协议错误，请检查服务端日志。");
      if (!reply.result || reply.id !== id) throw new Error("MCP 响应格式不符合当前服务协议。");
      return reply.result;
    } finally { clearTimeout(timeout); }
  }

  function setStatus(label, message, style = "") {
    $("mcp-state").textContent = label; $("mcp-state").className = "badge " + style;
    $("mcp-check-result").textContent = message; $("mcp-check-result").className = "mcp-check-result " + style;
  }

  function clearTools(message) {
    $("mcp-tools").replaceChildren(); $("mcp-tool-count").textContent = "等待发现";
    $("mcp-tools-empty").hidden = false; $("mcp-tools-empty").textContent = message;
  }

  function renderTools(tools) {
    $("mcp-tools-empty").hidden = tools.length > 0;
    $("mcp-tools-empty").textContent = "服务没有返回可用工具。";
    $("mcp-tool-count").textContent = `${tools.length} 个工具`;
    $("mcp-tools").innerHTML = tools.map(tool => {
      const [title, description] = toolLabels[tool.name] || [tool.name, tool.description || "服务未提供工具说明。"];
      return `<article class="mcp-tool"><div class="mcp-tool-heading"><h3>${esc(title)}</h3><code>${esc(tool.name)}</code></div><p>${esc(description)}</p><details class="disclosure"><summary>查看参数 JSON Schema</summary><pre tabindex="0">${esc(JSON.stringify(tool.inputSchema, null, 2))}</pre></details></article>`;
    }).join("");
  }

  async function check() {
    if (checking) return;
    const attempt = ++generation;
    checking = true; $("check-mcp").disabled = true;
    clearTools("正在从服务发现工具…");
    setStatus("检查中", "正在初始化 MCP 并读取工具目录，不提交配音任务。");
    try {
      const initialized = await rpc("initialize", {protocolVersion:"2025-03-26", capabilities:{}, clientInfo:{name:"qwen3-tts-workbench", version:"1.0.0"}}, 1);
      const protocol = initialized.protocolVersion;
      if (typeof protocol !== "string" || !protocol) throw new Error("MCP 未返回协商后的协议版本。");
      await rpc("notifications/initialized", undefined, undefined, protocol);
      const result = await rpc("tools/list", {}, 2, protocol);
      if (!Array.isArray(result.tools) || result.tools.some(t => !t || typeof t.name !== "string" || !t.inputSchema)) throw new Error("MCP 工具目录格式无效。");
      if (attempt !== generation) return;
      renderTools(result.tools);
      setStatus("已连接", `已发现 ${result.tools.length} 个工具 · 协议 ${protocol}。模型是否就绪请查看运行状态。`, "ready");
      checked = true;
    } catch (error) {
      if (attempt !== generation) return;
      let message;
      if (error.status === 401 || error.status === 403) message = "鉴权未通过。请在左下方填写有效 API Token，再检查连接。";
      else if (error.status === 404) message = "未找到 MCP 入口。请检查 ENABLE_MCP=1 及代理的 /mcp/ 转发配置；修改服务配置后需重启生效。";
      else if (error.name === "AbortError") message = "MCP 连接检查超时，请确认服务地址和网络可达后重试。";
      else message = "MCP 连接检查失败。请确认服务在线、代理支持 Streamable HTTP，并检查服务端日志后重试。";
      setStatus("未连接", message, "warning"); clearTools("连接未通过，暂不能确认可用工具。");
      checked = true;
    } finally {
      checking = false; $("check-mcp").disabled = false;
      if (attempt !== generation && !$("view-mcp").hidden) check();
    }
  }

  async function copyText(id, label) {
    const source = $(id), text = source.value ?? source.textContent;
    try {
      await copyToClipboard(text);
      notice(`${label}已复制。`);
    } catch {
      if (source.select) source.select();
      else { const range = document.createRange(); range.selectNodeContents(source); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); }
      notice("浏览器未允许自动复制，内容已选中，请手动复制。");
    }
  }

  $("check-mcp").addEventListener("click", check);
  $("copy-mcp-endpoint").addEventListener("click", () => copyText("mcp-endpoint", "MCP 地址"));
  $("copy-mcp-config").addEventListener("click", () => copyText("mcp-config", "配置示例"));
  $("mcp-call-example").textContent = JSON.stringify({name:"submit_narration", arguments:{request:{text:"这是通过智能体提交的配音文稿。", voice_id:"<voice_id>", language:"Chinese", compatibility:"generic"}}}, null, 2);
  refreshContext();
  return {
    refreshContext,
    show() { refreshContext(); if (!checked) check(); },
    credentialsChanged() {
      generation++; checked = false; clearTools("鉴权信息已更新，等待重新检查连接。");
      setStatus("待检查", "鉴权信息已更新。");
      if (!$("view-mcp").hidden) check();
    },
  };
})();
if (state.view === "mcp") window.McpPanel.show();
