"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const state = {mode:"text", voices:[], plan:null, planRequest:null, planStale:false, submitting:false, jobId:null, editId:null, audioUrls:[], job:null, polling:false, health:null, sampleUrl:null, voicePreviewUrl:null, previewVoiceId:null, uploadFromStudio:false, voiceEntry:null};
const views = {studio:["配音工作台", "让每一段文字，都有合适的声音。"], voices:["音色管理", "保存参考录音，为每一次配音选择声音。"], jobs:["任务与产物", "查看生成记录，回听与下载已完成的音频。"], runtime:["运行状态", "查看模型就绪状态、计算设备与控制能力。"]};
const jobNames = {queued:"排队中", running:"执行中", succeeded:"已完成", failed:"失败", cancelled:"已取消"};
const kindNames = {narration:"文稿配音", voice_prepare:"音色提取", model_preload:"模型加载", speech:"短句合成"};
const voiceNames = {ready:"已就绪", pending:"待提取", failed:"提取失败"};
const capabilityNames = {voice_clone:"参考音色克隆", instruction_control:"表达指令", combined_clone_instruction:"克隆与表达组合", reference_conditioning:"参考模式", control_granularity:"控制粒度", word_alignment:"词级对齐", numeric_controls:"语速、音高与增益", acoustic_validation:"听感验收"};
const capabilityValues = {supported:"支持", unsupported:"不支持", experimental:"实验能力", "icl-and-x-vector":"完整参考 / 说话人特征", "x-vector-only-experimental":"说话人特征 · 实验", "presets-only":"预设音色", "sentence-or-short-paragraph":"句子或短段", "rubberband-and-ffmpeg":"Rubber Band / FFmpeg", "pending-human-listening":"待人工试听"};
const issueNames = {instruction_unsupported:"表达指令无法执行", instruction_numeric_conflict:"表达要求与数值控制冲突", unknown_control:"存在未实现的控制", unsupported_style:"表达风格无法映射", voice_mapping_required:"需要明确选择本地音色", phoneme_unsupported:"不支持音素输入", say_as_unsupported:"读法规则无法执行", experimental_opt_in:"组合表达需要明确启用实验", reference_mode_unsupported:"参考模式与实验能力不匹配", inline_alignment_unsupported:"句内控制需要词级对齐", ssml_element_unsupported:"存在未实现的 SSML 元素"};
function notice(message, error = false) {
  $("message").hidden = false; $("message").textContent = message; $("message").className = error ? "error" : "";
}
function showView(name) {
  document.querySelectorAll(".view").forEach(node => node.hidden = node.id !== `view-${name}`);
  document.querySelectorAll(".nav").forEach(node => {
    node.classList.toggle("active", node.dataset.view === name);
    if (node.dataset.view === name) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
  $("view-title").textContent = views[name][0]; $("view-description").textContent = views[name][1];
}
function showPanel(name) {
  document.querySelectorAll(".inspector-tab").forEach(node => {
    const active = node.dataset.panel === name;
    node.classList.toggle("active", active); node.setAttribute("aria-selected", String(active)); node.tabIndex = active ? 0 : -1;
  });
  $("panel-plan").hidden = name !== "plan"; $("panel-task").hidden = name !== "task";
}
function refreshVoiceEntryTabs() {
  const enabled = !!state.health?.features?.quick_voice_clone?.enabled;
  if (state.voiceEntry === null && state.health?.features) state.voiceEntry = enabled ? "quick" : "upload";
  const selected = state.voiceEntry === "quick" && enabled ? "quick" : "upload";
  $("voice-tab-quick").hidden = !enabled;
  document.querySelectorAll(".voice-entry-tab").forEach(node => {
    const active = node.dataset.voiceEntry === selected;
    node.classList.toggle("active", active); node.setAttribute("aria-selected", String(active)); node.tabIndex = active ? 0 : -1;
  });
  $("quick-clone-card").hidden = selected !== "quick";
  $("voice-upload-card").hidden = selected !== "upload";
}
function selectVoiceEntry(name) {
  if (name === "quick" && !state.health?.features?.quick_voice_clone?.enabled) return;
  if (name !== "quick") window.QuickClone?.leaveRecording();
  state.voiceEntry = name; refreshVoiceEntryTabs();
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if ($("api-token").value) headers.set("Authorization", "Bearer " + $("api-token").value);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {...options, headers});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = body.error || {type:`http_${response.status}`, message:response.statusText};
    const e = new Error(`[${error.type}] ${error.message}`); e.details = error.details; throw e;
  }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function bind(id, callback) { $(id).addEventListener("click", () => run(callback, $(id))); }
async function run(callback, button) {
  if (button) button.disabled = true;
  try { await callback(); } catch (e) { if (e.details?.units) renderPlan(e.details); notice(e.message, true); }
  finally { if (button) button.disabled = false; updateGenerationAvailability(); }
}
function parseObjectJSON(source, label) {
  const raw = source.replace(/^\uFEFF/, "");
  if (!raw.trim()) throw new Error(`${label}为空，请输入 JSON 对象。普通文章可以使用「文本配音」。`);
  let value;
  try { value = JSON.parse(raw); }
  catch (error) {
    const offset = error.message.match(/position (\d+)/), lineColumn = error.message.match(/line (\d+) column (\d+)/);
    let location = "";
    if (lineColumn) location = `（第 ${lineColumn[1]} 行，第 ${lineColumn[2]} 列）`;
    else if (offset) {
      const prefix = raw.slice(0, Number(offset[1])), lines = prefix.split("\n");
      location = `（第 ${lines.length} 行，第 ${lines.at(-1).length + 1} 列）`;
    }
    throw new Error(`${label} JSON 格式有误${location}。请检查双引号、逗号和括号是否完整。`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}必须是 JSON 对象（以 { 开头）。普通文章可以使用「文本配音」。`);
  return value;
}
function isPlainText(source) {
  const raw = source.replace(/^\uFEFF/, "").trim();
  if (!raw || /^[{\[\"]/.test(raw) || /^<(?:[!?]|[a-z][\w:.-]*(?:\s|>))/i.test(raw)) return false;
  try { JSON.parse(raw); return false; } catch { return true; }
}
function usePlainText(source) {
  const profile = parseObjectJSON($("profile-editor").value.trim() ? $("profile-editor").value : "{}", "音色配置");
  if (Object.keys(profile).length) throw new Error("检测到普通文章，但导演稿音色配置中已有参数。请切换到「文本配音」，粘贴正文并确认语速等设置；原配置会保留在导演稿页签。");
  const existing = $("text-editor").value;
  if (existing.trim() && existing !== source) throw new Error("检测到普通文章，但「文本配音」中已有另一份正文。请切换到「文本配音」后选择要使用的文稿，避免覆盖原文。");
  $("text-editor").value = source; selectMode("text");
  notice("已识别为普通文本，并切换到「文本配音」。正文已完整保留，可以直接预检和生成。");
}
function requestBody() {
  const value = {voice_id:$("voice-select").value || null, compatibility:$("compatibility").value, allow_degraded:$("allow-degraded").checked, allow_experimental:$("allow-experimental").checked, generation:{seed:Number($("seed").value)}};
  if (state.mode === "director") {
    const director = $("director-editor").value, profile = $("profile-editor").value.trim() ? $("profile-editor").value : "{}";
    if (isPlainText(director)) usePlainText(director);
    else {
      if (/^\s*(?:\uFEFF)?\s*</.test(director)) throw new Error("这看起来是 XML 或 SSML。请切换到「SSML」页签输入，导演稿页签用于 JSON。");
      value.narration_director = parseObjectJSON(director, "导演稿"); value.voice_profile = parseObjectJSON(profile, "音色配置");
      value.source_documents = {narration_director:director, voice_profile:profile};
    }
  }
  if (state.mode === "ssml") { value.ssml = $("ssml-editor").value; value.voice_mapping = parseObjectJSON($("voice-mapping").value.trim() ? $("voice-mapping").value : "{}", "音色名称映射"); }
  if (state.mode === "text") {
    value.text = $("text-editor").value;
    if (!value.text.trim()) throw new Error("请先输入要配音的正文，短句或整篇文章均可。");
    value.speed = Number($("text-speed").value); value.voice_profile = {instructions:$("text-instructions").value}; value.compatibility = "generic";
    if (!Number.isFinite(value.speed) || value.speed < .25 || value.speed > 4) throw new Error("语速倍率应在 0.25 到 4 之间。");
  }
  return value;
}
function updateGenerationAvailability() {
  const blocked = state.health?.model?.state === "failed" || state.health?.audio?.state === "failed" || state.health?.alive === false;
  $("submit-button").disabled = blocked || state.submitting;
  $("submit-label").textContent = state.submitting ? "处理中…" : "生成配音";
  $("submit-button").title = blocked ? "模型或音频工具不可用，请先查看运行诊断。预检仍可使用。" : "自动预检当前文稿，通过后生成配音";
  document.querySelectorAll("[data-audition], [data-page]").forEach(button => {
    button.disabled = blocked || state.submitting || state.planStale || !state.plan?.valid;
    button.title = blocked ? "模型尚未就绪" : state.planStale ? "文稿已变化，请重新预检" : "";
  });
}
function gpuLabel(model) {
  const selection = model.gpu_selection;
  if (selection?.host_index != null) return `宿主机 GPU ${selection.host_index}`;
  if (selection?.host_uuid) return `指定 GPU · ${selection.host_uuid.slice(4,12)}`;
  return model.device === "test-only" ? "受控测试后端" : "未指定宿主机 GPU";
}
function modelErrorDescription(model) {
  const error = model.error, details = error?.details;
  if (details?.driver_version_mismatch) return `NVIDIA 驱动版本不匹配：内核 ${details.kernel_driver}，用户态库 ${details.user_driver_library}。需修复驱动环境后重新加载模型。`;
  if (error?.type === "gpu_unavailable") return "CUDA 初始化失败。请检查 NVIDIA 驱动与用户态库版本，以及指定 GPU 的可用性。文稿预检仍可使用。";
  return typeof error === "string" ? error : error?.message || "查看运行诊断了解失败原因。文稿预检仍可使用。";
}
async function health() {
  let h;
  try {
    const response = await fetch("/health", {cache:"no-store"});
    if (!response.ok) throw new Error("无法读取服务状态"); h = await response.json();
  } catch (error) {
    state.health = {alive:false}; $("connection").textContent = "连接中断"; $("connection").className = "badge failed";
    $("model-alert").hidden = false; $("model-alert-title").textContent = "无法连接配音服务";
    $("model-alert-description").textContent = "请检查服务进程与网络连接；页面保留当前文稿。";
    updateGenerationAvailability(); window.QuickClone?.refresh(); return;
  }
  state.health = h;
  $("token-field").hidden = !h.api_auth_enabled;
  const model = h.model, selection = model.gpu_selection || {}, names = {unloaded:"待加载", loading:"加载中", ready:"已就绪", failed:"加载 / 推理失败"};
  const modelId = model.identity?.model_id || model.model_id || "—";
  const shortModel = modelId === "controlled-test-tone" ? "受控测试后端" : modelId.split("/").pop().replace("Qwen3-TTS-12Hz-", "");
  $("connection").textContent = h.alive ? "服务在线" : "服务启动中"; $("connection").className = "badge " + (h.alive ? "ready" : "");
  $("model-state").textContent = `${shortModel} · ${names[model.state] || model.state}`;
  $("model-state").className = model.state === "failed" ? "failed" : model.state === "ready" ? "ready" : ""; $("model-state").title = modelId;
  $("device-state").textContent = gpuLabel(model);
  $("device-state").title = [selection.host_name, selection.host_selector, `进程内编号 ${model.device}`].filter(Boolean).join(" · ");
  $("dtype-state").textContent = `${model.dtype === "float16" ? "FP16" : model.dtype} / ${model.attention}`;
  $("tool-state").textContent = h.audio.state === "ready" ? "已就绪" : h.audio.state === "failed" ? "不可用" : "检查中";
  $("tool-state").className = h.audio.state; $("runtime-dot").className = "status-dot " + model.state;
  $("runtime-dot").setAttribute("aria-label", `模型${names[model.state] || model.state}`);
  const failed = model.state === "failed" || h.audio.state === "failed"; $("model-alert").hidden = !failed;
  if (failed) {
    $("model-alert-title").textContent = model.state === "failed" ? "模型不可用 · 当前无法生成音频" : "音频工具不可用 · 当前无法生成音频";
    $("model-alert-description").textContent = model.state === "failed" ? modelErrorDescription(model) : "音频处理或编码检查失败，请查看完整诊断。";
  }
  $("runtime-detail").textContent = JSON.stringify(h, null, 2);
  $("capabilities").innerHTML = Object.entries(model.capabilities || {}).map(([key,value]) => `<div class="capability-row"><span>${esc(capabilityNames[key] || key)}</span><span class="badge ${value === "supported" ? "ready" : value === "unsupported" ? "failed" : value === "experimental" ? "warning" : ""}">${esc(capabilityValues[value] || value)}</span></div>`).join("");
  const runtimeStat = (title, value, detail, status = "") => `<div class="runtime-stat"><span>${esc(title)}</span><strong class="${status}">${esc(value)}</strong><small>${esc(detail)}</small></div>`;
  $("runtime-overview").innerHTML = runtimeStat("模型状态", names[model.state] || model.state, shortModel, model.state) + runtimeStat("计算设备", gpuLabel(model), selection.host_name || model.hardware?.gpu || "按服务配置选择") + runtimeStat("推理精度", model.dtype === "float16" ? "FP16" : model.dtype, `Attention · ${model.attention}`) + runtimeStat("音频处理", h.audio.state === "ready" ? "已就绪" : "不可用", "Rubber Band + FFmpeg", h.audio.state);
  const facts = [["宿主机设备",gpuLabel(model)], ["GPU UUID",selection.host_uuid || model.hardware?.uuid || "尚未检测"], ["进程内编号",model.device], ["模型可推理",h.inference_ready ? "是" : "否"], ["设备隔离",selection.isolated ? "仅指定 GPU 可见" : model.device === "test-only" ? "服务测试替身" : "未配置"]];
  $("device-detail").innerHTML = `<div class="device-facts">${facts.map(([key,value]) => `<div class="device-fact"><span>${esc(key)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>${selection.isolated ? `<p class="mapping-explanation">已隔离选择${esc(gpuLabel(model))}，进程中唯一可见的 GPU 会重新编号为 ${esc(model.device)}。设备选择与模型是否加载成功分别报告。</p>` : ""}${model.state === "failed" ? `<div class="runtime-error">${esc(modelErrorDescription(model))}<br><small>${esc(model.error?.type || "model_failed")}</small></div>` : ""}`;
  updateGenerationAvailability(); window.QuickClone?.refresh();
}
async function voices() {
  const result = await json("/v1/voices"); state.voices = result.voices;
  const previous = $("voice-select").value;
  $("voice-count").textContent = result.voices.length;
  $("voice-select").innerHTML = '<option value="">请选择本地音色</option>' + result.voices.map(v => `<option value="${esc(v.voice_id)}">${esc(v.name)} · ${esc(voiceNames[v.conditioning_state] || v.conditioning_state)}</option>`).join("");
  if (result.voices.some(v => v.voice_id === previous)) $("voice-select").value = previous;
  else if (result.voices.length) $("voice-select").value = result.voices[0].voice_id;
  if (previous !== $("voice-select").value) markPlanStale();
  $("preview-voice").disabled = !result.voices.some(v => v.voice_id === $("voice-select").value && !v.preset);
  $("voice-list").innerHTML = result.voices.length ? result.voices.map(v => `<div class="voice-item"><span class="voice-avatar">${icon("voice")}</span><div class="voice-body"><div class="voice-heading"><h3>${esc(v.name)}</h3></div><p title="${esc(v.voice_id)}">${esc(v.mode === "icl" ? "完整参考" : v.mode === "x_vector_only" ? "说话人特征" : "预设音色")}${v.reference?.duration_seconds ? ` · ${v.reference.duration_seconds.toFixed(1)} 秒` : ""}</p>${v.error ? `<p class="failed">${esc(v.error.message)}</p>` : ""}</div><div class="voice-actions"><span class="badge ${v.conditioning_state === "ready" ? "ready" : v.conditioning_state === "failed" ? "failed" : "warning"}">${esc(voiceNames[v.conditioning_state] || v.conditioning_state)}</span>${v.preset ? "" : `<button class="link-button" data-sample="${esc(v.voice_id)}">试听</button>`}<button class="secondary" data-select-voice="${esc(v.voice_id)}">使用</button>${v.preset ? "" : `<details class="voice-menu"><summary aria-label="${esc(v.name)}的更多操作">⋯</summary><div class="voice-menu-items"><button class="link-button" data-edit="${esc(v.voice_id)}">编辑对应文字</button><button class="link-button" data-prepare="${esc(v.voice_id)}">重新提取</button></div></details>`}</div><div class="sample-slot"></div></div>`).join("") : `<div class="empty-state"><svg class="icon empty-icon"><use href="#i-voice"/></svg><strong>还没有参考音色</strong><p>选择清晰录音，添加你的第一个音色。</p></div>`;
  $("voice-list").querySelectorAll("[data-select-voice]").forEach(b => b.onclick = () => { if ($("voice-select").value !== b.dataset.selectVoice) { markPlanStale(); clearVoicePreview(); } $("voice-select").value = b.dataset.selectVoice; $("preview-voice").disabled = !!state.voices.find(v => v.voice_id === b.dataset.selectVoice)?.preset; showView("studio"); });
  $("voice-list").querySelectorAll(".voice-menu").forEach(menu => menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    document.querySelectorAll(".voice-menu[open]").forEach(other => { if (other !== menu) other.open = false; });
    const anchor = menu.querySelector("summary").getBoundingClientRect(), height = menu.querySelector(".voice-menu-items").offsetHeight;
    menu.classList.toggle("open-up", innerHeight - anchor.bottom < height + 8 && anchor.top > height + 8);
  }));
  $("voice-list").querySelectorAll("[data-sample]").forEach(b => b.onclick = () => run(async () => {
    const slot = b.closest(".voice-item").querySelector(".sample-slot"); slot.innerHTML = '<audio controls aria-label="原始参考录音"></audio>';
    await playUrl(slot.querySelector("audio"), `/v1/voices/${b.dataset.sample}/sample`);
  }, b));
  $("voice-list").querySelectorAll("[data-edit]").forEach(b => b.onclick = () => {
    const v = state.voices.find(x => x.voice_id === b.dataset.edit); state.editId = v.voice_id;
    $("edit-voice-name").textContent = v.name; $("edit-ref-text").value = v.ref_text; $("edit-reference-mode").value = v.mode;
    b.closest("details").open = false; $("edit-voice-card").showModal();
  });
  $("voice-list").querySelectorAll("[data-prepare]").forEach(b => b.onclick = () => run(async () => {
    const job = await json(`/v1/voices/${b.dataset.prepare}/prepare`, {method:"POST"});
    await selectJob(job.job_id); notice("参考条件提取已加入队列");
  }, b));
}
function sourceMeta() {
  const source = $("director-editor").value;
  $("source-meta").textContent = source.length ? `${source.split("\n").length} 行 · ${source.length.toLocaleString()} 字符` : "等待输入文稿";
  const text = $("text-editor").value;
  $("text-meta").textContent = text.length ? `${text.length.toLocaleString()} 字符` : "等待输入正文";
  $("compatibility-field").hidden = state.mode === "text"; $("compatibility").disabled = state.mode === "text";
  $("expression-field").hidden = state.mode !== "text";
  $("settings-summary").textContent = `${state.mode === "text" ? "通用规则" : $("compatibility").value === "generic" ? "通用规则" : "原技能规则"} · Seed ${$("seed").value}`;
}
function markPlanStale() {
  sourceMeta();
  if (!state.plan) return;
  state.planStale = true; $("plan-summary").textContent = "文稿或设置已修改 · 请重新预检";
  $("plan-summary").className = "plan-summary failed"; $("step-plan").classList.remove("done");
  updateGenerationAvailability();
}
function renderPlan(plan) {
  state.plan = plan; state.planRequest = null; state.planStale = false;
  $("plan-empty").hidden = true; $("plan-summary").hidden = false; $("plan-overview").hidden = false;
  $("plan-summary").textContent = `${plan.valid ? "预检通过" : "预检未通过"} · ${plan.valid ? "可以检查分段并安排生成" : "请处理下方问题"}`;
  $("plan-summary").className = "plan-summary " + (plan.valid ? "" : "failed");
  $("plan-count").textContent = `${plan.pages.length} 页`;
  $("step-plan").classList.toggle("done", plan.valid);
  const stats = [[plan.pages.length,"文稿页数"], [plan.units.length,"生成单元"], [new Set(plan.units.map(u => u.voice_id)).size,"参考音色"]];
  $("plan-overview").innerHTML = stats.map(([value,label]) => `<div class="plan-stat"><strong>${value.toLocaleString()}</strong><span>${label}</span></div>`).join("");
  const errors = plan.issues.filter(i => i.severity === "error").length;
  $("plan-review").hidden = !plan.issues.length;
  $("plan-issues").innerHTML = plan.issues.length ? `<details class="disclosure issue-disclosure ${errors ? "has-error" : ""}" ${errors ? "open" : ""}><summary>${errors ? `${errors} 项需处理的问题` : `${plan.issues.length} 项控制与降级记录`}<span class="muted">展开查看</span></summary>${plan.issues.map(i => `<div class="issue ${esc(i.severity)}"><strong>${esc(issueNames[i.code] || i.code)}${i.degraded ? " · 已允许降级" : ""}</strong><p>${esc(i.message)}</p><code>${esc(i.code)} · ${esc(i.location)}</code></div>`).join("")}</details>` : "";
  $("plan-units").innerHTML = plan.pages.map((p,index) => {
    const units = plan.units.filter(u => u.page === p.page);
    const unitHTML = units.map(u => {
      const name = state.voices.find(v => v.voice_id === u.voice_id)?.name || u.voice_id;
      const c = u.postprocessing;
      return `<div class="unit"><div class="unit-heading"><span class="unit-number">段 ${String(u.segment).padStart(2,"0")}</span><span title="${esc(u.voice_id)}">${esc(name)}</span></div><p>${esc(u.text)}</p>${u.text === u.spoken_text ? "" : `<p class="spoken-text">实际读法 · ${esc(u.spoken_text)}</p>`}<div class="control-chips"><span class="control-chip">语速 ${c.tempo.toFixed(3)}×</span><span class="control-chip">音高 ${c.pitch_semitones > 0 ? "+" : ""}${c.pitch_semitones.toFixed(2)} st</span><span class="control-chip">增益 ${c.gain_db} dB</span></div><div class="unit-actions"><button class="link-button" data-audition="${u.page}:${u.segment}">${icon("play")}试生成此段</button></div></div>`;
    }).join("");
    return `<details class="plan-page" ${index === 0 ? "open" : ""}><summary><span class="page-number">${String(p.page).padStart(2,"0")}</span><span class="page-title"><strong>${esc(p.role || `第 ${p.page} 页`)}</strong><small>${units.length} 个单元${p.target_seconds ? ` · 参考时长 ${p.target_seconds} 秒` : ""}</small></span><span class="page-chevron" aria-hidden="true">›</span></summary>${p.direction ? `<div class="page-direction"><span>表达要求</span>${esc(p.direction)}</div>` : ""}${unitHTML}<div class="page-actions"><span>第 ${p.page} 页 · ${units.reduce((sum,u) => sum + u.text.length,0)} 字符</span><button class="secondary" data-page="${p.page}">${icon("play")}试生成此页</button></div></details>`;
  }).join("");
  $("plan-units").querySelectorAll("[data-audition],[data-page]").forEach(b => b.onclick = () => run(async () => {
    const req = requestBody();
    if (b.dataset.audition) { const [page,segment] = b.dataset.audition.split(":").map(Number); req.selection = {pages:[page], segments:[segment]}; }
    else req.selection = {pages:[Number(b.dataset.page)]};
    await submit(req);
  }, b));
  updateGenerationAvailability(); showPanel("plan");
}
async function compile() {
  const mode = state.mode, request = requestBody(), migrated = mode === "director" && state.mode === "text";
  await compileRequest(request);
  if (!migrated) $("message").hidden = true;
}
async function compileRequest(request) {
  const plan = await json("/v1/narrations/compile", {method:"POST", body:JSON.stringify(request)});
  renderPlan(plan); state.planRequest = JSON.stringify(request); return plan;
}
async function submit(req, preflight = false) {
  if (state.submitting) return;
  state.submitting = true; updateGenerationAvailability();
  try {
    if (preflight) {
      const signature = JSON.stringify(req);
      const plan = state.plan && !state.planStale && state.planRequest === signature ? state.plan : await compileRequest(req);
      if (JSON.stringify(requestBody()) !== signature) { markPlanStale(); notice("预检期间文稿或设置发生变化，请再次点击生成。正文已保留。", true); return; }
      if (!plan.valid) { showPanel("plan"); notice("文稿预检发现问题，请查看执行计划并处理后再生成。", true); return; }
    }
    const job = await json("/v1/narrations", {method:"POST", body:JSON.stringify(req)});
    await selectJob(job.job_id); notice("配音任务已提交，进度与音频将在右侧更新。");
  } finally { state.submitting = false; updateGenerationAvailability(); }
}
function releaseAudio() { state.audioUrls.forEach(url => URL.revokeObjectURL(url)); state.audioUrls = []; }
function clearVoicePreview() {
  if (state.voicePreviewUrl) URL.revokeObjectURL(state.voicePreviewUrl);
  state.voicePreviewUrl = null; state.previewVoiceId = null;
  $("selected-voice-preview").pause(); $("selected-voice-preview").removeAttribute("src"); $("selected-voice-preview").hidden = true;
}
async function previewVoice() {
  const voiceId = $("voice-select").value;
  if (!voiceId) throw new Error("请先选择参考音色。");
  const audio = $("selected-voice-preview");
  if (state.previewVoiceId !== voiceId) {
    const blob = await (await api(`/v1/voices/${voiceId}/sample`)).blob();
    if ($("voice-select").value !== voiceId) return;
    clearVoicePreview(); state.voicePreviewUrl = URL.createObjectURL(blob); state.previewVoiceId = voiceId;
    audio.src = state.voicePreviewUrl;
  }
  audio.hidden = false;
  try { await audio.play(); } catch { notice("参考录音已准备好，可使用播放器试听。"); }
}
async function selectJob(ident) {
  state.jobId = ident; state.job = null; releaseAudio(); $("artifacts").innerHTML = "";
  $("job-empty").hidden = true; $("job-content").hidden = false; $("active-job").textContent = `读取任务 · ${ident}`;
  $("job-error").hidden = true; $("job-result-note").hidden = true;
  $("job-status").textContent = "读取中"; $("job-percentage").textContent = "—"; $("job-progress").value = 0;
  showView("studio"); showPanel("task"); await pollJob();
}
function formatDate(seconds) { return new Date(seconds * 1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}); }
function progressText(job) {
  const detail = job.progress_detail;
  if (!detail || job.status !== "running") return "";
  const names = {preparing:"准备模型与音色", synthesizing:"生成音频", postprocessing:"处理语速与音量", unit_complete:"片段已生成", assembling:"拼接音频", encoding:"编码音频", saving:"保存产物"};
  const parts = [names[detail.stage] || "执行中"];
  if (detail.units_total > 1) parts.push(`第 ${detail.unit_index} / ${detail.units_total} 段`);
  if (detail.stage === "synthesizing" && Number.isFinite(detail.generated_audio_seconds)) parts.push(`已生成约 ${detail.generated_audio_seconds.toFixed(1)} 秒音频`);
  return parts.join(" · ");
}
async function pollJob() {
  if (!state.jobId || state.polling) return;
  state.polling = true;
  try {
    const job = await json(`/v1/jobs/${state.jobId}`);
    if (job.job_id !== state.jobId) return;
    $("active-job").textContent = `${jobNames[job.status] || job.status} · ${job.job_id}${job.cancel_requested && job.status === "running" ? "\n已请求取消，等待安全边界" : ""}`;
    $("job-status").textContent = jobNames[job.status] || job.status;
    $("job-status").className = "badge " + (job.status === "succeeded" ? "ready" : job.status === "failed" ? "failed" : job.status === "running" ? "warning" : "");
    $("job-kind").textContent = kindNames[job.kind] || job.kind;
    $("job-percentage").textContent = job.status === "failed" || job.status === "cancelled" ? "—" : `${job.progress_detail?.estimated && job.status === "running" ? "约 " : ""}${Math.round(job.progress * 100)}%`;
    $("job-progress").value = job.progress;
    $("job-progress-detail").textContent = progressText(job);
    $("job-progress-detail").hidden = !$("job-progress-detail").textContent;
    $("job-time").textContent = `提交于 ${formatDate(job.created_at)}`;
    $("cancel-button").hidden = !["queued","running"].includes(job.status);
    $("cancel-button").disabled = !!job.cancel_requested;
    $("task-indicator").hidden = false; $("task-indicator").className = "task-indicator " + (job.status === "failed" ? "failed" : "");
    $("step-audio").classList.toggle("done", job.status === "succeeded");
    $("job-error").hidden = !job.error;
    if (job.error) $("job-error").innerHTML = `<strong>${job.error.type === "gpu_unavailable" ? "GPU 不可用，未生成音频" : "任务执行失败"}</strong>${esc(job.error.message)}<br><small>${esc(job.error.type)}</small>`;
    const previous = state.job?.status; state.job = job;
    if (job.status === "succeeded" && previous !== "succeeded") {
      if (job.artifacts.length) await renderArtifacts(job);
      else { $("job-result-note").hidden = false; $("job-result-note").textContent = "任务已完成，此任务没有音频产物。"; }
    }
    if (["succeeded","failed","cancelled"].includes(job.status) && previous !== job.status) await voices();
  } finally { state.polling = false; }
}
async function playUrl(audio, url) {
  const blob = await (await api(url)).blob();
  if (!audio?.isConnected) return;
  const object = URL.createObjectURL(blob); state.audioUrls.push(object); audio.src = object;
}
function artifactName(artifact) {
  const names = {manifest:"生成记录", timeline:"时间轴", "execution-plan":"执行计划", "compiled-plan":"执行计划", "original-request":"原始请求", "source-input":"输入原文", "source-narration_director":"导演稿原文", "source-voice_profile":"音色配置原文"};
  return names[artifact.artifact_id] || artifact.artifact_id;
}
function downloadButton(artifact, label, className = "link-button") {
  return artifact ? `<button class="${className}" data-download="${esc(artifact.artifact_id)}">${esc(label || artifactName(artifact))}</button>` : "";
}
async function renderArtifacts(job) {
  releaseAudio();
  const artifacts = job.artifacts, full = artifacts.find(a => a.artifact_id === "full-mp3") || artifacts.find(a => a.artifact_id === "full-wav");
  const raw = artifacts.filter(a => /^unit-\d+-raw-wav$/.test(a.artifact_id));
  const pages = artifacts.filter(a => /^page-\d+-mp3$/.test(a.artifact_id));
  const records = artifacts.filter(a => a.media_type === "application/json");
  const playback = [full, ...pages].filter(Boolean);
  const fullWav = artifacts.find(a => a.artifact_id === "full-wav");
  $("artifacts").innerHTML = `<div class="artifact-group">${full ? `<div class="full-player"><div class="audio-heading"><h3>${icon("studio")}<span id="player-title">配音结果</span></h3><div class="audio-downloads">${downloadButton(full,"下载 MP3","secondary")}${downloadButton(fullWav,"WAV")}</div></div>${pages.length > 1 ? `<label class="comparison-select-label"><span class="sr-only">选择整稿或逐页试听</span><select id="playback-select" aria-label="选择整稿或逐页试听">${playback.map(a => `<option value="${esc(a.artifact_id)}">${a === full ? "整稿音频" : `第 ${Number(a.artifact_id.match(/page-(\d+)/)[1])} 页`}</option>`).join("")}</select></label>` : ""}<audio id="full-audio" controls aria-label="成品音频"></audio></div>` : ""}${raw.length ? `<details class="disclosure compare-disclosure" id="compare-disclosure"><summary>原始与处理后对照<span class="muted">${raw.length} 个单元</span></summary><div class="disclosure-body"><label class="comparison-select-label">选择片段<select id="comparison-select">${raw.map(a => `<option value="${esc(a.artifact_id.replace("-raw-wav",""))}">生成单元 ${Number(a.artifact_id.match(/unit-(\d+)/)[1])}</option>`).join("")}</select></label><div class="comparison"><label>模型原始音频<audio id="raw-audio" controls aria-label="模型原始 WAV"></audio><span id="raw-download"></span></label><label>数值处理后音频<audio id="processed-audio" controls aria-label="处理后 WAV"></audio><span id="processed-download"></span></label></div></div></details>` : ""}${pages.length > 1 ? `<details class="disclosure artifact-disclosure" open><summary>逐页音频<span class="muted">${pages.length} 页</span></summary><div class="disclosure-body page-downloads">${pages.map(a => {const wav = artifacts.find(x => x.artifact_id === a.artifact_id.replace(/-mp3$/, "-wav")); return `<div class="page-download-row"><span>第 ${Number(a.artifact_id.match(/page-(\d+)/)[1])} 页</span>${downloadButton(a,"MP3")}${downloadButton(wav,"WAV")}</div>`;}).join("")}</div></details>` : ""}${records.length ? `<details class="disclosure artifact-disclosure" id="audit-disclosure"><summary>执行记录与源文稿<span class="muted">${records.length} 个文件</span></summary><div class="disclosure-body"><div class="artifact-links">${records.map(a => downloadButton(a)).join("")}</div></div></details>` : ""}<p class="listen-note">请试听确认声音、术语读法与段落衔接。</p></div>`;
  async function compare() {
    const unit = $("comparison-select").value;
    for (const kind of ["raw","processed"]) {
      const artifact = artifacts.find(a => a.artifact_id === `${unit}-${kind}-wav`);
      $(`${kind}-download`).innerHTML = downloadButton(artifact,"下载 WAV");
      if (artifact) await playUrl($(`${kind}-audio`), artifact.url);
    }
  }
  $("artifacts").onclick = event => {
    const button = event.target.closest("[data-download]");
    if (!button) return;
    run(async () => {
      const artifact = artifacts.find(a => a.artifact_id === button.dataset.download);
      const blob = await (await api(artifact.url)).blob(), url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = artifact.artifact_id + (artifact.media_type === "application/json" ? ".json" : artifact.media_type === "audio/mpeg" ? ".mp3" : ".wav");
      link.click(); setTimeout(() => URL.revokeObjectURL(url),10000);
    }, button);
  };
  if (full) await playUrl($("full-audio"), full.url);
  if (state.jobId !== job.job_id) return;
  if ($("playback-select")) $("playback-select").onchange = () => run(async () => {
    const artifact = playback.find(a => a.artifact_id === $("playback-select").value);
    $("player-title").textContent = artifact === full ? "配音结果" : `第 ${Number(artifact.artifact_id.match(/page-(\d+)/)[1])} 页试听`;
    await playUrl($("full-audio"), artifact.url);
  });
  if (raw.length) { $("comparison-select").onchange = () => run(compare); await compare(); }
}
async function jobs() {
  const result = await json("/v1/jobs"); $("jobs-count").textContent = `${result.jobs.length} 项`;
  $("job-list").innerHTML = result.jobs.length ? result.jobs.map(j => `<tr><td class="job-table-id" title="${esc(j.job_id)}">${esc(j.job_id.slice(0,18))}…</td><td>${esc(kindNames[j.kind] || j.kind)}</td><td><span class="badge ${j.status === "succeeded" ? "ready" : j.status === "failed" ? "failed" : j.status === "running" ? "warning" : ""}">${esc(jobNames[j.status] || j.status)}</span></td><td><div class="table-progress"><progress value="${j.progress}" max="1" aria-label="任务进度"></progress>${Math.round(j.progress*100)}%</div></td><td class="muted">${formatDate(j.created_at)}</td><td><button class="link-button" data-job="${esc(j.job_id)}">查看 ${icon("arrow")}</button></td></tr>`).join("") : '<tr><td colspan="6" class="table-empty">还没有生成任务，先从工作台提交一份文稿。</td></tr>';
  $("job-list").querySelectorAll("[data-job]").forEach(b => b.onclick = () => run(() => selectJob(b.dataset.job)));
}
function selectMode(mode) {
  if (state.mode !== mode) markPlanStale(); state.mode = mode;
  document.querySelectorAll(".tab").forEach(node => {
    const active = node.dataset.mode === mode; node.classList.toggle("active", active); node.setAttribute("aria-selected",String(active)); node.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".mode").forEach(node => node.hidden = node.id !== `mode-${mode}`);
  sourceMeta();
}
function keyboardTabs(selector, activate) {
  document.querySelectorAll(selector).forEach(node => node.addEventListener("keydown", event => {
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
    event.preventDefault(); const tabs = [...document.querySelectorAll(selector)].filter(tab => !tab.hidden && !tab.disabled), index = tabs.indexOf(node);
    const target = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs.at(-1) : tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    activate(target); target.focus();
  }));
}
document.querySelectorAll(".nav").forEach(b => b.onclick = () => { state.uploadFromStudio = false; showView(b.dataset.view); if (b.dataset.view === "jobs") run(jobs); if (b.dataset.view === "voices") run(voices); });
document.querySelectorAll(".tab").forEach(b => b.onclick = () => selectMode(b.dataset.mode));
document.querySelectorAll(".inspector-tab").forEach(b => b.onclick = () => showPanel(b.dataset.panel));
document.querySelectorAll(".voice-entry-tab").forEach(b => b.onclick = () => selectVoiceEntry(b.dataset.voiceEntry));
document.addEventListener("click", event => { if (!event.target.closest(".voice-menu")) document.querySelectorAll(".voice-menu[open]").forEach(menu => menu.open = false); });
document.addEventListener("keydown", event => { if (event.key === "Escape") document.querySelectorAll(".voice-menu[open]").forEach(menu => menu.open = false); });
keyboardTabs(".tab", node => selectMode(node.dataset.mode)); keyboardTabs(".inspector-tab", node => showPanel(node.dataset.panel));
keyboardTabs(".voice-entry-tab", node => selectVoiceEntry(node.dataset.voiceEntry));
for (const [file,editor] of [["director-file","director-editor"], ["profile-file","profile-editor"]]) {
  $(file).onchange = () => run(async () => {
    const f = $(file).files[0]; if (!f) return;
    if (f.size > 2000000) throw new Error("JSON 文件过大，请按服务文本上限拆分");
    const source = new TextDecoder("utf-8", {fatal:true,ignoreBOM:true}).decode(await f.arrayBuffer());
    parseObjectJSON(source, file === "director-file" ? "导演稿" : "音色配置"); $(editor).value = source;
    if (file === "profile-file") $("profile-disclosure").open = true;
    markPlanStale(); notice(`已导入 ${f.name}`);
  });
}
$("sample-file").onchange = () => {
  const f = $("sample-file").files[0]; if (state.sampleUrl) URL.revokeObjectURL(state.sampleUrl);
  $("sample-preview").hidden = !f;
  $("sample-name").textContent = f ? f.name : "选择参考录音";
  if (f) {
    state.sampleUrl = URL.createObjectURL(f); $("sample-preview").src = state.sampleUrl;
    if (!$("voice-name").value.trim()) $("voice-name").value = f.name.replace(/\.wav$/i, "").slice(0,100);
  }
};
$("voice-form").onsubmit = event => {
  event.preventDefault(); run(async () => {
    const form = new FormData($("voice-form"));
    if (form.get("mode") === "auto") form.set("mode", String(form.get("ref_text") || "").trim() ? "icl" : "x_vector_only");
    const v = await json("/v1/voices", {method:"POST",body:form});
    await voices(); $("voice-select").value = v.voice_id; clearVoicePreview(); markPlanStale();
    $("voice-form").reset(); $("sample-name").textContent = "选择参考录音";
    $("sample-preview").pause(); $("sample-preview").removeAttribute("src"); $("sample-preview").hidden = true;
    if (state.sampleUrl) URL.revokeObjectURL(state.sampleUrl); state.sampleUrl = null;
    if (state.uploadFromStudio) { state.uploadFromStudio = false; showView("studio"); }
    notice("音色已保存，参考条件提取已加入队列。");
  }, $("voice-form").querySelector('button[type="submit"]'));
};
for (const id of ["director-editor","profile-editor","ssml-editor","voice-mapping","text-editor","text-speed","text-instructions","seed"]) $(id).addEventListener("input", markPlanStale);
for (const id of ["voice-select","compatibility","allow-degraded","allow-experimental"]) $(id).addEventListener("change", markPlanStale);
$("voice-select").addEventListener("change", () => { clearVoicePreview(); $("preview-voice").disabled = !state.voices.some(v => v.voice_id === $("voice-select").value && !v.preset); });
document.querySelectorAll("[data-speed-step]").forEach(button => button.onclick = () => {
  const speed = $("text-speed"), current = Number(speed.value);
  speed.value = Math.max(.25,Math.min(4,(Number.isFinite(current) ? current : 1) + Number(button.dataset.speedStep))).toFixed(2);
  markPlanStale();
});
bind("go-upload", () => { state.uploadFromStudio = true; showView("voices"); selectVoiceEntry("upload"); $("sample-file").click(); $("voice-form").scrollIntoView({block:"nearest"}); });
bind("preview-voice", previewVoice); bind("refresh-voices", voices); bind("refresh-jobs", jobs);
bind("compile-button", compile); bind("submit-button", () => submit(requestBody(), true)); bind("refresh-button", pollJob);
bind("cancel-button", async () => { await json(`/v1/jobs/${state.jobId}/cancel`, {method:"POST"}); await pollJob(); });
bind("close-edit", () => $("edit-voice-card").close());
bind("save-ref-text", async () => {
  await json(`/v1/voices/${state.editId}`, {method:"PATCH",body:JSON.stringify({ref_text:$("edit-ref-text").value,mode:$("edit-reference-mode").value})});
  $("edit-voice-card").close(); markPlanStale(); await voices(); notice("参考文字已保存，受影响的参考条件会重新提取。");
});
bind("load-example", async () => {
  const responses = await Promise.all([fetch("/examples/narration_director.json"),fetch("/examples/voice_profile.json")]);
  for (const response of responses) if (!response.ok) throw new Error("示例文件读取失败");
  const [director,profile] = await Promise.all(responses.map(response => response.text()));
  $("director-editor").value = director; $("profile-editor").value = profile; markPlanStale();
});
bind("load-ssml", async () => {
  const response = await fetch("/examples/minimal.ssml"); if (!response.ok) throw new Error("SSML 示例读取失败");
  $("ssml-editor").value = await response.text(); $("voice-mapping").value = JSON.stringify({"reference-speaker":$("voice-select").value},null,2); markPlanStale();
});
bind("inspect-error", () => { showView("runtime"); $("diagnostic-disclosure").open = true; });
$("api-token").onchange = () => run(async () => { await voices(); $("message").hidden = true; });
sourceMeta(); run(async () => { await health(); await voices(); });
setInterval(() => run(health), 3000);
setInterval(() => {
  if (state.jobId && (!state.job || ["queued", "running"].includes(state.job.status))) run(pollJob);
}, 500);
