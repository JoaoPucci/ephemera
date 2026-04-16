(() => {
  const form = document.getElementById('login-form');
  const err = document.getElementById('login-error');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;
    const apiKey = document.getElementById('api-key').value;
    const body = new URLSearchParams({ api_key: apiKey });
    const res = await fetch('/send/login', {
      method: 'POST',
      body,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    if (res.ok) {
      // Force a fresh load of /send so the server serves sender.html with the session cookie.
      window.location.reload();
    } else {
      err.textContent = 'Invalid API key.';
      err.hidden = false;
    }
  });
})();
