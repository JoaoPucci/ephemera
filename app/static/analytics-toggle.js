// Per-user analytics-opt-in toggle. Two surfaces, one handler:
//
//   - desktop pill `#analytics-toggle` in top-chrome-right (visible above
//     the mobile breakpoint). On opt-IN it opens `#analytics-popover`
//     anchored under the pill; on opt-OUT it flips immediately and
//     surfaces a transient ack in `#analytics-toggle-ack`.
//   - drawer row `#chrome-menu-analytics` inside the hamburger menu
//     (visible below the mobile breakpoint). On opt-IN it expands the
//     sibling `.chrome-menu-row-disclosure` block in place; on opt-OUT
//     it flips instantly and surfaces the ack in `.chrome-menu-row-ack`.
//
// Asymmetric on purpose (designer round on PR #72): the privacy-violating
// direction (off -> on) gates on a confirm step so the user reads what's
// being collected; the privacy-preserving direction (on -> off) is one
// click. Mirrors ATT-style flows.
//
// All visible copy is rendered from the JSON i18n catalogue so non-English
// users don't see a server-rendered English fallback. Buttons ship with
// empty text nodes carrying `data-i18n="<key>"`; this module fills them
// on init and on i18n locale change. Disclosure/popover content is
// likewise rendered from JSON; the confirm/cancel buttons inside them
// use `data-i18n` for their own labels.
(() => {
  const desktopBtn = document.getElementById('analytics-toggle');
  const drawerBtn = document.getElementById('chrome-menu-analytics');
  if (!desktopBtn && !drawerBtn) return;

  const popover = document.getElementById('analytics-popover');
  const drawerDisclosure = document.getElementById('chrome-menu-analytics-disclosure');
  const desktopAck = document.getElementById('analytics-toggle-ack');
  const desktopAckTip = document.getElementById('analytics-toggle-ack-tip');

  // ---- i18n: fill data-i18n placeholders on init ----
  function t(key, fallback) {
    if (window.i18n?.t) {
      const val = window.i18n.t(key);
      // window.i18n.t falls back to the key itself on miss; treat that
      // as the fallback case so we don't render the raw dotted-key path.
      if (val && val !== key) return val;
    }
    return fallback;
  }

  function fillLabels(root) {
    if (!root) return;
    const nodes = root.querySelectorAll('[data-i18n]');
    for (const el of nodes) {
      const key = el.getAttribute('data-i18n');
      const text = t(key, '');
      if (text) el.textContent = text;
    }
  }

  fillLabels(document);

  // ---- State sync from /api/me + cross-surface broadcasts ----
  function setState(enabled) {
    const value = enabled ? 'true' : 'false';
    if (desktopBtn) desktopBtn.setAttribute('aria-checked', value);
    if (drawerBtn) drawerBtn.setAttribute('aria-checked', value);
  }
  window.addEventListener('ephemera:me-loaded', (e) => {
    setState(Boolean(e.detail?.analytics_opt_in));
  });
  window.addEventListener('ephemera:me-updated', (e) => {
    setState(Boolean(e.detail?.analytics_opt_in));
  });

  // ---- Confirm dialog open/close ----
  // Each surface has its own affordance shape (popover for desktop,
  // inline disclosure for drawer) but the open/close lifecycle is the
  // same: the triggering button gets aria-expanded toggled, the panel
  // gains/loses `hidden`. We track the currently-open panel in a single
  // variable so an outside-click handler can dismiss whichever is up.
  let openPanel = null;
  let openButton = null;

  function openConfirm(surface) {
    if (surface === 'desktop' && popover && desktopBtn) {
      openPanel = popover;
      openButton = desktopBtn;
    } else if (surface === 'drawer' && drawerDisclosure && drawerBtn) {
      openPanel = drawerDisclosure;
      openButton = drawerBtn;
    } else {
      return;
    }
    openPanel.hidden = false;
    openButton.setAttribute('aria-expanded', 'true');
    // Move focus to the cancel button -- safer default than confirm,
    // matches OS-dialog convention where Esc/outside-click both behave
    // like cancel. The user must reach for confirm explicitly.
    const cancelBtn = openPanel.querySelector(
      '.analytics-popover-cancel, .chrome-menu-row-disclosure-cancel'
    );
    if (cancelBtn) cancelBtn.focus();
  }

  function closeConfirm() {
    if (!openPanel) return;
    openPanel.hidden = true;
    if (openButton) {
      openButton.setAttribute('aria-expanded', 'false');
      // Return focus to the trigger so keyboard users don't lose place.
      openButton.focus();
    }
    openPanel = null;
    openButton = null;
  }

  // ---- Outside-click + Esc dismissal ----
  document.addEventListener('click', (e) => {
    if (!openPanel) return;
    // Click inside the panel or on the trigger? Leave it open.
    if (openPanel.contains(e.target)) return;
    if (openButton?.contains(e.target)) return;
    closeConfirm();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && openPanel) {
      e.preventDefault();
      closeConfirm();
    }
  });

  // ---- Opt-OUT confirmation feedback (per-surface) ----
  // Each surface has a sighted-user channel and a screen-reader channel.
  // Both fire on opt-OUT.
  //
  //   * Desktop:
  //       - Sighted: a position-fixed tooltip appears below the chrome
  //         row (`#analytics-toggle-ack-tip`, .is-visible class). Out of
  //         flow, no layout push on lang-picker / theme-toggle.
  //       - SR: aria-live=polite text on the visually-hidden span next
  //         to the pill (`#analytics-toggle-ack`).
  //   * Drawer:
  //       - Sighted: briefly swap the row label text to the ack string,
  //         then revert. Same pattern as the sign-out confirm. No layout
  //         push because the swap happens on the existing label slot.
  //       - SR: aria-live text on the row's visually-hidden ack span.
  //
  // Both channels carry the same string; both clear after 1.5s.
  let ackTimer = null;
  function announceDisabledAck(surface) {
    const text = t('analytics.disabled_ack', 'Sharing turned off');
    if (ackTimer) clearTimeout(ackTimer);

    if (surface === 'desktop') {
      // Sighted: tooltip-style overlay.
      if (desktopAckTip) {
        desktopAckTip.textContent = text;
        desktopAckTip.classList.add('is-visible');
      }
      // SR: aria-live span.
      if (desktopAck) desktopAck.textContent = text;
      ackTimer = setTimeout(() => {
        if (desktopAckTip) {
          desktopAckTip.classList.remove('is-visible');
          // Wait for fade transition before clearing the textContent
          // so the text doesn't pop out before opacity finishes.
          setTimeout(() => {
            if (desktopAckTip && !desktopAckTip.classList.contains('is-visible')) {
              desktopAckTip.textContent = '';
            }
          }, 250);
        }
        if (desktopAck) desktopAck.textContent = '';
      }, 1500);
      return;
    }

    if (surface === 'drawer' && drawerBtn) {
      // Sighted: label-swap on the row label.
      const labelEl = drawerBtn.querySelector('.chrome-menu-row-label');
      const original = labelEl ? labelEl.textContent : '';
      if (labelEl) labelEl.textContent = text;
      // SR: aria-live span on the row.
      const srAck = drawerBtn.querySelector('.chrome-menu-row-ack');
      if (srAck) srAck.textContent = text;
      ackTimer = setTimeout(() => {
        // Revert only if the label wasn't already changed by something
        // else (e.g. an i18n locale flip mid-1.5s). The locale change
        // would have rewritten textContent through the data-i18n
        // pipeline, in which case we leave it alone.
        if (labelEl && labelEl.textContent === text) labelEl.textContent = original;
        if (srAck) srAck.textContent = '';
      }, 1500);
    }
  }

  // ---- PATCH + state propagation ----
  async function commit(next) {
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
      return persisted;
    } catch {
      // No toast: the switch never animated (we set state from the server
      // response, not optimistically), so the user-perceived state never
      // diverged from the server's. Silent failure is the right shape.
      return null;
    }
  }

  // ---- Click handlers ----
  function handleTriggerClick(surface) {
    return async () => {
      const btn = surface === 'desktop' ? desktopBtn : drawerBtn;
      const currentlyOn = btn.getAttribute('aria-checked') === 'true';
      if (currentlyOn) {
        // Asymmetric off-path: instant flip + SR announcement.
        const persisted = await commit(false);
        if (persisted === false) announceDisabledAck(surface);
        return;
      }
      // Off -> on: open the confirm dialog. Don't flip yet.
      openConfirm(surface);
    };
  }

  function wireConfirmActions(panel) {
    if (!panel) return;
    const cancelBtn = panel.querySelector(
      '.analytics-popover-cancel, .chrome-menu-row-disclosure-cancel'
    );
    const confirmBtn = panel.querySelector(
      '.analytics-popover-confirm, .chrome-menu-row-disclosure-confirm'
    );
    if (cancelBtn) cancelBtn.addEventListener('click', closeConfirm);
    if (confirmBtn) {
      confirmBtn.addEventListener('click', async () => {
        await commit(true);
        // Close regardless of PATCH outcome. On failure the switch stays
        // off (commit() didn't update aria-checked), and the next click
        // will re-open the dialog -- which is what the user expects.
        closeConfirm();
      });
    }
  }

  if (desktopBtn) desktopBtn.addEventListener('click', handleTriggerClick('desktop'));
  if (drawerBtn) drawerBtn.addEventListener('click', handleTriggerClick('drawer'));
  wireConfirmActions(popover);
  wireConfirmActions(drawerDisclosure);
})();
