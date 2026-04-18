// Tracked-list UI: render, poll, per-row actions (copy / cancel / remove),
// panel collapse/expand, and the "clear past entries" batch action.
//
// Single-source-of-truth is the server (/api/secrets/tracked); the browser
// just joins in its local URL cache to decide which rows are re-copyable.
import { getUrl, forgetUrl, gcUrls } from './url-cache.js';

// ---------- polling ----------

let trackedPollId = null;
const TRACKED_POLL_MS = 5000;

function startTrackedPoll() {
  if (trackedPollId) return;
  trackedPollId = setInterval(pollTrackedOnce, TRACKED_POLL_MS);
}

function stopTrackedPoll() {
  if (trackedPollId) { clearInterval(trackedPollId); trackedPollId = null; }
}

async function pollTrackedOnce() {
  const items = await fetchTracked();
  if (items === null) return;  // transient network error; try again next tick

  const list = document.getElementById('tracked-list');
  const existing = [...list.querySelectorAll('li[data-id]')];
  const existingMap = new Map(existing.map(li => [li.dataset.id, li.dataset.status]));
  const serverMap = new Map(items.map(i => [i.id, i.status]));

  const same = existing.length === items.length
    && [...serverMap].every(([id, s]) => existingMap.get(id) === s);

  // Respect in-flight user interaction (copy flash, remove click).
  const busy = list.querySelector('[data-busy="1"]') !== null;

  if (!same && !busy) await renderTrackedList();

  if (!items.some(i => i.status === 'pending')) stopTrackedPoll();
}

async function fetchTracked() {
  // Returns null on failure (so callers can leave local state alone)
  // and an array (possibly empty) on success.
  try {
    const res = await fetch('/api/secrets/tracked');
    if (res.status === 401) { window.location.reload(); return null; }
    if (!res.ok) return null;
    const body = await res.json();
    return body.items || [];
  } catch {
    return null;
  }
}

async function untrackOnServer(id) {
  try {
    await fetch(`/api/secrets/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch {}
}

async function cancelOnServer(id) {
  try {
    const res = await fetch(`/api/secrets/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
    return res.ok || res.status === 204;
  } catch { return false; }
}

function fmtRelative(iso) {
  const then = new Date(iso).getTime();
  const diff = (Date.now() - then) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + ' min ago';
  if (diff < 86400) return Math.floor(diff / 3600) + ' h ago';
  return Math.floor(diff / 86400) + ' d ago';
}

async function copyRowUrl(li, timeEl, originalTimeText, url) {
  if (li.dataset.busy === '1') return;
  li.dataset.busy = '1';
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(url);
      ok = true;
    } else {
      const ta = document.createElement('textarea');
      ta.value = url;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }
  } catch {
    ok = false;
  }
  li.classList.add(ok ? 'flash-copy' : 'flash-error');
  timeEl.textContent = ok ? 'copied to clipboard' : 'copy failed';
  li.setAttribute('aria-live', 'polite');
  setTimeout(() => {
    li.classList.remove('flash-copy', 'flash-error');
    timeEl.textContent = originalTimeText;
    delete li.dataset.busy;
  }, 1500);
}

// ---------- render ----------

