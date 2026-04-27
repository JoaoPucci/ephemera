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

// ---------- char-limit hints (counter, paste-warning, ceiling-reached) ----------
//
// Three discrete states share a single hint slot per field. State precedence:
//
//   paste-trim  > ceiling-reached  > approaching  > idle
//
// `paste-trim` and (for the textarea) `paste-large` are set by the paste
// handler and rendered on the paste-induced input event (e.inputType
// === 'insertFromPaste'). Subsequent typing reverts to the regular
// counter-vs-ceiling computation.
//
// Telemetry: we track the largest pre-truncation content size in the
// compose session and submit it as `intended_content_size_bytes` when
// it crosses 95% of the cap. Backend writes a `content.limit_hit`
// analytics event with that size + `was_paste` flag.

const MAX_CONTENT = 100_000;
const MAX_LABEL = 60;
const MAX_PASSPHRASE = 200;
const PASTE_LARGE_THRESHOLD = 10_000; // soft warning for >10KB chunk

// Telemetry session state. We track only "did the user cross the 95%
// threshold during this compose session" -- a single sticky bit, not a
// size. The signal that's hard to recover server-side is the
// threshold-crossing itself: an over-cap paste (200K -> truncated to
// 100K) and an edit-down (typed 100K -> deleted to 50K) both leave the
// final content much smaller than the user's intent. The flag closes
// that gap; the backend gets nothing else.
let intendedContentSize = 0; // chars, drives counter UX + threshold gate
let nearCapHit = false; // sticky session bit reported as `near_cap` on submit
const TELEMETRY_THRESHOLD = MAX_CONTENT * 0.95;

function _formatNumber(n) {
  return n.toLocaleString(window.i18n.currentLocale);
}

