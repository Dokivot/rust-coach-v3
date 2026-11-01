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

function rememberConversation(identifier) {
  try { localStorage.setItem(storageKey, identifier); } catch {}
}

function storedConversation() {
  try { return localStorage.getItem(storageKey); } catch { return null; }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error?.message || "请求失败，请刷新页面后重试。");
    error.status = response.status;
    error.code = body.error?.code;
    throw error;
  }
  return body;
}

function setError(message = "") {
  errorElement.textContent = message;
  errorElement.hidden = !message;
}

function updateControls() {
  input.disabled = busy || !configured || !conversation;
  sendButton.disabled = busy || !configured || !conversation || !input.value.trim();
  document.querySelectorAll('[data-action="new"]').forEach((button) => { button.disabled = busy; });
  document.querySelectorAll('[data-action="delete"]').forEach((button) => { button.disabled = busy || !conversation; });
  document.querySelector("#input-count").textContent = `${input.value.length} / 6000`;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function addMessage(role, content, pending = false, truncated = false) {
  const article = document.createElement("article");
  article.className = `message ${role}${pending ? " pending" : ""}`;
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
    notice.textContent = "回复达到本次输出上限，你可以继续追问。";
    article.append(notice);
  }
  messagesElement.append(article);
  return article;
}

function renderConversation() {
  messagesElement.replaceChildren();
  document.querySelector("#welcome").hidden = Boolean(conversation?.turns.length);
  for (const turn of conversation?.turns || []) {
    addMessage("user", turn.user);
    addMessage("assistant", turn.assistant, false, turn.truncated);
  }
  scrollArea.scrollTop = scrollArea.scrollHeight;
}

async function createConversation() {
  conversation = await api("/api/conversations", { method: "POST", body: "{}" });
  rememberConversation(conversation.id);
  pendingRequest = null;
  renderConversation();
}

document.querySelector("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || busy || !configured || !conversation) return;
  busy = true;
  setError();
  if (!pendingRequest || pendingRequest.message !== message) {
    pendingRequest = { request_id: crypto.randomUUID(), message };
  }
  document.querySelector("#welcome").hidden = true;
  addMessage("user", message);
  addMessage("assistant", "正在思考，请稍等…", true);
  input.value = "";
  updateControls();
  scrollArea.scrollTop = scrollArea.scrollHeight;
  try {
    const turn = await api(`/api/conversations/${conversation.id}/messages`, {
      method: "POST", body: JSON.stringify(pendingRequest),
    });
    if (!conversation.turns.some((existing) => existing.request_id === turn.request_id)) {
      conversation.turns.push(turn);
    }
    pendingRequest = null;
    document.querySelector("#connection-label").textContent = turn.calculation ? "本地计算链路已处理" : "本次对话连接成功";
  } catch (error) {
    input.value = message;
    setError(error.message || "连接中断，请重试。失败的消息不会加入对话历史。");
  } finally {
    busy = false;
    renderConversation();
    updateControls();
    input.focus();
  }
});

input.addEventListener("input", updateControls);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    document.querySelector("#chat-form").requestSubmit();
  }
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    if (busy) return;
    input.value = button.dataset.prompt;
    updateControls();
    if (configured) input.focus();
    else document.querySelector("#setup").scrollIntoView({ block: "nearest" });
  });
});

document.querySelectorAll('[data-action="new"]').forEach((button) => button.addEventListener("click", async () => {
  busy = true;
  updateControls();
  setError();
  try {
    await createConversation();
    input.value = "";
  } catch (error) { setError(error.message); }
  finally { busy = false; updateControls(); input.focus(); }
}));

document.querySelectorAll('[data-action="delete"]').forEach((button) => button.addEventListener("click", async () => {
  if (!conversation || !window.confirm("删除当前会话及其本地记录？此操作不可撤销。")) return;
  busy = true;
  updateControls();
  setError();
  try {
    await api(`/api/conversations/${conversation.id}`, { method: "DELETE" });
    conversation = null;
    rememberConversation("");
    await createConversation();
    input.value = "";
  } catch (error) { setError(error.message); }
  finally { busy = false; updateControls(); }
}));

async function initialize() {
  try {
    const config = await api("/api/config");
    configured = config.configured;
    document.querySelector("#model-name").textContent = config.model;
    document.querySelector("#connection-label").textContent = configured ? "模型已配置 · 未验证连接" : "等待配置密钥";
    document.querySelector("#connection-dot").classList.toggle("ready", configured);
    document.querySelector("#setup").hidden = configured;
    document.querySelector("#context-note").textContent = `使用最近最多 ${config.history_turns} 轮上下文 · 内容会发送至模型服务`;
    const identifier = storedConversation();
    if (identifier) {
      try { conversation = await api(`/api/conversations/${identifier}`); }
      catch (error) { if (![404, 422].includes(error.status)) throw error; }
    }
    if (!conversation) await createConversation();
    renderConversation();
  } catch (error) {
    document.querySelector("#connection-label").textContent = "服务连接失败";
    setError(error.message || "无法连接服务，请刷新页面。");
  }
  updateControls();
}

initialize();
