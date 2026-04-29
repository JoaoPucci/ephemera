import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests-js/**/*.test.js'],
    globals: false,
    restoreMocks: true,
    coverage: {
      // v8: native Node v8 coverage, no instrumentation transform — faster
      // and more accurate than istanbul for plain ES modules like ours.
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      include: ['app/static/**/*.js'],
      exclude: ['app/static/swagger/**'],
      // Uniform per-file floor across every app/static/**/*.js module.
      // The glob applies a single threshold tuple to every matched file
      // individually -- CI breaks if any one module slides below the
      // floor, not just the aggregate average. The floor is set BELOW
      // the lowest current actual so a benign refactor doesn't tip CI
      // red on noise; substantive coverage erosion still trips the gate.
      //
      // The direction is to ratchet UP, never relax. When a refactor
      // shrinks a file and the percentage moves around, close the gap
      // with new tests instead of lowering the bar -- that's the only
      // way coverage compounds. New modules clear this floor on day one
      // (write the tests alongside the code) and gradually pull these
      // numbers higher as the suite matures.
      thresholds: {
        'app/static/**/*.js': {
          statements: 88,
          branches: 65,
          functions: 85,
          lines: 90,
        },
      },
    },
  },
});
