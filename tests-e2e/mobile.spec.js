// Mobile-viewport variant of the smoke test. Runs the same sign-in →
// create → reveal golden path under an iPhone 13 device profile so
// regressions that surface only at narrow widths (touch hit-targets
// covered by a fixed header, textareas that don't expand, CSS rules
// that fire at <768px and break the compose form) get caught before
// release.
//
// The chrome-menu drawer is a mobile-specific UI element (a
// hamburger-revealed sheet, replacing the desktop top-right cluster
// of controls); this spec exercises the bare flow without opening
// it. A future spec can extend this one to drive the menu's
// language switcher / sign-out / analytics-toggle interactions if
// those need their own mobile-layout coverage.
import { expect, test } from '@playwright/test';
import { generateSync } from 'otplib';

const USERNAME = 'e2e-mobile';
const PASSWORD = 'e2e-password-123';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';

// iPhone 13 viewport size + mobile / touch flags, applied on Chromium
// (the rest of the suite's browser too). Skipping `devices['iPhone 13']`
// in full because that profile pins `defaultBrowserType: 'webkit'`,
// which CI doesn't install -- viewport-only override on Chromium
// catches the same narrow-width regressions without dragging in a
// second browser binary.
const MOBILE = {
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 3,
  isMobile: true,
  hasTouch: true,
};

test.use(MOBILE);

test.beforeEach(async ({ request }) => {
  await request.post('/_test/limiter/reset', {
    headers: { Origin: 'http://127.0.0.1:8765' },
  });
});

test('golden path on iPhone viewport: sign in, create a text secret, reveal it', async ({
  browser,
}) => {
  const sender = await browser.newContext(MOBILE);
  const senderPage = await sender.newPage();

  await senderPage.goto('/send');
  await senderPage.fill('#username', USERNAME);
  await senderPage.fill('#password', PASSWORD);
  await senderPage.fill('#code', generateSync({ secret: TOTP_SECRET, strategy: 'totp' }));
  await senderPage.click('#login-form button[type="submit"]');

  await expect(senderPage.locator('#content')).toBeVisible();

  const plaintext = `e2e-mobile ${Date.now()}`;
  await senderPage.fill('#content', plaintext);
  await senderPage.click('#submit-btn');

  await expect(senderPage.locator('#result-url')).toBeVisible();
  const shareUrl = ((await senderPage.locator('#result-url').textContent()) ?? '').trim();
  expect(shareUrl).toMatch(/#[A-Za-z0-9_-]+$/);

  // Receiver also runs in a phone-sized viewport. The reveal flow's
  // mobile layout is the realistic scenario -- a recipient clicking a
  // share link from chat is most likely on a phone.
  const receiver = await browser.newContext(MOBILE);
  const receiverPage = await receiver.newPage();
  await receiverPage.goto(shareUrl);
  await expect(receiverPage.locator('#reveal-btn')).toBeVisible();
  await receiverPage.locator('#reveal-btn').click();
  await expect(receiverPage.locator('#revealed-text')).toHaveText(plaintext);

  await sender.close();
  await receiver.close();
});
