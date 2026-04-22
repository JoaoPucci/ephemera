// Compose form: submit handler (text + image paths), tab toggle, drag-and-
// drop dropzone, create-another reset, and the status widget that polls
// the just-created secret's status after a successful create.
import { cacheUrl } from './url-cache.js';
import { renderTrackedList } from './tracked-list.js';

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
    ppToggle.textContent = showing ? 'show' : 'hide';
    ppToggle.setAttribute('aria-pressed', String(!showing));
    ppToggle.setAttribute(
      'aria-label',
      showing ? 'show passphrase' : 'hide passphrase',
    );
  });
}

let activeTab = 'text';

function setTab(name) {
  activeTab = name;
  tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  Object.entries(panels).forEach(([k, el]) => (el.hidden = k !== name));
}

tabs.forEach(t => t.addEventListener('click', () => setTab(t.dataset.tab)));

// ---------- dropzone ----------

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('drag'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('drag');
  if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; showPreview(); }
});
fileInput.addEventListener('change', showPreview);
clearFile.addEventListener('click', (e) => { e.stopPropagation(); fileInput.value = ''; preview.hidden = true; });

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
    if (activeTab === 'text') {
      const content = document.getElementById('content').value;
      if (!content.trim()) throw new Error(window.i18n.t('error.please_enter_message'));
      const body = {
        content,
        content_type: 'text',
        expires_in: Number(document.getElementById('expires_in').value),
        passphrase: document.getElementById('passphrase').value || null,
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
      const pw = document.getElementById('passphrase').value;
      if (pw) fd.append('passphrase', pw);
      fd.append('track', track ? 'true' : 'false');
      if (label) fd.append('label', label);
      res = await fetch('/api/secrets', { method: 'POST', body: fd });
    }

    if (res.status === 401) { window.location.reload(); return; }
    if (!res.ok) {
      let msg = window.i18n.t('error.request_failed', { status: res.status });
      // Server's {code, message} shape: prefer the localized error.<code>
      // toast when available, else the English fallback `message`, else the
      // generic "Request failed (N)" above.
      try {
        const j = await res.json();
        if (j.detail && j.detail.code) {
          const key = 'error.' + j.detail.code;
          const localized = window.i18n.t(key);
          msg = localized === key ? (j.detail.message || msg) : localized;
        } else if (typeof j.detail === 'string') {
          msg = j.detail;
        }
      } catch {}
      throw new Error(msg);
    }
    const data = await res.json();
    if (track && data.url && data.id) cacheUrl(data.id, data.url);
    showResult(data);
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

function showResult({ url, id, expires_at }) {
  compose.hidden = true;
  document.getElementById('result-url').textContent = url;
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
  if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
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
  statuses.forEach((s) => valueEl.classList.remove(s));
  const s = (data && data.status) || 'pending';
  valueEl.classList.add(s);
  valueEl.textContent = window.i18n.t('status.' + s);
  if (data && data.viewed_at) {
    detailEl.textContent = window.i18n.t('sender.viewed_at_prefix')
      + new Date(data.viewed_at).toLocaleString(window.i18n.currentLocale);
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
