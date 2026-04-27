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
      // Per-file thresholds. Each entry pins a floor for one module; CI
      // breaks if coverage on that file slides below the listed numbers.
      // Globs without an entry here have no enforced floor -- this is
      // intentional during the build-out phase, where theme.js, reveal.js,
      // copy.js still sit at 0-50% and their thresholds will land alongside
      // their test suites in their own PRs. Adding a file here without a
      // matching test suite would either fail CI (over-tight floor) or be
      // vacuous (zero floor).
      //
      // Numbers are set ~3 points below current actuals so a benign
      // refactor doesn't tip CI red on noise; substantive coverage
      // erosion still trips the gate.
      thresholds: {
        'app/static/sender/tracked-list.js': {
          statements: 83,
          branches: 65,
          functions: 85,
          lines: 85,
        },
        'app/static/sender/url-cache.js': {
          statements: 90,
          branches: 80,
          functions: 90,
          lines: 90,
        },
        'app/static/chrome-menu.js': {
          statements: 88,
          branches: 73,
          functions: 90,
          lines: 92,
        },
        'app/static/i18n.js': {
          statements: 83,
          branches: 80,
          functions: 88,
          lines: 85,
        },
      },
    },
  },
});
