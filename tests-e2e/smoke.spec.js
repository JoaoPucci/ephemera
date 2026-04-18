import { test, expect } from '@playwright/test';
import { authenticator } from 'otplib';

// Matches tests-e2e/seed.py. Keep in lockstep.
const USERNAME = 'e2e';
const PASSWORD = 'e2e-password-123';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXP';

test('golden path: sign in, create a text secret, receiver reveals it exactly once', async ({ browser }) => {
  // --- sender context ---
  const sender = await browser.newContext();
  const senderPage = await sender.newPage();

  await senderPage.goto('/send');
  await senderPage.fill('#username', USERNAME);
  await senderPage.fill('#password', PASSWORD);
  await senderPage.fill('#code', authenticator.generate(TOTP_SECRET));
  await senderPage.click('#login-form button[type="submit"]');

  // Page reloads into the sender form.
  await expect(senderPage.locator('#content')).toBeVisible();

  const plaintext = `e2e message ${Date.now()}`;
  await senderPage.fill('#content', plaintext);
  await senderPage.click('#submit-btn');

  await expect(senderPage.locator('#result-url')).toBeVisible();
  const shareUrl = (await senderPage.locator('#result-url').textContent() ?? '').trim();
  expect(shareUrl).toMatch(/#[A-Za-z0-9_-]+$/);

  // --- receiver context (separate storage so no session cookie leaks) ---
  const receiver = await browser.newContext();
  const receiverPage = await receiver.newPage();

  await receiverPage.goto(shareUrl);
  await expect(receiverPage.locator('#reveal-btn')).toBeVisible();
  await receiverPage.locator('#reveal-btn').click();
  await expect(receiverPage.locator('#revealed-text')).toHaveText(plaintext);

  // Second visit to the same URL must show the "gone" state -- one-shot
  // viewing is the whole point of the service.
  const secondVisit = await receiver.newPage();
  await secondVisit.goto(shareUrl);
  await expect(secondVisit.locator('#state-gone')).toBeVisible();

  await sender.close();
  await receiver.close();
});
