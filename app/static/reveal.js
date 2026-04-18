// ES module. Top-level code runs once on import, wiring listeners
// against the DOM that's present when <script type="module"> executes.
import { copyWithFeedback } from './copy.js';

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

const revealLabel = revealBtn.textContent;
revealBtn.addEventListener('click', reveal);

async function reveal() {
  // Hoist the in-flight guard to the very first line so a rapid second
  // tap can't slip through any sync work before the first handler yields
  // at `await`. A destroyed secret we failed to render is unrecoverable,
  // so we're deliberately strict here.
  if (revealBtn.disabled) return;
  revealBtn.disabled = true;
  errBox.hidden = true;

  const fragment = (window.location.hash || '').replace(/^#/, '');
  if (!fragment) {
    revealBtn.disabled = false;
    errBox.textContent = 'This link is missing its decryption key.';
    errBox.hidden = false;
    return;
  }
  const body = { key: fragment };
  if (!passphraseWrap.hidden) body.passphrase = passphraseInput.value;
  // Visible "we're working on it" state -- without this, a slow network
  // looks identical to a dead click and a nervous user taps again.
  revealBtn.textContent = 'Revealing…';

  const restoreButton = () => {
    revealBtn.disabled = false;
    revealBtn.textContent = revealLabel;
  };

  let res;
  try {
    res = await fetch(`/s/${encodeURIComponent(token)}/reveal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    restoreButton();
    errBox.textContent = 'Network error. Try again.';
    errBox.hidden = false;
    return;
  }

  if (res.status === 401) {
    restoreButton();
    errBox.textContent = 'Wrong passphrase.';
    errBox.hidden = false;
    return;
  }
  if (res.status === 410) return show('gone');
  if (res.status === 429) {
    restoreButton();
    errBox.textContent = 'Too many requests. Please wait a moment.';
    errBox.hidden = false;
    return;
  }
  if (res.status === 404) return show('gone');
  if (!res.ok) {
    restoreButton();
    errBox.textContent = 'Failed to reveal secret.';
    errBox.hidden = false;
    return;
  }

  const data = await res.json();
  if (data.content_type === 'image') {
    const img = document.getElementById('revealed-image');
    const src = `data:${data.mime_type};base64,${data.content}`;
    img.src = src;
    show('image');
    document.getElementById('main-card').classList.add('wide');
    wireZoom(img, src);
  } else {
    document.getElementById('revealed-text').textContent = data.content;
    const btn = document.getElementById('copy-btn');
    btn.hidden = false;
    btn.addEventListener('click', (e) => {
      copyWithFeedback(e.currentTarget, data.content);
    });
    show('text');
  }
}

function wireZoom(thumb, src) {
  const overlay = document.getElementById('zoom-overlay');
  const zoomImg = document.getElementById('zoom-image');
  const closeBtn = document.getElementById('zoom-close');

  function open() {
    zoomImg.src = src;
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
    closeBtn.focus();
  }
  function close() {
    overlay.hidden = true;
    zoomImg.src = '';
    document.body.style.overflow = '';
    thumb.focus();
  }

  thumb.addEventListener('click', open);
  thumb.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });
  overlay.addEventListener('click', close);
  closeBtn.addEventListener('click', (e) => { e.stopPropagation(); close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !overlay.hidden) close();
  });
}

init();
