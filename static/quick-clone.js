"use strict";
(() => {
  const qc = {draft:null, file:null, filename:"", previewUrl:null, resultUrl:null, recorder:null, stream:null,
    timer:null, started:0, starting:false, busy:false, creating:false, auditioning:false, voiceId:null,
    ready:false, sequence:0, controller:null, auditionJob:null};
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const terminal = status => ["succeeded", "failed", "cancelled"].includes(status);
  const feature = () => state.health?.features?.quick_voice_clone;
  const canRecord = () => window.isSecureContext && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";
  const recording = () => qc.recorder?.state === "recording";
  function status(message, error = false) {
    $("quick-status").textContent = message;
    $("quick-status").className = "quick-status" + (error ? " failed" : "");
  }
  function refresh() {
    const enabled = !!feature()?.enabled;
    refreshVoiceEntryTabs(); $("go-quick-clone").hidden = !enabled;
    const available = feature()?.available;
    $("quick-asr-state").textContent = available ? "自动识别已就绪" : feature()?.status === "unchecked" ? "检查自动识别" : "自动识别暂不可用";
    $("quick-asr-state").className = "badge " + (available ? "ready" : "warning");
    const limit = feature()?.max_reference_seconds || 30;
    $("quick-recording-hint").textContent = `在安静环境自然说一段话，建议 10–20 秒，最长 ${limit} 秒。`;
    $("quick-context-note").hidden = canRecord();
    $("quick-context-message").textContent = !window.isSecureContext
      ? "当前页面使用 HTTP，浏览器无法开启麦克风。请通过 HTTPS 或 localhost 打开，也可以直接上传短录音。"
      : "当前浏览器不支持网页录音，请使用支持录音的浏览器，或上传已有的短录音。";
    const httpsUrl = feature()?.https_url;
    $("quick-secure-actions").hidden = window.isSecureContext || !httpsUrl;
    if (httpsUrl) $("quick-secure-link").href = httpsUrl + "/voices?tab=quick";
    const localCa = !!feature()?.https_local_ca;
    $("quick-ca-link").hidden = !localCa; $("quick-ca-hint").hidden = window.isSecureContext || !httpsUrl || !localCa;
    const locked = qc.busy || qc.creating || qc.auditioning;
    const expired = qc.draft && qc.draft.expires_at * 1000 <= Date.now();
    const openSecurePage = !window.isSecureContext && !!httpsUrl;
    $("quick-record").disabled = !enabled || (!canRecord() && !openSecurePage) || locked || qc.starting;
    $("quick-record").hidden = recording(); $("quick-stop").hidden = !recording();
    $("quick-record-label").textContent = openSecurePage ? "打开录音页面" : qc.starting ? "等待麦克风…" : qc.file ? "重新录音" : "开始录音";
    $("quick-file").disabled = !enabled || locked || qc.starting || recording();
    $("quick-file-label").classList.toggle("disabled", $("quick-file").disabled);
    $("quick-retry").disabled = !enabled || (!qc.draft && !qc.file) || locked || recording() || qc.starting || !!qc.voiceId;
    $("quick-reset").disabled = qc.creating || !(qc.draft || qc.file || qc.starting || recording());
    $("quick-name").disabled = qc.creating;
    $("quick-voice-id").disabled = qc.creating || !!qc.voiceId;
    $("quick-manual").disabled = qc.creating || !!qc.voiceId;
    $("quick-ref-text").disabled = qc.busy || qc.creating || recording();
    $("quick-create").disabled = !enabled || !qc.draft || expired || locked || recording() || qc.starting || !!qc.voiceId
      || !$("quick-name").value.trim() || !$("quick-ref-text").value.trim();
    $("quick-retry").textContent = expired ? "重新上传并识别" : "重新识别文字";
    $("quick-create").textContent = qc.creating ? "提交音色…" : "生成音色";
    $("quick-audition").disabled = !qc.ready || qc.auditioning;
    $("quick-audition").textContent = qc.auditioning ? "正在生成试听…" : "用新句子试听";
    $("quick-use").disabled = !qc.ready;
  }
  function stopTracks() {
    if (qc.timer) clearInterval(qc.timer); qc.timer = null;
    qc.stream?.getTracks().forEach(track => track.stop()); qc.stream = null;
    $("quick-timer").classList.remove("recording");
  }
  function clearPreview() {
    $("quick-sample-preview").pause(); $("quick-sample-preview").removeAttribute("src");
    $("quick-sample-preview").hidden = true;
    if (qc.previewUrl) URL.revokeObjectURL(qc.previewUrl); qc.previewUrl = null;
  }
  function preview(blob) {
    clearPreview(); qc.previewUrl = URL.createObjectURL(blob);
    $("quick-sample-preview").src = qc.previewUrl; $("quick-sample-preview").hidden = false;
  }
  function clearResult() {
    $("quick-result-audio").pause(); $("quick-result-audio").removeAttribute("src"); $("quick-result-audio").hidden = true;
    if (qc.resultUrl) URL.revokeObjectURL(qc.resultUrl); qc.resultUrl = null;
    $("quick-result").hidden = true; $("quick-prepare").hidden = true;
    if (qc.voiceId) $("quick-voice-id").value = "";
    $("quick-result-voice-id").textContent = "";
    qc.voiceId = null; qc.ready = false;
  }
  function discardDraft() {
    if (qc.draft) api(`/v1/voices/references/${qc.draft.draft_id}`, {method:"DELETE"}).catch(() => {});
    qc.draft = null;
  }
  function reset() {
    qc.sequence++; qc.controller?.abort(); qc.controller = null;
    if (recording()) qc.recorder.stop(); qc.recorder = null; qc.starting = false; stopTracks();
    if (qc.auditionJob) json(`/v1/jobs/${qc.auditionJob}/cancel`, {method:"POST"}).catch(() => {});
    qc.auditionJob = null; discardDraft(); clearPreview(); clearResult();
    qc.file = null; qc.filename = ""; qc.busy = false; qc.auditioning = false;
    $("quick-file").value = ""; $("quick-ref-text").value = ""; $("quick-timer").textContent = "00:00";
    status("录音结束后会自动识别文字，也可以上传已有录音。"); refresh();
  }
  function defaultName() {
    const now = new Date(), pad = value => String(value).padStart(2, "0");
    return `我的音色 ${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  }
  function applyDraft(draft, previousText) {
    qc.draft = draft;
    if ($("quick-ref-text").value === previousText && draft.ref_text) $("quick-ref-text").value = draft.ref_text;
    const duration = draft.reference.duration_seconds.toFixed(1);
    if (draft.asr_error) status(`${duration} 秒录音已保留。${draft.asr_error.message}`, true);
    else if (draft.transcription_state === "pending") status(`${duration} 秒录音已保存，请填写录音对应文字后生成音色。`);
    else status(`${duration} 秒录音 · 文字已识别，请回听并确认后生成音色。`);
  }
  async function upload(file, filename, preserveText = false) {
    if (file.size > (feature()?.max_upload_bytes || 20 * 1024 * 1024)) {
      status("录音文件过大，请选择更短的录音。", true); return;
    }
    qc.sequence++; const sequence = qc.sequence;
    qc.controller?.abort(); discardDraft(); clearResult(); qc.file = file; qc.filename = filename;
    qc.controller = new AbortController(); qc.busy = true;
    if (!preserveText) $("quick-ref-text").value = "";
    const previousText = $("quick-ref-text").value;
    if (!$("quick-name").value.trim()) $("quick-name").value = defaultName();
    preview(file); status("正在处理录音并识别文字…"); refresh();
    try {
      const body = new FormData(); body.set("file", file, filename);
      body.set("transcribe", $("quick-manual").checked ? "false" : "true");
      const draft = await json("/v1/voices/references", {method:"POST", body, signal:qc.controller.signal});
      if (sequence !== qc.sequence) return;
      applyDraft(draft, previousText);
      const response = await api(draft.sample_url, {signal:qc.controller.signal});
      const wav = await response.blob(); if (sequence === qc.sequence) preview(wav);
    } catch (error) {
      if (sequence === qc.sequence && error.name !== "AbortError") status(`处理未完成：${error.message} 可以重新识别或重新录音。`, true);
    } finally {
      if (sequence === qc.sequence) { qc.busy = false; qc.controller = null; refresh(); }
    }
  }
  async function retry() {
    $("quick-manual").checked = false;
    if (!qc.draft || qc.draft.expires_at * 1000 <= Date.now()) { if (qc.file) await upload(qc.file, qc.filename, true); return; }
    const sequence = qc.sequence, previousText = $("quick-ref-text").value;
    qc.busy = true; qc.controller = new AbortController(); status("正在重新识别录音…"); refresh();
    try {
      const draft = await json(`/v1/voices/references/${qc.draft.draft_id}/transcribe`, {method:"POST", signal:qc.controller.signal});
      if (sequence === qc.sequence) applyDraft(draft, previousText);
    } catch (error) {
      if (sequence === qc.sequence && error.name !== "AbortError") status(`识别未完成：${error.message}`, true);
    } finally {
      if (sequence === qc.sequence) { qc.busy = false; qc.controller = null; refresh(); }
    }
  }
  async function startRecording() {
    if (!canRecord() || qc.starting || recording()) return;
    reset(); qc.starting = true; const sequence = qc.sequence;
    status("请允许浏览器使用麦克风。"); refresh();
    let stream;
    try {
      document.querySelectorAll("audio").forEach(audio => audio.pause());
      stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1}});
      if (sequence !== qc.sequence || $("view-voices").hidden) { stream.getTracks().forEach(track => track.stop()); return; }
      const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mime ? {mimeType:mime} : {}), chunks = [];
      qc.stream = stream; qc.recorder = recorder; qc.starting = false; qc.started = performance.now();
      recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      recorder.onerror = () => { if (sequence === qc.sequence) { reset(); status("录音中断，请检查麦克风后重试。", true); } };
      recorder.onstop = () => {
        stream.getTracks().forEach(track => track.stop()); if (sequence !== qc.sequence) return;
        stopTracks();
        qc.recorder = null;
        const type = recorder.mimeType || mime || "audio/webm", extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
        const blob = new Blob(chunks, {type});
        if (!blob.size) { status("未收到录音，请检查麦克风后重新录制。", true); refresh(); return; }
        upload(blob, `recording.${extension}`);
      };
      recorder.start(250); $("quick-timer").classList.add("recording");
      status("正在录音，自然说一段完整的话，结束后点击停止。");
      qc.timer = setInterval(() => {
        const elapsed = (performance.now() - qc.started) / 1000;
        $("quick-timer").textContent = `00:${String(Math.floor(elapsed)).padStart(2, "0")}`;
        if (elapsed >= (feature()?.max_reference_seconds || 30) - .25 && recorder.state === "recording") recorder.stop();
      }, 100);
    } catch (error) {
      stream?.getTracks().forEach(track => track.stop());
      if (sequence === qc.sequence) {
        qc.starting = false; qc.recorder = null; stopTracks();
        status(error.name === "NotAllowedError" ? "麦克风权限未获允许，请在浏览器中允许录音，或上传短录音。" : "无法开启麦克风，请检查设备后重试，或上传短录音。", true);
      }
    } finally { refresh(); }
  }
  async function waitJob(ident, sequence) {
    while (sequence === qc.sequence) {
      const job = await json(`/v1/jobs/${ident}`);
      if (terminal(job.status)) return job;
      await delay(1000);
    }
    return null;
  }
  async function watchPreparation(ident, sequence) {
    try {
      const job = await waitJob(ident, sequence); if (!job || sequence !== qc.sequence) return;
      await voices(); if (sequence !== qc.sequence) return;
      if (job.status !== "succeeded") {
        $("quick-result-status").textContent = `音色已保存，提取未完成：${job.error?.message || "任务已取消"}`;
        $("quick-prepare").hidden = false; refresh(); return;
      }
      qc.ready = true; $("voice-select").value = qc.voiceId; clearVoicePreview(); markPlanStale();
      $("preview-voice").disabled = false;
      $("quick-result-status").textContent = "音色已就绪并自动选中，可以试听新句子或返回工作台配音。";
      status("音色已保存到音色库。"); refresh();
    } catch (error) {
      if (sequence === qc.sequence) {
        $("quick-result-status").textContent = `未能确认音色状态：${error.message}。可刷新音色库或重新提取。`;
        $("quick-prepare").hidden = false; refresh();
      }
    }
  }
  async function create() {
    if (!qc.draft || qc.creating || qc.voiceId) return;
    const sequence = qc.sequence; qc.creating = true; refresh();
    try {
      const body = new FormData(); body.set("draft_id", qc.draft.draft_id);
      body.set("name", $("quick-name").value.trim()); body.set("ref_text", $("quick-ref-text").value.trim()); body.set("mode", "icl");
      if ($("quick-voice-id").value.trim()) body.set("voice_id", $("quick-voice-id").value.trim());
      const voice = await json("/v1/voices", {method:"POST", body});
      if (sequence !== qc.sequence) return;
      qc.voiceId = voice.voice_id; $("quick-result").hidden = false;
      $("quick-result-voice-id").textContent = voice.voice_id;
      $("quick-result-status").textContent = "音色已保存，正在排队提取参考条件…";
      await voices();
      if (voice.preparation_job_id) watchPreparation(voice.preparation_job_id, sequence);
      else {
        $("quick-result-status").textContent = `音色已保存，暂未开始提取：${voice.error?.message || "请稍后重新提取"}`;
        $("quick-prepare").hidden = false;
      }
    } catch (error) { if (sequence === qc.sequence) status(`生成音色未完成：${error.message}`, true); }
    finally { if (sequence === qc.sequence) { qc.creating = false; refresh(); } }
  }
  async function audition() {
    if (!qc.ready || qc.auditioning) return;
    const sequence = qc.sequence; qc.auditioning = true; refresh();
    $("quick-result-status").textContent = "正在用新音色生成试听句子…";
    try {
      const job = await json("/v1/narrations", {method:"POST", body:JSON.stringify({text:"你好，欢迎使用我的新音色。", voice_id:qc.voiceId, compatibility:"generic"})});
      if (sequence !== qc.sequence) { json(`/v1/jobs/${job.job_id}/cancel`, {method:"POST"}).catch(() => {}); return; }
      qc.auditionJob = job.job_id;
      const result = await waitJob(job.job_id, sequence); if (!result || sequence !== qc.sequence) return;
      if (result.status !== "succeeded") throw new Error(result.error?.message || "试听任务已取消");
      const artifact = result.artifacts.find(a => a.artifact_id === "full-mp3") || result.artifacts.find(a => a.artifact_id === "full-wav");
      if (!artifact) throw new Error("试听任务未返回音频");
      const blob = await (await api(artifact.url)).blob(); if (sequence !== qc.sequence) return;
      if (qc.resultUrl) URL.revokeObjectURL(qc.resultUrl); qc.resultUrl = URL.createObjectURL(blob);
      $("quick-result-audio").src = qc.resultUrl; $("quick-result-audio").hidden = false;
      $("quick-result-status").textContent = "试听已生成，请播放并确认音色与读法。";
    } catch (error) { if (sequence === qc.sequence) $("quick-result-status").textContent = `试听未完成：${error.message}`; }
    finally { if (sequence === qc.sequence) { qc.auditioning = false; qc.auditionJob = null; refresh(); } }
  }
  $("go-quick-clone").onclick = () => { state.uploadFromStudio = false; showView("voices", {history:false}); selectVoiceEntry("quick"); $("quick-clone-card").scrollIntoView({block:"nearest"}); };
  $("quick-record").onclick = () => {
    if (!window.isSecureContext && feature()?.https_url) { location.assign(feature().https_url + "/voices?tab=quick"); return; }
    startRecording();
  };
  $("quick-stop").onclick = () => { if (recording()) qc.recorder.stop(); };
  $("quick-reset").onclick = reset;
  $("quick-retry").onclick = () => run(retry);
  $("quick-file").onchange = () => { const file = $("quick-file").files[0]; if (file) upload(file, file.name); };
  $("quick-manual").onchange = () => {
    if ($("quick-manual").checked && qc.busy && qc.file) upload(qc.file, qc.filename, true);
    else if (!$("quick-manual").checked && qc.draft && qc.draft.transcription_state === "pending") run(retry);
    else refresh();
  };
  $("quick-name").oninput = refresh; $("quick-ref-text").oninput = refresh;
  $("quick-clone-form").onsubmit = event => { event.preventDefault(); create(); };
  $("quick-audition").onclick = audition;
  $("copy-quick-voice-id").onclick = () => copyVoiceId(qc.voiceId, $("quick-result-voice-id"));
  $("quick-use").onclick = () => { $("voice-select").value = qc.voiceId; clearVoicePreview(); markPlanStale(); showView("studio"); };
  $("quick-prepare").onclick = () => run(async () => {
    const sequence = qc.sequence;
    const job = await json(`/v1/voices/${qc.voiceId}/prepare`, {method:"POST"});
    $("quick-prepare").hidden = true; $("quick-result-status").textContent = "正在重新提取音色…";
    watchPreparation(job.job_id, sequence);
  }, $("quick-prepare"));
  window.addEventListener("pagehide", () => { qc.sequence++; qc.controller?.abort(); if (recording()) qc.recorder.stop(); stopTracks(); clearPreview(); });
  window.QuickClone = {refresh, leaveRecording:() => { if (recording() || qc.starting) reset(); }}; refresh();
})();
