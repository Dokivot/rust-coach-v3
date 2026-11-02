window.VisionUI = (() => {
  let hooks;
  let config = {};
  let pending = [];

  function updateControls() {
    const disabled = !hooks || hooks.busy() || !hooks.conversation();
    document.querySelector("#vision-add").disabled = disabled;
    document.querySelector("#vision-files").disabled = disabled;
    document.querySelectorAll(".vision-pending button").forEach(button => { button.disabled = disabled; });
  }

  function renderPending() {
    const container = document.querySelector("#vision-pending");
    container.replaceChildren();
    container.hidden = !pending.length;
    pending.forEach((item, index) => {
      const card = document.createElement("div");
      card.className = "vision-pending-card";
      const image = document.createElement("img");
      image.src = item.url;
      image.alt = "待发送图片 " + (index + 1);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "移除图片 " + (index + 1));
      remove.addEventListener("click", () => {
        if (hooks.busy()) return;
        URL.revokeObjectURL(item.url);
        pending = pending.filter(candidate => candidate !== item);
        hooks.invalidate();
        renderPending();
        hooks.refreshControls();
      });
      card.append(image, remove);
      container.append(card);
    });
    updateControls();
  }

  function addFiles(files) {
    if (hooks.busy() || !hooks.conversation()) return;
    hooks.error();
    for (const file of files) {
      if (pending.length >= 4) { hooks.error("每次最多发送四张图片。"); break; }
      if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
        hooks.error("请使用 PNG、JPEG 或 WebP 图片。"); continue;
      }
      if (file.size > (config.max_bytes || 8388608)) { hooks.error("图片超过 8 MiB，请缩小后再发。"); continue; }
      pending.push({blob: file, url: URL.createObjectURL(file), attachment: null});
    }
    hooks.invalidate();
    renderPending();
    hooks.refreshControls();
  }

  async function requestFields(attachments, conversationId) {
    if (attachments.length && !config.enabled) {
      await hooks.api("/api/vision/probe", {method: "POST", body: "{}"});
      config = (await hooks.api("/api/config")).vision;
    }
    for (const item of attachments) {
      if (item.attachment) continue;
      const body = new FormData();
      body.append("file", item.blob, "screenshot");
      const response = await fetch("/api/conversations/" + conversationId + "/attachments", {method: "POST", body});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || "图片上传失败，请重试。");
      item.attachment = result;
    }
    return {attachment_ids: attachments.map(item => item.attachment.id)};
  }

  function takePending() {
    const attachments = pending;
    pending = [];
    renderPending();
    return attachments;
  }

  function restorePending(attachments) {
    pending = attachments;
    renderPending();
  }

  function release(attachments) {
    attachments.forEach(item => URL.revokeObjectURL(item.url));
  }

  function clear() {
    release(takePending());
  }

  function init(callbacks) {
    hooks = callbacks;
    document.querySelector("#vision-add").onclick = () => document.querySelector("#vision-files").click();
    document.querySelector("#vision-files").onchange = event => { addFiles([...event.target.files]); event.target.value = ""; };
    document.addEventListener("paste", event => {
      const files = [...(event.clipboardData?.files || [])];
      if (files.length) { event.preventDefault(); addFiles(files); }
    });
    const dropZone = document.querySelector(".composer-area");
    dropZone.addEventListener("dragover", event => {
      if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); dropZone.classList.add("vision-drop"); }
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("vision-drop"));
    dropZone.addEventListener("drop", event => {
      event.preventDefault(); dropZone.classList.remove("vision-drop"); addFiles([...event.dataTransfer.files]);
    });
    updateControls();
  }

  return {init, configure: capabilities => { config = capabilities; }, updateControls, requestFields,
    takePending, restorePending, release, clear, hasImages: () => pending.length > 0};
})();
