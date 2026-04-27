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
      // Every app/static/**/*.js module that has a dedicated test suite
      // has an entry here. Numbers are set ~3 points below current
      // actuals so a benign refactor doesn't tip CI red on noise;
      // substantive coverage erosion still trips the gate.
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
        'app/static/theme.js': {
          statements: 92,
          branches: 85,
          functions: 95,
          lines: 92,
        },
        'app/static/reveal.js': {
          statements: 96,
          branches: 87,
          functions: 95,
          lines: 97,
        },
        'app/static/copy.js': {
          statements: 95,
          branches: 95,
          functions: 95,
          lines: 95,
        },
      },
    },
  },
});
