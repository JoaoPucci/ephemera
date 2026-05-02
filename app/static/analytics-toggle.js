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

  // Bootstrap initial state from /api/me directly. Prior versions of
  // this module relied on `ephemera:me-loaded` from sender.js, but
  // that's only emitted on the sender page -- on any other authed
  // page (or future authed surface) the toggle would render stuck at
  // the template default `aria-checked="false"`, showing the wrong
  // state for opted-in users and turning a corrective click into a
  // no-op. Fetching here makes the toggle self-sufficient on every
  // page that renders it, and the duplicate /api/me on the sender
  // page is cheap (small JSON, browser HTTP cache). The me-updated
  // event below remains the cross-surface sync channel for in-page
  // PATCH results.
  (async function bootstrap() {
    try {
      const res = await fetch('/api/me');
      if (!res.ok) return;
      const me = await res.json();
      setState(Boolean(me.analytics_opt_in));
    } catch {
      // Auth/network errors are handled at the page level (sender.js
      // will reload on 401 etc.). Toggle stays at default; nothing
      // useful we can do here.
    }
  })();
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

  // Position the desktop tip at show-time. Anchored to the trigger
  // pill's rect rather than the chrome row's right edge because
  // lang-picker / theme-toggle neighbours shift width with locale.
  function positionDesktopTip(text) {
    if (!desktopAckTip || !desktopBtn) return;
    const rect = desktopBtn.getBoundingClientRect();
    const isRTL = document.documentElement.dir === 'rtl' || document.dir === 'rtl';
    desktopAckTip.textContent = text;
    desktopAckTip.style.top = `${rect.bottom + 6}px`;
    if (isRTL) {
      desktopAckTip.style.left = `${rect.left}px`;
      desktopAckTip.style.right = 'auto';
    } else {
      desktopAckTip.style.right = `${window.innerWidth - rect.right}px`;
      desktopAckTip.style.left = 'auto';
    }
    desktopAckTip.classList.add('is-visible');
  }

  // Clean up the desktop tip after its 1.5s show window. Two-stage:
  // remove the visibility class to start the fade, then clear text
  // and inline positioning after the 250ms transition so the next
  // show recomputes fresh against current layout.
  function clearDesktopTip() {
    if (desktopAckTip) {
      desktopAckTip.classList.remove('is-visible');
      setTimeout(() => {
        if (desktopAckTip && !desktopAckTip.classList.contains('is-visible')) {
          desktopAckTip.textContent = '';
          desktopAckTip.style.top = '';
          desktopAckTip.style.left = '';
          desktopAckTip.style.right = '';
        }
      }, 250);
    }
    if (desktopAck) desktopAck.textContent = '';
  }

  function announceDesktopAck(text) {
    positionDesktopTip(text);
    if (desktopAck) desktopAck.textContent = text;
    ackTimer = setTimeout(clearDesktopTip, 1500);
  }

  function announceDrawerAck(text) {
    if (!drawerBtn) return;
    const labelEl = drawerBtn.querySelector('.chrome-menu-row-label');
    const original = labelEl ? labelEl.textContent : '';
    if (labelEl) labelEl.textContent = text;
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

  function announceDisabledAck(surface) {
    const text = t('analytics.disabled_ack', 'Sharing turned off');
    if (ackTimer) clearTimeout(ackTimer);
    if (surface === 'desktop') announceDesktopAck(text);
    else if (surface === 'drawer') announceDrawerAck(text);
  }

  // ---- PATCH + state propagation ----
  // Serialize to one in-flight PATCH at a time; queue at most one
  // pending intent. Rapid toggle clicks (or alternating clicks across
  // desktop+drawer surfaces) cannot overlap on the wire -- the second
  // click's intent waits in `queuedNext` until the current one resolves,
  // then drains. This guarantees the FINAL setState() reflects the
  // user's most recent click, even if intermediate requests fail (a
  // "drop stale response" guard alone could leave aria-checked stuck
  // on the pre-flip value when the newest request fails after an
  // older one already succeeded server-side -- "lost update").
  //
  // Trade-off: a queued click's caller gets back `null` (state will
  // land via the broadcast `ephemera:me-updated` event after the
  // queue drains). The original (uncoalesced) caller still gets the
  // FINAL persisted state, so the opt-OUT ack-firing logic stays
  // correct in the common single-click case.
  let inFlight = false;
  let queuedNext = null;
  async function commit(next) {
    if (inFlight) {
      queuedNext = next;
      return null;
    }
    inFlight = true;
    let finalResult = null;
    let toApply = next;
    try {
      while (toApply !== null) {
        try {
          const res = await fetch('/api/me/preferences', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ analytics_opt_in: toApply }),
          });
          if (!res.ok) throw new Error(`patch failed: ${res.status}`);
          const me = await res.json();
          const persisted = Boolean(me.analytics_opt_in);
          setState(persisted);
          window.dispatchEvent(new CustomEvent('ephemera:me-updated', { detail: me }));
          finalResult = persisted;
        } catch {
          // No toast: the switch never animated (state comes from the
          // server response, not optimistically), so the user-
          // perceived state never diverged from the server's. Silent
          // failure is the right shape -- next click can retry.
          finalResult = null;
        }
        if (queuedNext !== null) {
          toApply = queuedNext;
          queuedNext = null;
        } else {
          toApply = null;
        }
      }
    } finally {
      inFlight = false;
    }
    return finalResult;
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
