/**
 * Foundry Systems Cockpit Utilities
 * 1-click clipboard copy, toast notifications, modal helpers.
 */

function copyToClipboard(text, label = "Master Prompt") {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(`Copied ${label} to clipboard!`);
    }).catch(err => {
      fallbackCopyText(text, label);
    });
  } else {
    fallbackCopyText(text, label);
  }
}

function fallbackCopyText(text, label) {
  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.style.position = "fixed";
  textArea.style.left = "-999999px";
  textArea.style.top = "-999999px";
  document.body.appendChild(textArea);
  textArea.focus();
  textArea.select();
  try {
    document.execCommand('copy');
    showToast(`Copied ${label} to clipboard!`);
  } catch (err) {
    console.error('Fallback copy failed', err);
    showToast("Copy failed. Please manually select and copy.", "error");
  }
  document.body.removeChild(textArea);
}

function showToast(message, type = "success") {
  let toastContainer = document.getElementById("toast-container");
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.id = "toast-container";
    toastContainer.className = "fixed bottom-5 right-5 z-50 flex flex-col gap-2.5 pointer-events-none";
    document.body.appendChild(toastContainer);
  }

  const toast = document.createElement("div");
  let bgClass = "glass-panel border-emerald-500/50 text-emerald-300 shadow-2xl";
  let icon = '<span class="text-emerald-400 font-bold text-sm">✓</span>';

  if (type === "error") {
    bgClass = "glass-panel border-rose-500/50 text-rose-200 shadow-2xl";
    icon = '<span class="text-rose-400 font-bold text-sm">⚠</span>';
  } else if (type === "amber") {
    bgClass = "glass-panel border-amber-500/50 text-amber-300 shadow-2xl";
    icon = '<span class="text-amber-400 font-bold text-sm">⚡</span>';
  } else if (type === "info") {
    bgClass = "glass-panel border-sky-500/50 text-sky-300 shadow-2xl";
    icon = '<span class="text-sky-400 font-bold text-sm">ℹ</span>';
  }

  toast.className = `px-4 py-3 rounded-xl border backdrop-blur-md flex items-start gap-3 text-xs font-mono toast-enter pointer-events-auto max-w-md ${bgClass}`;
  toast.innerHTML = `
    <div class="shrink-0 mt-0.5">${icon}</div>
    <span class="tracking-wide leading-relaxed">${message}</span>
    <button onclick="this.closest('.toast-enter, [class*=toast]').remove()" class="shrink-0 ml-2 text-slate-400 hover:text-slate-200 text-sm font-bold transition-colors">✕</button>
  `;

  toastContainer.appendChild(toast);

  // Auto-dismiss after 6s for longer messages
  const dismissTime = message.length > 80 ? 6000 : 3500;
  setTimeout(() => {
    toast.classList.remove("toast-enter");
    toast.classList.add("toast-leave");
    setTimeout(() => {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 300);
  }, dismissTime);
}

function toggleModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    if (modal.classList.contains("hidden")) {
      modal.classList.remove("hidden");
      modal.classList.add("flex");
    } else {
      modal.classList.add("hidden");
      modal.classList.remove("flex");
    }
  }
}

function toggleSelectAll(masterCheckbox, targetName = "job_ids") {
  const checkboxes = document.querySelectorAll(`input[name="${targetName}"]`);
  checkboxes.forEach(cb => {
    cb.checked = masterCheckbox.checked;
  });
}

/**
 * Queue Inspector: Expand / Collapse All Messages
 */
let allMessagesExpanded = false;

function toggleExpandAllMessages() {
  allMessagesExpanded = !allMessagesExpanded;
  const btn = document.getElementById("toggle-expand-all-btn");
  const cards = document.querySelectorAll(".pitch-content");

  cards.forEach(card => {
    if (allMessagesExpanded) {
      card.classList.remove("line-clamp-2");
    } else {
      card.classList.add("line-clamp-2");
    }
  });

  if (btn) {
    btn.innerHTML = allMessagesExpanded
      ? '<span>👁️ Collapse All Messages</span>'
      : '<span>👁️ Expand All Messages</span>';
  }
}

function toggleRowExpand(jobId) {
  const el = document.getElementById(`msg-text-${jobId}`);
  if (el) {
    el.classList.toggle("line-clamp-2");
  }
}

/**
 * Queue Inspector: Edit Message Modal
 */
