// Minimal login.html fixture for the login.js handler tests.
//
// The form is pre-populated with default values so most tests can skip
// the "fill the inputs" step and go straight to the assertion they care
// about (in-flight guard, mode toggle, recovery-code formatter, etc.).
// Tests that exercise input behavior overwrite the values themselves.
export function mountLoginForm() {
  document.body.innerHTML = `
    <form id="login-form">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" required>

      <label for="password">Password</label>
      <div class="input-with-action">
        <input type="password" id="password" name="password" required>
        <button type="button" id="toggle-password" class="input-action"
                aria-label="show password" aria-pressed="false">show</button>
      </div>

      <label for="code" id="code-label">6-digit code</label>
      <div class="input-with-action">
        <input type="text" id="code" name="code"
               autocomplete="one-time-code"
               inputmode="numeric" pattern="[0-9]{6}"
               minlength="6" maxlength="6"
               enterkeyhint="go"
               autocapitalize="off" spellcheck="false"
               aria-describedby="code-hint"
               required>
        <button type="button" id="toggle-code" class="input-action"
                aria-label="show code" aria-pressed="false" hidden>show</button>
      </div>
      <p class="form-hint" id="code-hint" hidden aria-live="polite" aria-atomic="true"></p>

      <button type="submit">Sign in</button>

      <p class="error" id="login-error" hidden></p>
      <button type="button" id="toggle-code-mode" class="link">Use a recovery code</button>
    </form>
  `;
  document.getElementById('username').value = 'admin';
  document.getElementById('password').value = 'pw';
  document.getElementById('code').value = '123456';
}
