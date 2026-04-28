// i18n.js fixture builder.
//
// i18n.js reads two <script type="application/json"> nodes from the DOM
// at init: the active-locale catalog (#i18n-catalog) and the English
// fallback (#i18n-fallback). The picker is a <select id="lang-picker">.
// Each test passes its own catalog payloads + activeLocale + auth state.
export function mountI18n({
  catalog = {},
  fallback = {},
  activeLocale = 'ja',
  authenticated = false,
  hasPicker = true,
} = {}) {
  document.documentElement.setAttribute('lang', activeLocale);
  const authAttr = authenticated ? ' data-authenticated="true"' : '';
  const picker = hasPicker
    ? `<select id="lang-picker">
         <option value="en">English</option>
         <option value="ja"${activeLocale === 'ja' ? ' selected' : ''}>日本語</option>
         <option value="es">Español</option>
       </select>`
    : '';
  document.body.outerHTML = `
    <body${authAttr}>
      <script type="application/json" id="i18n-catalog">${JSON.stringify(catalog)}</script>
      <script type="application/json" id="i18n-fallback">${JSON.stringify(fallback)}</script>
      ${picker}
    </body>
  `;
}
