import { expect, test } from '@playwright/test';
import { generateSync } from 'otplib';

// Per-spec fixture user (see image.spec.js for the per-user-isolation rationale).
const USERNAME = 'e2e-passphrase';
const PASSWORD = 'e2e-password-123';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';

// max_passphrase_attempts defaults to 5 (app/config.py). The 5th wrong
// attempt is the one that burns the secret -- attempts 1-4 each return
// 401, attempt 5 increments the counter to 5 (>= cap) and the server
// burns + returns 410. The reveal page treats 410 as `state-gone`.
const PASSPHRASE = 'correct horse battery staple';
const WRONG_PASSPHRASE = 'wrong-attempt-x';
const ATTEMPTS_BEFORE_BURN = 5;

test('passphrase + burn-after-5: wrong passphrase 5 times destroys the secret', async ({
  browser,
}) => {
  // --- sender: create a passphrase-protected text secret ---
  const sender = await browser.newContext();
  const senderPage = await sender.newPage();
  await senderPage.goto('/send');
  await senderPage.fill('#username', USERNAME);
  await senderPage.fill('#password', PASSWORD);
  await senderPage.fill('#code', generateSync({ secret: TOTP_SECRET, strategy: 'totp' }));
  await senderPage.click('#login-form button[type="submit"]');
  await expect(senderPage.locator('#content')).toBeVisible();

  await senderPage.fill('#content', `protected message ${Date.now()}`);
  await senderPage.fill('#passphrase', PASSPHRASE);
  await senderPage.click('#submit-btn');

  await expect(senderPage.locator('#result-url')).toBeVisible();
  const shareUrl = ((await senderPage.locator('#result-url').textContent()) ?? '').trim();

  // --- receiver: hammer the passphrase until burn ---
  const receiver = await browser.newContext();
  const receiverPage = await receiver.newPage();

  await receiverPage.goto(shareUrl);
  // The /meta probe reports passphrase_required: true -> the wrap unhides.
  await expect(receiverPage.locator('#passphrase-wrap')).toBeVisible();

  // Attempts 1..N-1 each return 401; the page surfaces an error and restores
  // the button so the next attempt can fire. The receive-rate-limiter is
  // 10/min/IP, so 5 attempts comfortably fits.
  for (let i = 1; i < ATTEMPTS_BEFORE_BURN; i++) {
    await receiverPage.fill('#passphrase', `${WRONG_PASSPHRASE}-${i}`);
    await receiverPage.click('#reveal-btn');
    await expect(receiverPage.locator('#reveal-error')).toBeVisible();
    // Wait for the button to be re-enabled before the next click; otherwise
    // a fast harness would race the in-flight guard and lose an attempt.
    await expect(receiverPage.locator('#reveal-btn')).toBeEnabled();
  }

  // Attempt N: the server crosses the cap, burns the secret, returns 410.
  // reveal.js routes 410 to state-gone.
  await receiverPage.fill('#passphrase', `${WRONG_PASSPHRASE}-final`);
  await receiverPage.click('#reveal-btn');
  await expect(receiverPage.locator('#state-gone')).toBeVisible();

  // Even the correct passphrase doesn't bring it back -- the row is gone.
  const followUp = await receiver.newPage();
  await followUp.goto(shareUrl);
  await expect(followUp.locator('#state-gone')).toBeVisible();

  await sender.close();
  await receiver.close();
});
