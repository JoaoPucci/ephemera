# Swagger UI bundled assets

Pinned to Swagger UI **5.17.14**. Last refreshed: **2026-04-20**.

## Why these files live in-tree

The HTML shell served at `/docs` loads these files via `<script src>` and
`<link href>` rather than from a CDN, so the app's strict
Content-Security-Policy (`script-src 'self'`) applies with no CDN-host
exception. `init.js` exists as a separate same-origin file for the same
reason -- the `SwaggerUIBundle({ ... })` invocation can't be an inline
`<script>` block under `script-src 'self'`.

## Files

| file                   | source                                                                | sha256                                                               |
| ---------------------- | --------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `swagger-ui-bundle.js` | `https://unpkg.com/swagger-ui-dist@<VERSION>/swagger-ui-bundle.js`    | `c2e4a9ef08144839ff47c14202063ecfe4e59e70a4e7154a26bd50d880c88ba1`   |
| `swagger-ui.css`       | `https://unpkg.com/swagger-ui-dist@<VERSION>/swagger-ui.css`          | `40170f0ee859d17f92131ba707329a88a070e4f66874d11365e9a77d232f6117`   |
| `favicon-32x32.png`    | `https://unpkg.com/swagger-ui-dist@<VERSION>/favicon-32x32.png`       | `3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837`   |

`init.js` is hand-written -- not fetched -- see the file itself.

The sha256 column is an in-repo integrity pin for **the currently-
committed files**. The refresh recipe below recomputes fresh hashes
against the new upstream, so these values are expected to change on
every refresh. Their purpose is to give a reviewer reading a future
refresh PR a diff anchor: if the file changed but the hash didn't,
something's off.

## Refresh recipe

Pick the target release from <https://github.com/swagger-api/swagger-ui/releases>
and run the three fetches against that version:

```sh
VERSION=5.X.Y
cd app/static/swagger
curl -fsSL -o swagger-ui-bundle.js "https://unpkg.com/swagger-ui-dist@${VERSION}/swagger-ui-bundle.js"
curl -fsSL -o swagger-ui.css      "https://unpkg.com/swagger-ui-dist@${VERSION}/swagger-ui.css"
curl -fsSL -o favicon-32x32.png   "https://unpkg.com/swagger-ui-dist@${VERSION}/favicon-32x32.png"
sha256sum swagger-ui-bundle.js swagger-ui.css favicon-32x32.png
```

Cross-check the three hashes against a second source before committing
-- e.g., the `dist` tarball on <https://github.com/swagger-api/swagger-ui/releases>
or the `@swagger-api/swagger-ui-dist` npm tarball's `shasum` from
`npm view swagger-ui-dist@${VERSION} dist.shasum` (npm ships a tarball
shasum, not per-file, but a compromised unpkg would also have to match
npm's tarball shasum to stay coherent). Then paste the three `sha256sum`
values into the table above in the same commit that updates the files,
so a reviewer can compare the diff against the recorded hashes.

Then bump the "Pinned to" and "Last refreshed" lines at the top of this
file. Smoke-test by logging in, opening `/docs`, and confirming the UI
renders with no 404s in the browser network tab -- if Swagger UI changed
a file name or added a new sub-resource, those would show up there.

Releases are slow (roughly annual major bumps, point releases a few
times a year); keeping these pinned rather than CDN-loaded means a
supply-chain compromise at unpkg or the upstream npm registry doesn't
reach the app between refreshes. Recording the hashes here makes the
*refresh itself* auditable: the moment a compromise would land is the
refresh PR, and the hashes are the diff-time anchor that a reviewer
compares against the two independent sources above.
