// Shared two-click confirm pattern (decision #18 in ARCHITECTURE.md).
//
// Four sites use it: the desktop user-pill sign-out, the mobile drawer's
// sign-out row, per-row cancel on the tracked list, and the tracked
// list's clear-history action. They all do the same thing: first click
// adds `.armed` class + swaps the label to a localized "confirm?"
// string + sets a 3-second auto-disarm timeout; second click within the
// window clears the timeout and runs the action. The helper unifies
// that flow so a future fifth site doesn't ship a fifth subtly-different
// implementation.
//
// Usage:
//
//   import { bindTwoClickConfirm } from './two-click.js';
//
//   bindTwoClickConfirm(button, {
//     // Optional. The element whose textContent flips on arm/disarm.
//     // Defaults to the button itself. Use this when the button has an
//     // icon sibling and a separate <span> for the label, so the icon
//     // stays put across state transitions.
//     labelEl: someChildSpan,
//
//     // Optional i18n key for the armed-state label. Default
//     // 'button.confirm' covers the cancel + clear-history sites; the
//     // sign-out sites pass their own scope-specific keys.
//     confirmKey: 'button.sign_out_confirm',
//
//     // Optional aria-label override while armed. The sender pill is
//     // the only site that swaps aria-label; the rest rely on
//     // aria-pressed alone (per ARIA Authoring Practices). When set,
//     // the helper saves the rest-state aria-label and restores it on
//     // disarm.
//     armedAriaLabel: 'Click again to confirm sign out',
//
//     // Optional. e.stopPropagation() before the arm/confirm dispatch.
//     // True for the tracked-list buttons (nested in row click targets);
//     // false (default) for the sign-out triggers.
//     stopPropagation: true,
//
//     // Optional override of the 3-second arm window.
//     armDurationMs: 3000,
//
//     // The action to run on the second click. Helper auto-disarms in
//     // a finally block after onConfirm resolves, so the caller doesn't
//     // need to manually remove the armed class. Set up the
//     // "in-flight" UX (disable button, show "doing…" label, etc.)
//     // inside onConfirm; the helper will swap the label back to its
//     // captured rest-state value once onConfirm returns.
//     onConfirm: async () => { ... },
//   });
//
// The rest-state label is captured at arm time (not init time), so
// dynamic labels like clear-history's count-bearing pluralization
// ("Clear 3 past entries") are handled for free -- the snapshot picks
// up whatever the label was when the user clicked, regardless of
// re-renders that mutated it before then.

export function bindTwoClickConfirm(button, opts = {}) {
  if (!button) return;
  const {
    labelEl = null,
    confirmKey = 'button.confirm',
    armedAriaLabel = null,
    stopPropagation = false,
    armDurationMs = 3000,
    onConfirm,
  } = opts;
  if (typeof onConfirm !== 'function') return;

  const labelTarget = labelEl ?? button;
  // Snapshot only when armedAriaLabel is in play so the helper doesn't
  // capture irrelevant aria state on the bulk of call sites that don't
  // touch aria-label.
  const baseAria = armedAriaLabel != null ? (button.getAttribute('aria-label') ?? '') : null;

  let armTimer = null;
  let idleSnapshot = null;

  function disarm() {
    armTimer = null;
    button.classList.remove('armed');
    if (idleSnapshot != null) {
      labelTarget.textContent = idleSnapshot;
      idleSnapshot = null;
    }
    if (armedAriaLabel != null) button.setAttribute('aria-label', baseAria);
  }

  button.addEventListener('click', async (e) => {
    if (stopPropagation) e.stopPropagation();
    if (!button.classList.contains('armed')) {
      // Capture rest-state label BEFORE flipping to confirm so disarm
      // restores whatever was visible when the user clicked.
      idleSnapshot = labelTarget.textContent;
      button.classList.add('armed');
      labelTarget.textContent = window.i18n.t(confirmKey);
      if (armedAriaLabel != null) button.setAttribute('aria-label', armedAriaLabel);
      armTimer = setTimeout(disarm, armDurationMs);
      return;
    }
    // Second click: cancel the auto-disarm timer, run the action,
    // disarm in finally so a thrown onConfirm still leaves the
    // button in a clean state for the next click.
    if (armTimer != null) {
      clearTimeout(armTimer);
      armTimer = null;
    }
    try {
      await onConfirm();
    } catch (err) {
      // The click handler is async so a rejecting onConfirm leaks an
      // unhandled rejection up to window. Swallow it here -- callers
      // that want failure to surface in the UI should do so inside
      // onConfirm (set an error label, mark the button as failed, etc.).
      // Log to console.error so a regression doesn't go silent in dev.
      console.error('two-click onConfirm failed:', err);
    } finally {
      disarm();
    }
  });
}
