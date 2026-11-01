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
let playerBinding = null;

function canSendLocally() {
  return input.value.trim() === "/state" || input.value.trim().startsWith("/calc ");
}

function chatOption(value, text) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = text;
  return option;
}

async function refreshPlayerState() {
  if (!conversation) return;
  const [scopes, binding] = await Promise.all([
    api("/api/player-state/scopes"), api(`/api/conversations/${conversation.id}/player-state`),
  ]);
  const scopeInput = document.querySelector("#chat-player-scope");
  const containerInput = document.querySelector("#chat-inventory-container");
  const oldContainer = playerBinding?.scope_id === binding.scope_id ? containerInput.value : "";
  playerBinding = binding;
  scopeInput.replaceChildren(chatOption("", "不关联状态"));
  for (const scope of scopes) scopeInput.append(chatOption(scope.id, `${scope.label} · ${scope.server_id} / ${scope.wipe_id}`));
  scopeInput.value = binding.scope_id || "";
  containerInput.replaceChildren(chatOption("", "仅用消息中的库存"));
  for (const [id, container] of Object.entries(binding.snapshot?.containers || {})) {
    const option = chatOption(id, `${id} · ${container.usable_for_exact_calculation ? "完整盘点" : "需重新盘点"}`);
    option.disabled = !container.usable_for_exact_calculation;
    containerInput.append(option);
    if (id === oldContainer && !option.disabled) containerInput.value = id;
  }
  document.querySelector("#chat-state-note").textContent = binding.snapshot ?
    `读取档案版本 ${binding.snapshot.revision} 的有效观测。库存只在选择容器后用于计算；不会自动保存聊天陈述或扣减库存。输入 /state 查看。` :
    "尚未关联玩家状态。可以先录入档案；新对话默认不继承旧档案。";
}

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
  input.disabled = busy || !conversation;
  sendButton.disabled = busy || (!configured && !canSendLocally()) || !conversation || !input.value.trim();
  document.querySelector("#chat-player-scope").disabled = busy || !conversation;
  document.querySelector("#chat-inventory-container").disabled = busy || !playerBinding?.scope_id;
  document.querySelectorAll('.presentation-settings select, #presentation-reset').forEach((control) => { control.disabled = busy || !conversation; });
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
    const main = turn.presentation?.blocks.filter((block) => block.placement === "main").map((block) => block.text).join("\n\n");
    const article = addMessage("assistant", main ?? turn.assistant, false, turn.truncated);
    if (turn.presentation) renderPresentation(article, turn.presentation);
    if (turn.advice) renderAdviceRecord(article, turn.advice);
    if (turn.player_state_ref) {
      const note = document.createElement("p");
      note.className = "message-notice";
      note.textContent = `本轮使用玩家状态版本 ${turn.player_state_ref.revision}；后续更新不会改写这条历史回复。`;
      article.append(note);
    }
  }
  scrollArea.scrollTop = scrollArea.scrollHeight;
}

function renderPresentation(article, presentation) {
  const context = presentation.context;
  const english = context.language === "en";
  const detailBlocks = presentation.blocks.filter((block) => block.placement === "detail");
  if (detailBlocks.length) {
    const details = document.createElement("details");
    details.className = "advice-record";
    const summary = document.createElement("summary");
    summary.textContent = english ? "Full reasoning, original tool reports and sources" : "展开完整说明、原始计算报告与来源";
    const body = document.createElement("div");
    body.className = "message-content";
    body.textContent = detailBlocks.map((block) => block.text).join("\n\n");
    details.append(summary, body);
    article.append(details);
  }
  const audit = document.createElement("details");
  audit.className = "advice-record";
  const summary = document.createElement("summary");
  summary.textContent = english ? "Expression policy and checks" : "表达规范与校验记录";
  const body = document.createElement("pre");
  body.textContent = JSON.stringify({persona: context.persona_version, prompts: context.prompt_version,
    renderer: context.renderer_version, preferences: context.preferences, scenario: context.scenario,
    checks: presentation.checks, repairs: presentation.repair_count, fallback: presentation.fallback_reason,
    protected_payload_hash: presentation.protected_payload_hash}, null, 2);
  audit.append(summary, body);
  article.append(audit);
}

function renderPreferences() {
  const preferences = conversation?.presentation_preferences || {verbosity: "standard", language: "auto", terminology: "plain"};
  for (const [key, value] of Object.entries(preferences)) document.querySelector(`#presentation-${key}`).value = value;
  document.querySelector("#presentation-label").textContent = {concise: "简洁", standard: "标准", detailed: "详细"}[preferences.verbosity];
}

