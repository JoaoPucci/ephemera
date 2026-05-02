// Compose-form orchestration: submit, tab toggle, "create another" reset,
// and the wire-up that hands the input fields off to per-concern modules.
//
// Concerns broken out into siblings under sender/:
//   hints.js        -- counter / paste-warning / ceiling-reached hints
//   dropzone.js     -- click + drag-and-drop wiring for the Image tab
//   status-poll.js  -- live status pill for the just-created secret
//
// The remaining job of this file is the page-level orchestration that
// ties all of those together: read /api/me to learn the analytics
// opt-in state, gate near_cap telemetry on it, run the submit pipeline
// (text vs image), and rebuild the form on "create another."

import { bindMaskToggle } from '../mask-toggle.js';
import { bindDropzone } from './dropzone.js';
import { bindCounterHint, bindPassphraseHint } from './hints.js';
import { startStatusPoll, stopStatusPoll } from './status-poll.js';
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

// ---------- char-limit hint constants ----------
//
// MAX_CONTENT also drives the telemetry threshold below; the others
// are passed straight through to bindCounterHint / bindPassphraseHint.

const MAX_CONTENT = 100_000;
const MAX_LABEL = 60;
const MAX_PASSPHRASE = 200;
const PASTE_LARGE_THRESHOLD = 10_000; // soft warning for >10KB chunk

// ---------- cap-proximity telemetry state ----------
//
// We track only "did the user cross the 95% threshold during this
// compose session" -- a single sticky bit, not a size. The signal that's
// hard to recover server-side is the threshold-crossing itself: an
// over-cap paste (200K -> truncated to 100K) and an edit-down (typed
// 100K -> deleted to 50K) both leave the final content much smaller
// than the user's intent. The flag closes that gap; the backend gets
// nothing else.

let intendedContentSize = 0; // chars, drives counter UX + threshold gate
let nearCapHit = false; // sticky session bit reported as `near_cap` on submit
const TELEMETRY_THRESHOLD = MAX_CONTENT * 0.95;

// Per-user analytics consent, mirrored from /api/me on load and from the
// chrome-menu drawer toggle's PATCH response on flip. Default false: until
// /api/me lands we assume opt-out, which is the right direction (no signal
// over the wire on a fast submit before the page settled). Browser-side
// gate so a user who has opted out doesn't even ship `near_cap` in the
// request body -- defense-in-depth against future logging regressions
// that might capture bodies, even though the server-side gate would drop
// it anyway.
let analyticsOptIn = false;
window.addEventListener('ephemera:me-loaded', (e) => {
  analyticsOptIn = Boolean(e.detail?.analytics_opt_in);
});
window.addEventListener('ephemera:me-updated', (e) => {
  const next = Boolean(e.detail?.analytics_opt_in);
  // Reset the sticky session bit on ANY consent transition, not just
  // opt-OUT. Opt-OUT case: user turned this off, no signal from this
  // session anymore. Opt-IN case: user crossed 95% while opted out,
  // then opts in before submit -- without a reset, near_cap=true would
  // ride pre-consent activity into the new state, breaking the per-
  // user opt-in boundary the toggle exists to enforce. Either way the
  // sticky bit's session is the consent-invariant period; flipping
  // consent ends that session.
  if (analyticsOptIn !== next) nearCapHit = false;
  analyticsOptIn = next;
});

// ---------- wire up the hints ----------

const contentInput = document.getElementById('content');
const contentHint = document.getElementById('content-hint');
if (contentInput && contentHint) {
  bindCounterHint(contentInput, contentHint, MAX_CONTENT, {
    counterAt: 0.75,
    warningAt: 0.95,
    pasteLargeThreshold: PASTE_LARGE_THRESHOLD,
    onIntendedSize: (sizeChars) => {
      if (sizeChars > intendedContentSize) intendedContentSize = sizeChars;
      // Sticky session bit. Once the user crosses the threshold (typed,
      // pasted, OR pasted-and-truncated), nearCapHit stays true through
      // any subsequent edit-down, so submit reports the threshold-
      // crossing fact regardless of the final value's size.
      if (sizeChars >= TELEMETRY_THRESHOLD) nearCapHit = true;
    },
  });
}

const labelInput = document.getElementById('label');
const labelHint = document.getElementById('label-hint');
if (labelInput && labelHint) {
  bindCounterHint(labelInput, labelHint, MAX_LABEL, {
    counterAt: 0.75,
    warningAt: 1.0, // label has no warning band; counter -> error at ceiling
    useShortTrimMessage: true,
  });
}

// ---------- passphrase (visibility toggle + approaching-max hint) ----------
//
// Passphrase visibility toggle (same pattern as login.js). The passphrase
// is sender-entered and communicated out-of-band, so masking protects
// against shoulder-surfing during composition; a show button is available
// for when the sender genuinely needs to read back what they typed.

const ppInput = document.getElementById('passphrase');
const ppToggle = document.getElementById('toggle-passphrase');
const passphraseHintEl = document.getElementById('passphrase-hint');
if (ppInput && passphraseHintEl) {
  bindPassphraseHint(ppInput, passphraseHintEl, MAX_PASSPHRASE, 0.9);
}
// aria-label stays at its template-rendered (gettext) value; aria-pressed
// carries the state per the ARIA Authoring Practices toggle pattern, so
// screen readers don't need a label swap. We omit the aria{Show,Hide}Key
// options to bindMaskToggle to opt out of the aria-label flip.
bindMaskToggle(ppInput, ppToggle);

