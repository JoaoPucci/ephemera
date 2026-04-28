// Sender page entry. Most of the page's JS lives in sender/ submodules;
// this file just imports them (their top-level code wires handlers) and
// handles the two small bits that don't belong to either: the top-right
// user pill + logout button, and the initial /api/me fetch that populates
// the username in that pill.
import { copyWithFeedback } from './copy.js';
import './sender/form.js'; // side-effect import: wires compose form + status widget
import { renderTrackedList } from './sender/tracked-list.js';
import { bindTwoClickConfirm } from './two-click.js';

// "Copy URL" button on the success screen.
document.getElementById('copy-url').addEventListener('click', (e) => {
  const url = document.getElementById('result-url').textContent;
  copyWithFeedback(e.currentTarget, url);
});

// "Copy passphrase" reads the unmasked value from data-real, regardless of
// whether the screen is currently showing dots or the real string -- copying
// the dots would be a frustrating footgun.
document.getElementById('copy-passphrase').addEventListener('click', (e) => {
  const real = document.getElementById('result-passphrase').dataset.real || '';
  copyWithFeedback(e.currentTarget, real);
});

// Show/hide on the result-screen passphrase. Mirrors the compose-form pattern:
// data-masked carries the visual state; aria-pressed carries the ARIA state.
{
  const toggle = document.getElementById('toggle-result-passphrase');
  const passphraseEl = document.getElementById('result-passphrase');
  toggle.addEventListener('click', () => {
    const masked = passphraseEl.dataset.masked === 'true';
    if (masked) {
      passphraseEl.textContent = passphraseEl.dataset.real || '';
      passphraseEl.dataset.masked = 'false';
      toggle.setAttribute('aria-pressed', 'true');
      toggle.textContent = window.i18n.t('button.hide');
    } else {
      const real = passphraseEl.dataset.real || '';
      passphraseEl.textContent = '•'.repeat(Math.min(real.length, 16));
      passphraseEl.dataset.masked = 'true';
      toggle.setAttribute('aria-pressed', 'false');
      toggle.textContent = window.i18n.t('button.show');
    }
  });
}

// Top-left user pill: shows the signed-in username. Two-click confirm
// for sign-out so accidental clicks don't blow away the session.
const userBtn = document.getElementById('user-btn');
const userNameEl = document.getElementById('user-name');
if (userBtn) {
  bindTwoClickConfirm(userBtn, {
    labelEl: userBtn.querySelector('.user-action'),
    confirmKey: 'button.sign_out_confirm',
    armedAriaLabel: 'Click again to confirm sign out',
    onConfirm: async () => {
      try {
        await fetch('/send/logout', { method: 'POST' });
      } catch {}
      // reload() re-fetches the same URL; setting href to the same URL is
      // a no-op in most browsers, which is why an old version appeared to
      // "do nothing".
      window.location.reload();
    },
  });
}

(async function loadMe() {
  try {
    const res = await fetch('/api/me');
    if (res.status === 401) {
      window.location.reload();
      return;
    }
    if (!res.ok) return;
    const me = await res.json();
    if (userNameEl && me.username) userNameEl.textContent = me.username;
    if (userBtn)
      userBtn.setAttribute('aria-label', `Signed in as ${me.username}. Click to sign out.`);
    // Broadcast the resolved /api/me payload so unrelated modules (the
    // chrome-menu drawer toggle, the sender form's analytics-aware submit
    // path) can react without each having to re-fetch. CustomEvent shape
    // keeps the consumers decoupled from this module's bootstrap order.
    window.dispatchEvent(new CustomEvent('ephemera:me-loaded', { detail: me }));
  } catch {}
})();

// Initial tracked-list paint; the module's own polling kicks in from there.
renderTrackedList();
