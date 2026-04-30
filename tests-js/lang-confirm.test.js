// Tests for lang-confirm.js -- the sender-side guard that intercepts
// language-picker changes when the form is dirty (typed content OR
// attached image) and shows a confirm dialog before allowing the page
// reload that setLocale() ultimately triggers.
//
// Two surfaces need the guard:
//   - desktop top-chrome <select id="lang-picker">
//   - mobile drawer <select id="chrome-menu-lang">
// Both call window.i18n.setLocale(lang) on change. lang-confirm.js
// must intercept BEFORE setLocale so cancel can keep the form intact.
//
// Designer brief signed off (PR thread): clickable picker on dirty
// form, in-app dialog (not native confirm()), default focus = Cancel,
// Escape + click-outside = cancel, body copy branches on whether an
// image is attached.

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, loadModule } from './helpers.js';

function mountSenderWithLangConfirm() {
  document.body.innerHTML = `
    <select id="lang-picker">
      <option value="en" selected>English</option>
      <option value="ja">日本語</option>
    </select>
    <span id="chrome-menu-lang-label">English</span>
    <select id="chrome-menu-lang">
      <option value="en" selected>English</option>
      <option value="ja">日本語</option>
    </select>
    <form id="secret-form">
      <textarea id="content" name="content"></textarea>
      <input type="file" id="file">
    </form>
    <section id="result" hidden></section>
    <div id="lang-confirm-dialog" role="dialog" aria-modal="true"
         aria-labelledby="lang-confirm-title" hidden>
      <h2 id="lang-confirm-title" data-i18n="lang_confirm.title"></h2>
      <p id="lang-confirm-body" data-i18n="lang_confirm.body"
         data-i18n-image="lang_confirm.body_with_image"></p>
      <div>
        <button type="button" id="lang-confirm-cancel" data-i18n="lang_confirm.cancel"></button>
        <button type="button" id="lang-confirm-confirm" data-i18n="lang_confirm.confirm"></button>
      </div>
    </div>
  `;
}

// loadModule() installs a fresh window.i18n stub before importing the
// module under test, which would clobber any vi.fn() we tried to put on
// setLocale ahead of time. Call this AFTER loadModule so the spy
// replaces the real stub method on the same window.i18n object the
// module is reading -- since the module reads window.i18n.setLocale at
// call time (not import time), this catches it.
function spyOnSetLocale() {
  return vi.spyOn(window.i18n, 'setLocale').mockImplementation(() => {});
}

function fireChange(selectId, value) {
  const sel = document.getElementById(selectId);
  sel.value = value;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
}

function dialogVisible() {
  const dlg = document.getElementById('lang-confirm-dialog');
  return dlg && !dlg.hidden;
}

describe('lang-confirm.js — fixture absent (no dialog rendered)', () => {
  it('loads cleanly when the dialog markup is missing (e.g. on receiver pages)', async () => {
    document.body.innerHTML = '<div id="not-the-form"></div>';
    await expect(loadModule('lang-confirm')).resolves.toBeDefined();
  });
});

describe('lang-confirm.js — clean form (textarea empty, no file)', () => {
  beforeEach(mountSenderWithLangConfirm);

  // lang-confirm.js itself does NOT call setLocale on a clean form --
  // its job is "don't block when clean, intercept when dirty". The
  // actual setLocale call is fired by chrome-menu.js / i18n.js change
  // handlers downstream. So the assertion here is that
  // (a) no dialog opened, (b) the change event was not stopped (no
  // stopImmediatePropagation), which we observe via the picker still
  // holding the new value rather than reverting to the prior one.

  it('does not intercept desktop picker change on a clean form', async () => {
    await loadModule('lang-confirm');
    fireChange('lang-picker', 'ja');
    await flushAsync();
    expect(dialogVisible()).toBe(false);
    expect(document.getElementById('lang-picker').value).toBe('ja');
  });

  it('does not intercept mobile drawer picker change on a clean form', async () => {
    await loadModule('lang-confirm');
    fireChange('chrome-menu-lang', 'ja');
    await flushAsync();
    expect(dialogVisible()).toBe(false);
    expect(document.getElementById('chrome-menu-lang').value).toBe('ja');
  });
});

