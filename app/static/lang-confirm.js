// Language-switch confirm dialog -- sender-side guard against losing
// typed content / attached image when the picker triggers a reload.
//
// Loaded only on the sender (the only surface with "dirty form" state
// to lose) via {% if chrome_variant == "sender" %} in _layout.html.
// On other surfaces this file isn't fetched at all.
//
// Behaviour:
//   - When the form is clean, picker change passes straight through to
//     window.i18n.setLocale (no dialog).
//   - When dirty (textarea has content OR file is attached, AND the
//     result panel is hidden), picker change opens the dialog and
//     prevents the change from propagating to other listeners. Cancel
//     reverts the select to the current locale; Confirm calls
//     setLocale to commit the switch (which then reloads the page).
//
// The change-event interception runs in the CAPTURE phase so this
// listener fires BEFORE chrome-menu.js / i18n.js own change handlers
// (which do the setLocale call). On Cancel we just don't propagate;
// on Confirm we call setLocale ourselves.

(() => {
  const dialog = document.getElementById('lang-confirm-dialog');
  if (!dialog) return; // sender-only guard; no-op everywhere else

  const body = document.getElementById('lang-confirm-body');
  const cancelBtn = document.getElementById('lang-confirm-cancel');
  const confirmBtn = document.getElementById('lang-confirm-confirm');

  const desktopPicker = document.getElementById('lang-picker');
  const drawerPicker = document.getElementById('chrome-menu-lang');
  // Visible label inside the drawer language row that chrome-menu.js
  // updates on the select's input event. We have to keep this in sync
  // on cancel; otherwise the row visually claims the previewed (but
  // cancelled) language, even though the active locale didn't change.
  const drawerPickerLabel = document.getElementById('chrome-menu-lang-label');
  const form = document.getElementById('secret-form');
  const contentInput = document.getElementById('content');
  const fileInput = document.getElementById('file');
  const result = document.getElementById('result');

  // Snapshot of which select fired the change, and what the locale was
  // before the user touched the picker -- used to revert on Cancel.
  let pendingTarget = null;
  let priorValue = '';
  let pendingLang = '';
  let lastFocusedBeforeOpen = null;

  function isFormDirty() {
    // Result panel showing means the user has already created the
    // secret -- form content is stale, switching language is fine.
    if (result && !result.hidden) return false;
    if (!form) return false;
    const hasText = !!(contentInput && contentInput.value.length > 0);
    const hasFile = !!(fileInput?.files && fileInput.files.length > 0);
    return hasText || hasFile;
  }

  function updateBodyText() {
    if (!body) return;
    // The dialog renders both body keys via data-i18n / data-i18n-image;
    // the i18n shim populates whichever is the active text on init.
    // For the image variant we re-resolve at open time.
    const hasFile = !!(fileInput?.files && fileInput.files.length > 0);
    const key = hasFile
      ? body.getAttribute('data-i18n-image') || body.getAttribute('data-i18n')
      : body.getAttribute('data-i18n');
    if (key && window.i18n && typeof window.i18n.t === 'function') {
      body.textContent = window.i18n.t(key);
    }
  }

  function openDialog(targetSelect, oldValue, newValue) {
    pendingTarget = targetSelect;
    priorValue = oldValue;
    pendingLang = newValue;
    updateBodyText();
    dialog.hidden = false;
    lastFocusedBeforeOpen = document.activeElement;
    // Default focus to Cancel -- destructive action does NOT get
    // default focus, per WCAG 3.3.4 (Error Prevention) and the
    // designer brief.
    if (cancelBtn) cancelBtn.focus();
  }

  function closeDialog() {
    dialog.hidden = true;
    pendingTarget = null;
    priorValue = '';
    pendingLang = '';
    if (lastFocusedBeforeOpen && typeof lastFocusedBeforeOpen.focus === 'function') {
      lastFocusedBeforeOpen.focus();
    }
    lastFocusedBeforeOpen = null;
  }

  function cancel() {
    // Revert the select that fired the change so the dropdown does not
    // visually lie about the active locale.
    if (pendingTarget) pendingTarget.value = priorValue;
    // Also restore the drawer's visible language label. chrome-menu.js
    // updates this label on the select's `input` event (before the
    // change event fires), so by the time we get here on the drawer
    // path the label may already show the previewed-but-cancelled
    // language. Read the option text for the reverted value to
    // reconstruct the right display string.
    if (pendingTarget === drawerPicker && drawerPickerLabel) {
      const restoredOpt = drawerPicker.options[drawerPicker.selectedIndex];
      if (restoredOpt) drawerPickerLabel.textContent = restoredOpt.textContent;
    }
    closeDialog();
  }

  function confirm() {
    const lang = pendingLang;
    closeDialog();
    if (lang && window.i18n && typeof window.i18n.setLocale === 'function') {
      window.i18n.setLocale(lang);
    }
  }

  // Capture-phase listener so we intercept BEFORE chrome-menu.js /
  // i18n.js handlers see the event. stopImmediatePropagation prevents
  // their setLocale call.
  function onPickerChange(e) {
    const target = e.target;
    if (target !== desktopPicker && target !== drawerPicker) return;
    const newValue = target.value;
    // Determine the prior locale by reading the OPTION marked
    // `selected` in markup -- the user just changed the value, so
    // the old `selected` attribute still points at the prior locale.
    const selectedOpt = target.querySelector('option[selected]');
    const priorLocale = selectedOpt ? selectedOpt.value : newValue;
    if (newValue === priorLocale) return; // no-op
    if (!isFormDirty()) return; // pass through to native handlers
    e.stopImmediatePropagation();
    e.preventDefault();
    openDialog(target, priorLocale, newValue);
  }

  if (desktopPicker) {
    desktopPicker.addEventListener('change', onPickerChange, true);
  }
  if (drawerPicker) {
    drawerPicker.addEventListener('change', onPickerChange, true);
  }

  if (cancelBtn) cancelBtn.addEventListener('click', cancel);
  if (confirmBtn) confirmBtn.addEventListener('click', confirm);

  // Returns the focusable elements inside the dialog panel, in tab
  // order. Used by both the focus trap and to decide where Tab should
  // wrap to. Recomputed on each call because the dialog could in
  // principle gain or lose interactive children (none today, but the
  // contract is "trap whatever is in the panel right now").
  function dialogFocusables() {
    if (!dialog) return [];
    return Array.from(
      dialog.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
    ).filter((el) => !el.hasAttribute('disabled'));
  }

  // Focus trap: Tab from the last focusable wraps to the first;
  // Shift+Tab from the first wraps to the last. With aria-modal set
  // to true on the dialog and this trap in place, AT users can't
  // navigate to controls behind the dialog.
  function handleDialogTab(e) {
    const focusables = dialogFocusables();
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // Capture-phase keydown listener so we see Escape and Tab BEFORE
  // chrome-menu.js's own document-level Escape handler can close the
  // drawer as a side effect of cancelling the language switch.
  // stopPropagation prevents that handler from firing at all while
  // the dialog is open.
  document.addEventListener(
    'keydown',
    (e) => {
      if (dialog.hidden) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        cancel();
        return;
      }
      if (e.key === 'Tab') handleDialogTab(e);
    },
    true
  );

  // Click on the scrim (the dialog element itself, outside the panel)
  // closes like Cancel. Clicks inside the panel don't bubble to here
  // because they hit child elements first.
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) cancel();
  });
})();
