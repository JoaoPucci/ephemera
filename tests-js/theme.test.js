import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// theme.js applies its initial theme on the very first synchronous tick of
// import (before DOMContentLoaded), so each test's setup work -- localStorage
// seed, matchMedia stub -- has to happen BEFORE loadModule(). beforeEach
// clears state; tests stage their starting conditions, then load.
// ---------------------------------------------------------------------------

const KEY = 'ephemera_theme_v1';
const TOGGLE_HTML = '<button id="theme-toggle"></button>';

// Build a matchMedia stub that returns a fixed `matches` value and exposes
// the registered 'change' listener so a test can simulate an OS theme flip.
function stubMatchMedia({ prefersDark = false } = {}) {
  const listeners = [];
  const mql = {
    matches: prefersDark,
    addEventListener: vi.fn((event, fn) => {
      if (event === 'change') listeners.push(fn);
    }),
    removeEventListener: vi.fn(),
  };
  window.matchMedia = vi.fn(() => mql);
  return {
    mql,
    fireSystemChange: (matches) => {
      for (const fn of listeners) fn({ matches });
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.body.innerHTML = '';
  // jsdom doesn't ship matchMedia by default. theme.js's `?.` chains
  // tolerate that, but we want explicit control per-test, so we delete
  // it from prior runs that may have set a stub on the same window.
  delete window.matchMedia;
});

afterEach(() => {
  delete window.matchMedia;
});

// ---------------------------------------------------------------------------
// Initial apply (before any DOM event)
// ---------------------------------------------------------------------------

describe('theme.js — initial apply on import', () => {
  it('falls back to "light" when neither localStorage nor matchMedia provides a hint', async () => {
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('uses the system preference on first visit when matchMedia reports prefers-dark', async () => {
    stubMatchMedia({ prefersDark: true });
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('respects a "light" localStorage choice even when the system prefers dark', async () => {
    // User picked light at some point. System OS later flipped to dark
    // (e.g. moved to a dark-themed laptop). The user's pick is sticky:
    // their choice was explicit and shouldn't be overridden until they
    // reset it.
    stubMatchMedia({ prefersDark: true });
    localStorage.setItem(KEY, 'light');
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('respects a "dark" localStorage choice even when the system prefers light', async () => {
    stubMatchMedia({ prefersDark: false });
    localStorage.setItem(KEY, 'dark');
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('falls through to systemPref when localStorage carries an unrecognised value', async () => {
    // Defensive read: if some other code (or a corrupted browser profile)
    // wrote a non-{light,dark} value, theme.js must not apply it as-is.
    stubMatchMedia({ prefersDark: true });
    localStorage.setItem(KEY, 'purple');
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});

// ---------------------------------------------------------------------------
// Toggle wiring
// ---------------------------------------------------------------------------

describe('theme.js — toggle click', () => {
  it('flips light -> dark and writes the choice to localStorage', async () => {
    stubMatchMedia({ prefersDark: false });
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');
    expect(document.documentElement.dataset.theme).toBe('light');

    document.getElementById('theme-toggle').click();

    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem(KEY)).toBe('dark');
  });

  it('flips dark -> light from a stored "dark" preference', async () => {
    localStorage.setItem(KEY, 'dark');
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    document.getElementById('theme-toggle').click();

    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem(KEY)).toBe('light');
  });

  it('round-trips light -> dark -> light across two clicks', async () => {
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');

    const btn = document.getElementById('theme-toggle');
    btn.click();
    expect(document.documentElement.dataset.theme).toBe('dark');
    btn.click();
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem(KEY)).toBe('light');
  });

  it('bails silently when #theme-toggle is absent (initial apply still ran)', async () => {
    // Theme should still apply at import time; only the click wiring is
    // skipped when the button isn't in the DOM.
    stubMatchMedia({ prefersDark: true });
    // No #theme-toggle in the body.
    document.body.innerHTML = '<div></div>';
    await expect(loadModule('theme')).resolves.toBeDefined();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});

// ---------------------------------------------------------------------------
// System-change follower
// ---------------------------------------------------------------------------

describe('theme.js — system-pref change follower', () => {
  it('applies the new system preference when the user has NOT explicitly picked', async () => {
    const { fireSystemChange } = stubMatchMedia({ prefersDark: false });
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');
    expect(document.documentElement.dataset.theme).toBe('light');

    // OS theme flips to dark; theme.js follows since localStorage is empty.
    fireSystemChange(true);

    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('ignores system changes once the user has explicitly picked a theme', async () => {
    // User clicked the toggle (or had a saved preference). Their choice is
    // sticky -- the OS flipping themes shouldn't override it.
    const { fireSystemChange } = stubMatchMedia({ prefersDark: false });
    localStorage.setItem(KEY, 'light');
    document.body.innerHTML = TOGGLE_HTML;
    await loadModule('theme');
    expect(document.documentElement.dataset.theme).toBe('light');

    fireSystemChange(true);

    // User chose light; OS prefers dark; user wins.
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('does not register a change listener when matchMedia is unavailable', async () => {
    // No matchMedia stub -- simulates an environment without prefers-color-
    // scheme support. theme.js must not throw and must not subscribe.
    document.body.innerHTML = TOGGLE_HTML;
    await expect(loadModule('theme')).resolves.toBeDefined();
    expect(document.documentElement.dataset.theme).toBe('light');
  });
});
