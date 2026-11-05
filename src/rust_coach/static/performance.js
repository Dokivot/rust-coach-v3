const runsElement = document.querySelector("#runs");
const statusElement = document.querySelector("#status");
const refreshButton = document.querySelector("#refresh");

async function refreshPerformance() {
  refreshButton.disabled = true;
  try {
    const response = await fetch("/api/performance?limit=100");
    if (!response.ok) throw new Error("无法读取运行记录");
    const report = await response.json();
    statusElement.textContent = "共 " + report.total_runs + " 条运行记录" +
      (report.truncated ? "，仅展示最近 100 条" : "") + "。计量状态：" + report.summary.recording_status +
      "。未启用整次请求共享预算。";
    runsElement.replaceChildren();
    for (const run of [...report.runs].reverse()) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = run.started_at + " · " + run.kind + " · " + run.status + " · " +
        (run.duration_ms === null ? "尚未结束" : run.duration_ms + " ms") + " · 模型 " + run.summary.model_calls + " 次";
      const content = document.createElement("pre");
      content.textContent = JSON.stringify(run, null, 2);
      details.append(summary, content);
      runsElement.append(details);
    }
  } catch (error) {
    statusElement.textContent = error.message;
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", refreshPerformance);
refreshPerformance();
