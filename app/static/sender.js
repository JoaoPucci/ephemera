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

// Top-right user pill: shows the signed-in username, click to log out.
const userBtn = document.getElementById('user-btn');
const userNameEl = document.getElementById('user-name');
if (userBtn) {
  userBtn.addEventListener('click', async () => {
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
