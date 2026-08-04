const state = {
  token: sessionStorage.getItem("factoryToken") || "",
  accounts: [],
  selectedAccountId: localStorage.getItem("factoryAccount") || "",
  drafts: [],
  jobs: [],
  selectedDraftId: null,
  liveMode: false,
  pendingAttachments: [],
};

const DEFAULT_REQUEST_TIMEOUT_MS = 30000;
const CREATIVE_REQUEST_TIMEOUT_MS = 120000;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  if (window.lucide) window.lucide.createIcons();
  loadBootstrap();
});

function bindEvents() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $$("[data-view-jump]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewJump)));
  $("#menuButton").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
  ["#refreshButton", "#overviewRefresh", "#queueRefresh"].forEach((id) => $(id).addEventListener("click", loadBootstrap));
  ["#newDraftButton", "#addDraftIcon"].forEach((id) => $(id).addEventListener("click", newDraft));
  $("#quickGenerateButton").addEventListener("click", () => { showView("creative"); $("#chatInput").focus(); });
  $("#chatForm").addEventListener("submit", sendChat);
  $("#attachPhotoButton").addEventListener("click", () => $("#photoInput").click());
  $("#photoInput").addEventListener("change", (event) => uploadPhotos(event.target.files));
  $("#dispatchButton").addEventListener("click", dispatchDraft);
  $("#saveDraftButton").addEventListener("click", saveDraft);
  $("#testMode").addEventListener("click", () => setMode(false));
  $("#liveMode").addEventListener("click", () => setMode(true));
  $$("[data-status]").forEach((button) => button.addEventListener("click", () => updateDraftStatus(button.dataset.status)));
  $("#saveSchedule").addEventListener("click", saveSchedule);
  $("#authForm").addEventListener("submit", (event) => {
    event.preventDefault();
    state.token = $("#tokenInput").value.trim();
    sessionStorage.setItem("factoryToken", state.token);
    $("#authScreen").classList.add("hidden");
    loadBootstrap();
  });
}

