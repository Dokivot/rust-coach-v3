const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send");
const messagesElement = document.querySelector("#messages");
const errorElement = document.querySelector("#error");
const scrollArea = document.querySelector("#scroll-area");
const storageKey = "rust-coach-conversation";
let conversation = null;
let configured = false;
let busy = false;
let pendingRequest = null;
let sendingDraft = null;

function rememberConversation(identifier) {
  try { localStorage.setItem(storageKey, identifier); } catch {}
}

function storedConversation() {
  try { return localStorage.getItem(storageKey); } catch { return null; }
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"Content-Type": "application/json", ...options.headers}});
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error?.message || "请求失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return body;
}

async function sendChat(conversationId, payload, onStage) {
  const response = await fetch("/api/conversations/" + conversationId + "/messages", {
    method: "POST", headers: {"Content-Type": "application/json", "Accept": "text/event-stream"},
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error?.message || "请求失败，请稍后重试。");
  }
  if (!response.headers?.get("content-type")?.includes("text/event-stream")) return response.json();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sequence = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) throw new Error("连接中断，文字和图片已恢复，请重试。");
      buffer = (buffer + decoder.decode(value, {stream: true})).replace(/\r\n/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const event = JSON.parse(data);
        if (event.request_id !== payload.request_id) throw new Error("回复与当前请求不匹配，请重试。");
        if (event.type === "error") throw new Error(event.error?.message || "处理失败，请重试。");
        if (event.type === "result") {
          if (event.turn?.request_id !== payload.request_id) throw new Error("回复与当前请求不匹配，请重试。");
          return event.turn;
        }
        if (event.type === "stage" && Number.isInteger(event.sequence) && event.sequence > sequence &&
            typeof event.stage === "string" && typeof event.label === "string") {
          sequence = event.sequence;
          onStage(event);
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function setError(message = "") {
  errorElement.textContent = message;
  errorElement.hidden = !message;
}

function updateControls() {
  const local = input.value.trim() === "/state" || input.value.trim().startsWith("/calc ") || /^\/(?:memory|knowledge)(?:\s|$)/.test(input.value.trim());
  input.disabled = busy || !conversation;
  sendButton.disabled = busy || (!configured && !local) || !conversation || (!input.value.trim() && !VisionUI.hasImages());
  document.querySelector("#new-chat").disabled = busy;
  document.querySelector("#send-status").textContent = busy ? (sendingDraft?.status || "正在处理…") : "";
  VisionUI.updateControls();
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function addMessage(role, content, pending = false, truncated = false) {
  const article = document.createElement("article");
  article.className = "message " + role + (pending ? " pending" : "");
  const header = document.createElement("div");
  header.className = "message-header";
  const avatar = document.createElement("span");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "你" : "R";
  const name = document.createElement("span");
  name.textContent = role === "user" ? "你" : "Rust Coach";
  header.append(avatar, name);
  const body = document.createElement("div");
  body.className = "message-content";
  body.textContent = content;
  article.append(header, body);
  if (truncated) {
    const notice = document.createElement("p");
    notice.className = "message-notice";
    notice.textContent = "回复尚未说完，可以继续追问。";
    article.append(notice);
  }
  messagesElement.append(article);
  return article;
}

function renderConversation() {
  messagesElement.replaceChildren();
  document.querySelector("#welcome").hidden = Boolean(conversation?.turns.length || sendingDraft);
  for (const turn of conversation?.turns || []) {
    const userMessage = addMessage("user", turn.user || "已发送截图");
    const imageIds = turn.visual?.request_identity?.attachment_ids || [];
    for (const attachment of turn.visual?.attachments || []) {
      if (!imageIds.includes(attachment.id)) continue;
      const image = document.createElement("img");
      image.src = attachment.thumbnail_url;
      image.alt = "已发送的截图";
      image.className = "message-image";
      const scale = Math.min(360 / attachment.width, 220 / attachment.height, 1);
      image.width = Math.round(attachment.width * scale);
      image.height = Math.round(attachment.height * scale);
      image.loading = "lazy";
      userMessage.append(image);
    }
    const blocks = turn.presentation?.blocks;
    const main = blocks?.filter(block => block.placement === "main").map(block => block.text).join("\n\n");
    const assistantMessage = addMessage("assistant", main || turn.assistant, false, turn.truncated);
    const detailBlocks = blocks?.filter(block => block.placement === "detail" && block.text.trim()) || [];
    if (detailBlocks.length) {
      const details = document.createElement("details");
      details.className = "answer-details";
      const summary = document.createElement("summary");
      summary.textContent = turn.presentation.context?.language === "en"
        ? "View calculations, observations and sources" : "查看计算、观测与来源详情";
      details.append(summary);
      for (const block of detailBlocks) {
        const content = document.createElement("div");
        content.className = "answer-detail-content";
        content.textContent = block.text;
        details.append(content);
      }
      appendKnowledgeFeedback(details, turn, conversation.id);
      assistantMessage.append(details);
    }
    if (turn.request_id) appendPerformance(assistantMessage, conversation.id, turn.request_id);
  }
  if (sendingDraft) {
    const userMessage = addMessage("user", sendingDraft.message || "图片消息", true);
    for (const attachment of sendingDraft.attachments) {
      const image = document.createElement("img");
      image.src = attachment.url;
      image.alt = "正在发送的截图";
      image.className = "message-image";
      userMessage.append(image);
    }
    const assistantMessage = addMessage("assistant", sendingDraft.status, true);
    if (sendingDraft.stages.length) {
      const stages = document.createElement("ol");
      stages.className = "agent-stages";
      stages.setAttribute("aria-label", "执行阶段");
      for (const [index, stage] of sendingDraft.stages.entries()) {
        const item = document.createElement("li");
        const current = index === sendingDraft.stages.length - 1 && stage.label === sendingDraft.status;
        item.className = "agent-stage " + (current ? "current" : "completed");
        item.textContent = stage.label.replace(/^正在/, "");
        if (current) item.setAttribute("aria-current", "step");
        stages.append(item);
      }
      assistantMessage.append(stages);
    }
  }
  requestAnimationFrame(() => { scrollArea.scrollTop = scrollArea.scrollHeight; });
}

function appendPerformance(message, conversationId, requestId) {
  const details = document.createElement("details");
  details.className = "answer-details";
  const summary = document.createElement("summary");
  summary.textContent = "查看处理耗时与调用";
  const content = document.createElement("div");
  content.className = "answer-detail-content";
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.textContent = "刷新记录";
  let loading = false;
  async function load() {
    if (loading) return;
    loading = true;
    content.textContent = "正在读取记录…";
    try {
      const report = await api("/api/conversations/" + conversationId + "/performance?request_id=" + encodeURIComponent(requestId));
      const labels = {chat: "本次处理", background_memory: "后台记忆", vision_probe: "视觉探测"};
      const lines = report.runs.map(run => {
        const elapsed = run.duration_ms === null ? "尚未结束" : (run.duration_ms / 1000).toFixed(3) + " 秒";
        return (labels[run.kind] || run.kind) + "：" + elapsed + "，模型调用 " + run.summary.model_calls +
          " 次，网页请求 " + run.summary.web_requests + " 次" +
          (run.outcome?.replayed ? "，复用了已有回答" : "") +
          (run.status !== "completed" ? "，状态 " + run.status : "");
      });
      if (!lines.length) lines.push("此回答没有已保存的计量记录，可能早于计量功能。");
      if (report.background_memory_status === "pending") lines.push("后台记忆尚未完成，可稍后刷新。");
      if (report.truncated) lines.push("仅展示最近记录，汇总不是全部历史消耗。");
      if (report.summary.recording_status !== "complete") lines.push("计量记录不完整，不能据此核算总消耗。");
      lines.push("服务端处理耗时，不含图片上传和浏览器传输；前台与后台时间可能重叠，不相加。");
      lines.push("费用未知：尚未接入供应商价格与账单，不能把调用次数当作金额。");
      content.textContent = lines.join("\n");
    } catch (error) {
      content.textContent = "无法读取计量记录：" + error.message;
    } finally {
      loading = false;
    }
  }
  details.addEventListener("toggle", () => { if (details.open) load(); });
  refresh.addEventListener("click", load);
  details.append(summary, content, refresh);
  message.append(details);
}

function appendKnowledgeFeedback(details, turn, conversationId) {
  const knowledge = turn.presentation?.knowledge;
  if (!knowledge) return;
  const identifiers = [...new Set([...(knowledge.documents || []).map(item => item.id), ...(knowledge.rule_dependencies || [])])];
  if (!identifiers.length) return;
  const form = document.createElement("form");
  const evidenceLabel = document.createElement("label");
  evidenceLabel.textContent = "反馈存在问题的依据 ";
  const evidence = document.createElement("select");
  for (const identifier of identifiers) {
    const option = document.createElement("option");
    option.value = identifier;
    option.textContent = identifier;
    evidence.append(option);
  }
  evidence.value = identifiers[0];
  evidenceLabel.append(evidence);
  const reasonLabel = document.createElement("label");
  reasonLabel.textContent = " 问题说明 ";
  const reason = document.createElement("input");
  reason.type = "text";
  reason.required = true;
  reason.maxLength = 1000;
  reasonLabel.append(reason);
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "提交待核实反馈";
  const status = document.createElement("span");
  status.setAttribute("role", "status");
  let pending = null;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (submit.disabled || !reason.value.trim()) return;
    if (!pending || pending.reason !== reason.value.trim() || pending.document_ids[0] !== evidence.value) {
      pending = {request_id: crypto.randomUUID(), turn_request_id: turn.request_id,
        document_ids: [evidence.value], reason: reason.value.trim()};
    }
    submit.disabled = true;
    status.textContent = "正在保存…";
    try {
      await api("/api/conversations/" + conversationId + "/knowledge-feedback", {method: "POST", body: JSON.stringify(pending)});
      status.textContent = "已保存待核实反馈；不会自动修改规则。";
      reason.disabled = evidence.disabled = true;
    } catch (error) {
      status.textContent = error.message;
      submit.disabled = false;
    }
  });
  form.append(evidenceLabel, reasonLabel, submit, status);
  details.append(form);
}

async function createConversation() {
  const created = await api("/api/conversations", {method: "POST", body: "{}"});
  VisionUI.clear();
  conversation = created;
  rememberConversation(conversation.id);
  pendingRequest = null;
  renderConversation();
}

document.querySelector("#chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message = input.value.trim();
  const local = message === "/state" || message.startsWith("/calc ") || /^\/(?:memory|knowledge)(?:\s|$)/.test(message);
  if ((!message && !VisionUI.hasImages()) || busy || (!configured && !local) || !conversation) return;
  busy = true;
  setError();
  const draft = {message, text: input.value, conversationId: conversation.id, attachments: VisionUI.takePending()};
  draft.status = draft.attachments.some(item => !item.attachment) ? "正在上传图片…" : "正在发送…";
  draft.stages = draft.attachments.some(item => !item.attachment) ? [{stage: "upload", label: draft.status}] : [];
  sendingDraft = draft;
  input.value = "";
  renderConversation();
  updateControls();
  let sent = false;
  try {
    const images = await VisionUI.requestFields(draft.attachments, draft.conversationId);
    if (!pendingRequest || pendingRequest.message !== message ||
        JSON.stringify(pendingRequest.attachment_ids) !== JSON.stringify(images.attachment_ids)) {
      pendingRequest = {request_id: crypto.randomUUID(), message, ...images};
    }
    draft.status = "正在等待处理…";
    renderConversation();
    updateControls();
    const turn = await sendChat(draft.conversationId, pendingRequest, event => {
      if (sendingDraft !== draft) return;
      draft.status = event.label;
      draft.stages.push({stage: event.stage, label: event.label});
      renderConversation();
      updateControls();
    });
    if (!conversation.turns.some(existing => existing.request_id === turn.request_id)) conversation.turns.push(turn);
    pendingRequest = null;
    sent = true;
  } catch (error) {
    input.value = draft.text;
    VisionUI.restorePending(draft.attachments);
    setError(error.message || "连接中断，文字和图片已保留，请重试。");
  } finally {
    sendingDraft = null;
    busy = false;
    renderConversation();
    if (sent) VisionUI.release(draft.attachments);
    updateControls();
    input.focus();
  }
});

input.addEventListener("input", updateControls);
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    document.querySelector("#chat-form").requestSubmit();
  }
});

document.querySelector("#new-chat").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  updateControls();
  setError();
  try { await createConversation(); input.value = ""; }
  catch (error) { setError(error.message); }
  finally { busy = false; updateControls(); input.focus(); }
});

async function initialize() {
  VisionUI.init({api, conversation: () => conversation, busy: () => busy, error: setError,
    invalidate: () => { pendingRequest = null; }, refreshControls: updateControls});
  try {
    const config = await api("/api/config");
    VisionUI.configure(config.vision);
    configured = config.configured;
    document.querySelector("#setup").hidden = configured;
    const identifier = storedConversation();
    if (identifier) {
      try { conversation = await api("/api/conversations/" + identifier); }
      catch (error) { if (![404, 422].includes(error.status)) throw error; }
    }
    if (!conversation) await createConversation();
    renderConversation();
  } catch (error) { setError(error.message || "无法连接服务，请刷新页面。"); }
  updateControls();
}

initialize();
