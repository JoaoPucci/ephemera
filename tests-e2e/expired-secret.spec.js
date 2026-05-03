// End-to-end: receiver sees the "gone" state when a secret expires
// between create and reveal. The unit-level expiry test in
// tests/test_receiver.py creates a secret directly via models.create_secret
// with a negative expires_in (bypassing the API's Pydantic floor); this
// spec drives the create through the public API, then uses the
// /_test/secret/{token}/expire-now hook to flip the row's expires_at
// into the past, then verifies the receiver UI lands on #state-gone.
//
// Why expire-now and not a global clock-fast-forward: the limiter, TOTP
// verification, and session expiry all read different clocks. Advancing
// every clock together would either invalidate the active session or
// introduce a separate test for time-source consistency. Touching one
// row's expires_at is the smallest surface that exercises the
// receiver's expiry path without dragging unrelated systems along.
import { expect, test } from '@playwright/test';
import { generateSync } from 'otplib';

const USERNAME = 'e2e-expired-secret';
const PASSWORD = 'e2e-password-123';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';

test.beforeEach(async ({ request }) => {
  await request.post('/_test/limiter/reset', {
    headers: { Origin: 'http://127.0.0.1:8765' },
  });
});

test('receiver lands on the gone state when secret expired between create and reveal', async ({
  browser,
  request,
}) => {
  // --- sender context: create a secret normally -------------------------
  const sender = await browser.newContext();
  const senderPage = await sender.newPage();
  await senderPage.goto('/send');
  await senderPage.fill('#username', USERNAME);
  await senderPage.fill('#password', PASSWORD);
  await senderPage.fill('#code', generateSync({ secret: TOTP_SECRET, strategy: 'totp' }));
  await senderPage.click('#login-form button[type="submit"]');
  await expect(senderPage.locator('#content')).toBeVisible();

  await senderPage.fill('#content', `e2e-expired ${Date.now()}`);
  await senderPage.click('#submit-btn');
  await expect(senderPage.locator('#result-url')).toBeVisible();
  const shareUrl = ((await senderPage.locator('#result-url').textContent()) ?? '').trim();
  // shareUrl shape: http://127.0.0.1:8765/s/<token>#<key>
  // Pull `<token>` out of the path component.
  const token = (shareUrl.split('#')[0] ?? '').split('/').pop();
  expect(token).toBeTruthy();

  // --- server-side: force the secret into the past ----------------------
  const expireResp = await request.post(`/_test/secret/${token}/expire-now`, {
    headers: { Origin: 'http://127.0.0.1:8765' },
  });
  expect(expireResp.ok()).toBeTruthy();

  // --- receiver context: visit the link, expect gone state --------------
  const receiver = await browser.newContext();
  const receiverPage = await receiver.newPage();
  await receiverPage.goto(shareUrl);
  await expect(receiverPage.locator('#state-gone')).toBeVisible();
  // The reveal button must NOT be visible -- the gone state shows the
  // "this link has expired or already been viewed" copy, not the
  // password / fragment-decode UI.
  await expect(receiverPage.locator('#reveal-btn')).toBeHidden();

  await sender.close();
  await receiver.close();
});
