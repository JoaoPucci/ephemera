// Minimal sender.html fixture -- the elements sender.js, sender/form.js,
// sender/tracked-list.js, and the result-screen handlers query during
// load and during create-secret. The IIFE wiring guards against missing
// elements where it matters (chrome-menu / analytics-toggle), so this
// fixture stays focused on the compose form + tracked panel + result row.
//
// When the real app/templates/sender.html template grows, mirror the
// new elements here so the tests stay representative.
export function mountSender() {
  document.body.innerHTML = `
    <button type="button" id="user-btn" class="user-btn" aria-label="Signed in as admin. Click to sign out.">
      <span class="user-dot"></span>
      <span id="user-name">…</span>
      <span class="user-sep">·</span>
      <span class="user-action">sign out</span>
    </button>
    <section id="tracked-section" hidden>
      <button type="button" id="tracked-header" aria-expanded="false"></button>
      <span id="tracked-count">0</span>
      <div id="tracked-body">
        <ul id="tracked-list"></ul>
        <button type="button" id="tracked-clear" hidden>
          <span id="tracked-clear-label">Clear past entries</span>
        </button>
      </div>
    </section>
    <div id="compose">
      <div class="tabs">
        <button class="tab active" data-tab="text" type="button">Text</button>
        <button class="tab" data-tab="image" type="button">Image</button>
      </div>
      <form id="secret-form">
        <section id="panel-text">
          <textarea id="content" name="content"
                    maxlength="100000"
                    aria-describedby="content-hint"></textarea>
          <p class="form-hint" id="content-hint" hidden aria-live="polite" aria-atomic="true"></p>
        </section>
        <section id="panel-image" hidden>
          <div id="dropzone">
            <input type="file" id="file" hidden>
            <div id="preview" hidden>
              <span id="file-name"></span>
              <button type="button" id="clear-file">clear</button>
            </div>
          </div>
        </section>
        <select id="expires_in" name="expires_in"><option value="3600" selected>1h</option></select>
        <input type="text" id="passphrase" name="passphrase"
               maxlength="200"
               aria-describedby="passphrase-hint">
        <p class="form-hint" id="passphrase-hint" hidden aria-live="polite" aria-atomic="true"></p>
        <label><input type="checkbox" id="track" name="track"> Track</label>
        <div id="label-wrap" hidden>
          <input type="text" id="label" maxlength="60" aria-describedby="label-hint">
          <p class="form-hint" id="label-hint" aria-live="polite" aria-atomic="true">Up to 60 characters. Shown only to you.</p>
        </div>
        <button type="submit" id="submit-btn">Create Secret</button>
        <p class="error" id="sender-error" hidden></p>
      </form>
    </div>
    <section id="result" hidden>
      <div class="result-row">
        <span class="result-eyebrow">URL</span>
        <code id="result-url"></code>
        <button type="button" id="copy-url" class="copy-btn">Copy URL</button>
      </div>
      <div class="result-row" id="result-passphrase-row" hidden>
        <span class="result-eyebrow">Passphrase</span>
        <code id="result-passphrase" data-masked="true"></code>
        <button type="button" id="toggle-result-passphrase"
                aria-label="show passphrase" aria-pressed="false"
                data-i18n-show="button.show" data-i18n-hide="button.hide"></button>
        <button type="button" id="copy-passphrase" class="copy-btn">Copy passphrase</button>
      </div>
      <p id="result-expiry"></p>
      <div id="status-widget" hidden>
        <span id="status-value">pending</span>
        <span id="status-detail"></span>
      </div>
      <button type="button" id="create-another" class="link">Create another</button>
    </section>
  `;
}
