// Property-based tests for app/static pure-function shapes.
//
// Mirror of tests/test_property.py-shaped runs on the Python side
// (PR #110 / #119): for the small set of frontend functions whose
// invariants are expressible as universal claims (round-trip,
// idempotence, "value preserved when this transformation runs"),
// fast-check generates inputs across the input space rather than the
// fixed examples a unit test pins. Catches edge cases the unit tests
// don't enumerate -- empty strings, unicode, large dictionaries,
// boundary lengths -- that have historically been a source of
// frontend regressions in the Python suite.
//
// Two surfaces under test today:
//   - sender/url-cache.js: cacheUrl / forgetUrl / getUrl / gcUrls
//     are stateful but tiny; their invariants are easy to express
//     ("after cacheUrl(id, url), getUrl(id) === url"; "gc keeps the
//     known set, drops the unknown").
//   - i18n.t() interpolation: `{{var}}` substitution is the
//     historically tricky bit (unknown placeholders must survive,
//     unrelated vars must not affect the output, dotted-key lookup
//     must traverse correctly).
//
// Both modules go through the loadModule helper in helpers.js so we
// exercise the production code rather than a re-implementation.

import * as fc from 'fast-check';
import { beforeEach, describe, expect, it } from 'vitest';
import { mountI18n } from './fixtures/i18n.js';
import { loadModule } from './helpers.js';

// fast-check's default 100 runs is overkill for these small surfaces
// and blows test runtime. 50 catches the same classes of bugs in
// half the time -- the existing Python property suite uses similar
// run counts (PR #119's bcrypt round-trip uses 200 because hashing
// is the bottleneck; here every iteration is microseconds).
const PROP_RUNS = 50;

// Arbitrary URL-cache ids. The id input space here is the full
// non-empty string range -- url-cache.js now uses `Object.hasOwn`
// for ownership checks (instead of the truthy-coalesce that walked
// the prototype chain), so prototype-name ids like `"toString"` no
// longer leak the inherited function reference back through getUrl.
const cacheIdArb = fc.string({ minLength: 1, maxLength: 50 });

// ---------------------------------------------------------------------------
// sender/url-cache.js
// ---------------------------------------------------------------------------

