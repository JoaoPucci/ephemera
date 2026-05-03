// Exercises the reveal-endpoint rate limiter end-to-end. The unit-level
// limiter tests in tests/test_security.py patch `time.monotonic` and
// drive the limiter directly; this spec drives it through the live HTTP
// surface to verify the FastAPI dependency wires up the same way in a
// running server -- a class of regression where the limiter is correct
// in isolation but the route forgets to hang `Depends(reveal_rate_limit)`
// off the endpoint.
//
// The reveal limiter is keyed by client IP at 10 hits / 60 seconds.
// Hitting 11 reveal requests within the window from the same browser
// should land the 11th at HTTP 429.
//
// `beforeEach` calls /_test/limiter/reset so this spec doesn't trip
// 429s left over from earlier specs in the same Playwright session,
// and so an interrupted re-run starts clean.
import { expect, test } from '@playwright/test';

test.beforeEach(async ({ request }) => {
  await request.post('/_test/limiter/reset', {
    headers: { Origin: 'http://127.0.0.1:8765' },
  });
});

test('reveal endpoint returns 429 after 10 hits in the same minute', async ({ request }) => {
  // Each of the first 10 attempts hits an unknown token, which the
  // route classifies as 410 (gone) -- but the rate-limit Depends
  // runs BEFORE the row lookup, so every attempt counts toward the
  // limiter.
  for (let i = 0; i < 10; i++) {
    const r = await request.post('/s/totally-fake-token-xyz/reveal', {
      data: { key: 'invalid-key-fragment' },
      headers: { Origin: 'http://127.0.0.1:8765' },
    });
    expect(r.status()).not.toBe(429);
  }

  const limited = await request.post('/s/totally-fake-token-xyz/reveal', {
    data: { key: 'invalid-key-fragment' },
    headers: { Origin: 'http://127.0.0.1:8765' },
  });
  expect(limited.status()).toBe(429);
});

test('limiter reset hook lets a fresh batch through after reset', async ({ request }) => {
  // Pin the test-hook itself: a no-op reset endpoint would silently
  // make the suite green even if its real effect was missing. Burn
  // the limiter, reset, then verify the next batch isn't immediately
  // 429'd.
  for (let i = 0; i < 11; i++) {
    await request.post('/s/totally-fake-token-xyz/reveal', {
      data: { key: 'invalid-key-fragment' },
      headers: { Origin: 'http://127.0.0.1:8765' },
    });
  }
  await request.post('/_test/limiter/reset', {
    headers: { Origin: 'http://127.0.0.1:8765' },
  });

  const fresh = await request.post('/s/totally-fake-token-xyz/reveal', {
    data: { key: 'invalid-key-fragment' },
    headers: { Origin: 'http://127.0.0.1:8765' },
  });
  expect(fresh.status()).not.toBe(429);
});
