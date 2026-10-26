/* CCTV CV — sign-in page. Keeps credentials out of storage; the server sets
 * an HttpOnly session cookie on success. */
const form = document.getElementById('login-form');
const errorBox = document.getElementById('login-error');
const submit = document.getElementById('login-submit');
const usernameInput = document.getElementById('login-username');
const passwordInput = document.getElementById('login-password');
document.title = `${window.dashboardI18n.t('Sign in')} · CCTV CV`;
window.addEventListener('dashboard-language-change', () => {
  document.title = `${window.dashboardI18n.t('Sign in')} · CCTV CV`;
});

function safeNext() {
  const value = new URLSearchParams(location.search).get('next') || '/';
  return value.startsWith('/') && !value.startsWith('//') ? value : '/';
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

async function readJson(response) {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

async function alreadySignedIn() {
  try {
    const response = await fetch('/api/auth/me', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
    return response.ok;
  } catch {
    return false;
  }
}

(async () => {
  if (await alreadySignedIn()) location.replace(safeNext());
})();

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  if (!username || !password) {
    showError('Enter your username and password.');
    return;
  }

  submit.disabled = true;
  submit.textContent = 'Signing in…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await readJson(response);
    if (!response.ok) {
      showError((data && data.detail) || 'Sign-in failed. Try again.');
      passwordInput.value = '';
      passwordInput.focus();
      return;
    }
    location.replace(safeNext());
  } catch {
    showError('Could not reach the server. Check your connection.');
  } finally {
    submit.disabled = false;
    submit.textContent = 'Sign in';
  }
});
