// Char-limit hint binders for the sender form (content textarea, label
// input, passphrase input). Each text input has a slot below it that
// surfaces a counter, a paste-trim error, an "approaching ceiling"
// warning, or stays empty. The state machine + i18n strings live here
// so the form-level wiring in sender/form.js can stay focused on
// orchestration.
//
// Two binders ship:
//
//   bindCounterHint(input, hintEl, max, opts)
//     The full counter UX -- shows usage at 75% of cap, escalates to
//     warning at 95%, freezes (with .is-error class) at the ceiling.
//     Detects oversize pastes (browser truncates silently at maxlength,
//     so we surface that explicitly) and oversized chunks (UTF-8 byte
//     threshold for the textarea's "large paste" warning).
//
//     opts:
//       counterAt           fraction of max to start the counter (default 0.75)
//       warningAt           fraction at which to add .is-warning (default 0.95)
//       pasteLargeThreshold paste byte size that trips the paste-large
//                           warning (Infinity = never; the textarea opts in)
//       useShortTrimMessage true on label field; omits the "(was N)"
//                           parenthetical from the trim message since
//                           short labels make the original size implicit
//       onIntendedSize(sizeChars)
//                           telemetry callback fired on every observed
//                           intended size (post-paste OR per keystroke).
//                           Caller decides what to do with it -- typically
//                           flips a sticky "near cap was crossed" bit.
//
//   bindPassphraseHint(input, hintEl, max, threshold = 0.9)
//     A simpler one-shot warning: "approaching maximum" at 90% of cap,
//     "maximum reached" at the cap. No counter, no error escalation --
//     the 200-char cap is a deliberate input ceiling, not an oversight.

function _formatNumber(n) {
  return n.toLocaleString(window.i18n.currentLocale);
}

function _formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${_formatNumber(Math.round(n / 1024))} KB`;
  return `${_formatNumber(Math.round((n / 1024 / 1024) * 10) / 10)} MB`;
}

function _setHint(hintEl, content, modifier) {
  // modifier: 'warning' | 'error' | null. content === null hides the hint.
  if (content === null) {
    hintEl.hidden = true;
    hintEl.textContent = '';
    hintEl.classList.remove('is-warning', 'is-error');
    return;
  }
  hintEl.hidden = false;
  hintEl.textContent = content;
  hintEl.classList.toggle('is-warning', modifier === 'warning');
  hintEl.classList.toggle('is-error', modifier === 'error');
}

export function bindCounterHint(input, hintEl, max, opts = {}) {
  const counterAt = (opts.counterAt ?? 0.75) * max;
  const warningAt = (opts.warningAt ?? 0.95) * max;
  const pasteLargeThreshold = opts.pasteLargeThreshold ?? Number.POSITIVE_INFINITY;
  const useShortTrim = !!opts.useShortTrimMessage;
  // Static text rendered into the slot from the template (e.g. label's
  // "Up to 60 characters. Shown only to you."). Captured once on init so
  // the idle state can restore it.
  const idleText = hintEl.textContent.trim() || null;

  let pasteOverrideMessage = null;
  let pasteOverrideModifier = null;

  function _showIdle() {
    if (idleText !== null) _setHint(hintEl, idleText, null);
    else _setHint(hintEl, null, null);
  }

  _showIdle();

  input.addEventListener('paste', (e) => {
    const pasted = e.clipboardData?.getData('text') ?? '';
    const selStart = input.selectionStart ?? 0;
    const selEnd = input.selectionEnd ?? 0;
    const currentLen = input.value.length;
    const intendedAfter = currentLen - (selEnd - selStart) + pasted.length;

    if (intendedAfter > max) {
      // Browser will silently truncate at maxlength. Show paste-trim error.
      pasteOverrideMessage = useShortTrim
        ? window.i18n.t('hint.label_trimmed', { max: _formatNumber(max) })
        : window.i18n.t('hint.paste_trimmed', {
            max: _formatNumber(max),
            original: _formatNumber(intendedAfter),
          });
      pasteOverrideModifier = 'error';
      if (opts.onIntendedSize) opts.onIntendedSize(intendedAfter);
    } else if (pasteLargeThreshold !== Number.POSITIVE_INFINITY) {
      // Threshold is UTF-8 bytes ("10KB chunk"); JS .length is UTF-16
      // code units, which diverges 2-4x from byte length for CJK/emoji.
      // Encode for an accurate byte count -- a 4K-character BMP CJK
      // paste is 4K code units but ~12K UTF-8 bytes and should trip
      // this. Skipped for fields that opt out (Infinity sentinel) so
      // we don't allocate a TextEncoder for label/passphrase pastes
      // that never use this branch.
      const pastedBytes = new TextEncoder().encode(pasted).length;
      if (pastedBytes >= pasteLargeThreshold) {
        pasteOverrideMessage = window.i18n.t('hint.content_paste_large', {
          size: _formatBytes(pastedBytes),
        });
        pasteOverrideModifier = 'warning';
        if (opts.onIntendedSize) opts.onIntendedSize(intendedAfter);
      } else {
        pasteOverrideMessage = null;
      }
    } else {
      pasteOverrideMessage = null;
    }
  });

  input.addEventListener('input', (e) => {
    if (pasteOverrideMessage !== null && e.inputType === 'insertFromPaste') {
      _setHint(hintEl, pasteOverrideMessage, pasteOverrideModifier);
      pasteOverrideMessage = null;
      return;
    }
    pasteOverrideMessage = null;

    const len = input.value.length;
    if (opts.onIntendedSize && len > 0) opts.onIntendedSize(len);

    if (len >= max) {
      // Frozen counter at ceiling. The frozen-ness IS the signal.
      _setHint(
        hintEl,
        window.i18n.t('hint.counter', {
          used: _formatNumber(len),
          max: _formatNumber(max),
        }),
        'error'
      );
    } else if (len >= warningAt) {
      _setHint(
        hintEl,
        window.i18n.t('hint.counter', {
          used: _formatNumber(len),
          max: _formatNumber(max),
        }),
        'warning'
      );
    } else if (len >= counterAt) {
      _setHint(
        hintEl,
        window.i18n.t('hint.counter', {
          used: _formatNumber(len),
          max: _formatNumber(max),
        }),
        null
      );
    } else {
      _showIdle();
    }
  });
}

export function bindPassphraseHint(input, hintEl, max, threshold = 0.9) {
  const warnAt = threshold * max;
  input.addEventListener('input', () => {
    const len = input.value.length;
    if (len >= max) {
      _setHint(hintEl, window.i18n.t('hint.max_reached'), 'warning');
    } else if (len >= warnAt) {
      _setHint(hintEl, window.i18n.t('hint.passphrase_approaching'), 'warning');
    } else {
      _setHint(hintEl, null, null);
    }
  });
}
