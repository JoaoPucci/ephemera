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

| file                   | source                                                                |
| ---------------------- | --------------------------------------------------------------------- |
| `swagger-ui-bundle.js` | `https://unpkg.com/swagger-ui-dist@<VERSION>/swagger-ui-bundle.js`    |
| `swagger-ui.css`       | `https://unpkg.com/swagger-ui-dist@<VERSION>/swagger-ui.css`          |
| `favicon-32x32.png`    | `https://unpkg.com/swagger-ui-dist@<VERSION>/favicon-32x32.png`       |

`init.js` is hand-written -- not fetched -- see the file itself.

## Refresh recipe

Pick the target release from <https://github.com/swagger-api/swagger-ui/releases>
and run the three fetches against that version:

```sh
VERSION=5.X.Y
cd app/static/swagger
curl -fsSL -o swagger-ui-bundle.js "https://unpkg.com/swagger-ui-dist@${VERSION}/swagger-ui-bundle.js"
curl -fsSL -o swagger-ui.css      "https://unpkg.com/swagger-ui-dist@${VERSION}/swagger-ui.css"
curl -fsSL -o favicon-32x32.png   "https://unpkg.com/swagger-ui-dist@${VERSION}/favicon-32x32.png"
```

Then bump the "Pinned to" and "Last refreshed" lines at the top of this
file. Smoke-test by logging in, opening `/docs`, and confirming the UI
renders with no 404s in the browser network tab -- if Swagger UI changed
a file name or added a new sub-resource, those would show up there.

Releases are slow (roughly annual major bumps, point releases a few
times a year); keeping these pinned rather than CDN-loaded means a
supply-chain compromise at unpkg or the upstream npm registry doesn't
reach the app between refreshes.
