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

function setError(message = "") {
  errorElement.textContent = message;
  errorElement.hidden = !message;
}

function updateControls() {
  const local = input.value.trim() === "/state" || input.value.trim().startsWith("/calc ") || /^\/memory(?:\s|$)/.test(input.value.trim());
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
      assistantMessage.append(details);
    }
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
    addMessage("assistant", sendingDraft.status, true);
  }
  requestAnimationFrame(() => { scrollArea.scrollTop = scrollArea.scrollHeight; });
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
  const local = message === "/state" || message.startsWith("/calc ") || /^\/memory(?:\s|$)/.test(message);
  if ((!message && !VisionUI.hasImages()) || busy || (!configured && !local) || !conversation) return;
  busy = true;
  setError();
  const draft = {message, text: input.value, conversationId: conversation.id, attachments: VisionUI.takePending()};
  draft.status = draft.attachments.some(item => !item.attachment) ? "正在上传图片…" : "正在发送…";
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
    draft.status = "正在思考…";
    renderConversation();
    updateControls();
    const turn = await api("/api/conversations/" + draft.conversationId + "/messages", {method: "POST", body: JSON.stringify(pendingRequest)});
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
