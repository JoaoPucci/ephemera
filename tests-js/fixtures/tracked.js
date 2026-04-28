// Tracked-list panel fixture for tests-js/tracked-list.test.js. The IIFE
// wireClearHistory + wireTrackedToggle in tracked-list.js bail when their
// elements are missing, so the fixture must be in place BEFORE
// loadModule('sender/tracked-list').
export function mountTrackedFixture() {
  document.body.innerHTML = `
    <section id="tracked-section" hidden>
      <button type="button" id="tracked-header" aria-expanded="false">
        <span class="tracked-panel-title">Tracked</span>
        <span id="tracked-count">0</span>
      </button>
      <div id="tracked-body">
        <ul id="tracked-list"></ul>
        <button type="button" id="tracked-clear" hidden>
          <span id="tracked-clear-label">Clear past entries</span>
        </button>
      </div>
    </section>
  `;
}
