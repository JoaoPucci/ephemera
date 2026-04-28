# tests-js/fixtures/

DOM fixtures for the jsdom unit tests, one file per page (matches the
`app/static/<page>.html` template they shadow).

Each fixture module exports a `mount<Name>()` function that writes a
minimal slice of the page's markup into `document.body`. Tests import
the relevant `mount` function and invoke it in `beforeEach`. The
fixtures are intentionally minimal — they include only the elements
the module under test queries — so they're cheap to keep in sync when
the real template grows.

When you change a real template under `app/static/*.html`, also touch
the matching fixture here so the unit tests stay representative. Tests
will silently keep passing if the fixture diverges; the only signal
you'd get is the next bug-report describing UI behavior the unit
tests "covered" but didn't actually exercise.
