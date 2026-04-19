// ES module. Top-level code runs once on import, against the DOM present
// at the time the html file loads this via <script type="module">.

const form = document.getElementById('login-form');
const err = document.getElementById('login-error');
const codeInput = document.getElementById('code');
const codeLabel = document.getElementById('code-label');
const toggle = document.getElementById('toggle-code-mode');
const codeToggleBtn = document.getElementById('toggle-code');

// Browsers often restore form values on reload. A TOTP code is one-shot
// by definition, so wipe it on page load. Also wipe if the page is
// restored from the bfcache (back button).
const clearOneShotFields = () => { codeInput.value = ''; };
clearOneShotFields();
window.addEventListener('pageshow', clearOneShotFields);

let backupMode = false;

// Password visibility toggle
const pwInput = document.getElementById('password');
const pwToggle = document.getElementById('toggle-password');
pwToggle.addEventListener('click', () => {
  const showing = pwInput.getAttribute('type') === 'text';
  pwInput.setAttribute('type', showing ? 'password' : 'text');
  pwToggle.textContent = showing ? 'show' : 'hide';
  pwToggle.setAttribute('aria-pressed', String(!showing));
  pwToggle.setAttribute('aria-label', showing ? 'show password' : 'hide password');
});

function setMode(backup) {
  backupMode = backup;
  if (backup) {
    // Recovery codes are long-lived single-use credentials. Mask the
    // field on-screen so shoulder-surfing can't lift one by just watching
    // the user type -- same rationale as the sender-form and receiver-form
    // passphrase fields.
    codeLabel.textContent = 'Recovery code';
    codeInput.setAttribute('type', 'password');
    codeInput.setAttribute('autocomplete', 'off');
    codeInput.setAttribute('inputmode', 'text');
    codeInput.setAttribute('pattern', '[0-9A-Za-z\\-]*');
    codeInput.placeholder = 'XXXXX-XXXXX';
    codeToggleBtn.hidden = false;
    toggle.textContent = 'Use 6-digit code';
  } else {
    // TOTP codes rotate every 30s with anti-replay; masking them buys
    // nothing and costs UX. Leave plain text, hide the show/hide button.
    codeLabel.textContent = '6-digit code';
    codeInput.setAttribute('type', 'text');
    codeInput.setAttribute('autocomplete', 'one-time-code');
    codeInput.setAttribute('inputmode', 'numeric');
    codeInput.placeholder = '';
    codeToggleBtn.hidden = true;
    toggle.textContent = 'Use a recovery code';
  }
  // Reset the show/hide button's internal state whenever the mode flips.
  codeToggleBtn.setAttribute('aria-pressed', 'false');
  codeToggleBtn.setAttribute('aria-label', 'show code');
  codeToggleBtn.textContent = 'show';
  codeInput.value = '';
  codeInput.focus();
}

toggle.addEventListener('click', () => setMode(!backupMode));

// Show/hide toggle for the recovery-code field. Only wired when the toggle
// exists in the DOM (defensive -- the button is part of the current html
// but if a future refactor removes it, the handler just silently no-ops).
if (codeToggleBtn) {
  codeToggleBtn.addEventListener('click', () => {
    const showing = codeInput.getAttribute('type') === 'text';
    codeInput.setAttribute('type', showing ? 'password' : 'text');
    codeToggleBtn.textContent = showing ? 'show' : 'hide';
    codeToggleBtn.setAttribute('aria-pressed', String(!showing));
    codeToggleBtn.setAttribute(
      'aria-label',
      showing ? 'show code' : 'hide code',
    );
  });
}

const submitBtn = form.querySelector('button[type="submit"]');
const submitLabel = submitBtn.textContent;

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  // In-flight guard: stops a rapid double-tap from firing two logins.
  // TOTP anti-replay would reject the second request anyway and overwrite
  // the success state with an "invalid credentials" flash.
  if (submitBtn.disabled) return;
  err.hidden = true;
  submitBtn.disabled = true;
  submitBtn.textContent = 'Signing in…';

  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  const code = codeInput.value;
  const body = new URLSearchParams({ username, password, code });

  let res;
  try {
    res = await fetch('/send/login', {
      method: 'POST',
      body,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
  } catch {
    submitBtn.disabled = false;
    submitBtn.textContent = submitLabel;
    err.textContent = 'Network error. Please try again.';
    err.hidden = false;
    return;
  }

  if (res.ok) {
    // Leave the button disabled — the page is about to reload.
    window.location.reload();
    return;
  }
  if (res.status === 423) {
    const data = await res.json().catch(() => ({}));
    const until = data.detail && data.detail.until;
    err.textContent = until
      ? `Too many failed attempts. Locked until ${new Date(until).toLocaleString()}.`
      : 'Too many failed attempts. Account locked.';
  } else if (res.status === 429) {
    err.textContent = 'Too many attempts. Please wait a moment.';
  } else if (res.status === 422) {
    err.textContent = 'Form fields out of date — hard-refresh the page (Ctrl+Shift+R) and try again.';
  } else if (res.status === 401) {
    err.textContent = 'Invalid credentials.';
  } else {
    err.textContent = `Unexpected error (HTTP ${res.status}). Check server logs.`;
  }
  err.hidden = false;
  submitBtn.disabled = false;
  submitBtn.textContent = submitLabel;
});
