// Swagger UI bootstrap. Split from the HTML shell so we stay under the
// app's strict CSP (script-src 'self' with no 'unsafe-inline' nor any
// inline <script> blocks). Loaded from /static/swagger/init.js, which
// matches 'self' for scripts.
//
// SwaggerUIBundle is defined by swagger-ui-bundle.js, served adjacent
// under /static/swagger/. The openapi schema URL is same-origin and
// auth-gated, so the browser's session cookie authenticates the XHR.
window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  presets: [
    SwaggerUIBundle.presets.apis,
    SwaggerUIBundle.SwaggerUIStandalonePreset,
  ],
  plugins: [SwaggerUIBundle.plugins.DownloadUrl],
  layout: "BaseLayout",
});
