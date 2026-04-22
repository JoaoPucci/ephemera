// Shared helper: copy text to clipboard with visible button feedback.
// Label swap to "Copied" (or "Copy failed"), subtle color shift, reverts after ~1.8s.
export async function copyWithFeedback(button, text) {
  if (button.dataset.busy === '1') return;
  button.dataset.busy = '1';
  const original = button.textContent;
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    } else {
      // Fallback: select a hidden textarea and execCommand('copy').
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }
  } catch {
    ok = false;
  }
  button.classList.add(ok ? 'copied' : 'copy-error');
  button.textContent = ok ? window.i18n.t('button.copied') : window.i18n.t('button.copy_failed');
  button.setAttribute('aria-live', 'polite');
  setTimeout(() => {
    button.textContent = original;
    button.classList.remove('copied', 'copy-error');
    delete button.dataset.busy;
  }, 1800);
}
