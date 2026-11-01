window.VisionUI = (() => {
  let hooks;
  let config = {};
  let pending = [];
  let editing = false;
  const attempts = new Map();
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, handler) => {
    const node = element("button", text);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  };
  function field(parent, title, type, value = "") {
    const label = element("label", title + " ");
    const control = element("input");
    control.type = type;
    if (type === "checkbox") control.checked = Boolean(value);
    else control.value = value;
    label.append(control);
    parent.append(label);
    return control;
  }
  function subjectField(parent, value = "unknown") {
    const label = element("label", "图片主体 ");
    const select = element("select");
    for (const [key, text] of Object.entries({unknown: "自动判断", self: "本人", opponent: "对手", teammate: "队友"})) {
      const option = element("option", text);
      option.value = key;
      select.append(option);
    }
    select.value = value;
    label.append(select);
    parent.append(label);
    return select;
  }
  function localTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  async function locked(action) {
    if (hooks.busy()) return;
    hooks.setBusy(true);
    hooks.error();
    try { return await action(); }
    catch (error) { hooks.error(error.message || "截图操作失败，请重试。"); }
    finally { hooks.setBusy(false); }
  }
  function updateControls() {
    if (!hooks) return;
    const disabled = hooks.busy() || !hooks.conversation();
    document.querySelectorAll(".visual-card > button, .visual-image button").forEach(control => { control.disabled = disabled; });
    document.querySelectorAll(".vision-composer button, .vision-composer input, .vision-composer select").forEach(control => { control.disabled = disabled; });
    document.querySelector("#vision-add").disabled = disabled || pending.length >= 4;
    document.querySelector("#vision-probe").disabled = disabled || !config.configured;
    document.querySelectorAll(".visual-card fieldset").forEach(control => { control.disabled = disabled || control.dataset.inactive === "true"; });
  }
  function renderPending() {
    const host = document.querySelector("#vision-pending");
    host.replaceChildren();
    pending.forEach((item, index) => {
      const card = element("div", undefined, "vision-pending-card");
      const preview = element("img");
      preview.src = item.url;
      preview.alt = `待发送截图 ${index + 1}`;
      card.append(preview);
      card.append(element("p", "默认按当前情况分析，可直接发送。"));
      const metadata = element("details");
      metadata.append(element("summary", "补充主体或时间（可选）"));
      const subject = subjectField(metadata, item.subject);
      subject.addEventListener("change", () => { item.subject = subject.value; hooks.invalidate(); });
      const time = field(metadata, "截图时间（可留空）", "datetime-local", item.time);
      time.addEventListener("change", () => { item.time = time.value; hooks.invalidate(); });
      card.append(metadata);
      card.append(button("裁剪 / 遮盖", () => locked(async () => {
        const blob = await edit(item.blob);
        if (!blob) return;
        if (blob.size > (config.max_bytes || 8388608)) throw new Error("处理后的图片过大，请缩小或裁剪后重试。");
        if (item.attachment) await hooks.api(item.attachment.url, {method: "DELETE"});
        URL.revokeObjectURL(item.url);
        Object.assign(item, {blob, url: URL.createObjectURL(blob), attachment: null});
        hooks.invalidate();
        renderPending();
      })));
      card.append(button("移除", () => locked(async () => {
        if (item.attachment) await hooks.api(item.attachment.url, {method: "DELETE"});
        URL.revokeObjectURL(item.url);
        pending = pending.filter(entry => entry !== item);
        hooks.invalidate();
        renderPending();
        hooks.refreshControls();
      })));
      host.append(card);
    });
    updateControls();
  }
  async function edit(file) {
    const image = await createImageBitmap(file, {imageOrientation: "from-image"});
    if (image.width * image.height > 20000000 || Math.max(image.width, image.height) > 8192) {
      image.close();
      throw new Error("图片超过像素限制，请用系统图片工具缩小后再添加。");
    }
    const dialog = document.querySelector("#vision-editor");
    const canvas = document.querySelector("#vision-edit-canvas");
    const source = document.createElement("canvas");
    const context = canvas.getContext("2d");
    let start = null;
    let selection = null;
    const draw = () => {
      canvas.width = source.width;
      canvas.height = source.height;
      context.drawImage(source, 0, 0);
      if (selection) {
        context.strokeStyle = "#aee287";
        context.lineWidth = Math.max(2, source.width / 350);
        context.strokeRect(...selection);
      }
    };
    const reset = () => {
      source.width = image.width;
      source.height = image.height;
      source.getContext("2d").drawImage(image, 0, 0);
      selection = null;
      draw();
    };
    const point = event => {
      const rect = canvas.getBoundingClientRect();
      return [Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * canvas.width / rect.width)),
        Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * canvas.height / rect.height))];
    };
    canvas.onpointerdown = event => { start = point(event); canvas.setPointerCapture(event.pointerId); };
    canvas.onpointermove = event => {
      if (!start) return;
      const end = point(event);
      selection = [Math.min(start[0], end[0]), Math.min(start[1], end[1]), Math.abs(start[0] - end[0]), Math.abs(start[1] - end[1])].map(Math.round);
      draw();
    };
    canvas.onpointerup = () => { start = null; };
    document.querySelector("#vision-reset").onclick = reset;
    document.querySelector("#vision-redact").onclick = () => {
      if (!selection) return;
      const painter = source.getContext("2d");
      painter.fillStyle = "#111111";
      painter.fillRect(...selection);
      selection = null;
      draw();
    };
    document.querySelector("#vision-crop").onclick = () => {
      if (!selection || selection[2] < 2 || selection[3] < 2) return;
      const pixels = source.getContext("2d").getImageData(...selection);
      source.width = selection[2]; source.height = selection[3];
      source.getContext("2d").putImageData(pixels, 0, 0);
      selection = null;
      draw();
    };
    reset();
    dialog.showModal();
    return new Promise(resolve => {
      const finish = blob => { dialog.close(); image.close(); resolve(blob); };
      document.querySelector("#vision-editor-cancel").onclick = () => finish(null);
      dialog.oncancel = event => { event.preventDefault(); finish(null); };
      document.querySelector("#vision-editor-use").onclick = () => source.toBlob(finish, "image/png");
    });
  }
  async function addFiles(files) {
    if (editing || hooks.busy() || !hooks.conversation()) return;
    editing = true;
    await locked(async () => {
      for (const file of files) {
        if (pending.length >= 4) throw new Error("每轮最多四张图片。");
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw new Error("仅支持 PNG、JPEG、WebP 静态图片。");
        if (file.size > (config.max_bytes || 8388608)) throw new Error("单张图片最多 8 MiB，请先压缩或裁剪。");
        const blob = file;
        pending.push({blob, url: URL.createObjectURL(blob), subject: "unknown", time: "", attachment: null});
      }
      document.querySelector(".vision-composer").open = true;
      hooks.invalidate();
    });
    editing = false;
    renderPending();
    hooks.refreshControls();
  }
  async function requestFields() {
    if (pending.length && !config.enabled) {
      await hooks.api("/api/vision/probe", {method: "POST", body: "{}"});
      config = (await hooks.api("/api/config")).vision;
      document.querySelector("#vision-status").textContent = "接口已验证";
    }
    const context = {};
    for (const item of pending) {
      if (!item.attachment) {
        const form = new FormData();
        form.append("file", item.blob, "screenshot.png");
        const response = await fetch(`/api/conversations/${hooks.conversation().id}/attachments`, {method: "POST", body: form});
        const body = await response.json();
        if (!response.ok) throw new Error(body.error?.message || "上传失败");
        item.attachment = body;
      }
      if (item.subject !== "unknown" || item.time) {
        context[item.attachment.id] = {subject: item.subject, ...(item.time ? {captured_at: new Date(item.time).toISOString()} : {})};
      }
    }
    return {attachment_ids: pending.map(item => item.attachment.id), image_context: context};
  }
  function clear() {
    pending.forEach(item => URL.revokeObjectURL(item.url));
    pending = [];
    renderPending();
  }
  function forgetConversation(conversation) {
    for (const turn of conversation.turns) {
      if (!turn.visual) continue;
      attempts.delete(turn.visual.draft_id);
      try { localStorage.removeItem("rust-coach-visual-" + turn.visual.draft_id); } catch {}
    }
  }
  async function reload() {
    await hooks.reload();
    hooks.refreshControls();
  }
  function rememberAttempt(draft, payload) {
    attempts.set(draft, payload);
    try { localStorage.setItem("rust-coach-visual-" + draft, JSON.stringify(payload)); } catch {}
  }
  function previousAttempt(draft) {
    try { return attempts.get(draft) || JSON.parse(localStorage.getItem("rust-coach-visual-" + draft) || "null"); }
    catch { return null; }
  }
  async function confirm(draftId, payload) {
    rememberAttempt(draftId, payload);
    const result = await hooks.api(`/api/conversations/${hooks.conversation().id}/visual-drafts/${draftId}/confirm`, {method: "POST", body: JSON.stringify(payload)});
    await reload();
    if (result.continuation_pending) hooks.error((result.receipt.saved ? "档案已保存。" : "本轮观测已确认。") + result.error.message + " 可点击“重试继续”。");
  }
  async function render(article, visual, interactive = true) {
    const owner = hooks.conversation().id;
    const card = element("details", undefined, "visual-card");
    card.append(element("summary", "查看识别详情与图片区域"));
    article.append(card);
    let draft;
    try { draft = await hooks.api(`/api/conversations/${owner}/visual-drafts/${visual.draft_id}`); }
    catch (error) { card.append(element("p", error.message, "vision-error")); return; }
    if (!card.isConnected || hooks.conversation().id !== owner) return;
    const payload = draft.payload;
    const accepted = payload.accepted;
    const resolved = accepted || payload.automatic;
    card.append(element("p", accepted ? `${accepted.saved ? "已保存到档案" : "仅用于本次任务"} · 观测时间 ${accepted.observed_at}` : "识别结果已直接用于本次分析。可以继续追问；修正和保存档案均为可选操作。"));
    if (!draft.available) card.append(element("p", "草稿或原图已过期、取消或删除。历史记录仅供回顾。", "vision-error"));
    const images = element("div", undefined, "visual-images");
    const canvases = new Map();
    for (const attachment of payload.attachments) {
      const holder = element("div", undefined, "visual-image");
      const canvas = element("canvas");
      const picture = new Image();
      const draw = bbox => {
        if (!picture.naturalWidth) return;
        canvas.width = picture.naturalWidth; canvas.height = picture.naturalHeight;
        const painter = canvas.getContext("2d");
        painter.drawImage(picture, 0, 0);
        if (bbox) {
          painter.strokeStyle = "#aee287";
          painter.lineWidth = Math.max(3, canvas.width / 250);
          painter.strokeRect(bbox[0] * canvas.width, bbox[1] * canvas.height, (bbox[2] - bbox[0]) * canvas.width, (bbox[3] - bbox[1]) * canvas.height);
        }
      };
      picture.onload = () => draw();
      picture.onerror = () => { canvas.hidden = true; holder.prepend(element("p", "原图不可用")); };
      picture.src = attachment.url;
      canvas.title = "点击在新标签页查看原图";
      canvas.onclick = () => window.open(attachment.url, "_blank", "noopener");
      canvases.set(attachment.id, bbox => { draw(bbox); canvas.scrollIntoView({block: "nearest"}); });
      holder.append(canvas, element("p", `图像指纹 ${attachment.sha256.slice(0, 12)} · ${attachment.width} × ${attachment.height}`));
      const remove = button("删除这张原图", () => locked(async () => { await hooks.api(attachment.url, {method: "DELETE"}); await reload(); }));
      remove.disabled = hooks.busy() || !draft.available;
      holder.append(remove);
      images.append(holder);
    }
    card.append(images);
    const regions = element("div", undefined, "vision-actions");
    for (const observation of accepted?.observations || payload.extraction.observations) {
      regions.append(button(`区域 ${observation.id}`, () => canvases.get(observation.attachment_id)?.(observation.bbox)));
    }
    card.append(regions);
    for (const observation of resolved?.observations || payload.extraction.observations) {
      card.append(element("p", `${observation.label}${observation.quantity === null ? "" : ` × ${observation.quantity}`} · ${observation.description}`));
    }
    const corrections = element("details");
    corrections.append(element("summary", "修正识别 / 保存到档案（可选）"));
    const controls = element("fieldset");
    const active = draft.status === "pending" && draft.available && interactive;
    controls.dataset.inactive = String(!active);
    const rows = (accepted?.observations || payload.extraction.observations).map(observation => {
      const row = element("div", undefined, "visual-row");
      const title = element("div", undefined, "visual-row-title");
      const checked = field(title, `保留 ${observation.label}`, "checkbox", true);
      title.append(button(`定位 ${observation.id}`, () => canvases.get(observation.attachment_id)?.(observation.bbox)));
      row.append(title);
      const item = field(row, "物品 ID（未知留空）", "text", observation.item_id || "");
      const quantity = field(row, "可见数量（未知留空）", "number", observation.quantity ?? "");
      quantity.min = "0"; quantity.max = "1000000"; quantity.step = "1";
      const description = field(row, "局部描述", "text", observation.description);
      description.parentElement.className = "visual-description";
      row.append(element("p", `${observation.region_kind} · ${observation.slot || "槽位未定"} · ${observation.uncertainty.join("；") || "自动识别"}`));
      controls.append(row);
      return {observation, checked, item, quantity, description};
    });
    const subjects = [...new Set([...Object.values(payload.image_context).map(value => value.subject),
      ...payload.extraction.observations.map(value => value.subject)].filter(value => value !== "unknown"))];
    const subject = subjectField(controls, resolved?.subject || (subjects.length === 1 ? subjects[0] : "self"));
    const captureTimes = Object.values(payload.image_context).map(value => value.captured_at).filter(Boolean).sort();
    const defaultTime = resolved?.observed_at || captureTimes[0] || draft.created_at;
    const observed = field(controls, "观测时间（自动填写，可修改）", "datetime-local", localTime(defaultTime));
    const container = field(controls, "库存容器标识", "text", resolved?.container_id || "backpack");
    const complete = field(controls, "覆盖整个容器", "checkbox", resolved?.inventory_complete);
    complete.disabled = payload.extraction.inventory_coverage !== "full_view";
    controls.append(element("p", `识别覆盖范围：${payload.extraction.inventory_coverage}。局部截图只计算总需求，不推断未拍到的库存为零。`));
    const totals = field(controls, "局部更新：我确认这些数字是容器内该物品的总量，而非单格堆叠数", "checkbox");
    const same = field(controls, "多图属于同一时点、同一容器，且没有重复格子", "checkbox");
    const details = element("details");
    details.append(element("summary", "计算目标参数（需要时修正；不填写 inventory）"));
    const calculation = element("textarea");
    calculation.setAttribute("aria-label", "计算目标 JSON");
    calculation.value = payload.extraction.calculation ? JSON.stringify(payload.extraction.calculation, null, 2) : "";
    calculation.placeholder = '{"operation":"craft","goals":{"gunpowder":10}}';
    details.append(calculation);
    controls.append(details);
    const continueTask = field(controls, "修改后重新回答原问题", "checkbox", true);
    const actions = element("div", undefined, "vision-actions");
    const submit = action => locked(async () => {
      const edits = rows.map(row => ({observation_id: row.observation.id, accepted: row.checked.checked,
        item_id: row.item.value.trim() || null, quantity: row.quantity.value === "" ? null : Number(row.quantity.value), description: row.description.value}));
      const body = {confirmation_id: crypto.randomUUID(), draft_revision: draft.revision,
        expected_state_revision: payload.scope_revision, action, subject: subject.value, observed_at: observed.value ? new Date(observed.value).toISOString() : defaultTime,
        container_id: container.value, inventory_complete: complete.checked, quantities_are_container_totals: totals.checked,
        same_snapshot: same.checked, edits, calculation: calculation.value.trim() ? JSON.parse(calculation.value) : null, continue_task: continueTask.checked};
      const prior = previousAttempt(draft.id);
      if (prior && JSON.stringify({...prior, confirmation_id: ""}) === JSON.stringify({...body, confirmation_id: ""})) body.confirmation_id = prior.confirmation_id;
      await confirm(draft.id, body);
    });
    actions.append(button("应用修改", () => submit("accept_for_turn")),
      button("保存到本人档案", () => submit("save_to_profile")),
      button("停止使用此图", () => locked(async () => { await hooks.api(`/api/conversations/${owner}/visual-drafts/${draft.id}/cancel`, {method: "POST", body: "{}"}); await reload(); })));
    controls.append(actions);
    corrections.append(controls);
    card.append(corrections);
    if (accepted && draft.available && interactive && previousAttempt(draft.id)) card.append(button("重试继续（不重复保存）", () => locked(() => confirm(draft.id, previousAttempt(draft.id)))));
    if (!active) card.append(element("p", "此卡片只读；可继续在聊天框追问。需要修正已确认信息时，请重新提交图片。"));
    if (payload.extraction.unknowns.length) card.append(element("p", "尚不明确：" + payload.extraction.unknowns.join("；")));
    updateControls();
  }
  function init(callbacks, capabilities) {
    hooks = callbacks;
    config = capabilities;
    const showStatus = () => { document.querySelector("#vision-status").textContent = config.enabled ? "接口已验证" : "首次发送时自动连接"; };
    showStatus();
    document.querySelector("#vision-add").onclick = () => document.querySelector("#vision-files").click();
    document.querySelector("#vision-files").onchange = event => { addFiles([...event.target.files]); event.target.value = ""; };
    document.querySelector("#vision-probe").onclick = () => locked(async () => {
      document.querySelector("#vision-status").textContent = "验证中…";
      try {
        await hooks.api("/api/vision/probe", {method: "POST", body: "{}"});
        config = (await hooks.api("/api/config")).vision;
      } finally { showStatus(); }
    });
    document.addEventListener("paste", event => {
      const files = [...(event.clipboardData?.files || [])];
      if (files.length) { event.preventDefault(); addFiles(files); }
    });
    const dropZone = document.querySelector(".composer-area");
    dropZone.addEventListener("dragover", event => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); dropZone.classList.add("vision-drop"); } });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("vision-drop"));
    dropZone.addEventListener("drop", event => { event.preventDefault(); dropZone.classList.remove("vision-drop"); addFiles([...event.dataTransfer.files]); });
    updateControls();
  }
  return {init, updateControls, requestFields, clear, render, forgetConversation, hasImages: () => pending.length > 0};
})();
