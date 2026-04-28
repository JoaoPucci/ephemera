import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// bindTwoClickConfirm is the helper four sites use (sender pill sign-out,
// chrome-menu drawer sign-out, tracked-list per-row cancel, tracked-list
// clear-history). Each site already has its own behavioural test suite
// asserting on the observable arm/confirm/disarm shape; this file pins
// the helper's own contract and the variations the API surface exposes.
// ---------------------------------------------------------------------------

function mountButton({ withInnerLabel = false, ariaLabel = null } = {}) {
  const aria = ariaLabel != null ? ` aria-label="${ariaLabel}"` : '';
  const inner = withInnerLabel ? '<span class="label">cancel</span>' : 'cancel';
  document.body.innerHTML = `<button type="button" id="btn"${aria}>${inner}</button>`;
  const button = document.getElementById('btn');
  return {
    button,
    label: withInnerLabel ? button.querySelector('.label') : button,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('two-click.js — arm / confirm flow on the button itself', () => {
  it('first click adds .armed and swaps the label to the localized confirm string', async () => {
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');
    const onConfirm = vi.fn();

    bindTwoClickConfirm(button, { onConfirm });
    button.click();

    expect(button.classList.contains('armed')).toBe(true);
    // 'button.confirm' resolves through the real en.json catalog (loaded
    // by the i18n stub helpers.installI18nStub uses) to "confirm?".
    expect(button.textContent).toBe('confirm?');
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('second click runs onConfirm and disarms automatically', async () => {
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');
    const onConfirm = vi.fn().mockResolvedValue(undefined);

    bindTwoClickConfirm(button, { onConfirm });
    button.click();
    button.click();
    // Drain the post-onConfirm `finally { disarm() }` microtask.
    await Promise.resolve();
    await Promise.resolve();

    expect(onConfirm).toHaveBeenCalledOnce();
    expect(button.classList.contains('armed')).toBe(false);
    expect(button.textContent).toBe('cancel');
  });

  it('honours a custom confirmKey', async () => {
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, {
      confirmKey: 'button.sign_out_confirm',
      onConfirm: vi.fn(),
    });
    button.click();

    // 'button.sign_out_confirm' -> "really sign out?" in en.json.
    expect(button.textContent).toBe('really sign out?');
  });
});

describe('two-click.js — labelEl swap (icon-preserving variant)', () => {
  it('flips only the inner label span and leaves the rest of the button alone', async () => {
    // Mirrors the tracked-list clear-history shape: button contains an
    // SVG icon + a separate label span; only the label flips so the
    // icon stays put.
    document.body.innerHTML = `
      <button type="button" id="btn">
        <svg id="icon"></svg>
        <span id="label">Clear 3 past entries</span>
      </button>
    `;
    const button = document.getElementById('btn');
    const labelEl = document.getElementById('label');
    const icon = document.getElementById('icon');
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, { labelEl, onConfirm: vi.fn() });
    button.click();

    expect(labelEl.textContent).toBe('confirm?');
    // Icon is still in place -- the button's other children weren't touched.
    expect(button.contains(icon)).toBe(true);
  });
});

describe('two-click.js — auto-disarm timer', () => {
  it('removes .armed and restores the label after armDurationMs', async () => {
    vi.useFakeTimers();
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');
    const onConfirm = vi.fn();

    bindTwoClickConfirm(button, { onConfirm });
    button.click();
    expect(button.classList.contains('armed')).toBe(true);

    vi.advanceTimersByTime(3001);

    expect(button.classList.contains('armed')).toBe(false);
    expect(button.textContent).toBe('cancel');
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('respects an armDurationMs override', async () => {
    vi.useFakeTimers();
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, { armDurationMs: 500, onConfirm: vi.fn() });
    button.click();
    expect(button.classList.contains('armed')).toBe(true);

    // Hasn't fired yet at 400ms.
    vi.advanceTimersByTime(400);
    expect(button.classList.contains('armed')).toBe(true);

    // Fires at 501ms.
    vi.advanceTimersByTime(101);
    expect(button.classList.contains('armed')).toBe(false);
  });

  it('clears the auto-disarm timer when the second click lands first', async () => {
    vi.useFakeTimers();
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');
    const onConfirm = vi.fn().mockResolvedValue(undefined);

    bindTwoClickConfirm(button, { onConfirm });
    button.click(); // arm
    button.click(); // confirm before timer

    // Drain the awaited disarm.
    await Promise.resolve();
    await Promise.resolve();

    // Advancing the clock past 3s should NOT re-trigger anything --
    // the timer was cleared when the second click ran.
    vi.advanceTimersByTime(5000);

    expect(onConfirm).toHaveBeenCalledOnce();
    expect(button.classList.contains('armed')).toBe(false);
  });
});

describe('two-click.js — armedAriaLabel option', () => {
  it('swaps aria-label while armed and restores on disarm', async () => {
    const { button } = mountButton({ ariaLabel: 'sign out' });
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, {
      armedAriaLabel: 'Click again to confirm sign out',
      onConfirm: vi.fn().mockResolvedValue(undefined),
    });

    button.click();
    expect(button.getAttribute('aria-label')).toBe('Click again to confirm sign out');

    button.click();
    await Promise.resolve();
    await Promise.resolve();
    expect(button.getAttribute('aria-label')).toBe('sign out');
  });

  it('leaves aria-label untouched when armedAriaLabel is not passed', async () => {
    const { button } = mountButton({ ariaLabel: 'cancel this' });
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, { onConfirm: vi.fn() });
    button.click();

    expect(button.getAttribute('aria-label')).toBe('cancel this');
  });
});

describe('two-click.js — stopPropagation', () => {
  it('calls e.stopPropagation when the option is set', async () => {
    document.body.innerHTML = '<div id="parent"><button id="btn">cancel</button></div>';
    const button = document.getElementById('btn');
    const parentClick = vi.fn();
    document.getElementById('parent').addEventListener('click', parentClick);
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, { stopPropagation: true, onConfirm: vi.fn() });
    button.click();

    expect(parentClick).not.toHaveBeenCalled();
  });

  it('lets clicks bubble when stopPropagation is omitted', async () => {
    document.body.innerHTML = '<div id="parent"><button id="btn">cancel</button></div>';
    const button = document.getElementById('btn');
    const parentClick = vi.fn();
    document.getElementById('parent').addEventListener('click', parentClick);
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, { onConfirm: vi.fn() });
    button.click();

    expect(parentClick).toHaveBeenCalledOnce();
  });
});

