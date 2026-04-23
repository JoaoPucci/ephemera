// Sender page entry. Most of the page's JS lives in sender/ submodules;
// this file just imports them (their top-level code wires handlers) and
// handles the two small bits that don't belong to either: the top-right
// user pill + logout button, and the initial /api/me fetch that populates
// the username in that pill.
import { copyWithFeedback } from './copy.js';
import './sender/form.js';   // side-effect import: wires compose form + status widget
import { renderTrackedList } from './sender/tracked-list.js';

// "Copy URL" button on the success screen.
document.getElementById('copy-url').addEventListener('click', (e) => {
  const url = document.getElementById('result-url').textContent;
  copyWithFeedback(e.currentTarget, url);
});

// Top-left user pill: shows the signed-in username. Two-click confirm
// for sign-out so accidental clicks don't blow away the session. Same
// shape as the tracked-list cancel button: first click arms (adds
// .armed class, swaps the action label to `button.confirm`, 3s auto-
// disarm timeout); second click while armed fires the logout.
const userBtn = document.getElementById('user-btn');
const userNameEl = document.getElementById('user-name');
if (userBtn) {
  const actionEl = userBtn.querySelector('.user-action');
  // Capture the original localised "sign out" label from the DOM so the
  // timeout can restore the user's locale without knowing which one it is
  // (template renders this via gettext; no JS-catalog entry needed).
  const signOutLabel = actionEl ? actionEl.textContent : '';
  const baseAria = userBtn.getAttribute('aria-label') || '';
  let armTimer = null;

  const disarm = () => {
    userBtn.classList.remove('armed');
    if (actionEl) actionEl.textContent = signOutLabel;
    userBtn.setAttribute('aria-label', baseAria);
  };

  userBtn.addEventListener('click', async () => {
    if (!userBtn.classList.contains('armed')) {
      userBtn.classList.add('armed');
      if (actionEl) actionEl.textContent = window.i18n.t('button.confirm');
      userBtn.setAttribute('aria-label', 'Click again to confirm sign out');
      armTimer = setTimeout(disarm, 3000);
      return;
    }
    if (armTimer) clearTimeout(armTimer);
    try {
      await fetch('/send/logout', { method: 'POST' });
    } catch {}
    // reload() re-fetches the same URL; setting href to the same URL is a no-op
    // in most browsers, which is why the old version appeared to "do nothing".
    window.location.reload();
  });
}

(async function loadMe() {
  try {
    const res = await fetch('/api/me');
    if (res.status === 401) { window.location.reload(); return; }
    if (!res.ok) return;
    const me = await res.json();
    if (userNameEl && me.username) userNameEl.textContent = me.username;
    if (userBtn) userBtn.setAttribute('aria-label', `Signed in as ${me.username}. Click to sign out.`);
  } catch {}
})();

// Initial tracked-list paint; the module's own polling kicks in from there.
renderTrackedList();
