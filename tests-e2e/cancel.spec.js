import { expect, test } from '@playwright/test';
import { generateSync } from 'otplib';

// Per-spec fixture user (see image.spec.js for the per-user-isolation rationale).
const USERNAME = 'e2e-cancel';
const PASSWORD = 'e2e-password-123';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';

test('sender-initiated cancel: tracked secret two-click revoke kills the URL', async ({
  browser,
}) => {
  // --- sender: log in and create a TRACKED text secret ---
  const sender = await browser.newContext();
  const senderPage = await sender.newPage();
  await senderPage.goto('/send');
  await senderPage.fill('#username', USERNAME);
  await senderPage.fill('#password', PASSWORD);
  await senderPage.fill('#code', generateSync({ secret: TOTP_SECRET, strategy: 'totp' }));
  await senderPage.click('#login-form button[type="submit"]');
  await expect(senderPage.locator('#content')).toBeVisible();

  await senderPage.fill('#content', `cancel test ${Date.now()}`);
  // Tracking is what surfaces the secret in the tracked-list panel and
  // wires the per-row Cancel button -- the cancel UI only exists for
  // tracked rows. (An untracked secret has no list entry, so there's
  // nowhere to attach the cancel action.)
  //
  // The native checkbox is visually hidden (`appearance: none` + opacity
  // styling) and the `.toggle-slider` span is the affordance the user
  // actually clicks. Playwright's actionability check rejects clicks on
  // the hidden input, so we click the wrapping label -- which is what a
  // real user does (browsers route label clicks to the associated input
  // and dispatch the change event the form handler listens for).
  await senderPage.locator('label.toggle').click();
  await senderPage.click('#submit-btn');

  await expect(senderPage.locator('#result-url')).toBeVisible();
  const shareUrl = ((await senderPage.locator('#result-url').textContent()) ?? '').trim();

  // The tracked panel renders + polls; expand it so the row is visible.
  await expect(senderPage.locator('#tracked-section')).toBeVisible();
  await senderPage.click('#tracked-header');
  const row = senderPage.locator('#tracked-list li').first();
  await expect(row).toBeVisible();

  // Two-click confirm: first click arms the button (.armed class + label
  // swaps to the localized "Confirm"); second click within 3s executes.
  // Per ARCHITECTURE.md decision #18 this pattern is project-wide; the
  // test pins it for the cancel surface specifically.
  const cancelBtn = row.locator('.tracked-cancel');
  await cancelBtn.click();
  await expect(cancelBtn).toHaveClass(/armed/);
  await cancelBtn.click();

  // Server flips the row to status=canceled. Wait for the polling sync
  // (every 5s) or the immediate post-action re-render to land.
  await expect(row.locator('.status-pill')).toHaveClass(/canceled/, { timeout: 8000 });

  // --- receiver: the URL is dead ---
  const receiver = await browser.newContext();
  const receiverPage = await receiver.newPage();
  await receiverPage.goto(shareUrl);
  // /meta returns 404 once the row is canceled (ciphertext NULL) -- the
  // landing page flips straight to state-gone.
  await expect(receiverPage.locator('#state-gone')).toBeVisible();

  await sender.close();
  await receiver.close();
});