function openEditMessageModal(jobId, companyName) {
  const textEl = document.getElementById(`msg-text-${jobId}`);
  if (!textEl) return;

  const currentText = textEl.innerText.trim();
  document.getElementById("edit-modal-job-id").value = jobId;
  const textarea = document.getElementById("edit-modal-textarea");
  textarea.value = currentText;

  const subtitle = document.getElementById("edit-modal-subtitle");
  if (subtitle) {
    subtitle.innerText = `Tailored copy for ${companyName}`;
  }

  updateEditWordCount();
  textarea.oninput = updateEditWordCount;

  const modal = document.getElementById("edit-message-modal");
  if (modal) {
    modal.classList.remove("hidden");
    modal.classList.add("flex");
  }
}

function closeEditMessageModal() {
  const modal = document.getElementById("edit-message-modal");
  if (modal) {
    modal.classList.add("hidden");
    modal.classList.remove("flex");
  }
}

function updateEditWordCount() {
  const textarea = document.getElementById("edit-modal-textarea");
  const countEl = document.getElementById("edit-modal-wordcount");
  if (textarea && countEl) {
    const words = textarea.value.trim().split(/\s+/).filter(Boolean).length;
    countEl.innerText = `${words} / 60 words`;
    if (words > 60) {
      countEl.className = "text-rose-400 font-bold";
    } else {
      countEl.className = "text-slate-400";
    }
  }
}

async function saveMessageEdit(event) {
  event.preventDefault();
  const jobId = document.getElementById("edit-modal-job-id").value;
  const newContent = document.getElementById("edit-modal-textarea").value.trim();
  const saveBtn = document.getElementById("edit-modal-save-btn");

  if (!newContent) {
    showToast("Message copy cannot be empty", "error");
    return;
  }

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerText = "Saving...";
  }

  try {
    const formData = new FormData();
    formData.append("new_content", newContent);

    const res = await fetch(`/queue/jobs/${jobId}/update-message`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();

    if (data.success) {
      const textEl = document.getElementById(`msg-text-${jobId}`);
      if (textEl) {
        textEl.innerText = data.updated_content;
      }
      showToast("Pitch updated and saved successfully!", "success");
      closeEditMessageModal();
    } else {
      showToast(data.error || "Failed to update pitch", "error");
    }
  } catch (err) {
    console.error("Save edit failed:", err);
    showToast("Network error while saving message", "error");
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.innerText = "Save Message";
    }
  }
}

/**
 * 1-Click Facebook Messenger Launcher
 */
async function launchMessenger(jobId, messengerUrl) {
  const textEl = document.getElementById(`msg-text-${jobId}`);
  const content = textEl ? textEl.innerText.trim() : "";

  if (!content) {
    showToast("No message copy available to send", "error");
    return;
  }

  // 1. Copy message text to operator's clipboard
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(content).catch(() => fallbackCopyText(content, "Pitch"));
  } else {
    fallbackCopyText(content, "Pitch");
  }

  // 2. Show floating toast
  showToast("📋 Message copied to clipboard! Opening Messenger...", "success");

  // 3. Open Messenger in new tab
  const targetUrl = messengerUrl || "https://m.me/";
  window.open(targetUrl, "_blank", "noopener,noreferrer");

  // 4. Simultaneously call POST /queue/jobs/{job_id}/mark-sent
  try {
    const res = await fetch(`/queue/jobs/${jobId}/mark-sent`, {
      method: "POST",
    });
    const data = await res.json();

    if (data.success) {
      // Highlight row green
      const row = document.getElementById(`job-row-${jobId}`);
      if (row) {
        row.classList.add("bg-emerald-950/20", "border-l-4", "border-emerald-500");
      }

      // Update status badge
      const statusBadge = document.getElementById(`job-status-${jobId}`);
      if (statusBadge) {
        statusBadge.className = "inline-flex items-center px-2.5 py-0.5 rounded-full text-[10px] font-mono font-bold bg-emerald-950/80 text-emerald-400 border border-emerald-500/30";
        statusBadge.innerText = "SENT";
      }

      // Update button
      const btn = document.getElementById(`btn-messenger-${jobId}`);
      if (btn) {
        btn.className = "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-950/80 text-emerald-300 border border-emerald-500/40 text-xs font-mono font-semibold";
        btn.innerHTML = '<span>✓ Sent via Messenger</span>';
      }

      showToast("🚀 Lead marked SENT & moved to Inbox Awaiting Reply!", "success");
    }
  } catch (err) {
    console.error("Mark sent error:", err);
  }
}
