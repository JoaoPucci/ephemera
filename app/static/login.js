(() => {
  const form = document.getElementById('login-form');
  const err = document.getElementById('login-error');
  const codeInput = document.getElementById('code');
  const codeLabel = document.getElementById('code-label');
  const toggle = document.getElementById('toggle-code-mode');

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
      codeLabel.textContent = 'Recovery code';
      codeInput.setAttribute('autocomplete', 'off');
      codeInput.setAttribute('inputmode', 'text');
      codeInput.setAttribute('pattern', '[0-9A-Za-z\\-]*');
      codeInput.placeholder = 'XXXXX-XXXXX';
      toggle.textContent = 'Use 6-digit code';
    } else {
      codeLabel.textContent = '6-digit code';
      codeInput.setAttribute('autocomplete', 'one-time-code');
      codeInput.setAttribute('inputmode', 'numeric');
      codeInput.placeholder = '';
      toggle.textContent = 'Use a recovery code';
    }
    codeInput.value = '';
    codeInput.focus();
  }

  toggle.addEventListener('click', () => setMode(!backupMode));

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;

    const password = document.getElementById('password').value;
    const code = codeInput.value;
    const body = new URLSearchParams({ password, code });

    const res = await fetch('/send/login', {
      method: 'POST',
      body,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });

    if (res.ok) {
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
  });
})();