function _formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${_formatNumber(Math.round(n / 1024))} KB`;
  return `${_formatNumber(Math.round((n / 1024 / 1024) * 10) / 10)} MB`;
}

function _setHint(hintEl, content, modifier) {
  // modifier: 'warning' | 'error' | null. content === null hides the hint.
  if (content === null) {
    hintEl.hidden = true;
    hintEl.textContent = '';
    hintEl.classList.remove('is-warning', 'is-error');
    return;
  }
  hintEl.hidden = false;
  hintEl.textContent = content;
  hintEl.classList.toggle('is-warning', modifier === 'warning');
  hintEl.classList.toggle('is-error', modifier === 'error');
}

// Wires a counter / paste-warning / ceiling-reached hint to a textarea or
// input with maxlength. opts:
//   counterAt       fraction of max to start showing the counter (default 0.75)
//   warningAt       fraction at which to add .is-warning (default 0.95)
//   pasteLargeThreshold     paste size that triggers the paste-large warning
//                           (Infinity by default = never; the textarea opts in)
//   useShortTrimMessage     true on the label field (omits the "(was X)"
//                           parenthetical from the trim message; the field is
//                           short enough that the original size is implicit)
//   onIntendedSize(sizeChars)
//                           telemetry callback, fired on every intended-size
//                           observation (post-paste OR per keystroke). The
//                           caller decides what to do with it (typically:
//                           flip a sticky "near cap was crossed" bit).
function _bindCounterHint(input, hintEl, max, opts = {}) {
  const counterAt = (opts.counterAt ?? 0.75) * max;
  const warningAt = (opts.warningAt ?? 0.95) * max;
  const pasteLargeThreshold = opts.pasteLargeThreshold ?? Number.POSITIVE_INFINITY;
  const useShortTrim = !!opts.useShortTrimMessage;
  // Static text rendered into the slot from the template (e.g. label's
  // "Up to 60 characters. Shown only to you."). Captured once on init so
  // the idle state can restore it.
  const idleText = hintEl.textContent.trim() || null;

  let pasteOverrideMessage = null;
  let pasteOverrideModifier = null;

  function _showIdle() {
    if (idleText !== null) _setHint(hintEl, idleText, null);
    else _setHint(hintEl, null, null);
  }

  _showIdle();

  input.addEventListener('paste', (e) => {
    const pasted = e.clipboardData?.getData('text') ?? '';
    const selStart = input.selectionStart ?? 0;
    const selEnd = input.selectionEnd ?? 0;
    const currentLen = input.value.length;
    const intendedAfter = currentLen - (selEnd - selStart) + pasted.length;

    if (intendedAfter > max) {
      // Browser will silently truncate at maxlength. Show paste-trim error.
      pasteOverrideMessage = useShortTrim
        ? window.i18n.t('hint.label_trimmed', { max: _formatNumber(max) })
        : window.i18n.t('hint.paste_trimmed', {
            max: _formatNumber(max),
            original: _formatNumber(intendedAfter),
          });
      pasteOverrideModifier = 'error';
      if (opts.onIntendedSize) opts.onIntendedSize(intendedAfter);
    } else if (pasteLargeThreshold !== Number.POSITIVE_INFINITY) {
      // Threshold is UTF-8 bytes ("10KB chunk"); JS .length is UTF-16
      // code units, which diverges 2-4x from byte length for CJK/emoji.
      // Encode for an accurate byte count -- a 4K-character BMP CJK
      // paste is 4K code units but ~12K UTF-8 bytes and should trip
      // this. Skipped for fields that opt out (Infinity sentinel) so
      // we don't allocate a TextEncoder for label/passphrase pastes
      // that never use this branch.
      const pastedBytes = new TextEncoder().encode(pasted).length;
      if (pastedBytes >= pasteLargeThreshold) {
        pasteOverrideMessage = window.i18n.t('hint.content_paste_large', {
          size: _formatBytes(pastedBytes),
        });
        pasteOverrideModifier = 'warning';
        if (opts.onIntendedSize) opts.onIntendedSize(intendedAfter);
      } else {
        pasteOverrideMessage = null;
      }
    } else {
      pasteOverrideMessage = null;
    }
  });

  input.addEventListener('input', (e) => {
    if (pasteOverrideMessage !== null && e.inputType === 'insertFromPaste') {
      _setHint(hintEl, pasteOverrideMessage, pasteOverrideModifier);
      pasteOverrideMessage = null;
      return;
    }
    pasteOverrideMessage = null;

    const len = input.value.length;
    if (opts.onIntendedSize && len > 0) opts.onIntendedSize(len);

    if (len >= max) {
      // Frozen counter at ceiling. The frozen-ness IS the signal.
      _setHint(
        hintEl,
        window.i18n.t('hint.counter', {
          used: _formatNumber(len),
          max: _formatNumber(max),
        }),
        'error'
      );
    } else if (len >= warningAt) {
      _setHint(
        hintEl,
        window.i18n.t('hint.counter', {
          used: _formatNumber(len),
          max: _formatNumber(max),
        }),
        'warning'
      );
    } else if (len >= counterAt) {
      _setHint(
        hintEl,
        window.i18n.t('hint.counter', {
          used: _formatNumber(len),
          max: _formatNumber(max),
        }),
        null
      );
    } else {
      _showIdle();
    }
  });
}

// Passphrase-style hint: a one-line "approaching maximum" warning at
// ~90% of the cap, swapping to "maximum reached" once the input is at
// the cap. No counter and no error escalation -- the 200-char cap is a
// deliberate ceiling on a deliberate input; at-limit gets a factual
// status (the prior "approaching" wording stayed on past the cap, which
// was literally inaccurate once the textarea's maxlength blocked
// further keystrokes), but it doesn't deserve a red error flip.
function _bindPassphraseHint(input, hintEl, max, threshold = 0.9) {
  const warnAt = threshold * max;
  input.addEventListener('input', () => {
    const len = input.value.length;
    if (len >= max) {
      _setHint(hintEl, window.i18n.t('hint.max_reached'), 'warning');
    } else if (len >= warnAt) {
      _setHint(hintEl, window.i18n.t('hint.passphrase_approaching'), 'warning');
    } else {
      _setHint(hintEl, null, null);
    }
  });
}

// ---------- wire up the hints ----------

const contentInput = document.getElementById('content');
const contentHint = document.getElementById('content-hint');
if (contentInput && contentHint) {
  _bindCounterHint(contentInput, contentHint, MAX_CONTENT, {
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
  _bindCounterHint(labelInput, labelHint, MAX_LABEL, {
    counterAt: 0.75,
    warningAt: 1.0, // label has no warning band; counter -> error at ceiling
    useShortTrimMessage: true,
  });
}

// ---------- passphrase (visibility toggle + approaching-max hint) ----------

// Passphrase visibility toggle (same pattern as login.js). The passphrase is
// sender-entered and communicated out-of-band, so masking protects against
// shoulder-surfing during composition; a show button is available for when
// the sender genuinely needs to read back what they typed.
const ppInput = document.getElementById('passphrase');
const ppToggle = document.getElementById('toggle-passphrase');
const passphraseHintEl = document.getElementById('passphrase-hint');
if (ppInput && passphraseHintEl) {
  _bindPassphraseHint(ppInput, passphraseHintEl, MAX_PASSPHRASE, 0.9);
}
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
      // Cap-proximity telemetry. Single sticky bit: presence-only signal
      // that the user crossed >=95% of the cap somewhere during this
      // compose session, even if they edited back down before hitting
      // submit. The backend records the bare event (no size, no paste-
      // vs-typed, no user identity) and only when analytics is enabled
      // operator-side.
      if (nearCapHit) body.near_cap = true;
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
