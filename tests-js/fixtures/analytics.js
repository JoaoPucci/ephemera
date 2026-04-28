// Analytics-toggle fixture: BOTH surfaces (desktop pill + drawer row + their
// respective confirm panels) so a single module load can wire each. The
// asymmetric opt-IN-vs-opt-OUT flow is the load-bearing UX here, and tests
// assert on whichever surface they care about.
export function mountAnalyticsSurfaces({ analyticsOptIn = false } = {}) {
  const checkedAttr = analyticsOptIn ? 'true' : 'false';
  const expanded = 'false';
  document.body.innerHTML = `
    <button id="analytics-toggle" class="analytics-toggle"
            role="button" aria-checked="${checkedAttr}"
            aria-haspopup="dialog" aria-expanded="${expanded}"
            aria-controls="analytics-popover">
      <span class="analytics-toggle-label" data-i18n="analytics.label"></span>
      <span class="analytics-toggle-dot"></span>
    </button>
    <div id="analytics-popover" role="dialog" hidden>
      <h2 data-i18n="analytics.dialog_title"></h2>
      <p data-i18n="analytics.dialog_body"></p>
      <p data-i18n="analytics.dialog_note"></p>
      <button class="analytics-popover-cancel" data-i18n="analytics.cancel"></button>
      <button class="analytics-popover-confirm" data-i18n="analytics.confirm"></button>
    </div>
    <span id="analytics-toggle-ack" class="visually-hidden"
          data-i18n-disabled-ack="analytics.disabled_ack"></span>
    <span id="analytics-toggle-ack-tip" class="analytics-toggle-ack-tip"
          data-i18n-disabled-ack="analytics.disabled_ack"></span>

    <button id="chrome-menu-analytics" class="chrome-menu-row chrome-menu-row-toggle"
            role="button" aria-checked="${checkedAttr}"
            aria-haspopup="true" aria-expanded="${expanded}"
            aria-controls="chrome-menu-analytics-disclosure">
      <span class="chrome-menu-row-label" data-i18n="analytics.dialog_title"></span>
      <span class="chrome-menu-row-ack" data-i18n-disabled-ack="analytics.disabled_ack"></span>
    </button>
    <div id="chrome-menu-analytics-disclosure" hidden>
      <p data-i18n="analytics.dialog_body"></p>
      <p data-i18n="analytics.dialog_note"></p>
      <button class="chrome-menu-row-disclosure-cancel" data-i18n="analytics.cancel"></button>
      <button class="chrome-menu-row-disclosure-confirm" data-i18n="analytics.confirm"></button>
    </div>
  `;
}