async function savePreferences(preferences) {
  if (!conversation || busy) return;
  busy = true;
  updateControls();
  try {
    conversation.presentation_preferences = await api(`/api/conversations/${conversation.id}/presentation`, {
      method: "POST", body: JSON.stringify(preferences),
    });
    pendingRequest = null;
    setError();
  } catch (error) { setError(error.message); }
  finally { busy = false; renderPreferences(); updateControls(); }
}

document.querySelectorAll('.presentation-settings select').forEach((control) => control.addEventListener("change", () => {
  savePreferences(Object.fromEntries(["verbosity", "language", "terminology"].map((key) => [key, document.querySelector(`#presentation-${key}`).value])));
}));
document.querySelector("#presentation-reset").addEventListener("click", () => savePreferences({verbosity: "standard", language: "auto", terminology: "plain"}));

function renderAdviceRecord(article, advice) {
  const details = document.createElement("details");
  details.className = "advice-record";
  const summary = document.createElement("summary");
  summary.textContent = `取证与决策记录 · ${advice.evidence.documents.length} 份正文 · ${advice.evidence.cache_hit ? "缓存" : "本轮取证/工具"} · ${(advice.elapsed_ms / 1000).toFixed(1)} 秒`;
  details.append(summary);
  const note = document.createElement("p");
  note.textContent = "建议按条件成立。网页原文匹配不等于事实已验证；所有候选预算分别计算，不能直接相加执行。";
  details.append(note);
  for (const documentRecord of advice.evidence.documents) {
    const source = document.createElement("p");
    const link = document.createElement("a");
    link.href = documentRecord.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = documentRecord.title;
    const snapshot = document.createElement("a");
    snapshot.href = `/api/evidence/${encodeURIComponent(documentRecord.id)}`;
    snapshot.target = "_blank";
    snapshot.rel = "noopener";
    snapshot.textContent = "查看留存快照";
    source.append(link, document.createTextNode(` · 抓取于 ${documentRecord.fetched_at} · 版本未核验 · `), snapshot);
    details.append(source);
  }
  for (const recommendation of [advice.answer?.recommendation, advice.answer?.alternative]) {
    for (const citation of recommendation?.citations || []) {
      const quote = document.createElement("blockquote");
      const source = advice.evidence.documents.find((documentRecord) => documentRecord.id === citation.document_id);
      quote.textContent = `${recommendation.candidate_id} 的依据原文（${source?.title || citation.document_id}）：${citation.quote}`;
      details.append(quote);
    }
  }
  const audit = document.createElement("pre");
  audit.textContent = JSON.stringify({status: advice.status, plan: advice.plan, events: advice.evidence.events,
    queries: advice.evidence.queries, repairs: advice.repairs, workflow: advice.workflow}, null, 2);
  details.append(audit);
  article.append(details);
}

async function createConversation() {
  conversation = await api("/api/conversations", { method: "POST", body: "{}" });
  rememberConversation(conversation.id);
  pendingRequest = null;
  renderConversation();
  renderPreferences();
  await refreshPlayerState();
}

document.querySelector("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || busy || (!configured && !canSendLocally()) || !conversation) return;
  busy = true;
  setError();
  const player_inventory_container = document.querySelector("#chat-inventory-container").value || null;
  if (!pendingRequest || pendingRequest.message !== message || pendingRequest.player_inventory_container !== player_inventory_container) {
    pendingRequest = { request_id: crypto.randomUUID(), message, player_inventory_container };
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
    document.querySelector("#connection-label").textContent = turn.calculation || message === "/state" ? "本地工具链路已处理" : "本次对话连接成功";
  } catch (error) {
    input.value = message;
    setError(error.message || "连接中断，请重试。失败的消息不会加入对话历史。");
  } finally {
    busy = false;
    renderConversation();
    renderPreferences();
    updateControls();
    input.focus();
  }
});

input.addEventListener("input", updateControls);
document.querySelector("#chat-player-scope").addEventListener("change", async (event) => {
  if (!conversation || busy) return;
  busy = true;
  updateControls();
  try {
    await api(`/api/conversations/${conversation.id}/player-state`, {method: "POST", body: JSON.stringify({scope_id: event.target.value || null})});
    pendingRequest = null;
    await refreshPlayerState();
    setError();
  } catch (error) { setError(error.message); }
  finally { busy = false; updateControls(); }
});
window.addEventListener("focus", async () => {
  if (busy || !conversation) return;
  try { await refreshPlayerState(); updateControls(); } catch (error) { setError(error.message); }
});
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
    else await refreshPlayerState();
    renderConversation();
    renderPreferences();
  } catch (error) {
    document.querySelector("#connection-label").textContent = "服务连接失败";
    setError(error.message || "无法连接服务，请刷新页面。");
  }
  updateControls();
}

initialize();
