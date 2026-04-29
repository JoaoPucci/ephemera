import { expect, test } from '@playwright/test';

// CSS cascade regression guard.
//
// PR #101 split the monolithic style.css into six layered stylesheets
// (tokens / base / forms / components / chrome / responsive). The split
// reordered some sections relative to the original source order, which
// changed the cascade outcome on a subtle case Codex caught:
// `responsive.css`'s `body { padding: 3.5rem 0 1.5rem }` shorthand at
// <=480px was clobbering `chrome.css`'s safe-area-aware `padding-top`
// from the `<=720px` block, sliding content under the notch on phones
// with viewport-fit=cover. The fix landed in 2198a0f.
//
// This spec asserts the load-bearing cascade decisions at four
// breakpoints so a future stylesheet edit (rule reorder, new <link>
// in _layout.html, new media query) that breaks the cascade fails
// loudly in CI instead of shipping. Each assertion encodes a property
// + viewport pair that a real product break would change.
//
// We use the /send route -- it renders the login form for an
// unauthenticated visitor, which still extends _layout.html and
// loads all six stylesheets, so no TOTP setup is required for this
// spec. (The auth-required cases are covered by smoke.spec.js.)

const ROUTE = '/send';

async function pixelStyle(page, selector, prop) {
  return await page.evaluate(
    ({ s, p }) => {
      const el = document.querySelector(s);
      if (!el) return null;
      return parseFloat(getComputedStyle(el)[p]);
    },
    { s: selector, p: prop }
  );
}

async function stringStyle(page, selector, prop) {
  return await page.evaluate(
    ({ s, p }) => {
      const el = document.querySelector(s);
      if (!el) return null;
      return getComputedStyle(el)[p];
    },
    { s: selector, p: prop }
  );
}

test.describe('CSS cascade @ desktop (1280x800)', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('card has the components.css 10px border-radius', async ({ page }) => {
    await page.goto(ROUTE);
    expect(await pixelStyle(page, '.card', 'borderRadius')).toBe(10);
  });

  test('top-chrome desktop pills are visible (chrome.css default)', async ({ page }) => {
    await page.goto(ROUTE);
    // .top-chrome's default display is flex (chrome.css). At >720px the
    // 720px-block's `display: none` override does not apply.
    const display = await stringStyle(page, '.top-chrome', 'display');
    expect(display).toBe('flex');
  });

  test('mobile hamburger menu is hidden (chrome.css default)', async ({ page }) => {
    await page.goto(ROUTE);
    // .chrome-menu's default display is none; only the @media(<=720px)
    // block flips it to block.
    const display = await stringStyle(page, '.chrome-menu', 'display');
    expect(display).toBe('none');
  });

  test('body uses base.css default padding (no responsive override at 1280px)', async ({
    page,
  }) => {
    await page.goto(ROUTE);
    // base.css: body { padding: 2.5rem 1.25rem } at 17px base = 42.5 / 21.25.
    const top = await pixelStyle(page, 'body', 'paddingTop');
    const left = await pixelStyle(page, 'body', 'paddingLeft');
    expect(top).toBeCloseTo(42.5, 0);
    expect(left).toBeCloseTo(21.25, 0);
  });
});

test.describe('CSS cascade @ tablet (600x800, in <=720 zone but >480)', () => {
  test.use({ viewport: { width: 600, height: 800 } });

  test('chrome.css 720px block hides top-chrome, shows hamburger', async ({ page }) => {
    await page.goto(ROUTE);
    expect(await stringStyle(page, '.top-chrome', 'display')).toBe('none');
    expect(await stringStyle(page, '.chrome-menu', 'display')).toBe('block');
  });

  test('body padding-top reflects chrome.css safe-area calc', async ({ page }) => {
    await page.goto(ROUTE);
    // chrome.css <=720px: padding-top: calc(3.25rem + max(0.5rem, env(safe-area-inset-top))).
    // On a no-notch viewport, env(...) resolves to 0, so the calc evaluates
    // to 3.25rem + 0.5rem = 3.75rem = 63.75px at the 17px root font-size.
    const top = await pixelStyle(page, 'body', 'paddingTop');
    expect(top).toBeCloseTo(63.75, 0);
  });

  test('card keeps 10px border-radius (responsive.css 480px block does not apply yet)', async ({
    page,
  }) => {
    await page.goto(ROUTE);
    expect(await pixelStyle(page, '.card', 'borderRadius')).toBe(10);
  });
});

