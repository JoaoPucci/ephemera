// Analytics-opt-in toggle. Wires two surfaces with a single handler:
//
//   - desktop pill `#analytics-toggle` in top-chrome-right (visible above
//     the mobile breakpoint)
//   - drawer row `#chrome-menu-analytics` inside the hamburger menu
//     (visible below the mobile breakpoint, in the auth-gated section)
//
// Both surfaces are kept in sync via the shared `ephemera:me-loaded` /
// `ephemera:me-updated` CustomEvents. Clicking either flips, optimistic
// PATCH /api/me/preferences, rollback if the patch fails. The server
// response is the source of truth -- we re-broadcast it so any other
// listener (sender form gating `near_cap`) sees the same value.
(() => {
  const desktopBtn = document.getElementById('analytics-toggle');
  const drawerBtn = document.getElementById('chrome-menu-analytics');
  if (!desktopBtn && !drawerBtn) return;

  function setState(enabled) {
    if (desktopBtn) desktopBtn.setAttribute('aria-checked', enabled ? 'true' : 'false');
    if (drawerBtn) drawerBtn.setAttribute('aria-checked', enabled ? 'true' : 'false');
  }

  // Initial state from the /api/me payload that sender.js broadcasts.
  // Both buttons start aria-checked="false" via the template; this just
  // promotes them once /api/me lands.
  window.addEventListener('ephemera:me-loaded', (e) => {
    setState(Boolean(e.detail?.analytics_opt_in));
  });
  // Keep both surfaces in lockstep when the OTHER surface flips. e.g.,
  // user clicks the drawer toggle, the resulting `ephemera:me-updated`
  // syncs the desktop pill (and vice-versa).
  window.addEventListener('ephemera:me-updated', (e) => {
    setState(Boolean(e.detail?.analytics_opt_in));
  });

  async function handleClick() {
    const current = (desktopBtn || drawerBtn).getAttribute('aria-checked') === 'true';
    const next = !current;
    // Optimistic flip so the switch animates immediately. Rollback on
    // failure -- a 401/500/network error keeps the user-perceived state
    // aligned with the server.
    setState(next);
    try {
      const res = await fetch('/api/me/preferences', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ analytics_opt_in: next }),
      });
      if (!res.ok) throw new Error(`patch failed: ${res.status}`);
      const me = await res.json();
      const persisted = Boolean(me.analytics_opt_in);
      setState(persisted);
      window.dispatchEvent(new CustomEvent('ephemera:me-updated', { detail: me }));
    } catch {
      // Rollback. No error toast: the toggle is small, the failure mode
      // is "the switch snaps back" -- which is itself the signal.
      setState(current);
    }
  }

  if (desktopBtn) desktopBtn.addEventListener('click', handleClick);
  if (drawerBtn) drawerBtn.addEventListener('click', handleClick);
})();