// ---------- tab toggle ----------

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

bindDropzone({ dropzone, fileInput, preview, fileName, clearFile });

// ---------- submit ----------

const submitBtn = document.getElementById('submit-btn');
const submitLabel = submitBtn.textContent;

// Build the JSON body for the text-secret POST. Throws if the
// content field is empty (mapped to a localized error in the
// caller's catch block).
function buildTextBody(passphrase, track, label) {
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
  // Cap-proximity telemetry. Single sticky bit: presence-only signal
  // that the user crossed >=95% of the cap somewhere during this
  // compose session, even if they edited back down before hitting
  // submit. Two-side gate: only ship `near_cap` if the user has
  // opted in. Server still validates and drops the field if its own
  // per-user gate is closed -- we don't trust the client. But not
  // sending it at all when consent is off honors the user's mental
  // model of "I turned this off; nothing related goes over the wire."
  if (nearCapHit && analyticsOptIn) body.near_cap = true;
  return body;
}

// Build the multipart/form-data payload for the image-secret POST.
// Throws if no file is selected (mapped to a localized error in the
// caller's catch block).
function buildImageFormData(passphrase, track, label) {
  if (!fileInput.files.length) throw new Error(window.i18n.t('error.please_select_image'));
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('expires_in', document.getElementById('expires_in').value);
  if (passphrase) fd.append('passphrase', passphrase);
  fd.append('track', track ? 'true' : 'false');
  if (label) fd.append('label', label);
  return fd;
}

// Translate a non-2xx response into a user-visible error message.
// Server returns `{detail: {code, message}}`; prefer the localized
// `error.<code>` toast when available, else the English `message`,
// else a generic "Request failed (N)" fallback.
async function parseErrorMessage(res) {
  let msg = window.i18n.t('error.request_failed', { status: res.status });
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
  return msg;
}

// Snapshot the passphrase before awaiting the request: the input
// stays editable while bcrypt-cost-12 hashing runs server-side (~5s
// on a shared CPU), and a mid-flight edit would split the user-
// visible value from what the backend stored, breaking the
// URL+passphrase pair for the receiver. The same captured value
// feeds both the request body and the result row.
async function submitSecret() {
  const track = document.getElementById('track').checked;
  const label = track ? (document.getElementById('label').value || '').trim() : '';
  const passphrase = document.getElementById('passphrase').value || '';
  let res;
  if (activeTab === 'text') {
    const body = buildTextBody(passphrase, track, label);
    res = await fetch('/api/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } else {
    res = await fetch('/api/secrets', {
      method: 'POST',
      body: buildImageFormData(passphrase, track, label),
    });
  }
  return { res, passphrase, track };
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  // In-flight guard: a rapid double-tap would otherwise create two
  // independent secrets. The UI only shows the URL of the last response,
  // so the first one silently orphans.
  if (submitBtn.disabled) return;
  errBox.hidden = true;
  submitBtn.disabled = true;
  submitBtn.textContent = window.i18n.t('button.creating');

  try {
    const { res, passphrase, track } = await submitSecret();
    if (res.status === 401) {
      window.location.reload();
      return;
    }
    if (!res.ok) throw new Error(await parseErrorMessage(res));
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

// ---------- result screen + status widget ----------

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
    startStatusPoll(id);
    renderTrackedList();
  } else {
    widget.hidden = true;
  }
  result.hidden = false;
}

// ---------- "create another" + track toggle sync ----------

document.getElementById('create-another').addEventListener('click', () => {
  stopStatusPoll();
  form.reset();
  fileInput.value = '';
  preview.hidden = true;
  result.hidden = true;
  compose.hidden = false;
  document.getElementById('status-widget').hidden = true;
  // Reset cap-proximity telemetry state so the next compose session starts
  // fresh -- otherwise a previous near-cap session would re-emit on every
  // subsequent submit, even if the new content is small.
  intendedContentSize = 0;
  nearCapHit = false;
  // form.reset() above clears input values but doesn't dispatch the input
  // events that drive the cap-proximity hints, so a previous warning or
  // error stays visible above the now-empty inputs. Synthesize an input
  // event per bound field; the binder's terminal `else` branch recomputes
  // against the empty value and restores idle state (hidden / static
  // idle-text). Kept here rather than duplicated in each binder so the
  // idle logic stays single-sourced.
  if (contentInput) contentInput.dispatchEvent(new InputEvent('input', { bubbles: true }));
  if (labelInput) labelInput.dispatchEvent(new InputEvent('input', { bubbles: true }));
  if (ppInput) ppInput.dispatchEvent(new InputEvent('input', { bubbles: true }));
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
  if (!trackCheckbox.checked) {
    document.getElementById('label').value = '';
    // Same stale-hint problem as create-another: the value is wiped without
    // an input event firing, so a prior at-ceiling error would still be
    // sitting in the hint slot when the user re-checks track. Synthesize
    // an input event so the binder restores idle.
    if (labelInput) labelInput.dispatchEvent(new InputEvent('input', { bubbles: true }));
  }
}
trackCheckbox.addEventListener('change', syncLabelVisibility);

setTab('text');
syncLabelVisibility();