describe('property: sender/url-cache.js', () => {
  let cacheUrl;
  let forgetUrl;
  let getUrl;
  let gcUrls;

  beforeEach(async () => {
    localStorage.clear();
    const mod = await loadModule('sender/url-cache');
    cacheUrl = mod.cacheUrl;
    forgetUrl = mod.forgetUrl;
    getUrl = mod.getUrl;
    gcUrls = mod.gcUrls;
  });

  it('cache then get round-trips any non-empty id/url pair', () => {
    // The `|| null` in getUrl() means empty-string URLs come back as
    // null, which is a documented quirk of the falsy-coalesce. Filter
    // empty strings here so the round-trip equality holds.
    fc.assert(
      fc.property(cacheIdArb, fc.string({ minLength: 1, maxLength: 500 }), (id, url) => {
        localStorage.clear();
        cacheUrl(id, url);
        expect(getUrl(id)).toBe(url);
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('cache then forget then get returns null', () => {
    fc.assert(
      fc.property(cacheIdArb, fc.string({ minLength: 1, maxLength: 500 }), (id, url) => {
        localStorage.clear();
        cacheUrl(id, url);
        forgetUrl(id);
        expect(getUrl(id)).toBeNull();
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('cache twice with different urls keeps the latest', () => {
    fc.assert(
      fc.property(
        cacheIdArb,
        fc.string({ minLength: 1, maxLength: 500 }),
        fc.string({ minLength: 1, maxLength: 500 }),
        (id, url1, url2) => {
          localStorage.clear();
          cacheUrl(id, url1);
          cacheUrl(id, url2);
          expect(getUrl(id)).toBe(url2);
        }
      ),
      { numRuns: PROP_RUNS }
    );
  });

  it('gc keeps every known id and drops every unknown one', () => {
    // Generate two disjoint sets of ids, cache values for both, then
    // run gc with only the "known" set. The known ids must keep
    // their cached value; the unknown ids must come back as null.
    fc.assert(
      fc.property(
        fc.uniqueArray(cacheIdArb, { minLength: 1, maxLength: 8 }),
        fc.uniqueArray(cacheIdArb, { minLength: 1, maxLength: 8 }),
        fc.string({ minLength: 1, maxLength: 100 }),
        (knownIds, unknownIds, url) => {
          // Make sure the two sets are actually disjoint -- shrunk
          // counterexamples can collide on identical ids otherwise.
          const knownSet = new Set(knownIds);
          const trulyUnknown = unknownIds.filter((id) => !knownSet.has(id));
          localStorage.clear();
          for (const id of knownIds) cacheUrl(id, url);
          for (const id of trulyUnknown) cacheUrl(id, url);
          gcUrls(knownIds);
          for (const id of knownIds) expect(getUrl(id)).toBe(url);
          for (const id of trulyUnknown) expect(getUrl(id)).toBeNull();
        }
      ),
      { numRuns: PROP_RUNS }
    );
  });

  it('gc on a fresh cache is a no-op (no entries to drop)', () => {
    fc.assert(
      fc.property(fc.array(cacheIdArb, { maxLength: 10 }), (anyIds) => {
        localStorage.clear();
        gcUrls(anyIds);
        for (const id of anyIds) expect(getUrl(id)).toBeNull();
      }),
      { numRuns: PROP_RUNS }
    );
  });
});

// ---------------------------------------------------------------------------
// i18n.t() interpolation
// ---------------------------------------------------------------------------

describe('property: i18n.t() interpolation', () => {
  // Mount once with a known catalog so each property iteration calls
  // a stable t() against a stable string. The catalog includes a
  // dotted key (`error.network`) and a string with two placeholders
  // so the property can drive variable substitution from both sides.
  beforeEach(async () => {
    mountI18n({
      catalog: {
        plain: 'no placeholders here',
        greeting: 'hello {{name}}, you have {{count}} messages',
        error: { network: 'network error' },
      },
    });
    await loadModule('i18n');
  });

  it('substitutes every {{var}} that has a corresponding entry in vars', () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 0, maxLength: 30 }),
        fc.integer({ min: -1_000_000, max: 1_000_000 }),
        (name, count) => {
          const result = window.i18n.t('greeting', { name, count });
          expect(result).toBe(`hello ${name}, you have ${count} messages`);
        }
      ),
      { numRuns: PROP_RUNS }
    );
  });

  it('leaves placeholders intact when the var is not provided', () => {
    // Pass a dictionary that intentionally doesn't include `name` or
    // `count`; both placeholders must come through unchanged. The
    // filter on the dict generator avoids accidental shadowing.
    fc.assert(
      fc.property(
        fc.dictionary(fc.string(), fc.string()).filter((d) => !('name' in d) && !('count' in d)),
        (extras) => {
          const result = window.i18n.t('greeting', extras);
          expect(result).toBe('hello {{name}}, you have {{count}} messages');
        }
      ),
      { numRuns: PROP_RUNS }
    );
  });

  it('returns the literal string for entries with no placeholders', () => {
    // Any vars dict (or none) must leave a placeholder-free string
    // unchanged; the interpolator must not invent placeholders.
    fc.assert(
      fc.property(fc.dictionary(fc.string(), fc.anything()), (vars) => {
        expect(window.i18n.t('plain', vars)).toBe('no placeholders here');
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('returns the key itself as a visible sentinel for unknown keys', () => {
    // The shim documents this behavior at app/static/i18n.js:60. Any
    // key not present in the catalog (and not in the fallback, which
    // is empty here) must come back as the key string itself, with
    // any vars ignored.
    fc.assert(
      fc.property(
        fc
          .string({ minLength: 1, maxLength: 50 })
          .filter((k) => !['plain', 'greeting', 'error.network'].includes(k)),
        fc.dictionary(fc.string(), fc.anything()),
        (key, vars) => {
          expect(window.i18n.t(key, vars)).toBe(key);
        }
      ),
      { numRuns: PROP_RUNS }
    );
  });

  it('walks dotted keys to find nested catalog entries', () => {
    // The dotted-key lookup is a separate code path (lookup() in
    // i18n.js); pin it as a property so a future restructure doesn't
    // silently break the depth-2 traversal. Vars on a placeholder-
    // free string still come through as the literal value.
    fc.assert(
      fc.property(fc.dictionary(fc.string(), fc.anything()), (vars) => {
        expect(window.i18n.t('error.network', vars)).toBe('network error');
      }),
      { numRuns: PROP_RUNS }
    );
  });
});

// ---------------------------------------------------------------------------
// sender/hints.js -- threshold transitions
// ---------------------------------------------------------------------------

describe('property: sender/hints.js threshold transitions', () => {
  // bindCounterHint has four bands keyed off `input.value.length`:
  //   len < counterAt*max         -- idle (hidden, OR shows the static
  //                                  idleText captured at init time)
  //   counterAt*max <= len < warningAt*max
  //                                -- counter visible, no modifier
  //   warningAt*max <= len < max  -- counter visible with .is-warning
  //   len >= max                  -- counter frozen with .is-error
  //
  // Properties test the boundary semantics across the full input
  // length range. Because bindCounterHint installs DOM listeners on
  // first call, each property mounts a fresh DOM + reload and drives
  // the binder via dispatched `input` events.

  const MAX = 100;
  const COUNTER_AT = Math.floor(0.75 * MAX);
  const WARNING_AT = Math.floor(0.95 * MAX);
  let bindCounterHint;
  let bindPassphraseHint;

  beforeEach(async () => {
    document.body.innerHTML = `
      <textarea id="content" maxlength="${MAX}"></textarea>
      <p id="content-hint" hidden></p>
      <input id="passphrase" maxlength="200" />
      <p id="passphrase-hint" hidden></p>
    `;
    const mod = await loadModule('sender/hints');
    bindCounterHint = mod.bindCounterHint;
    bindPassphraseHint = mod.bindPassphraseHint;
  });

  function setLenAndFire(input, len) {
    input.value = 'x'.repeat(len);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
  }

  it('counter hint stays idle when length is under counterAt', () => {
    const input = document.getElementById('content');
    const hint = document.getElementById('content-hint');
    bindCounterHint(input, hint, MAX);
    fc.assert(
      fc.property(fc.integer({ min: 0, max: COUNTER_AT - 1 }), (len) => {
        setLenAndFire(input, len);
        // No modifier classes; the hint is either hidden (no idle text
        // captured) or showing the idle text. The textarea here has no
        // pre-existing static text, so it stays hidden.
        expect(hint.classList.contains('is-warning')).toBe(false);
        expect(hint.classList.contains('is-error')).toBe(false);
        expect(hint.hidden).toBe(true);
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('counter hint shows counter (no modifier) in [counterAt, warningAt)', () => {
    const input = document.getElementById('content');
    const hint = document.getElementById('content-hint');
    bindCounterHint(input, hint, MAX);
    fc.assert(
      fc.property(fc.integer({ min: COUNTER_AT, max: WARNING_AT - 1 }), (len) => {
        setLenAndFire(input, len);
        expect(hint.hidden).toBe(false);
        expect(hint.classList.contains('is-warning')).toBe(false);
        expect(hint.classList.contains('is-error')).toBe(false);
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('counter hint adds is-warning in [warningAt, max)', () => {
    const input = document.getElementById('content');
    const hint = document.getElementById('content-hint');
    bindCounterHint(input, hint, MAX);
    fc.assert(
      fc.property(fc.integer({ min: WARNING_AT, max: MAX - 1 }), (len) => {
        setLenAndFire(input, len);
        expect(hint.hidden).toBe(false);
        expect(hint.classList.contains('is-warning')).toBe(true);
        expect(hint.classList.contains('is-error')).toBe(false);
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('counter hint flips to is-error at the ceiling and stays there', () => {
    const input = document.getElementById('content');
    const hint = document.getElementById('content-hint');
    bindCounterHint(input, hint, MAX);
    // The browser-side maxlength would truncate input above MAX, but
    // the binder's logic treats `len >= max` as the frozen-error band
    // regardless of truncation -- so we only test up to MAX itself.
    fc.assert(
      fc.property(fc.integer({ min: MAX, max: MAX + 50 }), (len) => {
        // Bypass maxlength by setting value directly (jsdom doesn't
        // enforce it; production would, but the binder reacts to the
        // value the input actually carries at the time of the input
        // event).
        input.value = 'x'.repeat(len);
        input.dispatchEvent(new InputEvent('input', { bubbles: true }));
        expect(hint.hidden).toBe(false);
        expect(hint.classList.contains('is-error')).toBe(true);
        expect(hint.classList.contains('is-warning')).toBe(false);
      }),
      { numRuns: PROP_RUNS }
    );
  });

  it('passphrase hint stays hidden below threshold and warns at/above', () => {
    // bindPassphraseHint(input, hintEl, max, threshold = 0.9). One
    // band: hidden below threshold*max, .is-warning at-or-above.
    // Verify monotonicity: any len < threshold-cap is hidden; any
    // len >= threshold-cap is visible with the warning class.
    const PMAX = 200;
    const PTHRESHOLD = Math.floor(0.9 * PMAX);
    const input = document.getElementById('passphrase');
    const hint = document.getElementById('passphrase-hint');
    bindPassphraseHint(input, hint, PMAX);
    fc.assert(
      fc.property(fc.integer({ min: 0, max: PMAX + 20 }), (len) => {
        input.value = 'x'.repeat(len);
        input.dispatchEvent(new InputEvent('input', { bubbles: true }));
        if (len >= PTHRESHOLD) {
          expect(hint.hidden).toBe(false);
          expect(hint.classList.contains('is-warning')).toBe(true);
        } else {
          expect(hint.hidden).toBe(true);
        }
      }),
      { numRuns: PROP_RUNS }
    );
  });
});
