// Compose form: submit handler (text + image paths), tab toggle, drag-and-
// drop dropzone, create-another reset, and the status widget that polls
// the just-created secret's status after a successful create.

import { renderTrackedList } from './tracked-list.js';
import { cacheUrl } from './url-cache.js';

const form = document.getElementById('secret-form');
const compose = document.getElementById('compose');
const tabs = document.querySelectorAll('.tab');
const panels = {
  text: document.getElementById('panel-text'),
  image: document.getElementById('panel-image'),
};
const result = document.getElementById('result');
const errBox = document.getElementById('sender-error');
const fileInput = document.getElementById('file');
const dropzone = document.getElementById('dropzone');
const preview = document.getElementById('preview');
const fileName = document.getElementById('file-name');
const clearFile = document.getElementById('clear-file');

// ---------- tabs ----------

// Passphrase visibility toggle (same pattern as login.js). The passphrase is
// sender-entered and communicated out-of-band, so masking protects against
// shoulder-surfing during composition; a show button is available for when
// the sender genuinely needs to read back what they typed.
const ppInput = document.getElementById('passphrase');
const ppToggle = document.getElementById('toggle-passphrase');
if (ppInput && ppToggle) {
  ppToggle.addEventListener('click', () => {
    const showing = ppInput.getAttribute('type') === 'text';
    ppInput.setAttribute('type', showing ? 'password' : 'text');
    // Read i18n keys from data-i18n-* on the button so the same handler shape
    // can serve any toggle that opts in via attributes.
    ppToggle.textContent = window.i18n.t(showing ? 'button.show' : 'button.hide');
    ppToggle.setAttribute('aria-pressed', String(!showing));
    // aria-label stays at its template-rendered (gettext) value; aria-pressed
    // carries the state per the ARIA Authoring Practices toggle pattern, so
    // screen readers don't need a label swap.
  });
}

let activeTab = 'text';

function setTab(name) {
  activeTab = name;
  for (const t of tabs) {
    t.classList.toggle('active', t.dataset.tab === name);
  }
  for (const [k, el] of Object.entries(panels)) {
    el.hidden = k !== name;
  }
}

for (const t of tabs) {
  t.addEventListener('click', () => setTab(t.dataset.tab));
}

// ---------- dropzone ----------

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    fileInput.click();
  }
});
dropzone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropzone.classList.add('drag');
});
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('drag');
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    showPreview();
  }
});
fileInput.addEventListener('change', showPreview);
clearFile.addEventListener('click', (e) => {
  e.stopPropagation();
  fileInput.value = '';
  preview.hidden = true;
});

function showPreview() {
  if (fileInput.files.length) {
    const f = fileInput.files[0];
    fileName.textContent = `${f.name} (${Math.round(f.size / 1024)} KB)`;
    preview.hidden = false;
  } else {
    preview.hidden = true;
  }
}

// ---------- submit ----------

const submitBtn = document.getElementById('submit-btn');
const submitLabel = submitBtn.textContent;

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  // In-flight guard: a rapid double-tap would otherwise create two
  // independent secrets. The UI only shows the URL of the last response,
  // so the first one silently orphans.
  if (submitBtn.disabled) return;
  errBox.hidden = true;
  submitBtn.disabled = true;
  submitBtn.textContent = window.i18n.t('button.creating');

  let res;
  try {
    const track = document.getElementById('track').checked;
    const label = track ? (document.getElementById('label').value || '').trim() : '';
    // Snapshot the passphrase before awaiting the request: the input stays
    // editable while bcrypt-cost-12 hashing runs server-side (~5s on a
    // shared CPU), and a mid-flight edit would split the user-visible value
    // from what the backend stored, breaking the URL+passphrase pair for
    // the receiver. We use this same captured value for the request body
    // AND for the result row so they're guaranteed to agree.
    const passphrase = document.getElementById('passphrase').value || '';
    if (activeTab === 'text') {
      const content = document.getElementById('content').value;
      if (!content.trim()) throw new Error(window.i18n.t('error.please_enter_message'));
      const body = {
        content,
        content_type: 'text',
        expires_in: Number(document.getElementById('expires_in').value),
        passphrase: passphrase || null,
        track,
      };
      if (label) body.label = label;
      res = await fetch('/api/secrets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } else {
      if (!fileInput.files.length) throw new Error(window.i18n.t('error.please_select_image'));
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      fd.append('expires_in', document.getElementById('expires_in').value);
      if (passphrase) fd.append('passphrase', passphrase);
      fd.append('track', track ? 'true' : 'false');
      if (label) fd.append('label', label);
      res = await fetch('/api/secrets', { method: 'POST', body: fd });
    }

    if (res.status === 401) {
      window.location.reload();
      return;
    }
    if (!res.ok) {
      let msg = window.i18n.t('error.request_failed', { status: res.status });
      // Server's {code, message} shape: prefer the localized error.<code>
      // toast when available, else the English fallback `message`, else the
      // generic "Request failed (N)" above.
      try {
        const j = await res.json();
        if (j.detail?.code) {
          const key = `error.${j.detail.code}`;
          const localized = window.i18n.t(key);
          msg = localized === key ? j.detail.message || msg : localized;
        } else if (typeof j.detail === 'string') {
          msg = j.detail;
        }
      } catch {}
      throw new Error(msg);
    }
    const data = await res.json();
    if (track && data.url && data.id) cacheUrl(data.id, data.url);
    showResult(data, passphrase);
  } catch (err) {
    errBox.textContent = err.message || window.i18n.t('error.generic');
    errBox.hidden = false;
  } finally {
    // Restore the button whether we succeeded or threw: on success the
    // compose form is hidden so the user won't notice, but "Create another"
    // brings the form back and it has to be usable again.
    submitBtn.disabled = false;
    submitBtn.textContent = submitLabel;
  }
});

