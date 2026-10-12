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
