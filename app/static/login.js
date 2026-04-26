// ES module. Top-level code runs once on import, against the DOM present
// at the time the html file loads this via <script type="module">.

const form = document.getElementById('login-form');
const err = document.getElementById('login-error');
const codeInput = document.getElementById('code');
const codeLabel = document.getElementById('code-label');
const codeHint = document.getElementById('code-hint');
const toggle = document.getElementById('toggle-code-mode');
const codeToggleBtn = document.getElementById('toggle-code');

// Recovery codes are 10 base32 chars grouped as XXXXX-XXXXX (11 visible).
// The format helper soft-coerces input as the user types (uppercase, drop
// non-alphanum, auto-insert dash after position 5, truncate to 10 alphanum).
// Backend `_normalize_backup_code` is also permissive, so this is purely
// a UX/visual nicety -- typos still go through bcrypt and miss; pasted
// raw 10-char codes still validate without a dash. "Soft" means we never
// reject a keystroke; we just normalize what's there.
const RECOVERY_VISIBLE_GROUP = 5;
const RECOVERY_ALPHANUM_TOTAL = 10;

function _softFormatRecoveryCode() {
  const before = codeInput.value;
  const cursorOriginal = codeInput.selectionStart ?? before.length;
  // Count alphanum chars to the left of the cursor in the raw value -- this
  // is what we want to preserve across the format pass, regardless of whether
  // an auto-dash is added or removed.
  const alphanumBeforeCursor = before.slice(0, cursorOriginal).replace(/[^A-Za-z0-9]/g, '').length;

  const alphanum = before
    .replace(/[^A-Za-z0-9]/g, '')
    .toUpperCase()
    .slice(0, RECOVERY_ALPHANUM_TOTAL);

  const formatted =
    alphanum.length > RECOVERY_VISIBLE_GROUP
      ? `${alphanum.slice(0, RECOVERY_VISIBLE_GROUP)}-${alphanum.slice(RECOVERY_VISIBLE_GROUP)}`
      : alphanum;

  // Step the cursor one to the right when crossing the auto-inserted dash,
  // so typing the 6th alphanum lands the cursor *after* the dash, not on it.
  const newCursor =
    alphanumBeforeCursor > RECOVERY_VISIBLE_GROUP ? alphanumBeforeCursor + 1 : alphanumBeforeCursor;

  if (formatted !== before) {
    codeInput.value = formatted;
    codeInput.setSelectionRange(newCursor, newCursor);
  }
}

// Browsers often restore form values on reload. A TOTP code is one-shot
// by definition, so wipe it on page load. Also wipe if the page is
// restored from the bfcache (back button).
const clearOneShotFields = () => {
  codeInput.value = '';
};
clearOneShotFields();
window.addEventListener('pageshow', clearOneShotFields);

let backupMode = false;

// Password visibility toggle. Strings go through window.i18n.t so the
// click-flipped label matches the page locale -- the template renders
// the initial "show" via gettext, then JS had been writing back English
// on every click, which produced a surprising locale-flip. Both toggles
// in this file have the same issue; same fix shape.
const pwInput = document.getElementById('password');
const pwToggle = document.getElementById('toggle-password');
pwToggle.addEventListener('click', () => {
  const showing = pwInput.getAttribute('type') === 'text';
  pwInput.setAttribute('type', showing ? 'password' : 'text');
  pwToggle.textContent = window.i18n.t(showing ? 'button.show' : 'button.hide');
  pwToggle.setAttribute('aria-pressed', String(!showing));
  pwToggle.setAttribute(
    'aria-label',
    window.i18n.t(showing ? 'login.aria_show_password' : 'login.aria_hide_password')
  );
});

function setMode(backup) {
  backupMode = backup;
  if (backup) {
    // Recovery codes are long-lived single-use credentials. Mask the
    // field on-screen so shoulder-surfing can't lift one by just watching
    // the user type -- same rationale as the sender-form and receiver-form
    // passphrase fields.
    codeLabel.textContent = window.i18n.t('login.code_label_recovery');
    codeInput.setAttribute('type', 'password');
    codeInput.setAttribute('autocomplete', 'off');
    codeInput.setAttribute('inputmode', 'text');
    codeInput.setAttribute('pattern', '[A-Za-z0-9]{5}-?[A-Za-z0-9]{5}');
    codeInput.setAttribute('minlength', '10');
    codeInput.setAttribute('maxlength', '11');
    codeInput.setAttribute('autocapitalize', 'characters');
    codeInput.placeholder = 'XXXXX-XXXXX';
    codeInput.addEventListener('input', _softFormatRecoveryCode);
    codeHint.hidden = false;
    codeHint.textContent = window.i18n.t('hint.recovery_format');
    codeToggleBtn.hidden = false;
    toggle.textContent = window.i18n.t('login.toggle_to_totp');
  } else {
    // TOTP codes rotate every 30s with anti-replay; masking them buys
    // nothing and costs UX. Leave plain text, hide the show/hide button.
    codeLabel.textContent = window.i18n.t('login.code_label_totp');
    codeInput.setAttribute('type', 'text');
    codeInput.setAttribute('autocomplete', 'one-time-code');
    codeInput.setAttribute('inputmode', 'numeric');
    codeInput.setAttribute('pattern', '[0-9]{6}');
    codeInput.setAttribute('minlength', '6');
    codeInput.setAttribute('maxlength', '6');
    codeInput.setAttribute('autocapitalize', 'off');
    codeInput.placeholder = '';
    codeInput.removeEventListener('input', _softFormatRecoveryCode);
    codeHint.hidden = true;
    codeHint.textContent = '';
    codeToggleBtn.hidden = true;
    toggle.textContent = window.i18n.t('login.toggle_to_recovery');
  }
  // Reset the show/hide button's internal state whenever the mode flips.
  codeToggleBtn.setAttribute('aria-pressed', 'false');
  codeToggleBtn.setAttribute('aria-label', window.i18n.t('login.aria_show_code'));
  codeToggleBtn.textContent = window.i18n.t('button.show');
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
    codeToggleBtn.textContent = window.i18n.t(showing ? 'button.show' : 'button.hide');
    codeToggleBtn.setAttribute('aria-pressed', String(!showing));
    codeToggleBtn.setAttribute(
      'aria-label',
      window.i18n.t(showing ? 'login.aria_show_code' : 'login.aria_hide_code')
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
  submitBtn.textContent = window.i18n.t('button.signing_in');

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
    err.textContent = window.i18n.t('error.network');
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
    const until = data.detail?.until;
    err.textContent = until
      ? window.i18n.t('error.locked_with_until', {
          until: new Date(until).toLocaleString(window.i18n.currentLocale),
        })
      : window.i18n.t('error.locked');
  } else if (res.status === 429) {
    err.textContent = window.i18n.t('error.too_many_attempts');
  } else if (res.status === 422) {
    err.textContent = window.i18n.t('error.form_stale');
  } else if (res.status === 401) {
    err.textContent = window.i18n.t('error.invalid_credentials');
  } else {
    err.textContent = window.i18n.t('error.unexpected_http', { status: res.status });
  }
  err.hidden = false;
  submitBtn.disabled = false;
  submitBtn.textContent = submitLabel;
});