// ---------- status widget (polls /api/secrets/{id}/status after create) ----------

let statusPoll = null;

function showResult({ url, id, expires_at }, passphrase) {
  compose.hidden = true;
  document.getElementById('result-url').textContent = url;

  // Passphrase isn't in the API response by design -- the server never
  // returns it. The submit handler snapshots the value from the input
  // before awaiting the request and hands it in here, so a user who
  // edits the field while the request is in flight doesn't end up with
  // a result screen showing a different value than the server stored.
  const passphraseRow = document.getElementById('result-passphrase-row');
  const passphraseEl = document.getElementById('result-passphrase');
  const passphraseToggle = document.getElementById('toggle-result-passphrase');
  if (passphrase) {
    passphraseEl.dataset.real = passphrase;
    passphraseEl.dataset.masked = 'true';
    // Cap the dot count so a shoulder-surfer can't infer real length.
    passphraseEl.textContent = '•'.repeat(Math.min(passphrase.length, 16));
    passphraseToggle.setAttribute('aria-pressed', 'false');
    passphraseToggle.textContent = window.i18n.t('button.show');
    passphraseRow.hidden = false;
  } else {
    passphraseEl.dataset.real = '';
    passphraseEl.textContent = '';
    passphraseRow.hidden = true;
  }

  const expiry = new Date(expires_at);
  document.getElementById('result-expiry').textContent =
    window.i18n.t('sender.expires_prefix') + expiry.toLocaleString(window.i18n.currentLocale);

  const track = document.getElementById('track').checked;
  const widget = document.getElementById('status-widget');
  if (track) {
    // The server is already authoritative — creation wrote track=1 + label.
    widget.hidden = false;
    startPolling(id);
    renderTrackedList();
  } else {
    widget.hidden = true;
  }
  result.hidden = false;
}

function stopPolling() {
  if (statusPoll) {
    clearInterval(statusPoll);
    statusPoll = null;
  }
}

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

async function startPolling(id) {
  stopPolling();
  const valueEl = document.getElementById('status-value');
  const detailEl = document.getElementById('status-detail');
  const tick = async () => {
    const data = await fetchStatus(id);
    paintStatus(valueEl, detailEl, data);
    if (data && (data.status === 'viewed' || data.status === 'burned' || data.status === 'gone')) {
      stopPolling();
      renderTrackedList();
    }
  };
  await tick();
  statusPoll = setInterval(tick, 5000);
}

// ---------- "create another" + track toggle sync ----------

document.getElementById('create-another').addEventListener('click', () => {
  stopPolling();
  form.reset();
  fileInput.value = '';
  preview.hidden = true;
  result.hidden = true;
  compose.hidden = false;
  document.getElementById('status-widget').hidden = true;
  // Wipe the previous passphrase from the result-row's dataset so it
  // doesn't outlive the visible UI. Without this, a user who clicks
  // "Create another" and then walks away leaves the previous plaintext
  // readable via DOM APIs until full page navigation.
  const passphraseEl = document.getElementById('result-passphrase');
  passphraseEl.dataset.real = '';
  passphraseEl.dataset.masked = 'true';
  passphraseEl.textContent = '';
  document.getElementById('result-passphrase-row').hidden = true;
  setTab('text');
});

const trackCheckbox = document.getElementById('track');
const labelWrap = document.getElementById('label-wrap');
function syncLabelVisibility() {
  labelWrap.hidden = !trackCheckbox.checked;
  if (!trackCheckbox.checked) document.getElementById('label').value = '';
}
trackCheckbox.addEventListener('change', syncLabelVisibility);

setTab('text');
syncLabelVisibility();
