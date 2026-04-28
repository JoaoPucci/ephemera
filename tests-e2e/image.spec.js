import { expect, test } from '@playwright/test';
import { generateSync } from 'otplib';

// Per-spec fixture user (seed.py provisions one per spec file). Anti-replay
// stores `totp_last_step` per user, so concurrent specs landing in the
// same 30s TOTP window need their own user to avoid cross-test collisions.
const USERNAME = 'e2e-image';
const PASSWORD = 'e2e-password-123';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';

// Smallest valid PNG: a 1x1 transparent pixel. Bytes are decoded inside
// the test so the spec stays self-contained -- no checked-in binary
// fixture, no extra file the harness has to keep in sync.
const TINY_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg==';

test('image golden path: sender drops a PNG, receiver reveals the data: URI', async ({
  browser,
}) => {
  // --- sender context ---
  const sender = await browser.newContext();
  const senderPage = await sender.newPage();

  await senderPage.goto('/send');
  await senderPage.fill('#username', USERNAME);
  await senderPage.fill('#password', PASSWORD);
  await senderPage.fill('#code', generateSync({ secret: TOTP_SECRET, strategy: 'totp' }));
  await senderPage.click('#login-form button[type="submit"]');
  await expect(senderPage.locator('#content')).toBeVisible();

  // Switch to the Image tab; the dropzone + file input swap in.
  await senderPage.click('.tab[data-tab="image"]');
  await expect(senderPage.locator('#dropzone')).toBeVisible();

  // Playwright drives the hidden file input directly -- avoids depending on
  // the OS-native drag-and-drop synthesis, which is flaky across runners.
  // The button-style dropzone attaches `click` -> `fileInput.click()`, so
  // setInputFiles on the input is the same code path the user's
  // "click to browse" gesture takes.
  await senderPage.locator('#file').setInputFiles({
    name: 'pixel.png',
    mimeType: 'image/png',
    buffer: Buffer.from(TINY_PNG_BASE64, 'base64'),
  });
  // The form's preview row reveals after the file lands.
  await expect(senderPage.locator('#preview')).toBeVisible();

  await senderPage.click('#submit-btn');

  await expect(senderPage.locator('#result-url')).toBeVisible();
  const shareUrl = ((await senderPage.locator('#result-url').textContent()) ?? '').trim();
  expect(shareUrl).toMatch(/#[A-Za-z0-9_-]+$/);

  // --- receiver context (separate storage so no session cookie leaks) ---
  const receiver = await browser.newContext();
  const receiverPage = await receiver.newPage();

  await receiverPage.goto(shareUrl);
  await expect(receiverPage.locator('#reveal-btn')).toBeVisible();
  await receiverPage.click('#reveal-btn');

  // Image render: <img src="data:image/png;base64,..."> appears.
  const img = receiverPage.locator('#revealed-image');
  await expect(img).toBeVisible();
  const src = await img.getAttribute('src');
  expect(src).toMatch(/^data:image\/png;base64,/);

  // The card swaps to the wider layout so images get room to breathe.
  await expect(receiverPage.locator('#main-card')).toHaveClass(/wide/);

  // Second visit shows "gone" -- one-shot viewing is the whole point.
  const secondVisit = await receiver.newPage();
  await secondVisit.goto(shareUrl);
  await expect(secondVisit.locator('#state-gone')).toBeVisible();

  await sender.close();
  await receiver.close();
});
