// Live-status widget for a just-created secret. Polls
// /api/secrets/{id}/status every 5 seconds, paints a pending/viewed/
// burned/expired/gone pill, and stops once the secret reaches a terminal
// status. The single-instance design (one global poll handle) is fine
// because there's only ever one create-secret result on screen at a
// time.
//
// Used from sender/form.js:
//
//   import { startStatusPoll, stopStatusPoll } from './status-poll.js';
//
//   startStatusPoll(secretId);  // after a successful create
//   stopStatusPoll();             // on "create another"
//
// The widget assumes #status-value and #status-detail exist in the DOM
// (template-rendered as part of the result screen). When polling stops
// (terminal status reached), the tracked-list panel is re-rendered so
// the row's pill flips synchronously alongside this widget's.

import { renderTrackedList } from './tracked-list.js';

let pollHandle = null;

async function fetchStatus(id) {
  try {
    const res = await fetch(`/api/secrets/${encodeURIComponent(id)}/status`);
    if (res.status === 404) return { status: 'gone' };
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

function paintStatus(valueEl, detailEl, data) {
  const statuses = ['pending', 'viewed', 'burned', 'expired', 'gone'];
  for (const s of statuses) {
    valueEl.classList.remove(s);
  }
  const s = data?.status || 'pending';
  valueEl.classList.add(s);
  valueEl.textContent = window.i18n.t(`status.${s}`);
  if (data?.viewed_at) {
    detailEl.textContent =
      window.i18n.t('sender.viewed_at_prefix') +
      new Date(data.viewed_at).toLocaleString(window.i18n.currentLocale);
  } else {
    detailEl.textContent = '';
  }
}

export async function startStatusPoll(id) {
  stopStatusPoll();
  const valueEl = document.getElementById('status-value');
  const detailEl = document.getElementById('status-detail');
  const tick = async () => {
    const data = await fetchStatus(id);
    paintStatus(valueEl, detailEl, data);
    if (data && (data.status === 'viewed' || data.status === 'burned' || data.status === 'gone')) {
      stopStatusPoll();
      renderTrackedList();
    }
  };
  await tick();
  pollHandle = setInterval(tick, 5000);
}

export function stopStatusPoll() {
  if (pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}