test.describe('CSS cascade @ phone (400x800, <=480)', () => {
  test.use({ viewport: { width: 400, height: 800 } });

  test('body padding-top still uses the safe-area calc (Codex P1 regression guard)', async ({
    page,
  }) => {
    // Load-bearing test for the bug fixed in 2198a0f. Pre-fix: responsive.css
    // had `padding: 3.5rem 0 1.5rem` shorthand which overrode chrome.css's
    // safe-area-aware padding-top to a flat 3.5rem (= 59.5px), breaking
    // notched-phone layouts. Post-fix: responsive.css uses longhand
    // padding-inline + padding-bottom only, leaving chrome.css's calc
    // authoritative.
    //
    // 3.5rem flat = 59.5px (the regressed value). The safe-area calc
    // resolves to 63.75px on a no-notch viewport. A floor of 62 catches
    // the regression cleanly: any flat 3.5rem assignment fails this.
    await page.goto(ROUTE);
    const top = await pixelStyle(page, 'body', 'paddingTop');
    expect(top).toBeGreaterThanOrEqual(62);
  });

  test('responsive.css full-bleeds the card (border-radius: 0, padding-inline: 0)', async ({
    page,
  }) => {
    await page.goto(ROUTE);
    expect(await pixelStyle(page, '.card', 'borderRadius')).toBe(0);
    expect(await pixelStyle(page, 'body', 'paddingLeft')).toBe(0);
    expect(await pixelStyle(page, 'body', 'paddingRight')).toBe(0);
  });

  test('responsive.css sets body padding-bottom to 1.5rem (longhand applied)', async ({ page }) => {
    // Sanity check that the longhand split (padding-inline + padding-bottom)
    // is applied -- a typo that drops padding-bottom would let base.css's
    // 2.5rem leak through, which is too generous on a phone.
    await page.goto(ROUTE);
    const bottom = await pixelStyle(page, 'body', 'paddingBottom');
    // 1.5rem at 17px = 25.5px.
    expect(bottom).toBeCloseTo(25.5, 0);
  });

  test('top-chrome stays hidden (cascade still picks up chrome.css 720px override)', async ({
    page,
  }) => {
    // The 720px block's display:none must still win over any responsive.css
    // rule. responsive.css does not target .top-chrome's display, so this
    // is really a check that no future edit accidentally adds one.
    await page.goto(ROUTE);
    expect(await stringStyle(page, '.top-chrome', 'display')).toBe('none');
  });
});

test.describe('CSS cascade @ tiny (350x800, <=360)', () => {
  test.use({ viewport: { width: 350, height: 800 } });

  test('responsive.css 360px block tightens the wordmark font-size', async ({ page }) => {
    // At <=360 the wordmark drops to 0.8rem so it stays clear of the
    // fixed hamburger button on phones. (Pre-tokenization-sweep this
    // describe block also asserted on a #user-name max-width, but
    // .user-btn is display:none below 720px from chrome.css, so its
    // children's responsive rules were dead code and were removed.)
    await page.goto(ROUTE);
    // 0.8rem at 17px root font = 13.6px.
    const fontSize = await pixelStyle(page, '.wordmark', 'fontSize');
    expect(fontSize).toBeCloseTo(13.6, 0);
  });

  test('480px overrides still apply at <=360 (responsive cascade compounds)', async ({ page }) => {
    // The 360px media query is narrower than 480, so both apply at 350px.
    // Verifies that a future split or reorder doesn't accidentally scope
    // the 480px rules so they stop matching at <=360.
    await page.goto(ROUTE);
    expect(await pixelStyle(page, '.card', 'borderRadius')).toBe(0);
  });
});
