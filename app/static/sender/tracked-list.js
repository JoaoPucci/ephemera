// Tracked-list UI: render, poll, per-row actions (copy / cancel / remove),
// panel collapse/expand, and the "clear past entries" batch action.
//
// Single-source-of-truth is the server (/api/secrets/tracked); the browser
// just joins in its local URL cache to decide which rows are re-copyable.
import { bindTwoClickConfirm } from '../two-click.js';
import { forgetUrl, gcUrls, getUrl } from './url-cache.js';

// ---------- polling ----------

let trackedPollId = null;
const TRACKED_POLL_MS = 5000;

function startTrackedPoll() {
  if (trackedPollId) return;
  trackedPollId = setInterval(pollTrackedOnce, TRACKED_POLL_MS);
}

function stopTrackedPoll() {
  if (trackedPollId) {
    clearInterval(trackedPollId);
    trackedPollId = null;
  }
}

async function pollTrackedOnce() {
  const items = await fetchTracked();
  if (items === null) return; // transient network error; try again next tick

  const list = document.getElementById('tracked-list');
  const existing = [...list.querySelectorAll('li[data-id]')];
  const existingMap = new Map(existing.map((li) => [li.dataset.id, li.dataset.status]));
  const serverMap = new Map(items.map((i) => [i.id, i.status]));

  const same =
    existing.length === items.length &&
    [...serverMap].every(([id, s]) => existingMap.get(id) === s);

  // Respect in-flight user interaction (copy flash, remove click).
  const busy = list.querySelector('[data-busy="1"]') !== null;

  if (!same && !busy) await renderTrackedList();

  if (!items.some((i) => i.status === 'pending')) stopTrackedPoll();
}

async function fetchTracked() {
  // Returns null on failure (so callers can leave local state alone)
  // and an array (possibly empty) on success.
  try {
    const res = await fetch('/api/secrets/tracked');
    if (res.status === 401) {
      window.location.reload();
      return null;
    }
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
  } catch {
    return false;
  }
}