export async function renderTrackedList() {
  const section = document.getElementById('tracked-section');
  const list = document.getElementById('tracked-list');
  const header = document.getElementById('tracked-header');
  const countEl = document.getElementById('tracked-count');

  const items = await fetchTracked();
  if (items === null) return;          // fetch failed; leave UI + URL cache alone
  gcUrls(items.map(i => i.id));

  if (items.length === 0) {
    section.hidden = true;
    section.classList.remove('open');
    if (header) header.setAttribute('aria-expanded', 'false');
    stopTrackedPoll();
    return;
  }
  section.hidden = false;
  if (countEl) countEl.textContent = String(items.length);

  list.innerHTML = '';
  for (const item of items) {
    const li = document.createElement('li');
    li.className = 'tracked-item';
    li.dataset.id = item.id;
    li.dataset.status = item.status;

    const fallback = item.content_type === 'image' ? 'Image secret' : 'Text secret';
    const labelText = (item.label && item.label.trim()) ? item.label : fallback;

    const labelEl = document.createElement('span');
    labelEl.className = 'label';
    labelEl.textContent = labelText;
    // Long labels truncate with ellipsis (see CSS); carry the full text
    // in the tooltip so hovering still reveals everything. Skip the
    // tooltip for fallback labels -- they're short and non-informative.
    if (item.label && item.label.trim()) {
      labelEl.title = labelText;
    }

    const timeEl = document.createElement('span');
    timeEl.className = 'time';
    // When there's no user-supplied label, the fallback ("Image secret" /
    // "Text secret") is already shown as the label -- no need to repeat it
    // in the footnote. Keep the footnote strictly about timing.
    let timeText = 'created ' + fmtRelative(item.created_at);
    if (item.status === 'viewed' && item.viewed_at) {
      timeText += ' · viewed ' + fmtRelative(item.viewed_at);
    } else if (item.status === 'burned' && item.viewed_at) {
      timeText += ' · burned ' + fmtRelative(item.viewed_at);
    } else if (item.status === 'canceled' && item.viewed_at) {
      timeText += ' · canceled ' + fmtRelative(item.viewed_at);
    } else if (item.status === 'expired') {
      timeText += ' · expired ' + fmtRelative(item.expires_at);
    }
    timeEl.textContent = timeText;
    // Exact timestamps on hover for older entries where "3d ago" is ambiguous.
    const hoverBits = [`created ${new Date(item.created_at).toLocaleString()}`];
    if (item.viewed_at) hoverBits.push(`${item.status} ${new Date(item.viewed_at).toLocaleString()}`);
    else if (item.status === 'expired') hoverBits.push(`expired ${new Date(item.expires_at).toLocaleString()}`);
    timeEl.title = hoverBits.join(' · ');

    const meta = document.createElement('div');
    meta.className = 'tracked-meta';
    meta.appendChild(labelEl);
    meta.appendChild(timeEl);

    const pill = document.createElement('span');
    pill.className = 'status-pill ' + item.status;
    pill.textContent = item.status;

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'tracked-remove';
    rm.setAttribute('aria-label', 'remove from list');
    rm.title = 'remove';
    rm.textContent = '×';

    const right = document.createElement('div');
    right.className = 'tracked-right';
    right.appendChild(pill);

    // Cancel action: only for pending (live) secrets. Two-click confirm
    // pattern so accidental clicks don't revoke a link.
    if (item.status === 'pending') {
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'tracked-cancel';
      cancelBtn.textContent = 'cancel';
      cancelBtn.title = 'Revoke the URL so the receiver can no longer view this';
      cancelBtn.setAttribute('aria-label', 'cancel this secret');
      let armTimer = null;
      cancelBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!cancelBtn.classList.contains('armed')) {
          cancelBtn.classList.add('armed');
          cancelBtn.textContent = 'confirm?';
          armTimer = setTimeout(() => {
            cancelBtn.classList.remove('armed');
            cancelBtn.textContent = 'cancel';
          }, 3000);
          return;
        }
        if (armTimer) clearTimeout(armTimer);
        cancelBtn.disabled = true;
        cancelBtn.textContent = 'canceling…';
        await cancelOnServer(item.id);
        forgetUrl(item.id);
        renderTrackedList();
      });
      right.appendChild(cancelBtn);
    }

    right.appendChild(rm);

    li.appendChild(meta);
    li.appendChild(right);

    const cachedUrl = getUrl(item.id);
    const copyable = Boolean(cachedUrl) && item.status === 'pending';

    if (copyable) {
      li.classList.add('copyable');
      li.setAttribute('role', 'button');
      li.setAttribute('tabindex', '0');
      li.title = 'Click to copy link';
      const activate = async (e) => {
        // Ignore clicks that originated on row-level action buttons.
        if (e.target && e.target.closest('.tracked-remove, .tracked-cancel')) return;
        await copyRowUrl(li, timeEl, timeText, cachedUrl);
      };
      li.addEventListener('click', activate);
      li.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(e); }
      });
    } else if (!cachedUrl && item.status === 'pending') {
      // Tracked on this server but we don't have the URL -- it was created in a
      // different browser (or this browser's storage got cleared). We can never
      // reconstruct it server-side because the key fragment never leaves the
      // creating browser. Make the row clearly "informational, not actionable".
      li.classList.add('orphan');
      li.title =
        'The URL includes an encryption key stored only in the browser where this ' +
        'secret was created. Open ephemera in that browser to copy the link.';
      const hint = document.createElement('span');
      hint.className = 'orphan-hint';
      hint.textContent = 'created elsewhere';
      meta.appendChild(hint);
    }

    rm.addEventListener('click', async (e) => {
      e.stopPropagation();
      await untrackOnServer(item.id);
      forgetUrl(item.id);
      renderTrackedList();
    });

    list.appendChild(li);
  }

  // Reveal the "clear past entries" action only when there's something to clear.
  const clearBtn = document.getElementById('tracked-clear');
  const clearLbl = document.getElementById('tracked-clear-label');
  const nonPending = items.filter(i => i.status !== 'pending').length;
  if (clearBtn) {
    clearBtn.hidden = nonPending === 0;
    if (clearLbl && nonPending > 0) {
      const word = nonPending === 1 ? 'entry' : 'entries';
      clearLbl.textContent = `Clear ${nonPending} past ${word}`;
    }
  }

  if (items.some(i => i.status === 'pending')) startTrackedPoll();
  else stopTrackedPoll();
}

// ---------- top-level wiring ----------

// Clear-history action: same 2-click arm pattern as per-row cancel. First
// click arms (danger tint + "confirm?"), second click within 3s executes.
// We mutate only the #tracked-clear-label span so the icon (a sibling SVG)
// stays put across state transitions.
(function wireClearHistory() {
  const clearBtn = document.getElementById('tracked-clear');
  const clearLbl = document.getElementById('tracked-clear-label');
  if (!clearBtn || !clearLbl) return;
  let armTimer = null;
  // Remember the last "idle" label so we can restore it (it carries the
  // current count, set by renderTrackedList, and may differ between calls).
  function idleLabel() { return clearBtn.dataset.idleLabel || 'Clear past entries'; }
  clearBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!clearBtn.classList.contains('armed')) {
      clearBtn.dataset.idleLabel = clearLbl.textContent;
      clearBtn.classList.add('armed');
      clearLbl.textContent = 'confirm?';
      armTimer = setTimeout(() => {
        clearBtn.classList.remove('armed');
        clearLbl.textContent = idleLabel();
      }, 3000);
      return;
    }
    if (armTimer) clearTimeout(armTimer);
    clearBtn.disabled = true;
    clearLbl.textContent = 'clearing…';
    try {
      await fetch('/api/secrets/tracked/clear', { method: 'POST' });
    } catch {}
    clearBtn.classList.remove('armed');
    clearLbl.textContent = idleLabel();
    clearBtn.disabled = false;
    renderTrackedList();
  });
})();

// Panel toggle: whole header bar acts as the expand/collapse control.
(function wireTrackedToggle() {
  const section = document.getElementById('tracked-section');
  const header = document.getElementById('tracked-header');
  if (!section || !header) return;
  header.addEventListener('click', () => {
    const open = section.classList.toggle('open');
    header.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
})();
