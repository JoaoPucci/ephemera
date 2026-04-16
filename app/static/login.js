(() => {
  const form = document.getElementById('login-form');
  const err = document.getElementById('login-error');
  const codeInput = document.getElementById('code');
  const codeLabel = document.getElementById('code-label');
  const toggle = document.getElementById('toggle-code-mode');

  let backupMode = false;

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
    } else {
      err.textContent = 'Invalid credentials.';
    }
    err.hidden = false;
  });
})();