function fmtRelative(iso) {
  // Intl.RelativeTimeFormat returns locale-correct phrasing AND handles
  // plural forms per target locale natively ("il y a 2 heures" in fr,
  // "2時間前" in ja, etc.). "short" keeps the phrasing compact to match
  // the tracked-list row UX; "auto" swaps numbers for words at the
  // obvious boundaries ("yesterday" instead of "1 day ago").
  const rtf = new Intl.RelativeTimeFormat(window.i18n.currentLocale, {
    numeric: 'auto',
    style: 'short',
  });
  const deltaSeconds = (new Date(iso).getTime() - Date.now()) / 1000; // negative = past
  const abs = Math.abs(deltaSeconds);
  if (abs < 60) return rtf.format(Math.round(deltaSeconds), 'second');
  if (abs < 3600) return rtf.format(Math.round(deltaSeconds / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(deltaSeconds / 3600), 'hour');
  return rtf.format(Math.round(deltaSeconds / 86400), 'day');
}

function pluralKey(base, n) {
  // Intl.PluralRules returns the CLDR category for the count ("one",
  // "other", and in some locales "zero"/"two"/"few"/"many"). Callers
  // place one template per category under the base key; the English
  // catalog covers "one" + "other", other locales add what they need.
  // The shim falls back to English if a category is missing.
  const cat = new Intl.PluralRules(window.i18n.currentLocale).select(n);
  return `${base}.${cat}`;
}

async function copyRowUrl(li, timeEl, originalTimeText, url) {
  if (li.dataset.busy === '1') return;
  li.dataset.busy = '1';
  let ok = false;
  try {
    if (navigator.clipboard?.writeText) {
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
  timeEl.textContent = ok ? window.i18n.t('tracked.copy_ok') : window.i18n.t('tracked.copy_fail');
  li.setAttribute('aria-live', 'polite');
  setTimeout(() => {
    li.classList.remove('flash-copy', 'flash-error');
    timeEl.textContent = originalTimeText;
    delete li.dataset.busy;
  }, 1500);
}

// ---------- render ----------

// Build the relative-time footnote shown next to the label. The
// footnote is strictly about timing -- the label itself already
// shows the user-supplied label or the content-type fallback.
function buildTimeText(item) {
  const t = window.i18n.t;
  let text = t('tracked.time_created', { when: fmtRelative(item.created_at) });
  if (item.status === 'viewed' && item.viewed_at) {
    text += ` · ${t('tracked.time_viewed', { when: fmtRelative(item.viewed_at) })}`;
  } else if (item.status === 'burned' && item.viewed_at) {
    text += ` · ${t('tracked.time_burned', { when: fmtRelative(item.viewed_at) })}`;
  } else if (item.status === 'canceled' && item.viewed_at) {
    text += ` · ${t('tracked.time_canceled', { when: fmtRelative(item.viewed_at) })}`;
  } else if (item.status === 'expired') {
    text += ` · ${t('tracked.time_expired', { when: fmtRelative(item.expires_at) })}`;
  }
  return text;
}

// Hover-title text: absolute timestamps, same event keys as the
// relative footnote. Helps disambiguate older entries whose relative
// phrase ("a few days ago") is fuzzy.
function buildHoverTitle(item, loc) {
  const t = window.i18n.t;
  const bits = [t('tracked.time_created', { when: new Date(item.created_at).toLocaleString(loc) })];
  if (item.viewed_at) {
    bits.push(
      t(`tracked.time_${item.status}`, {
        when: new Date(item.viewed_at).toLocaleString(loc),
      })
    );
  } else if (item.status === 'expired') {
    bits.push(
      t('tracked.time_expired', {
        when: new Date(item.expires_at).toLocaleString(loc),
      })
    );
  }
  return bits.join(' · ');
}

// Build the meta block (label + time footnote with hover title).
function buildMetaBlock(item) {
  const fallback =
    item.content_type === 'image'
      ? window.i18n.t('tracked.image_secret')
      : window.i18n.t('tracked.text_secret');
  const labelText = item.label?.trim() ? item.label : fallback;

  const labelEl = document.createElement('span');
  labelEl.className = 'label';
  labelEl.textContent = labelText;
  // Long labels truncate with ellipsis (see CSS); carry the full text
  // in the tooltip so hovering still reveals everything. Skip the
  // tooltip for fallback labels -- they're short and non-informative.
  if (item.label?.trim()) labelEl.title = labelText;

  const timeEl = document.createElement('span');
  timeEl.className = 'time';
  const timeText = buildTimeText(item);
  timeEl.textContent = timeText;
  timeEl.title = buildHoverTitle(item, window.i18n.currentLocale);

  const meta = document.createElement('div');
  meta.className = 'tracked-meta';
  meta.appendChild(labelEl);
  meta.appendChild(timeEl);
  return { meta, timeEl, timeText };
}

// Cancel action button: only for pending (live) secrets. Two-click
// confirm so accidental clicks don't revoke a link.
function buildCancelButton(item) {
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'tracked-cancel';
  cancelBtn.textContent = window.i18n.t('button.cancel');
  cancelBtn.title = window.i18n.t('tracked.tooltip_cancel');
  cancelBtn.setAttribute('aria-label', window.i18n.t('tracked.aria_cancel'));
  bindTwoClickConfirm(cancelBtn, {
    stopPropagation: true,
    onConfirm: async () => {
      cancelBtn.disabled = true;
      cancelBtn.textContent = window.i18n.t('button.canceling');
      await cancelOnServer(item.id);
      forgetUrl(item.id);
      renderTrackedList();
    },
  });
  return cancelBtn;
}

// Right-side actions: status pill, optional cancel button, remove button.
function buildRightBlock(item) {
  const pill = document.createElement('span');
  pill.className = `status-pill ${item.status}`;
  pill.textContent = window.i18n.t(`status.${item.status}`);

  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'tracked-remove';
  rm.setAttribute('aria-label', window.i18n.t('tracked.aria_remove'));
  rm.title = window.i18n.t('tracked.aria_remove');
  rm.textContent = '×';

  const right = document.createElement('div');
  right.className = 'tracked-right';
  right.appendChild(pill);
  if (item.status === 'pending') right.appendChild(buildCancelButton(item));
  right.appendChild(rm);
  return { right, rm };
}

// Wire row-level click/keyboard so a pending row with a cached URL
// copies the share link on Enter / Space / click.
function makeCopyableRow(li, timeEl, timeText, cachedUrl) {
  li.classList.add('copyable');
  li.setAttribute('role', 'button');
  li.setAttribute('tabindex', '0');
  li.title = window.i18n.t('tracked.tooltip_copy');
  const activate = async (e) => {
    // Ignore clicks that originated on row-level action buttons.
    if (e.target?.closest('.tracked-remove, .tracked-cancel')) return;
    await copyRowUrl(li, timeEl, timeText, cachedUrl);
  };
  li.addEventListener('click', activate);
  li.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      activate(e);
    }
  });
}

