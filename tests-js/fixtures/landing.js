// Receiver-side landing.html fixture for reveal.js tests.
//
// State sections (state-loading, state-ready, state-text, state-image,
// state-gone) match what reveal.js toggles via show(name). The zoom
// overlay sits outside the card so it can backdrop the whole viewport.
export function mountLanding() {
  document.body.innerHTML = `
    <main class="card" id="main-card">
      <section id="state-loading"><p>Loading…</p></section>
      <section id="state-ready" hidden>
        <div id="passphrase-wrap" hidden>
          <label for="passphrase">Passphrase</label>
          <div class="input-with-action">
            <input type="password" id="passphrase" autocomplete="off">
            <button type="button" id="toggle-passphrase" class="input-action"
                    aria-label="show passphrase" aria-pressed="false">show</button>
          </div>
        </div>
        <button id="reveal-btn" type="button">Reveal Secret</button>
        <p class="error" id="reveal-error" hidden></p>
      </section>
      <section id="state-text" hidden>
        <pre id="revealed-text"></pre>
        <button type="button" id="copy-btn" class="copy-btn" hidden>Copy to clipboard</button>
      </section>
      <section id="state-image" hidden>
        <img id="revealed-image" alt="" tabindex="0">
      </section>
      <section id="state-gone" hidden><h1>Gone.</h1></section>
    </main>
    <div id="zoom-overlay" hidden>
      <img id="zoom-image" alt="">
      <button type="button" id="zoom-close">close</button>
    </div>
  `;
}