async function api(path, options = {}) {
  const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchOptions } = options;
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, { ...fetchOptions, headers, signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds. Nothing was queued.`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  if (response.status === 401) {
    $("#authScreen").classList.remove("hidden");
    throw new Error("Authentication required");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

async function loadBootstrap() {
  try {
    const suffix = state.selectedAccountId ? `?account_id=${encodeURIComponent(state.selectedAccountId)}` : "";
    const data = await api(`/api/bootstrap${suffix}`);
    state.accounts = data.accounts;
    state.selectedAccountId = data.selected_account_id;
    state.drafts = data.drafts;
    state.jobs = data.jobs;
    localStorage.setItem("factoryAccount", state.selectedAccountId || "");
    render();
    $("#serverClock").textContent = `Synced ${formatTime(data.server_time)}`;
  } catch (error) {
    if (error.message !== "Authentication required") toast(error.message, true);
  }
}

async function selectAccount(accountId) {
  clearPendingAttachments();
  state.selectedAccountId = accountId;
  state.selectedDraftId = null;
  localStorage.setItem("factoryAccount", accountId);
  $(".sidebar").classList.remove("open");
  await loadBootstrap();
}

function render() {
  const account = selectedAccount();
  renderAccounts();
  if (!account) return;
  $("#accountTitle").textContent = account.display_name;
  $("#accountEyebrow").textContent = `${account.pipeline.toUpperCase()} account workspace`;
  $("#pipelineStatus").textContent = account.status.replace("_", " ");
  $("#cadenceValue").textContent = `Every ${account.interval_days} day${account.interval_days === 1 ? "" : "s"}`;
  $("#publishValue").textContent = account.publish_time;
  $("#platformValue").textContent = account.platforms.replace(",", " + ");
  $("#activeJobs").textContent = state.jobs.filter((job) => ["queued", "in_progress"].includes(job.status)).length;
  $("#draftCount").textContent = state.drafts.filter((draft) => draft.status !== "archived").length;
  $("#cadenceMetric").textContent = `${account.interval_days}d`;
  const complete = state.jobs.filter((job) => ["succeeded", "failed"].includes(job.status));
  const success = complete.filter((job) => job.status === "succeeded").length;
  $("#successRate").textContent = complete.length ? `${Math.round(success / complete.length * 100)}%` : "-";
  renderJobs();
  renderDrafts();
  renderSchedule(account);
  if (!state.selectedDraftId && state.drafts.length) state.selectedDraftId = state.drafts[0].id;
  renderWorkbench();
  $("#quickGenerateButton").disabled = !account.creative_ready;
}

function renderAccounts() {
  $("#accountList").innerHTML = state.accounts.map((account) => `
    <button class="account-item ${account.account_id === state.selectedAccountId ? "active" : ""}" data-account="${escapeHtml(account.account_id)}">
      <span class="account-avatar">${initials(account.display_name)}</span>
      <span class="account-copy"><strong>${escapeHtml(account.display_name)}</strong><span>${account.status.replace("_", " ")}</span></span>
      <span class="state-dot ${account.status}"></span>
    </button>`).join("");
  $$('[data-account]').forEach((button) => button.addEventListener("click", () => selectAccount(button.dataset.account)));
}

function renderJobs() {
  const rows = state.jobs.length ? state.jobs.map(jobRow).join("") : `<tr><td colspan="7" class="empty-row">No cloud jobs for this account yet.</td></tr>`;
  $("#allJobs").innerHTML = rows;
  $("#recentJobs").innerHTML = state.jobs.length ? state.jobs.slice(0, 6).map((job) => `
    <tr><td>${escapeHtml(job.concept_id)}</td><td>${statusPill(job.status)}</td><td>${formatDate(job.publish_at)}</td><td>${formatDate(job.created_at)}</td><td>${job.github_run_url ? `<a class="job-link" href="${job.github_run_url}" target="_blank" title="Open run">↗</a>` : ""}</td></tr>`).join("") : `<tr><td colspan="5" class="empty-row">No generation history.</td></tr>`;
}

function jobRow(job) {
  return `<tr><td>${escapeHtml(job.id)}</td><td>${escapeHtml(job.concept_id)}</td><td>${statusPill(job.status)}</td><td>${job.dry_run ? "Test" : "Live"}</td><td>${formatDate(job.publish_at)}</td><td>${formatDate(job.updated_at)}</td><td>${job.github_run_url ? `<a class="job-link" href="${job.github_run_url}" target="_blank">Open ↗</a>` : "Matching run..."}</td></tr>`;
}

function renderDrafts() {
  const html = state.drafts.length ? state.drafts.map((draft) => `
    <div class="draft-row ${draft.id === state.selectedDraftId ? "active" : ""}" data-draft="${draft.id}">
      <div><strong>${escapeHtml(draft.title)}</strong><span>${escapeHtml(draft.status)} · v${draft.version}</span></div><time>${formatShortDate(draft.updated_at)}</time>
    </div>`).join("") : `<div class="empty-row">No drafts. Start with a brief in Creative lab.</div>`;
  $("#recentDrafts").innerHTML = html;
  $("#draftRail").innerHTML = html;
  $$('[data-draft]').forEach((row) => row.addEventListener("click", () => { clearPendingAttachments(); state.selectedDraftId = row.dataset.draft; showView("creative"); renderDrafts(); renderWorkbench(); }));
}

function renderWorkbench() {
  const draft = selectedDraft();
  $("#draftTitle").textContent = draft?.title || "New concept";
  $$("[data-status]").forEach((button) => button.classList.toggle("active", (draft?.status || "draft") === button.dataset.status));
  if (!draft?.chat_history?.length) {
    $("#chatHistory").innerHTML = `<div class="empty-chat"><i data-lucide="message-square-text"></i><h3>Shape the next video</h3><p>Describe the topic, hook, pacing, visual direction, CTA, or a correction to an existing draft.</p></div>`;
  } else {
    $("#chatHistory").innerHTML = draft.chat_history.map((message) => {
      const attachments = (message.attachments || []).map((name) => `<span><i data-lucide="image"></i>${escapeHtml(name)}</span>`).join("");
      return `<div class="message ${message.role}">${escapeHtml(message.content)}${attachments ? `<div class="message-attachments">${attachments}</div>` : ""}</div>`;
    }).join("");
    $("#chatHistory").scrollTop = $("#chatHistory").scrollHeight;
  }
  renderAttachmentTray();
  const stats = draftStats(draft);
  $("#sceneCount").textContent = stats.scenes ?? "-";
  $("#durationEstimate").textContent = stats.duration ? `${stats.duration}s` : "-";
  $("#draftVersion").textContent = draft ? `v${draft.version}` : "-";
  $("#dispatchButton").disabled = !draft;
  $("#saveDraftButton").disabled = !draft;
  if (window.lucide) window.lucide.createIcons();
}

function renderSchedule(account) {
  $("#scheduleEnabled").checked = account.enabled;
  $("#scheduleEnabled").disabled = !account.autopilot_ready;
  $("#intervalDays").value = account.interval_days;
  $("#scheduleTime").value = account.publish_time;
  $("#scheduleTimezone").value = account.timezone;
  $("#scheduleRequirement").textContent = account.autopilot_ready
    ? "Recurring generation uses this account's approved concept rotation. Manual drafts and direct publishing remain available independently."
    : "Manual drafts, direct cloud generation, and Metricool publishing are available. Add an autopilot manifest only when you want recurring scheduled generation.";
}

async function sendChat(event) {
  event.preventDefault();
  const message = $("#chatInput").value.trim();
  if (!message) return;
  const button = $("#sendChat");
  setBusy(button, true, "Thinking");
  const startedAt = Date.now();
  const elapsedTimer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    setBusy(button, true, `Thinking ${seconds}s`);
  }, 1000);
  try {
    const data = await api("/api/chat", { method: "POST", timeoutMs: CREATIVE_REQUEST_TIMEOUT_MS, body: JSON.stringify({
      account_id: state.selectedAccountId,
      message,
      draft_id: state.selectedDraftId,
      attachment_ids: state.pendingAttachments.map((attachment) => attachment.id),
    }) });
    const index = state.drafts.findIndex((draft) => draft.id === data.draft.id);
    if (index >= 0) state.drafts[index] = data.draft; else state.drafts.unshift(data.draft);
    state.selectedDraftId = data.draft.id;
    $("#chatInput").value = "";
    clearPendingAttachments();
    render();
    toast("Creative draft saved to the cloud.");
  } catch (error) { toast(error.message, true); }
  finally {
    window.clearInterval(elapsedTimer);
    setBusy(button, false, "Send");
  }
}

async function updateDraftStatus(status) {
  const draft = selectedDraft();
  if (!draft) return;
  try {
    const updated = await api(`/api/drafts/${draft.id}`, { method: "PATCH", body: JSON.stringify({ status }) });
    Object.assign(draft, updated);
    renderWorkbench(); renderDrafts();
  } catch (error) { toast(error.message, true); }
}

async function saveDraft() {
  const draft = selectedDraft();
  if (!draft) return;
  try {
    const updated = await api(`/api/drafts/${draft.id}`, { method: "PATCH", body: JSON.stringify({ title: draft.title, caption: draft.caption }) });
    Object.assign(draft, updated); render(); toast("Draft saved.");
  } catch (error) { toast(error.message, true); }
}

async function dispatchDraft() {
  const draft = selectedDraft();
  if (!draft) return;
  const button = $("#dispatchButton");
  setBusy(button, true, "Queueing");
  const localValue = $("#publishAt").value;
  const publishAt = localValue ? new Date(localValue).toISOString() : null;
  try {
    const job = await api(`/api/drafts/${draft.id}/generate`, { method: "POST", body: JSON.stringify({ publish_at: publishAt, dry_run: !state.liveMode, skip_publish: $("#skipPublish").checked }) });
    state.jobs.unshift(job); showView("queue"); render(); toast("Video queued in the cloud.");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false, "Send to cloud"); }
}

async function saveSchedule() {
  const button = $("#saveSchedule"); setBusy(button, true, "Saving");
  try {
    const account = await api(`/api/accounts/${state.selectedAccountId}/schedule`, { method: "PATCH", body: JSON.stringify({ enabled: $("#scheduleEnabled").checked, interval_days: Number($("#intervalDays").value), publish_time: $("#scheduleTime").value, timezone: $("#scheduleTimezone").value.trim() }) });
    const index = state.accounts.findIndex((item) => item.account_id === account.account_id); state.accounts[index] = account; render(); toast("Cloud schedule updated.");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false, "Save schedule"); }
}

function newDraft() {
  clearPendingAttachments(); state.selectedDraftId = null; showView("creative"); renderWorkbench(); $("#chatInput").focus();
}

async function uploadPhotos(fileList) {
  const files = [...(fileList || [])];
  $("#photoInput").value = "";
  if (!files.length) return;
  const draftAttachmentCount = selectedDraft()?.attachments?.length || 0;
  const room = 6 - draftAttachmentCount - state.pendingAttachments.length;
  if (room <= 0) { toast("A draft can use at most 6 reference photos.", true); return; }
  const selected = files.slice(0, room);
  if (selected.length < files.length) toast(`Only ${selected.length} more photo${selected.length === 1 ? "" : "s"} can be attached.`, true);
  const button = $("#attachPhotoButton");
  button.disabled = true;
  try {
    for (const file of selected) {
      const previewUrl = URL.createObjectURL(file);
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        const uploaded = await api(`/api/accounts/${encodeURIComponent(state.selectedAccountId)}/attachments`, { method: "POST", body: form });
        state.pendingAttachments.push({ ...uploaded, preview_url: previewUrl });
        renderAttachmentTray();
      } catch (error) {
        URL.revokeObjectURL(previewUrl);
        throw error;
      }
    }
    toast(`${selected.length} reference photo${selected.length === 1 ? "" : "s"} attached.`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function renderAttachmentTray() {
  const existing = selectedDraft()?.attachments || [];
  const existingHtml = existing.map((attachment) => `
    <div class="attachment-card stored" title="${escapeHtml(attachment.filename)}">
      <span class="attachment-placeholder"><i data-lucide="image"></i></span>
      <span>${escapeHtml(attachment.filename)}</span>
    </div>`).join("");
  const pendingHtml = state.pendingAttachments.map((attachment) => `
    <div class="attachment-card" title="${escapeHtml(attachment.filename)}">
      <img class="attachment-thumb" src="${attachment.preview_url}" alt="">
      <span>${escapeHtml(attachment.filename)}</span>
      <button type="button" class="attachment-remove" data-remove-attachment="${escapeHtml(attachment.id)}" title="Remove ${escapeHtml(attachment.filename)}" aria-label="Remove ${escapeHtml(attachment.filename)}"><i data-lucide="x"></i></button>
    </div>`).join("");
  $("#attachmentTray").innerHTML = existingHtml + pendingHtml;
  $("#attachmentTray").classList.toggle("visible", Boolean(existingHtml || pendingHtml));
  $$('[data-remove-attachment]').forEach((button) => button.addEventListener("click", () => removePendingAttachment(button.dataset.removeAttachment)));
  if (window.lucide) window.lucide.createIcons();
}

function removePendingAttachment(attachmentId) {
  const index = state.pendingAttachments.findIndex((attachment) => attachment.id === attachmentId);
  if (index < 0) return;
  URL.revokeObjectURL(state.pendingAttachments[index].preview_url);
  state.pendingAttachments.splice(index, 1);
  renderAttachmentTray();
}

function clearPendingAttachments() {
  state.pendingAttachments.forEach((attachment) => URL.revokeObjectURL(attachment.preview_url));
  state.pendingAttachments = [];
  if ($("#attachmentTray")) renderAttachmentTray();
}

function setMode(live) {
  state.liveMode = live;
  $("#testMode").classList.toggle("active", !live);
  $("#liveMode").classList.toggle("active", live);
}

function showView(view) {
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $(".sidebar").classList.remove("open");
}

function selectedAccount() { return state.accounts.find((account) => account.account_id === state.selectedAccountId); }
function selectedDraft() { return state.drafts.find((draft) => draft.id === state.selectedDraftId); }
function initials(name) { return name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase(); }
function statusPill(status) { return `<span class="status-pill ${status}">${status.replace("_", " ")}</span>`; }
function formatDate(value) { return value ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value)) : "Not scheduled"; }
function formatShortDate(value) { return value ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(value)) : ""; }
function formatTime(value) { return value ? new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(value)) : ""; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
function draftStats(draft) {
  if (!draft?.creative_spec) return {};
  const spec = draft.creative_spec; const scenes = [...(spec.hooks || []), ...(spec.ctas || [])];
  (spec.mains || []).forEach((main) => scenes.push(...(main.segments || [main])));
  return { scenes: scenes.length, duration: scenes.reduce((total, scene) => total + Number(scene.duration_seconds || spec.defaults?.duration_seconds || 5), 0) };
}
function setBusy(button, busy, text) { button.disabled = busy; const span = button.querySelector("span"); if (span) span.textContent = text; }
function toast(message, error = false) { const node = document.createElement("div"); node.className = `toast${error ? " error" : ""}`; node.textContent = message; $("#toastRegion").appendChild(node); setTimeout(() => node.remove(), 4200); }