describe('lang-confirm.js — dirty form (textarea has content)', () => {
  beforeEach(() => {
    mountSenderWithLangConfirm();
    document.getElementById('content').value = 'a draft the user does not want lost';
  });

  it('opens the confirm dialog instead of calling setLocale', async () => {
    await loadModule('lang-confirm');
    const spy = spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();
    expect(dialogVisible()).toBe(true);
    expect(spy).not.toHaveBeenCalled();
  });

  it('Cancel reverts the picker to the current locale and closes the dialog without setLocale', async () => {
    await loadModule('lang-confirm');
    const spy = spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();

    document.getElementById('lang-confirm-cancel').click();
    await flushAsync();

    expect(dialogVisible()).toBe(false);
    expect(spy).not.toHaveBeenCalled();
    expect(document.getElementById('lang-picker').value).toBe('en');
  });

  it('Confirm proceeds with setLocale and closes the dialog', async () => {
    await loadModule('lang-confirm');
    const spy = spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();

    document.getElementById('lang-confirm-confirm').click();
    await flushAsync();

    expect(dialogVisible()).toBe(false);
    expect(spy).toHaveBeenCalledWith('ja');
  });

  it('Escape key closes the dialog like Cancel (no setLocale, picker reverts)', async () => {
    await loadModule('lang-confirm');
    const spy = spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await flushAsync();

    expect(dialogVisible()).toBe(false);
    expect(spy).not.toHaveBeenCalled();
    expect(document.getElementById('lang-picker').value).toBe('en');
  });

  it('default focus is on Cancel when the dialog opens', async () => {
    await loadModule('lang-confirm');
    spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();
    expect(document.activeElement.id).toBe('lang-confirm-cancel');
  });

  it('Cancel from the drawer also restores #chrome-menu-lang-label so the row stops claiming the previewed language', async () => {
    // Repro Codex P2: chrome-menu.js updates #chrome-menu-lang-label
    // on the drawer select's `input` event during preview. If cancel
    // only resets select.value without also fixing the label, the
    // drawer row keeps showing the previewed (cancelled) language
    // until the next interaction.
    await loadModule('lang-confirm');
    spyOnSetLocale();
    // Simulate the chrome-menu.js input-handler path: user previews
    // the new option in the drawer, label visibly flips to that
    // language's name, THEN the change event fires (which our guard
    // catches because the form is dirty).
    document.getElementById('chrome-menu-lang-label').textContent = '日本語';
    fireChange('chrome-menu-lang', 'ja');
    await flushAsync();
    document.getElementById('lang-confirm-cancel').click();
    await flushAsync();
    expect(document.getElementById('chrome-menu-lang').value).toBe('en');
    expect(document.getElementById('chrome-menu-lang-label').textContent).toBe('English');
  });

  it('Escape stops propagating so chrome-menu.js does not also close the drawer', async () => {
    // Repro Codex P2: without stopPropagation, the document-level
    // Escape handler in chrome-menu.js also fires and closes the
    // drawer as a side effect of cancelling the language switch.
    await loadModule('lang-confirm');
    spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();

    let drawerSawEscape = false;
    document.addEventListener(
      'keydown',
      (e) => {
        if (e.key === 'Escape') drawerSawEscape = true;
      },
      false // bubble phase, like chrome-menu.js
    );

    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    );
    await flushAsync();
    expect(drawerSawEscape).toBe(false);
  });

  it('Tab from the last focusable wraps to the first (focus trap)', async () => {
    await loadModule('lang-confirm');
    spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();

    const cancel = document.getElementById('lang-confirm-cancel');
    const confirmBtn = document.getElementById('lang-confirm-confirm');
    confirmBtn.focus();
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    );
    await flushAsync();
    expect(document.activeElement).toBe(cancel);
  });

  it('Shift+Tab from the first focusable wraps to the last (focus trap)', async () => {
    await loadModule('lang-confirm');
    spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();

    const cancel = document.getElementById('lang-confirm-cancel');
    const confirmBtn = document.getElementById('lang-confirm-confirm');
    cancel.focus();
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Tab',
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      })
    );
    await flushAsync();
    expect(document.activeElement).toBe(confirmBtn);
  });
});

describe('lang-confirm.js — dirty form (image attached, no text)', () => {
  beforeEach(() => {
    mountSenderWithLangConfirm();
    // jsdom doesn't let you assign FileList directly; fake the .files
    // property with a one-item-array stub. The guard only checks
    // .files.length, not the file content.
    Object.defineProperty(document.getElementById('file'), 'files', {
      configurable: true,
      get: () => [{ name: 'test.png' }],
    });
  });

  it('opens the dialog with the image-aware body copy', async () => {
    await loadModule('lang-confirm');
    spyOnSetLocale();
    fireChange('lang-picker', 'ja');
    await flushAsync();
    expect(dialogVisible()).toBe(true);
    const body = document.getElementById('lang-confirm-body');
    // The image-variant key must be the one swapped in -- the JS toggles
    // either data-i18n or the body text directly to the with-image
    // string when a file is attached.
    expect(body.textContent.toLowerCase()).toContain('image');
  });
});

describe('lang-confirm.js — result panel showing (post-create, not dirty)', () => {
  beforeEach(() => {
    mountSenderWithLangConfirm();
    // Form has stale content, but the result panel is visible -- the
    // sender-side state machine has moved on past "compose". Switching
    // language here is fine: the URL is server-side, the form is no
    // longer the active surface.
    document.getElementById('content').value = 'old draft content lingering in DOM';
    document.getElementById('result').hidden = false;
  });

  it('does not intercept the picker change (result-panel visible = treated as not-dirty)', async () => {
    await loadModule('lang-confirm');
    fireChange('lang-picker', 'ja');
    await flushAsync();
    expect(dialogVisible()).toBe(false);
    expect(document.getElementById('lang-picker').value).toBe('ja');
  });
});
