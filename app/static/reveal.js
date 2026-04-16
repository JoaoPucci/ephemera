(() => {
  const token = window.location.pathname.split('/').pop();
  const states = {
    loading: document.getElementById('state-loading'),
    ready:   document.getElementById('state-ready'),
    text:    document.getElementById('state-text'),
    image:   document.getElementById('state-image'),
    gone:    document.getElementById('state-gone'),
  };
  const passphraseWrap = document.getElementById('passphrase-wrap');
  const passphraseInput = document.getElementById('passphrase');
  const revealBtn = document.getElementById('reveal-btn');
  const errBox = document.getElementById('reveal-error');

  function show(name) {
    Object.entries(states).forEach(([k, el]) => (el.hidden = k !== name));
  }

  async function init() {
    let meta;
    try {
      const res = await fetch(`/s/${encodeURIComponent(token)}/meta`);
      if (res.status === 404) return show('gone');
      if (!res.ok) return show('gone');
      meta = await res.json();
    } catch {
      return show('gone');
    }
    passphraseWrap.hidden = !meta.passphrase_required;
    show('ready');
  }

  revealBtn.addEventListener('click', reveal);

  async function reveal() {
    errBox.hidden = true;
    const fragment = (window.location.hash || '').replace(/^#/, '');
    if (!fragment) {
      errBox.textContent = 'This link is missing its decryption key.';
      errBox.hidden = false;
      return;
    }
    const body = { key: fragment };
    if (!passphraseWrap.hidden) body.passphrase = passphraseInput.value;
    revealBtn.disabled = true;

    let res;
    try {
      res = await fetch(`/s/${encodeURIComponent(token)}/reveal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch {
      revealBtn.disabled = false;
      errBox.textContent = 'Network error. Try again.';
      errBox.hidden = false;
      return;
    }

    if (res.status === 401) {
      revealBtn.disabled = false;
      errBox.textContent = 'Wrong passphrase.';
      errBox.hidden = false;
      return;
    }
    if (res.status === 410) return show('gone');
    if (res.status === 429) {
      revealBtn.disabled = false;
      errBox.textContent = 'Too many requests. Please wait a moment.';
      errBox.hidden = false;
      return;
    }
    if (res.status === 404) return show('gone');
    if (!res.ok) {
      revealBtn.disabled = false;
      errBox.textContent = 'Failed to reveal secret.';
      errBox.hidden = false;
      return;
    }

    const data = await res.json();
    if (data.content_type === 'image') {
      const img = document.getElementById('revealed-image');
      img.src = `data:${data.mime_type};base64,${data.content}`;
      show('image');
      document.getElementById('main-card').classList.add('wide');
    } else {
      document.getElementById('revealed-text').textContent = data.content;
      const btn = document.getElementById('copy-btn');
      btn.hidden = false;
      btn.addEventListener('click', (e) => {
        window.copyWithFeedback(e.currentTarget, data.content);
      });
      show('text');
    }
  }

  init();
})();