describe('two-click.js — captures rest-state label at arm time, not init time', () => {
  it('restores whatever label was visible when the user clicked, not the init-time label', async () => {
    // Regression guard for the clear-history dynamic-count case: the
    // label changes between renders ("Clear 1 past entry" -> "Clear 3
    // past entries"), and disarm has to restore whatever the LAST
    // rendered value was, not the value at bind time.
    vi.useFakeTimers();
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');

    bindTwoClickConfirm(button, { onConfirm: vi.fn() });

    // Re-render mutates the label after init.
    button.textContent = 'Clear 3 past entries';

    button.click();
    expect(button.textContent).toBe('confirm?');

    vi.advanceTimersByTime(3001);
    expect(button.textContent).toBe('Clear 3 past entries');
  });
});

describe('two-click.js — defensive guards', () => {
  it('does nothing when button is null', async () => {
    const { bindTwoClickConfirm } = await loadModule('two-click');
    expect(() => bindTwoClickConfirm(null, { onConfirm: vi.fn() })).not.toThrow();
  });

  it('does nothing when onConfirm is missing or not a function', async () => {
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');

    // No-op: clicking should NOT add 'armed' since no handler was wired.
    bindTwoClickConfirm(button, {});
    button.click();
    expect(button.classList.contains('armed')).toBe(false);
  });

  it('disarms and logs to console.error when onConfirm rejects (no unhandled rejection)', async () => {
    // The click handler is async, so a rejecting onConfirm would leak as
    // an unhandled rejection on window if the helper didn't catch. The
    // helper swallows + logs via console.error so dev still sees the
    // failure without UX flickering or the page-level error handler firing.
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { button } = mountButton();
    const { bindTwoClickConfirm } = await loadModule('two-click');
    const onConfirm = vi.fn().mockRejectedValue(new Error('boom'));

    bindTwoClickConfirm(button, { onConfirm });
    button.click(); // arm
    button.click(); // confirm -> rejects

    // Drain microtasks to let the finally-block disarm run.
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(button.classList.contains('armed')).toBe(false);
    expect(button.textContent).toBe('cancel');
    expect(errSpy).toHaveBeenCalledWith('two-click onConfirm failed:', expect.any(Error));
    errSpy.mockRestore();
  });
});