// Mark a pending row as "informational, not actionable" -- the URL
// can never be reconstructed server-side because the key fragment
// never leaves the creating browser.
function makeOrphanRow(li, meta) {
  li.classList.add('orphan');
  li.title = window.i18n.t('tracked.tooltip_orphan');
  const hint = document.createElement('span');
  hint.className = 'orphan-hint';
  hint.textContent = window.i18n.t('tracked.orphan_hint');
  meta.appendChild(hint);
}

function buildTrackedItem(item) {
  const li = document.createElement('li');
  li.className = 'tracked-item';
  li.dataset.id = item.id;
  li.dataset.status = item.status;

  const { meta, timeEl, timeText } = buildMetaBlock(item);
  const { right, rm } = buildRightBlock(item);
  li.appendChild(meta);
  li.appendChild(right);

  const cachedUrl = getUrl(item.id);
  if (cachedUrl && item.status === 'pending') {
    makeCopyableRow(li, timeEl, timeText, cachedUrl);
  } else if (!cachedUrl && item.status === 'pending') {
    makeOrphanRow(li, meta);
  }

  rm.addEventListener('click', async (e) => {
    e.stopPropagation();
    await untrackOnServer(item.id);
    forgetUrl(item.id);
    renderTrackedList();
  });
  return li;
}

// Reveal the "clear past entries" action only when there's something to
// clear; refresh its label with the count-bearing pluralized phrase.
function refreshClearHistoryButton(items) {
  const clearBtn = document.getElementById('tracked-clear');
  const clearLbl = document.getElementById('tracked-clear-label');
  if (!clearBtn) return;
  const nonPending = items.filter((i) => i.status !== 'pending').length;
  clearBtn.hidden = nonPending === 0;
  if (clearLbl && nonPending > 0) {
    clearLbl.textContent = window.i18n.t(pluralKey('button.clear_past', nonPending), {
      n: nonPending,
    });
  }
}

export async function renderTrackedList() {
  const section = document.getElementById('tracked-section');
  const list = document.getElementById('tracked-list');
  const header = document.getElementById('tracked-header');
  const countEl = document.getElementById('tracked-count');

  const items = await fetchTracked();
  if (items === null) return; // fetch failed; leave UI + URL cache alone
  gcUrls(items.map((i) => i.id));

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
  for (const item of items) list.appendChild(buildTrackedItem(item));

  refreshClearHistoryButton(items);

  if (items.some((i) => i.status === 'pending')) startTrackedPoll();
  else stopTrackedPoll();
}

// ---------- top-level wiring ----------

// Clear-history action: same 2-click pattern as per-row cancel, via the
// shared helper. Targets the #tracked-clear-label span specifically so
// the icon (a sibling SVG inside the same button) stays put across state
// transitions. The helper captures the rest-state label at arm time, so
// the count-bearing pluralization ("Clear 3 past entries") is restored
// correctly even though the label changes between renders.
(function wireClearHistory() {
  const clearBtn = document.getElementById('tracked-clear');
  const clearLbl = document.getElementById('tracked-clear-label');
  if (!clearBtn || !clearLbl) return;
  bindTwoClickConfirm(clearBtn, {
    labelEl: clearLbl,
    stopPropagation: true,
    onConfirm: async () => {
      clearBtn.disabled = true;
      clearLbl.textContent = window.i18n.t('button.clearing');
      try {
        await fetch('/api/secrets/tracked/clear', { method: 'POST' });
      } catch {}
      clearBtn.disabled = false;
      renderTrackedList();
    },
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
