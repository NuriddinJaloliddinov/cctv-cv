/* CCTV CV — dashboard UI
 *
 * Sections, hash-routed:
 *   #/dashboard   overview: attendance, customers, recent and unconfirmed events
 *   #/cameras     camera discovery, configuration and the live analytics feed
 *   #/enrollment  face enrollment (photos / videos)
 *   #/attendance  confirmed entrance/exit attendance records
 *   #/customers   customer-tagged entrance/exit events (including unconfirmed)
 *   #/training    phone-use example review and labeling
 *   #/telegram    Telegram bot setup and admin-reviewed events
 */
const $ = (id) => document.getElementById(id);

// ---------- State ----------
const feeds = new Map();          // camera_id -> { cardEl, videoEl, pc, retries }
let savedCameras = [];            // from /api/cameras
let discoveredCameras = [];       // from /api/discover (ephemeral)
let persons = [];                 // from /api/persons
let selectedId = null;
let selectedDiscoveredId = null;
let editingId = null;
let cameraFilter = '';
let personFilter = '';
let faceEngine = { available: true, detail: '' };
let channelProfiles = [];
let discoveryBusy = false;
let discoveryReport = { warnings: [], networks: [], scanned_hosts: 0, duration_ms: 0 };
let personBusy = false;
let telegram = { configured: false, token_set: false, token_masked: '',
                 chat_id: '', bot_username: '', last_error: '',
                 events: { pending: 0, confirmed: 0, rejected: 0, failed: 0 } };
let events = [];
let eventsTimer = null;
let attendance = null;
let attendanceTimer = null;
let overviewData = null;
let overviewTimer = null;
let customerData = null;
let customersTimer = null;
let authUser = null;
let accounts = [];
let editingAccountId = null;
let faceReviewPolicy = null;
let faceCandidates = [];

// Person modal
let personMode = 'new';           // 'new' | 'edit'
let editingPersonId = null;
let selectedFiles = [];

// ============================================================
//  Helpers
// ============================================================

function log(msg) {
  const el = $('log');
  const stamp = new Date().toLocaleTimeString([], { hour12: false });
  el.textContent += `[${stamp}] ${msg}\n`;
  const lines = el.textContent.split('\n');
  if (lines.length > 400) el.textContent = lines.slice(-400).join('\n');
  el.scrollTop = el.scrollHeight;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  return readJson(r);
}

function redirectToLogin() {
  if (location.pathname.endsWith('/login.html')) return;
  const next = encodeURIComponent(location.pathname + location.hash);
  location.replace(`/login.html?next=${next}`);
}

async function readJson(r) {
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    if (r.status === 401 && !r.url.endsWith('/api/auth/login')) {
      redirectToLogin();
    }
    const msg = (data && data.detail) || (data && data.error) || r.statusText;
    const error = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    error.status = r.status;
    throw error;
  }
  return data;
}

function isAdminUser() {
  return Boolean(authUser && authUser.role === 'admin');
}

// Multipart upload — deliberately omits the JSON Content-Type header.
async function uploadJson(path, formData) {
  return readJson(await fetch(path, { method: 'POST', body: formData }));
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function displayAddress(address) {
  // Stream URLs often embed usernames/passwords; never print them in the UI.
  return String(address || '').replace(/(\w+:\/\/)[^/@]+@/g, '$1•••@');
}

function fmtFps(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n.toFixed(1) : '0.0';
}

function formatBytes(n) {
  if (n === undefined || n === null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function setBusy(btn, busy, label) {
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.idleLabel) btn.dataset.idleLabel = btn.textContent;
    btn.disabled = true;
    btn.classList.add('is-busy');
    if (label) btn.textContent = label;
  } else {
    btn.disabled = false;
    btn.classList.remove('is-busy');
    if (btn.dataset.idleLabel) {
      btn.textContent = btn.dataset.idleLabel;
      delete btn.dataset.idleLabel;
    }
  }
}

function toast(message, type = 'info') {
  const box = $('toasts');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = '<span class="toast-msg"></span>' +
    '<button class="toast-close" type="button" aria-label="Dismiss">✕</button>';
  el.querySelector('.toast-msg').textContent = message;

  const remove = () => {
    if (!el.isConnected || el.classList.contains('leaving')) return;
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector('.toast-close').onclick = remove;
  box.appendChild(el);
  setTimeout(remove, type === 'error' ? 9000 : 4500);
}

// ============================================================
//  Modals
// ============================================================

const openModals = new Set();
const modalFocus = new WeakMap();

function openModal(el, focusEl) {
  modalFocus.set(el, document.activeElement);
  el.hidden = false;
  openModals.add(el);
  setTimeout(() => {
    if (el.hidden) return;
    const target = focusEl
      || el.querySelector('input:not([type="file"]), textarea, select, button');
    if (target && typeof target.focus === 'function') target.focus();
  }, 40);
}

function closeModal(el) {
  if (!el || el.dataset.busy === 'true') return;
  el.hidden = true;
  openModals.delete(el);
  const target = modalFocus.get(el);
  if (target?.isConnected) {
    if (target.closest('.sidebar') && mobileSidebar.matches && !sidebarOpen) {
      $('sidebar-toggle').focus();
    } else target.focus();
  }
}

let confirmResolve = null;

function confirmDialog({ title = 'Are you sure?', message = '', confirmLabel = 'Delete' } = {}) {
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = message;
  $('confirm-ok').textContent = confirmLabel;
  openModal($('confirm-modal'), $('confirm-cancel'));
  return new Promise((resolve) => { confirmResolve = resolve; });
}

function settleConfirm(value) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;
  closeModal($('confirm-modal'));
  resolve(value);
}

$('confirm-ok').onclick = () => settleConfirm(true);
$('confirm-cancel').onclick = () => settleConfirm(false);

// Backdrop click / Escape
document.querySelectorAll('.modal').forEach((modal) => {
  modal.addEventListener('mousedown', (e) => {
    if (e.target !== modal) return;
    if (modal.id === 'confirm-modal') settleConfirm(false);
    else closeModal(modal);
  });
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') {
    const modal = [...openModals].filter((m) => !m.hidden).pop();
    if (!modal) return;
    const focusable = [...modal.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]')]
      .filter((el) => el.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
    return;
  }
  if (e.key !== 'Escape') return;
  if (confirmResolve) { settleConfirm(false); return; }
  const visible = [...openModals].filter((m) => !m.hidden).pop();
  if (visible) closeModal(visible);
  else if (sidebarOpen) closeSidebar();
});

// The mobile navigation is a real drawer, not a horizontally scrolling menu.
const mobileSidebar = window.matchMedia('(max-width: 820px)');
const sidebar = $('app-sidebar');
const sidebarToggle = $('sidebar-toggle');
const sidebarBackdrop = $('sidebar-backdrop');
let sidebarOpen = false;

function closeSidebar({ restoreFocus = true } = {}) {
  if (!sidebarOpen) return;
  sidebarOpen = false;
  document.body.classList.remove('sidebar-open');
  sidebarToggle.setAttribute('aria-expanded', 'false');
  sidebarBackdrop.hidden = true;
  sidebar.inert = mobileSidebar.matches;
  sidebar.removeAttribute('role');
  sidebar.removeAttribute('aria-modal');
  if (mobileSidebar.matches) sidebar.setAttribute('aria-hidden', 'true');
  if (restoreFocus && mobileSidebar.matches) sidebarToggle.focus();
}

function openSidebar() {
  if (!mobileSidebar.matches || sidebarOpen) return;
  sidebarOpen = true;
  sidebar.inert = false;
  sidebar.removeAttribute('aria-hidden');
  sidebar.setAttribute('role', 'dialog');
  sidebar.setAttribute('aria-modal', 'true');
  document.body.classList.add('sidebar-open');
  sidebarToggle.setAttribute('aria-expanded', 'true');
  sidebarBackdrop.hidden = false;
  $('sidebar-close').focus();
}

function syncSidebar() {
  const focusInside = sidebar.contains(document.activeElement);
  closeSidebar({ restoreFocus: false });
  sidebar.inert = mobileSidebar.matches;
  if (mobileSidebar.matches) sidebar.setAttribute('aria-hidden', 'true');
  else sidebar.removeAttribute('aria-hidden');
  if (mobileSidebar.matches && focusInside) sidebarToggle.focus();
}

sidebarToggle.addEventListener('click', () => {
  if (sidebarOpen) closeSidebar();
  else openSidebar();
});
$('sidebar-close').addEventListener('click', () => closeSidebar());
sidebarBackdrop.addEventListener('click', () => closeSidebar());
sidebar.querySelectorAll('.nav-link').forEach((link) => {
  link.addEventListener('click', () => {
    if (!sidebarOpen) return;
    closeSidebar({ restoreFocus: false });
    setTimeout(() => $('page-title').focus(), 0);
  });
});
sidebar.querySelector('.sidebar-account-actions').addEventListener('click', () => {
  closeSidebar({ restoreFocus: false });
});
mobileSidebar.addEventListener('change', syncSidebar);
syncSidebar();

document.addEventListener('keydown', (e) => {
  if (!sidebarOpen || e.key !== 'Tab' || [...openModals].some((m) => !m.hidden)) return;
  const items = [...sidebar.querySelectorAll('a:not([hidden]), button:not(:disabled), select:not(:disabled)')]
    .filter((el) => el.getClientRects().length && !el.closest('[hidden], [style*="display: none"]'));
  if (!items.length) return;
  if (e.shiftKey && document.activeElement === items[0]) {
    e.preventDefault();
    items.at(-1).focus();
  } else if (!e.shiftKey && document.activeElement === items.at(-1)) {
    e.preventDefault();
    items[0].focus();
  }
});

// ============================================================
//  Routing
// ============================================================

const VIEWS = {
  dashboard: {
    title: 'Overview',
    subtitle: 'Attendance, customers, recent events and alerts at a glance.',
  },
  cameras: {
    title: 'Cameras',
    subtitle: 'Add cameras, tune settings, and watch live analytics.',
  },
  enrollment: {
    title: 'Enrollment',
    subtitle: 'Register the people the system should recognise.',
  },
  attendance: {
    title: 'Attendance',
    subtitle: 'Entrance and exit records confirmed by Telegram administrators.',
  },
  customers: {
    title: 'Customers',
    subtitle: 'Customer-tagged entrance and exit events, including unconfirmed ones.',
  },
  server: {
    title: 'Server',
    subtitle: 'Measure this server against a proposed camera workload.',
  },
  training: {
    title: 'Training',
    subtitle: 'Review phone-use examples and collect missed cases.',
  },
  telegram: {
    title: 'Telegram',
    subtitle: 'Bot settings and admin-confirmed phone-use events.',
  },
  accounts: {
    title: 'Accounts',
    subtitle: 'Administrators, readers, passwords and sign-in protection.',
  },
};

function viewFromHash() {
  const name = (location.hash || '').replace(/^#\/?/, '');
  return Object.prototype.hasOwnProperty.call(VIEWS, name) ? name : 'dashboard';
}

function applyView({ scroll = true } = {}) {
  const view = viewFromHash();

  if (!isAdminUser() && ['accounts', 'training', 'server'].includes(view)) {
    location.hash = '#/dashboard';
    return;
  }

  for (const name of Object.keys(VIEWS)) {
    const section = $(`view-${name}`);
    if (section) section.hidden = name !== view;
  }

  document.querySelectorAll('.nav-link').forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });

  $('page-title').textContent = VIEWS[view].title;
  $('page-subtitle').textContent = VIEWS[view].subtitle;
  document.title = `${window.dashboardI18n.t(VIEWS[view].title)} · CCTV CV`;

  if (view === 'cameras') {
    renderCameras();
    renderSetup();
    if (isAdminUser()) loadDetectionSettings();
  } else if (view === 'dashboard') {
    loadOverview();
    ensureOverviewPolling();
  } else if (view === 'enrollment') {
    loadPersons();
    refreshFaceEngine();
    if (isAdminUser()) {
      loadFaceReviewSettings();
      loadFaceReviewCandidates();
    }
  } else if (view === 'attendance') {
    loadAttendance();
    ensureAttendancePolling();
  } else if (view === 'customers') {
    loadCustomers();
    ensureCustomersPolling();
  } else if (view === 'telegram') {
    if (isAdminUser()) loadTelegram();
    loadEvents();
    ensureEventsPolling();
  } else if (view === 'training') {
    loadReview();
    loadReviewCapture();
    loadTrainingStatus();
  } else if (view === 'accounts') {
    loadAccounts();
  }

  // Only the Telegram section needs live event updates; Attendance, Overview
  // and Customers poll their own views.
  if (view !== 'telegram') stopEventsPolling();
  if (view !== 'attendance') stopAttendancePolling();
  if (view !== 'dashboard') stopOverviewPolling();
  if (view !== 'customers') stopCustomersPolling();

  if (scroll) document.querySelector('.main-content').scrollTop = 0;
}

function applyRoleUi() {
  if (!authUser) return;
  const admin = isAdminUser();
  document.body.dataset.role = authUser.role;
  if ($('readonly-banner')) $('readonly-banner').hidden = admin;
  if ($('user-name')) $('user-name').textContent = authUser.username;
  if ($('user-role')) {
    $('user-role').textContent = admin ? 'Administrator' : 'Read-only';
  }
  if ($('user-avatar')) {
    $('user-avatar').textContent = String(authUser.username || '?')
      .slice(0, 1).toUpperCase();
  }
  // Dynamic lists include action buttons; re-render so readers do not get
  // controls that the server would reject anyway.
  if (typeof renderCameras === 'function') renderCameras();
  if (typeof renderPersons === 'function') renderPersons();
}

window.addEventListener('dashboard-language-change', () => {
  const view = viewFromHash();
  document.title = `${window.dashboardI18n.t(VIEWS[view].title)} · CCTV CV`;
  if (overviewData) renderOverview();
  if (attendance) renderAttendance();
  if (customerData) renderCustomers();
  if (events.length) renderEvents();
});

window.addEventListener('hashchange', () => {
  closeSidebar({ restoreFocus: false });
  applyView();
});

setInterval(() => {
  if (viewFromHash() === 'training') loadTrainingStatus();
}, 5000);

// ============================================================
//  Training: label collection (never silently trains/deploys a model)
// ============================================================

const REVIEW_LABELS = {
  unreviewed: 'Needs review', calling: 'Calling', looking: 'Looking',
  no_use: 'No phone use', no_person: 'No person', unclear: 'Unclear',
};

let reviewCapturePolicy = null;

function reviewCaptureScope() {
  return $('review-scope-exclusive').checked ? 'exclusive' : 'global';
}

function setReviewRate(value) {
  const preset = ['1', '2', '3', '5'].includes(String(value));
  $('review-capture-rate').value = preset ? String(value) : 'custom';
  $('review-capture-custom').value = String(value);
  $('review-capture-custom-group').hidden = preset;
}

function renderReviewCapture() {
  if (!reviewCapturePolicy) return;
  const policy = reviewCapturePolicy;
  const overrides = policy.overrides || {};
  const existing = [...$('review-capture-cameras').querySelectorAll('input:checked')]
    .map((input) => input.value);
  $('review-capture-cameras').innerHTML = savedCameras.length
    ? savedCameras.map((camera) => `
      <label class="check-row"><input type="checkbox" value="${escapeHtml(camera.id)}" ${existing.includes(camera.id) ? 'checked' : ''}>
        ${escapeHtml(camera.name)} ${overrides[camera.id] ? '(Exclusive override)' : ''}
      </label>`).join('')
    : '<p class="hint">Add a camera before using Exclusive settings.</p>';
  $('review-capture-status').textContent =
    `Global: ${policy.enabled ? 'On' : 'Off'}, ${policy.max_per_hour}/camera/hour · ` +
    `${Object.keys(overrides).length} Exclusive override(s). ` +
    (!policy.enabled ? 'Global Off disables all capture.' : '');
  updateReviewScopeFields();
}

function updateReviewScopeFields() {
  if (!reviewCapturePolicy) return;
  const exclusive = reviewCaptureScope() === 'exclusive';
  $('review-capture-cameras').hidden = !exclusive;
  const selected = [...$('review-capture-cameras').querySelectorAll('input:checked')];
  const first = exclusive && selected.length === 1
    ? reviewCapturePolicy.overrides?.[selected[0].value] : null;
  $('review-capture-enabled').checked = exclusive ? (first?.enabled ?? true)
    : reviewCapturePolicy.enabled;
  setReviewRate(first?.max_per_hour ?? reviewCapturePolicy.max_per_hour);
}

async function loadReviewCapture() {
  try {
    reviewCapturePolicy = await api('/api/training/capture');
    renderReviewCapture();
  } catch (e) { toast('Could not load capture setting: ' + e.message, 'error'); }
}

$('review-scope-global').onchange = updateReviewScopeFields;
$('review-scope-exclusive').onchange = updateReviewScopeFields;
$('review-capture-cameras').addEventListener('change', updateReviewScopeFields);
$('review-capture-rate').onchange = () => {
  $('review-capture-custom-group').hidden = $('review-capture-rate').value !== 'custom';
};
$('review-capture-save').onclick = async () => {
  const scope = reviewCaptureScope();
  const ids = [...$('review-capture-cameras').querySelectorAll('input:checked')]
    .map((input) => input.value);
  if (scope === 'exclusive' && !ids.length) {
    toast('Select at least one camera', 'error'); return;
  }
  const rate = Number(scope && ($('review-capture-rate').value === 'custom'
    ? $('review-capture-custom').value : $('review-capture-rate').value));
  if (!Number.isInteger(rate) || rate < 1 || rate > 120) {
    toast('Enter a whole-number hourly maximum between 1 and 120', 'error'); return;
  }
  if (scope === 'global' && Object.keys(reviewCapturePolicy?.overrides || {}).length &&
      !await confirmDialog({ title: 'Replace Exclusive settings?',
        message: 'Saving Global settings removes all existing camera-specific overrides.',
        confirmLabel: 'Apply globally' })) return;
  const button = $('review-capture-save');
  setBusy(button, true, 'Saving…');
  try {
    reviewCapturePolicy = await api('/api/training/capture', {
      method: 'PUT', body: JSON.stringify({ scope,
        enabled: $('review-capture-enabled').checked,
        max_per_hour: rate, camera_ids: scope === 'exclusive' ? ids : [] }),
    });
    renderReviewCapture();
    toast(scope === 'global' ? 'Global capture settings saved'
      : `Capture settings saved for ${ids.length} camera(s)`);
  } catch (e) { toast(e.message, 'error'); }
  finally { setBusy(button, false); }
};

async function loadReview() {
  const filter = $('review-camera');
  const upload = $('review-upload-camera');
  const filterId = filter.value;
  const uploadId = upload.value;
  filter.innerHTML = '<option value="">All cameras</option>' + savedCameras.map((c) =>
    `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`).join('');
  upload.innerHTML = '<option value="">Select camera</option>' + savedCameras.map((c) =>
    `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`).join('');
  filter.value = filterId;
  upload.value = uploadId;
  try {
    const params = new URLSearchParams();
    if (filter.value) params.set('camera_id', filter.value);
    if ($('review-label-filter').value) params.set('label', $('review-label-filter').value);
    const data = await api('/api/training/samples?' + params.toString());
    $('review-count').textContent = Object.values(data.counts).reduce((a, n) => a + n, 0);
    $('review-summary').textContent =
      `${(data.counts.calling || 0) + (data.counts.looking || 0)} phone-use · ` +
      `${data.counts.no_use || 0} no-use · ${data.counts.unreviewed || 0} awaiting review · ` +
      `${data.counts.no_person || 0} no-person · ${data.counts.unclear || 0} unclear`;
    const list = $('review-list');
    list.innerHTML = data.samples.length ? data.samples.map((s) => `
      <article class="review-item" data-id="${escapeHtml(s.id)}">
        <img src="${escapeHtml(s.image_url)}" loading="lazy" alt="Person crop for phone-use review">
        <p><b>${escapeHtml(savedCameras.find((c) => c.id === s.camera_id)?.name || s.camera_id)}</b>
        · ${escapeHtml(s.source)} ${s.score == null ? '' : `· model ${Math.round(s.score * 100)}%`}</p>
        <small>${escapeHtml(s.created_at)}</small>
        <label>Activity
          <select class="review-choice" aria-label="Activity label">
            ${Object.entries(REVIEW_LABELS).map(([value, text]) => `<option value="${value}" ${s.label === value ? 'selected' : ''}>${text}</option>`).join('')}
          </select>
        </label>
        <button type="button" class="btn-mini review-box" data-image="${escapeHtml(s.image_url)}" data-box="${escapeHtml(JSON.stringify(s.person_box || null))}">${s.person_box ? 'Edit person box' : 'Draw person box'}</button>
        <button type="button" class="btn-mini review-delete">Delete image</button>
      </article>`).join('') : '<div class="empty-state">No samples match these filters. Add a missed case below or enable opt-in capture.</div>';
  } catch (e) {
    toast('Could not load review samples: ' + e.message, 'error');
  }
}

$('review-refresh').onclick = loadReview;
$('review-camera').onchange = loadReview;
$('review-label-filter').onchange = loadReview;
$('review-list').addEventListener('change', async (event) => {
  if (!event.target.classList.contains('review-choice')) return;
  const id = event.target.closest('[data-id]').dataset.id;
  try {
    await api(`/api/training/samples/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ label: event.target.value }),
    });
    toast('Label saved');
    await loadReview();
  } catch (e) { toast(e.message, 'error'); loadReview(); }
});
$('review-list').addEventListener('click', async (event) => {
  if (event.target.classList.contains('review-box')) {
    openReviewBox(event.target);
    return;
  }
  if (!event.target.classList.contains('review-delete')) return;
  const id = event.target.closest('[data-id]').dataset.id;
  if (!await confirmDialog({ title: 'Delete sample?', message: 'This permanently removes the stored image and its label.' })) return;
  try {
    await api(`/api/training/samples/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await loadReview();
  } catch (e) { toast(e.message, 'error'); }
});

let reviewBoxSample = null;
let reviewBoxImage = null;
let reviewBox = null;
let reviewDragStart = null;
const reviewCanvas = $('review-box-canvas');

function drawReviewBox() {
  if (!reviewBoxImage) return;
  const ctx = reviewCanvas.getContext('2d');
  ctx.clearRect(0, 0, reviewCanvas.width, reviewCanvas.height);
  ctx.drawImage(reviewBoxImage, 0, 0);
  if (reviewBox) {
    ctx.strokeStyle = '#ffb400';
    ctx.lineWidth = Math.max(2, reviewCanvas.width / 400);
    ctx.strokeRect(reviewBox[0], reviewBox[1],
                   reviewBox[2] - reviewBox[0], reviewBox[3] - reviewBox[1]);
  }
  $('review-box-coordinates').textContent = reviewBox
    ? `Person box: ${reviewBox.join(', ')} (image pixels)`
    : 'No box. Draw a rectangle to select a person.';
}

function reviewPoint(event) {
  const rect = reviewCanvas.getBoundingClientRect();
  return [Math.max(0, Math.min(reviewCanvas.width, Math.round(
    (event.clientX - rect.left) * reviewCanvas.width / rect.width))),
  Math.max(0, Math.min(reviewCanvas.height, Math.round(
    (event.clientY - rect.top) * reviewCanvas.height / rect.height)))];
}

async function openReviewBox(button) {
  const sample = button.closest('[data-id]');
  const id = sample.dataset.id;
  let image;
  try {
    image = new Image();
    image.src = button.dataset.image;
    await image.decode();
  } catch (e) { toast('Could not load review image: ' + e.message, 'error'); return; }
  reviewBoxSample = id;
  reviewBoxImage = image;
  reviewBox = JSON.parse(button.dataset.box);
  reviewCanvas.width = image.naturalWidth;
  reviewCanvas.height = image.naturalHeight;
  reviewDragStart = null;
  drawReviewBox();
  openModal($('review-box-modal'), $('review-box-save'));
}

reviewCanvas.addEventListener('pointerdown', (event) => {
  if (!reviewBoxImage) return;
  reviewCanvas.setPointerCapture(event.pointerId);
  reviewDragStart = reviewPoint(event);
  event.preventDefault();
});
reviewCanvas.addEventListener('pointermove', (event) => {
  if (!reviewDragStart) return;
  const p = reviewPoint(event);
  reviewBox = [Math.min(p[0], reviewDragStart[0]), Math.min(p[1], reviewDragStart[1]),
               Math.max(p[0], reviewDragStart[0]), Math.max(p[1], reviewDragStart[1])];
  drawReviewBox();
});
reviewCanvas.addEventListener('pointerup', (event) => {
  if (!reviewDragStart) return;
  const p = reviewPoint(event);
  reviewBox = [Math.min(p[0], reviewDragStart[0]), Math.min(p[1], reviewDragStart[1]),
               Math.max(p[0], reviewDragStart[0]), Math.max(p[1], reviewDragStart[1])];
  reviewDragStart = null;
  if (reviewBox[2] - reviewBox[0] < 32 || reviewBox[3] - reviewBox[1] < 32) {
    reviewBox = null;
    toast('Draw a box at least 32 × 32 pixels', 'error');
  }
  drawReviewBox();
});
reviewCanvas.addEventListener('pointercancel', () => { reviewDragStart = null; });
$('review-box-clear').onclick = () => { reviewBox = null; drawReviewBox(); };
$('review-box-save').onclick = async () => {
  if (!reviewBoxSample) return;
  const button = $('review-box-save');
  setBusy(button, true, 'Saving…');
  try {
    await api(`/api/training/samples/${encodeURIComponent(reviewBoxSample)}/box`, {
      method: 'PUT', body: JSON.stringify({ box: reviewBox }),
    });
    closeModal($('review-box-modal'));
    reviewBoxSample = null;
    toast('Person box saved');
    await loadReview();
  } catch (e) { toast('Could not save box: ' + e.message, 'error'); }
  finally { setBusy(button, false); }
};
$('review-upload-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = $('review-file').files[0];
  const camera = $('review-upload-camera').value;
  if (!camera || !file) { toast('Select a camera and a file', 'error'); return; }
  const btn = $('review-upload');
  setBusy(btn, true, 'Uploading…');
  try {
    const form = new FormData();
    form.append('camera_id', camera);
    form.append('file', file);
    const data = await uploadJson('/api/training/samples', form);
    $('review-file').value = '';
    toast(`Added ${data.count} image(s) for review`);
    await loadReview();
  } catch (e) { toast('Upload failed: ' + e.message, 'error'); }
  finally { setBusy(btn, false); }
});

async function loadTrainingStatus() {
  try {
    const state = await api('/api/training/status');
    let text = 'No training run yet.';
    if (state.state === 'running') text = 'Training in a separate process. Keep live feeds stopped; this may take several minutes.';
    if (state.state === 'failed') text = `Training failed: ${state.error}`;
    if (state.state === 'complete') {
      const base = state.baseline, next = state.candidate;
      text = `${state.train} training / ${state.held_out} held-out images. ` +
        `Base recall ${Math.round(base.recall * 100)}%, precision ${Math.round(base.precision * 100)}%; ` +
        `candidate recall ${Math.round(next.recall * 100)}%, precision ${Math.round(next.precision * 100)}%. ` +
        (state.eligible ? 'Candidate passed the image-level gate; review live clips before deploying.'
          : 'Candidate did not pass the gate and cannot be deployed.');
    }
    if (state.active_id) text += ' A trained model is deployed; stop/start cameras to apply changes.';
    $('training-report').textContent = text;
    $('training-start').disabled = state.state === 'running';
    $('training-deploy').hidden = !(state.eligible && state.id && state.active_id !== state.id);
    $('training-deploy').dataset.id = state.id || '';
    $('training-rollback').hidden = !state.active_id;
  } catch (e) { $('training-report').textContent = 'Training status unavailable: ' + e.message; }
}

$('training-start').onclick = async () => {
  if (!await confirmDialog({ title: 'Train a candidate?', message: 'Stop all live cameras first. Training does not replace the current model.' , confirmLabel: 'Train' })) return;
  try {
    await api('/api/training/start', { method: 'POST' });
    await loadTrainingStatus();
  } catch (e) { toast(e.message, 'error'); }
};
$('training-deploy').onclick = async () => {
  if (!await confirmDialog({ title: 'Deploy trained model?', message: 'Image-level validation is not a guarantee of CCTV event recall. Stop cameras, deploy, then start cameras to trial. You can roll back.', confirmLabel: 'Deploy' })) return;
  try {
    await api(`/api/training/deploy/${encodeURIComponent($('training-deploy').dataset.id)}`, { method: 'POST' });
    await loadTrainingStatus(); toast('Deployed. Start cameras to use the candidate.');
  } catch (e) { toast(e.message, 'error'); }
};
$('training-rollback').onclick = async () => {
  if (!await confirmDialog({ title: 'Roll back model?', message: 'Stop cameras first. Starting them again will load the original classifier.', confirmLabel: 'Roll back' })) return;
  try {
    await api('/api/training/rollback', { method: 'POST' });
    await loadTrainingStatus(); toast('Rolled back. Start cameras to use the original model.');
  } catch (e) { toast(e.message, 'error'); }
};

// ============================================================
//  Server badge & capabilities
// ============================================================

let detectionPolicy = null;

function detectionScope() {
  return $('detect-scope-exclusive').checked ? 'exclusive' : 'global';
}

function updateDetectionScope() {
  if (!detectionPolicy) return;
  const exclusive = detectionScope() === 'exclusive';
  $('detect-cameras').hidden = !exclusive;
  const selected = [...$('detect-cameras').querySelectorAll('input:checked')];
  const chosen = exclusive && selected.length === 1
    ? detectionPolicy.overrides?.[selected[0].value] : null;
  $('detect-person').checked = chosen?.person ?? detectionPolicy.global.person;
  $('detect-phone').checked = chosen?.phone ?? detectionPolicy.global.phone;
}

function renderDetectionSettings() {
  if (!detectionPolicy) return;
  const previous = [...$('detect-cameras').querySelectorAll('input:checked')]
    .map((input) => input.value);
  $('detect-cameras').innerHTML = savedCameras.length
    ? savedCameras.map((cam) => `
      <label class="check-row"><input type="checkbox" value="${escapeHtml(cam.id)}" ${previous.includes(cam.id) ? 'checked' : ''}>
        ${escapeHtml(cam.name)} ${detectionPolicy.overrides[cam.id] ? '(Exclusive override)' : ''}
      </label>`).join('')
    : '<p class="hint">Add cameras to use Exclusive settings.</p>';
  $('detect-effective').innerHTML = savedCameras.map((cam) => {
    const flags = detectionPolicy.effective?.[cam.id];
    if (!flags) return '';
    return `<span class="badge ${flags.person ? 'badge-saved' : 'badge-warn'}">
      ${escapeHtml(cam.name)}: person ${flags.person ? 'On' : 'Off'}, phone-use ${flags.phone ? 'On' : 'Off'}
    </span>`;
  }).join(' ') || '<span class="hint">No saved cameras.</span>';
  updateDetectionScope();
  renderCameras();
}

async function loadDetectionSettings() {
  try {
    detectionPolicy = await api('/api/detection/settings');
    renderDetectionSettings();
  } catch (e) { toast('Could not load detection settings: ' + e.message, 'error'); }
}

$('detect-scope-global').onchange = updateDetectionScope;
$('detect-scope-exclusive').onchange = updateDetectionScope;
$('detect-cameras').addEventListener('change', updateDetectionScope);
$('detect-save').onclick = async () => {
  const scope = detectionScope();
  const ids = [...$('detect-cameras').querySelectorAll('input:checked')]
    .map((input) => input.value);
  if (scope === 'exclusive' && !ids.length) {
    toast('Select at least one saved camera', 'error'); return;
  }
  if (scope === 'global' && Object.keys(detectionPolicy?.overrides || {}).length &&
      !await confirmDialog({ title: 'Replace Exclusive settings?',
        message: 'Saving Global settings removes all camera-specific detection overrides.',
        confirmLabel: 'Apply globally' })) return;
  const button = $('detect-save');
  setBusy(button, true, 'Saving…');
  try {
    detectionPolicy = await api('/api/detection/settings', {
      method: 'PUT', body: JSON.stringify({ scope,
        person: $('detect-person').checked, phone: $('detect-phone').checked,
        camera_ids: scope === 'exclusive' ? ids : [] }),
    });
    renderDetectionSettings();
    toast(scope === 'global' ? 'Detection settings applied globally'
      : `Detection settings applied to ${ids.length} camera(s)`);
  } catch (e) { toast('Could not save detection settings: ' + e.message, 'error'); }
  finally { setBusy(button, false); }
};

async function checkServer() {
  const badge = $('server-badge');
  try {
    await api('/api/status');
    badge.classList.remove('offline');
    $('server-label').textContent = 'Server online';
  } catch {
    badge.classList.add('offline');
    $('server-label').textContent = 'Server offline';
  }
}

async function loadCapabilities() {
  try {
    const caps = await api('/api/capabilities');
    for (const id of ['device-select', 'edit-device']) {
      const sel = $(id);
      const current = sel.value;
      sel.innerHTML = '<option value="auto">Auto (recommended)</option>';
      for (const c of caps) {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.disabled = c.status !== 'ready';
        opt.textContent = c.status === 'ready'
          ? `${c.name} (${c.detail})`
          : `${c.name} — unavailable`;
        sel.appendChild(opt);
      }
      if ([...sel.options].some((o) => o.value === current && !o.disabled)) {
        sel.value = current;
      }
    }
    renderSetup();
  } catch (e) {
    log('Failed to load compute devices: ' + e.message);
  }
}

// ============================================================
//  Camera list & setup panel
// ============================================================

function selectedCamera() {
  return savedCameras.find((c) => c.id === selectedId) || null;
}

async function loadCameras({ preserveSelection = true } = {}) {
  try {
    savedCameras = await api('/api/cameras');
    if (!preserveSelection || !savedCameras.some((c) => c.id === selectedId)) {
      selectedId = savedCameras.length ? savedCameras[0].id : null;
    }
    renderCameras();
    renderSetup();
    if (isAdminUser() && viewFromHash() === 'cameras') loadDetectionSettings();
  } catch (e) {
    log('Failed to load cameras: ' + e.message);
    toast('Could not load cameras: ' + e.message, 'error');
  }
}

const DISCOVERY_STATUS = {
  ready: { label: 'video OK', cls: 'badge-saved' },
  credentials: { label: 'needs login', cls: 'badge-warn' },
  no_video: { label: 'no video', cls: 'badge-new' },
  error: { label: 'error', cls: 'badge-new' },
};

const DISCOVERY_KIND = {
  camera: 'Camera',
  nvr: 'NVR',
  onvif: 'ONVIF device',
  encoder: 'Encoder',
  rtsp: 'RTSP device',
};

function discoveryBadges(cam) {
  const kind = DISCOVERY_KIND[cam.kind] || cam.kind || '';
  const status = DISCOVERY_STATUS[cam.status] || DISCOVERY_STATUS.error;
  const channels = (cam.channels || []).length;
  const detail = cam.detail || '';
  const bits = [
    kind ? `<span class="badge badge-kind">${escapeHtml(kind)}</span>` : '',
    `<span class="badge ${status.cls}" title="${escapeHtml(detail)}">${escapeHtml(status.label)}</span>`,
  ];
  if (channels > 1) {
    const ok = cam.channels.filter((c) => c.accessible).length;
    bits.push(`<span class="badge">${ok}/${channels} ch</span>`);
  }
  return bits.join('');
}

function cameraRow(cam, saved) {
  const isSelected = saved ? cam.id === selectedId : cam.id === selectedDiscoveredId;
  const isLive = saved && feeds.has(cam.id);
  const badge = saved
    ? '<span class="badge badge-saved">saved</span>'
    : (cam.saved
      ? '<span class="badge badge-saved">saved</span>'
      : '<span class="badge badge-new">new</span>');
  const actions = saved && isAdminUser()
    ? `<button class="btn-mini" data-action="edit-camera" data-id="${escapeHtml(cam.id)}">Edit</button>
       <button class="btn-mini btn-mini-danger" data-action="delete-camera" data-id="${escapeHtml(cam.id)}">Delete</button>`
    : '';

  return `<div class="cam${isSelected ? ' sel' : ''}" data-id="${escapeHtml(cam.id)}"
                data-saved="${saved}" role="button" tabindex="0" aria-pressed="${isSelected}">
    <div class="cam-main">
      <div class="cam-name-row">
        <b class="cam-name" title="${escapeHtml(cam.name)}">${escapeHtml(cam.name)}</b>
        ${isLive ? '<span class="badge badge-live">live</span>' : ''}
        ${saved && cam.role ? `<span class="badge badge-kind">${escapeHtml(cam.role === 'entrance' ? 'Entrance' : 'Exit')}</span>` : ''}
        ${saved && cam.count_customer ? '<span class="badge badge-customer">Customer</span>' : ''}
        ${saved && detectionPolicy?.effective?.[cam.id]?.person === false ? '<span class="badge badge-warn">person off</span>'
          : saved && detectionPolicy?.effective?.[cam.id]?.phone === false ? '<span class="badge badge-warn">phone-use off</span>' : ''}
        ${saved ? '' : discoveryBadges(cam)}
      </div>
      <small title="${escapeHtml(displayAddress(cam.address))}">${escapeHtml(displayAddress(cam.address))}</small>
    </div>
    <div class="cam-actions">${badge}${actions}</div>
  </div>`;
}

function renderCameras() {
  const box = $('results');
  const q = cameraFilter.trim().toLowerCase();
  const matches = (c) => !q || `${c.name} ${c.address}`.toLowerCase().includes(q);

  const saved = savedCameras.filter(matches);
  const found = discoveredCameras.filter(matches);

  $('camera-count').textContent = String(savedCameras.length + discoveredCameras.length);

  if (!saved.length && !found.length) {
    if (q) {
      box.innerHTML = `<div class="empty-state empty-state-sm">
        <p>No cameras match “${escapeHtml(cameraFilter)}”.</p>
        <button class="btn-mini" data-action="clear-camera-filter">Clear filter</button>
      </div>`;
    } else {
      box.innerHTML = `<div class="empty-state empty-state-sm">
        <div class="empty-icon" aria-hidden="true">📹</div>
        <p>No cameras yet.${isAdminUser() ? '<br>Discover devices on your network or add a stream URL.' : ''}</p>
        ${isAdminUser() ? '<button class="btn-mini" data-action="discover">Discover devices</button>' : ''}
      </div>`;
    }
    return;
  }

  box.innerHTML = [
    ...saved.map((c) => cameraRow(c, true)),
    ...found.map((c) => cameraRow(c, false)),
  ].join('');
}

// Select a saved or discovered device by clicking its row.
$('results').addEventListener('click', (e) => {
  if (e.target.closest('button, [data-action]')) return;
  const row = e.target.closest('.cam');
  if (!row) return;
  if (row.dataset.saved !== 'true') {
    if (selectedDiscoveredId !== row.dataset.id) {
      $('discovered-user').value = '';
      $('discovered-pass').value = '';
    }
    selectedDiscoveredId = row.dataset.id;
    renderCameras();
    renderDiscoveredSetup();
    if (window.innerWidth <= 1100) {
      $('discovered-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    return;
  }
  if (selectedId === row.dataset.id) return;
  selectedId = row.dataset.id;
  renderCameras();
  renderSetup();
  if (window.innerWidth <= 1100) {
    $('selected-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
});

$('results').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  if (e.target.matches('.cam')) {
    e.preventDefault();
    e.target.click();
  }
});

function renderDiscoveredSetup() {
  const device = discoveredCameras.find((d) => d.id === selectedDiscoveredId);
  const panel = $('discovered-panel');
  panel.hidden = !device || device.saved;
  if (!device || device.saved) return;
  $('discovered-name').textContent = device.name;
  $('discovered-detail').textContent = device.verified
    ? 'Video is publicly accessible. Enter a login only if this device requires one.'
    : 'This endpoint answered without a login. Enter its credentials to connect and confirm video.';
}

function renderSetup() {
  const cam = selectedCamera();

  $('no-selection').hidden = !!cam;
  $('selected-panel').hidden = !cam;
  if (!cam) return;

  $('selected-name').textContent = cam.name;
  $('selected-name').title = cam.name;
  $('selected-address').textContent = displayAddress(cam.address);
  $('selected-address').title = displayAddress(cam.address);

  const devSel = $('device-select');
  const canUseSaved = [...devSel.options].some(
    (o) => o.value === (cam.device || 'auto') && !o.disabled);
  devSel.value = canUseSaved ? (cam.device || 'auto') : 'auto';
  $('fps').value = cam.fps || 10;

  const needsCreds = cam.source_type === 'onvif' && !cam.rtsp_url;
  $('creds').hidden = !needsCreds;
  if (needsCreds) {
    $('onvif-user').value = cam.onvif_user || '';
    $('onvif-pass').value = cam.onvif_pass || '';
  }

  $('find-feeds').hidden = cam.source_type !== 'onvif';

  const chans = $('channels');
  if (chans.dataset.camId !== cam.id) {
    chans.hidden = true;
    chans.innerHTML = '';
    chans.dataset.camId = '';
    channelProfiles = [];
  }

  const live = feeds.has(cam.id);
  const run = $('run');
  run.textContent = live ? 'Stop CV Model' : 'Run CV Model';
  run.classList.toggle('btn-success', !live);
  run.classList.toggle('btn-danger', live);

  const chip = $('run-status');
  chip.textContent = live ? 'Running' : 'Stopped';
  chip.className = `chip ${live ? 'chip-live' : 'chip-idle'}`;
}

// ============================================================
//  Discovery / manual add / CRUD
// ============================================================

function streamCredentials(url) {
  if (url) {
    try {
      const parsed = new URL(url);
      if (parsed.username) {
        return {
          username: decodeURIComponent(parsed.username),
          password: decodeURIComponent(parsed.password),
        };
      }
    } catch { /* A malformed URL will be rejected when it is added. */ }
  }
  return {
    username: $('discovered-user').value.trim(),
    password: $('discovered-pass').value,
  };
}

function renderDiscoverySummary(report) {
  const box = $('discover-summary');
  const nets = (report.networks || []).join(', ');
  const seconds = ((report.duration_ms || 0) / 1000).toFixed(1);
  const lines = [
    `<b>${report.scanned_hosts || 0} host(s)</b> scanned in ${seconds}s` +
      (nets ? ` · ${escapeHtml(nets)}` : ''),
    ...(report.warnings || []).map(
      (w) => `<span class="discover-warning">${escapeHtml(w)}</span>`),
  ];
  box.innerHTML = lines.join('<br>');
  box.hidden = false;
}

async function runDiscovery() {
  if (discoveryBusy) return;
  discoveryBusy = true;
  document.querySelectorAll('[data-action="discover"]').forEach(
    (b) => setBusy(b, true, 'Scanning…'));

  const subnets = $('discover-subnets').value
    .split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);

  try {
    const report = await api('/api/discover', {
      method: 'POST',
      body: JSON.stringify({
        subnets,
        deep: $('discover-deep').checked,
        include_saved: true,
      }),
    });
    discoveryReport = report;
    discoveredCameras = report.devices || [];
    selectedDiscoveredId = null;
    $('discovered-user').value = '';
    $('discovered-pass').value = '';
    renderCameras();
    renderDiscoveredSetup();
    renderDiscoverySummary(report);

    const ready = discoveredCameras.filter(
      (d) => d.status === 'ready' && !d.saved).length;
    const locked = discoveredCameras.filter(
      (d) => d.status === 'credentials' && !d.saved).length;
    if (discoveredCameras.length) {
      toast(
        `Found ${discoveredCameras.length} device endpoint(s): ${ready} publicly accessible` +
        (locked ? `, ${locked} need a login` : '') + '.',
        ready ? 'success' : 'info');
      log(`Discovery found ${discoveredCameras.length} endpoint(s) ` +
          `(${ready} publicly accessible, ${locked} needing credentials)`);
    } else {
      toast('No ONVIF or RTSP device found on the scanned networks.', 'info');
      log('Discovery found no ONVIF/RTSP device');
    }
    for (const warning of report.warnings || []) log('Discovery: ' + warning);
  } catch (e) {
    toast('Discovery failed: ' + e.message, 'error');
    log('Discovery failed: ' + e.message);
  } finally {
    discoveryBusy = false;
    document.querySelectorAll('[data-action="discover"]').forEach(
      (b) => setBusy(b, false));
  }
}

async function addManualUrl() {
  const input = $('manual-url');
  const url = input.value.trim();
  if (!url) {
    input.focus();
    toast('Enter a stream URL first.', 'error');
    return;
  }
  if (!/^(https?|rtsp):\/\//i.test(url)) {
    input.focus();
    toast('The URL must start with http://, https:// or rtsp://', 'error');
    return;
  }

  let host = 'Stream';
  try { host = new URL(url).host || host; } catch { /* keep default */ }
  const isRtsp = /^rtsp:\/\//i.test(url);
  const btn = $('add-manual');
  setBusy(btn, true, 'Adding…');

  try {
    const cam = await api('/api/cameras', {
      method: 'POST',
      body: JSON.stringify({
        name: `Manual: ${host}`,
        address: url,
        rtsp_url: url,
        source_type: isRtsp ? 'manual-rtsp' : 'mobile',
      }),
    });
    input.value = '';
    selectedId = cam.id;
    await loadCameras();
    if (window.innerWidth <= 1100) $('selected-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    toast(`Added ${cam.name}`, 'success');
    log(`Added camera: ${cam.name}`);
  } catch (e) {
    toast('Could not add camera: ' + e.message, 'error');
  } finally {
    setBusy(btn, false);
  }
}

async function addDiscovered(btn) {
  const deviceId = selectedDiscoveredId;
  let device = discoveredCameras.find((c) => c.id === deviceId);
  if (!device) return;
  const entered = streamCredentials(null);
  if (!device.verified && !entered.username && !entered.password) {
    toast('Enter this device’s username and password first.', 'info');
    $('discovered-user').focus();
    return;
  }

  setBusy(btn, true, 'Connecting…');
  try {
    if (!device.verified || entered.username || entered.password) {
      device = await api('/api/discover/connect', {
        method: 'POST',
        body: JSON.stringify({
          address: device.address,
          username: entered.username,
          password: entered.password,
        }),
      });
    }
    if (!device.verified || !device.rtsp_url) {
      toast('No video frame could be confirmed. Check this device’s login.', 'error');
      return;
    }
    const channels = (device.channels || []).filter(
      (c) => c.accessible && c.rtsp_url);
    const entries = channels.length > 1
      ? channels.map((channel) => ({
        name: `${device.name} – ${channel.name}`,
        address: `${device.address}#${channel.token}`,
        rtsp_url: channel.rtsp_url,
        source_type: 'onvif',
        ...streamCredentials(channel.rtsp_url),
      }))
      : [{
        name: device.name,
        address: device.address,
        rtsp_url: device.rtsp_url,
        source_type: device.source_type || 'onvif',
        ...streamCredentials(device.rtsp_url),
      }];
    let last = null;
    let added = 0;
    for (const entry of entries) {
      try {
        last = await api('/api/cameras', {
          method: 'POST',
          body: JSON.stringify({
            name: entry.name,
            address: entry.address,
            rtsp_url: entry.rtsp_url,
            source_type: entry.source_type,
            onvif_user: entry.username,
            onvif_pass: entry.password,
          }),
        });
        added += 1;
      } catch (e) {
        // A duplicate address means the channel is already saved; keep going.
        if (!/exist|unique|duplicate/i.test(e.message)) throw e;
      }
    }
    discoveredCameras = discoveredCameras.filter((c) => c.id !== deviceId);
    selectedDiscoveredId = null;
    if (last) selectedId = last.id;
    await loadCameras();
    renderDiscoveredSetup();
    if (window.innerWidth <= 1100) $('selected-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    toast(`Added ${added} camera${added === 1 ? '' : 's'}.`, 'success');
    log(`Added ${added} camera(s) from ${device.name}`);
  } catch (e) {
    toast('Could not connect to device: ' + e.message, 'error');
  } finally {
    const stillThere = document.body.contains(btn);
    if (stillThere) setBusy(btn, false);
  }
}

async function deleteCamera(id) {
  const cam = savedCameras.find((c) => c.id === id);
  if (!cam) return;

  const ok = await confirmDialog({
    title: 'Delete camera?',
    message: `“${cam.name}” will be removed. Any analysis running for it will stop.`,
    confirmLabel: 'Delete camera',
  });
  if (!ok) return;

  try {
    await api(`/api/cameras/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (feeds.has(id)) await stopFeed(id, { silent: true });
    if (selectedId === id) selectedId = null;
    await loadCameras({ preserveSelection: false });
    toast('Camera deleted.', 'success');
    log(`Deleted camera: ${cam.name}`);
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

// ---------- Edit camera modal ----------

let editingLine = null;
let editingCameraFrame = null;
let editLineStart = null;
let editLineBeforeDrag = null;
const editLineCanvas = $('edit-line-canvas');

function updateCameraRoleEditor() {
  const active = Boolean($('edit-role').value);
  $('edit-tripwire-direction').disabled = !active;
  $('edit-count-customer').disabled = !active;
  $('edit-count-customer-row').classList.toggle('is-disabled', !active);
  $('edit-line-load').hidden = !active;
  $('edit-line-area').hidden = !active || !editingCameraFrame;
  $('edit-line-pending').hidden = !active || !editingLine || Boolean(editingCameraFrame);
  drawCameraLine();
}

function drawCameraLine() {
  if (!editingCameraFrame) return;
  const ctx = editLineCanvas.getContext('2d');
  const w = editLineCanvas.width, h = editLineCanvas.height;
  ctx.drawImage(editingCameraFrame, 0, 0, w, h);
  if (!editingLine) {
    $('edit-line-status').textContent = 'Drag across the doorway to draw a line from A to B.';
    return;
  }
  const [ax, ay, bx, by] = editingLine;
  const dx = bx - ax, dy = by - ay;
  const len = Math.hypot(dx, dy) || .001;
  ctx.strokeStyle = '#ffd200'; ctx.lineWidth = Math.max(2, w / 350);
  ctx.beginPath(); ctx.moveTo(ax * w, ay * h); ctx.lineTo(bx * w, by * h); ctx.stroke();
  ctx.fillStyle = '#ffd200'; ctx.font = 'bold 18px sans-serif';
  ctx.fillText('A', ax * w + 6, ay * h - 6);
  ctx.fillText('B', bx * w + 6, by * h - 6);
  const midX = (ax + bx) / 2, midY = (ay + by) / 2;
  const sides = [[midX - dy / len * .05, midY + dx / len * .05],
                 [midX + dy / len * .05, midY - dx / len * .05]];
  sides.forEach(([x, y], i) => {
    ctx.beginPath(); ctx.arc(x * w, y * h, 7, 0, 2 * Math.PI);
    ctx.fillStyle = i ? '#0082ff' : '#ffd200'; ctx.fill();
    ctx.strokeStyle = '#111'; ctx.lineWidth = 2; ctx.stroke();
  });
  const [src, dst] = $('edit-tripwire-direction').value === 'positive_to_negative'
    ? [0, 1] : [1, 0];
  const from = sides[src], to = sides[dst];
  const vx = (to[0] - from[0]) * w, vy = (to[1] - from[1]) * h;
  const angle = Math.atan2(vy, vx), tipX = to[0] * w, tipY = to[1] * h;
  ctx.strokeStyle = '#16ff68'; ctx.lineWidth = 4;
  ctx.beginPath(); ctx.moveTo(from[0] * w, from[1] * h);
  ctx.lineTo(tipX, tipY);
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - 14 * Math.cos(angle - .55), tipY - 14 * Math.sin(angle - .55));
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - 14 * Math.cos(angle + .55), tipY - 14 * Math.sin(angle + .55));
  ctx.stroke();
  $('edit-line-status').textContent = `Alerts for ${$('edit-tripwire-direction').selectedOptions[0].textContent}.`;
}

function editLinePoint(event) {
  const rect = editLineCanvas.getBoundingClientRect();
  return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
          Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))];
}

editLineCanvas.addEventListener('pointerdown', (event) => {
  if (!editingCameraFrame) return;
  editLineCanvas.setPointerCapture(event.pointerId);
  editLineStart = editLinePoint(event);
  editLineBeforeDrag = editingLine;
  event.preventDefault();
});
editLineCanvas.addEventListener('pointermove', (event) => {
  if (!editLineStart) return;
  editingLine = [...editLineStart, ...editLinePoint(event)];
  drawCameraLine();
});
editLineCanvas.addEventListener('pointerup', (event) => {
  if (!editLineStart) return;
  editingLine = [...editLineStart, ...editLinePoint(event)];
  editLineStart = null;
  if (Math.hypot(editingLine[2] - editingLine[0],
                 editingLine[3] - editingLine[1]) < .08) {
    editingLine = editLineBeforeDrag;
    toast('Draw a longer line across the doorway', 'error');
  }
  drawCameraLine();
});
editLineCanvas.addEventListener('pointercancel', () => {
  editLineStart = null; editingLine = editLineBeforeDrag; drawCameraLine();
});
$('edit-line-clear').onclick = () => { editingLine = null; drawCameraLine(); };
$('edit-role').onchange = updateCameraRoleEditor;
$('edit-tripwire-direction').onchange = drawCameraLine;
$('edit-line-load').onclick = async () => {
  if (!editingId) return;
  const cameraId = editingId;
  const button = $('edit-line-load');
  setBusy(button, true, 'Loading frame…');
  try {
    const response = await fetch(`/api/cameras/${encodeURIComponent(cameraId)}/snapshot`,
                                 { cache: 'no-store' });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.detail || 'Camera frame unavailable');
    }
    const url = URL.createObjectURL(await response.blob());
    const image = new Image();
    try { image.src = url; await image.decode(); }
    finally { URL.revokeObjectURL(url); }
    if (editingId !== cameraId || $('camera-modal').hidden) return;
    editingCameraFrame = image;
    editLineCanvas.width = image.naturalWidth;
    editLineCanvas.height = image.naturalHeight;
    updateCameraRoleEditor();
  } catch (e) { toast('Could not load camera frame: ' + e.message, 'error'); }
  finally { setBusy(button, false); }
};

function openCameraModal(id) {
  const cam = savedCameras.find((c) => c.id === id);
  if (!cam) return;

  editingId = id;
  $('edit-name').value = cam.name;
  $('edit-address').value = cam.address;
  $('edit-rtsp').value = cam.rtsp_url || '';
  $('edit-source').value = cam.source_type || 'onvif';
  $('edit-user').value = cam.onvif_user || '';
  $('edit-pass').value = cam.onvif_pass || '';
  $('edit-device').value = cam.device || 'auto';
  $('edit-fps').value = cam.fps || 10;
  $('edit-phone-conf').value = cam.phone_conf != null ? cam.phone_conf : 0.25;
  $('edit-phone-pitch').value = cam.phone_pitch != null ? cam.phone_pitch : 0.70;
  $('edit-role').value = cam.role || '';
  $('edit-tripwire-direction').value = cam.tripwire_direction || 'positive_to_negative';
  $('edit-count-customer').checked = Boolean(cam.count_customer);
  editingLine = cam.tripwire ? [...cam.tripwire] : null;
  editingCameraFrame = null;
  updateCameraRoleEditor();
  openModal($('camera-modal'), $('edit-name'));
}

async function saveCamera() {
  if (!editingId) return;

  const body = {
    name: $('edit-name').value.trim(),
    address: $('edit-address').value.trim(),
    rtsp_url: $('edit-rtsp').value.trim() || null,
    source_type: $('edit-source').value,
    onvif_user: $('edit-user').value,
    onvif_pass: $('edit-pass').value,
    device: $('edit-device').value,
    fps: Math.max(1, Math.min(parseInt($('edit-fps').value, 10) || 10, 60)),
    phone_conf: Math.min(0.95, Math.max(0.02, parseFloat($('edit-phone-conf').value) || 0.25)),
    phone_pitch: Math.min(0.95, Math.max(0.1, parseFloat($('edit-phone-pitch').value) || 0.70)),
    role: $('edit-role').value || null,
    tripwire: $('edit-role').value ? editingLine : null,
    tripwire_direction: $('edit-role').value ? $('edit-tripwire-direction').value : null,
    count_customer: Boolean($('edit-role').value && $('edit-count-customer').checked),
  };

  if (!body.name || !body.address) {
    toast('Name and address are required.', 'error');
    return;
  }
  if (body.role && !body.tripwire) {
    toast('Load a camera frame and draw a line before assigning a role.', 'error');
    return;
  }

  const btn = $('edit-save');
  setBusy(btn, true, 'Saving…');
  try {
    await api(`/api/cameras/${encodeURIComponent(editingId)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    closeModal($('camera-modal'));
    editingId = null;
    await loadCameras();
    toast('Camera updated.', 'success');
    log(`Updated camera: ${body.name}`);
  } catch (e) {
    toast('Save failed: ' + e.message, 'error');
  } finally {
    setBusy(btn, false);
  }
}

// ============================================================
//  Live feeds
// ============================================================

let statsTimer = null;

function ensureStatsPolling() {
  if (statsTimer) return;
  statsTimer = setInterval(refreshFeedStats, 2000);
  refreshFeedStats();
}

function setFeedStatus(entry, state, label) {
  const el = entry?.cardEl.querySelector('[data-role="status"]');
  if (!el) return;
  el.className = `feed-status ${state}`;
  el.textContent = label;
}

function addFeedCard(cameraId, name, device, fps) {
  if (feeds.has(cameraId)) return feeds.get(cameraId);

  const container = $('feeds');
  container.querySelector('.empty-state')?.remove();

  const card = document.createElement('div');
  card.className = 'feed';
  card.innerHTML = `
    <div class="feed-head">
      <div class="feed-title">
        <b title="${escapeHtml(name)}">${escapeHtml(name)}</b>
        <small>${escapeHtml(device || 'auto')} · ${Number(fps) || 10} fps</small>
      </div>
      <div class="feed-tools">
        <span class="feed-status connecting" data-role="status">Connecting…</span>
        ${isAdminUser() ? '<button class="btn-mini" data-act="stop-feed" type="button">Stop</button>' : ''}
      </div>
    </div>
    <video autoplay playsinline muted></video>
    <div class="feed-foot"><span class="feed-stats">waiting for frames…</span></div>`;

  card.querySelector('[data-act="stop-feed"]')?.addEventListener(
    'click', () => stopFeed(cameraId));
  container.appendChild(card);

  const entry = {
    cardEl: card,
    videoEl: card.querySelector('video'),
    pc: null,
    retries: 0,
  };
  feeds.set(cameraId, entry);

  ensureStatsPolling();
  renderCameras();
  renderSetup();
  return entry;
}

async function connectWebRTC(cameraId) {
  const entry = feeds.get(cameraId);
  if (!entry) return;

  if (entry.pc) {
    try { await entry.pc.close(); } catch { /* already closed */ }
  }

  const pc = new RTCPeerConnection();
  entry.pc = pc;
  setFeedStatus(entry, 'connecting', 'Connecting…');

  pc.ontrack = (ev) => {
    if (feeds.get(cameraId)?.pc !== pc) return;
    const video = entry.videoEl;
    video.srcObject = ev.streams[0];
    // Keep the browser's receive buffer small (Chromium honours these).
    try { ev.receiver.jitterBufferTarget = 0; } catch { /* unsupported */ }
    try { ev.receiver.playoutDelayHint = 0; } catch { /* unsupported */ }
    video.play().catch(() => { /* autoplay blocked */ });
    setFeedStatus(entry, 'live', 'Live');
    entry.retries = 0;
  };

  pc.onconnectionstatechange = () => {
    if (feeds.get(cameraId)?.pc !== pc) return;
    const state = pc.connectionState;

    if (state === 'connected') {
      setFeedStatus(entry, 'live', 'Live');
      entry.retries = 0;
    } else if (state === 'connecting' || state === 'new') {
      setFeedStatus(entry, 'connecting', 'Connecting…');
    } else if (state === 'disconnected') {
      setFeedStatus(entry, 'connecting', 'Reconnecting…');
    } else if (state === 'failed') {
      if (entry.retries >= 10) {
        setFeedStatus(entry, 'error', 'Offline');
        toast('Live video connection failed. Press Stop, then Run again.', 'error');
        return;
      }
      entry.retries += 1;
      const delay = Math.min(1000 * entry.retries, 5000);
      setFeedStatus(entry, 'error', 'Reconnecting…');
      log(`Feed ${cameraId}: connection failed — retry ${entry.retries} in ${delay / 1000}s`);
      setTimeout(() => {
        if (feeds.get(cameraId)?.pc === pc) connectWebRTC(cameraId);
      }, delay);
    }
  };

  pc.addTransceiver('video', { direction: 'recvonly' });

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    const res = await api(`/api/webrtc/offer/${encodeURIComponent(cameraId)}`, {
      method: 'POST',
      body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
    });
    if (res.error) throw new Error(res.error);
    await pc.setRemoteDescription({ sdp: res.sdp, type: res.type });
    return true;
  } catch (e) {
    setFeedStatus(entry, 'error', 'Offline');
    log(`WebRTC ${cameraId}: ${e.message}`);
    return false;
  }
}

async function refreshFeedStats() {
  if (!feeds.size) {
    clearInterval(statsTimer);
    statsTimer = null;
    return;
  }

  try {
    const { active } = await api('/api/status');
    const seen = new Set();

    for (const s of active) {
      const entry = feeds.get(s.camera_id);
      if (!entry) continue;
      seen.add(s.camera_id);

      const el = entry.cardEl.querySelector('.feed-stats');
      if (el) {
        const parts = [
          `src ${fmtFps(s.source_fps)} fps`,
          `shown ${fmtFps(s.publish_fps)} fps`,
          `infer ${Math.round(Number(s.inference_ms) || 0)} ms`,
          `phones ${Number(s.phones) || 0}`,
        ];
        if (s.phone_use) parts.push(`in use ${s.phone_use}`);
        if (!s.person_detection_enabled) parts.push('person detection off');
        else if (!s.phone_use_detection_enabled) parts.push('phone-use off');
        if (s.display_age_ms != null) {
          parts.push(`age ${Math.round(Number(s.display_age_ms))} ms`);
        }
        el.textContent = parts.join(' · ');
      }

      if (!s.connected) {
        setFeedStatus(entry, 'error', 'Camera offline');
      } else if (entry.pc && entry.pc.connectionState === 'connected') {
        setFeedStatus(entry, 'live', 'Live');
      }
    }

    for (const [id, entry] of feeds) {
      if (seen.has(id)) continue;
      const el = entry.cardEl.querySelector('.feed-stats');
      if (el) el.textContent = 'waiting for the pipeline…';
    }
  } catch { /* server briefly unreachable — keep the last values */ }
}

async function stopFeed(cameraId, opts = {}) {
  const entry = feeds.get(cameraId);
  if (!entry) return;

  feeds.delete(cameraId);
  if (entry.pc) {
    try { await entry.pc.close(); } catch { /* already closed */ }
  }
  entry.videoEl.srcObject = null;
  entry.cardEl.remove();

  if (!opts.silent) {
    try {
      await api(`/api/stop/${encodeURIComponent(cameraId)}`, { method: 'POST' });
    } catch { /* pipeline may already be gone */ }
    const cam = savedCameras.find((c) => c.id === cameraId);
    toast(`Stopped ${cam ? cam.name : 'stream'}.`, 'info');
    log(`Stopped pipeline: ${cam ? cam.name : cameraId}`);
  }

  if (!feeds.size) {
    $('feeds').innerHTML =
      '<div class="empty-state empty-state-sm">No active streams. Select a camera and click Run CV Model.</div>';
  }

  renderCameras();
  renderSetup();
}

async function toggleRun(btn) {
  const cam = selectedCamera();
  if (!cam) {
    toast('Select a camera first.', 'error');
    return;
  }

  if (feeds.has(cam.id)) {
    await stopFeed(cam.id);
    return;
  }

  setBusy(btn, true, 'Starting…');
  try {
    if (cam.source_type === 'onvif' && !cam.rtsp_url) {
      const user = $('onvif-user').value;
      const pass = $('onvif-pass').value;
      if (user !== cam.onvif_user || pass !== cam.onvif_pass) {
        await api(`/api/cameras/${encodeURIComponent(cam.id)}`, {
          method: 'PUT',
          body: JSON.stringify({ onvif_user: user, onvif_pass: pass }),
        });
        cam.onvif_user = user;
        cam.onvif_pass = pass;
      }
      const r = await api(`/api/cameras/${encodeURIComponent(cam.id)}/rtsp`, {
        method: 'POST',
      });
      cam.rtsp_url = r.rtsp_url;
      log(`${cam.name}: resolved RTSP URL`);
    }

    const device = $('device-select').value;
    const fps = Math.max(1, Math.min(parseInt($('fps').value, 10) || 10, 60));

    await api('/api/start', {
      method: 'POST',
      body: JSON.stringify({ camera_id: cam.id, device, fps }),
    });

    const entry = addFeedCard(cam.id, cam.name, device, fps);
    log(`Started pipeline: ${cam.name} (${device}, ${fps} fps)`);
    toast(`Running ${cam.name}.`, 'success');

    const connected = await connectWebRTC(cam.id);
    if (!connected) toast('The pipeline started, but live video could not connect. Try Stop, then Run again.', 'error');
    entry.cardEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    await loadCameras();
  } catch (e) {
    log('Start failed: ' + e.message);
    toast('Could not start: ' + e.message, 'error');
  } finally {
    setBusy(btn, false);
    renderSetup();
  }
}

// ============================================================
//  ONVIF channel check
// ============================================================

async function findFeeds(btn) {
  const cam = selectedCamera();
  if (!cam) return;

  const user = $('onvif-user').value;
  const pass = $('onvif-pass').value;
  setBusy(btn, true, 'Checking feeds…');

  try {
    await api(`/api/cameras/${encodeURIComponent(cam.id)}`, {
      method: 'PUT',
      body: JSON.stringify({ onvif_user: user, onvif_pass: pass }),
    });
    cam.onvif_user = user;
    cam.onvif_pass = pass;

    const profiles = await api(`/api/cameras/${encodeURIComponent(cam.id)}/profiles`, {
      method: 'POST',
      body: JSON.stringify({ onvif_user: user, onvif_pass: pass }),
    });

    channelProfiles = profiles;
    if (selectedId === cam.id) renderChannels(cam, profiles);

    const ok = profiles.filter((p) => p.accessible).length;
    log(`${cam.name}: ${ok}/${profiles.length} feed(s) accessible`);
    toast(
      ok ? `${ok} of ${profiles.length} feed(s) responded.` : 'None of the feeds responded — check the credentials.',
      ok ? 'success' : 'error',
    );
  } catch (e) {
    toast('Feed check failed: ' + e.message, 'error');
    log('Feed check failed: ' + e.message);
  } finally {
    setBusy(btn, false);
  }
}

function renderChannels(cam, profiles) {
  const box = $('channels');
  box.dataset.camId = cam.id;
  box.hidden = false;

  if (!profiles.length) {
    box.innerHTML = '<div class="empty-state empty-state-sm">This device reports no media profiles.</div>';
    return;
  }

  box.innerHTML = profiles.map((p) => {
    const address = `${cam.address}#${p.token}`;
    const added = savedCameras.some((c) => c.address === address);
    const size = p.width ? `${p.width}×${p.height}` : '';
    const badge = p.accessible
      ? '<span class="badge badge-saved">video OK</span>'
      : `<span class="badge badge-new" title="${escapeHtml(p.error || 'No video')}">no video</span>`;

    let action = '';
    if (added) action = '<span class="badge">added</span>';
    else if (p.accessible) {
      action = `<button class="btn-mini btn-mini-primary" data-action="add-channel" data-token="${escapeHtml(p.token)}">Add</button>`;
    }

    return `<div class="channel">
      <div>
        <b title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</b>
        <small>${escapeHtml(size)} ${escapeHtml(p.encoding || '')}</small>
      </div>
      <div>${badge}${action}</div>
    </div>`;
  }).join('');
}

async function addChannel(btn) {
  const box = $('channels');
  const cam = savedCameras.find((c) => c.id === box.dataset.camId);
  const profile = channelProfiles.find((p) => p.token === btn.dataset.token);
  if (!cam || !profile || !profile.rtsp_url) return;

  setBusy(btn, true, 'Adding…');
  try {
    const created = await api('/api/cameras', {
      method: 'POST',
      body: JSON.stringify({
        name: `${cam.name} – ${profile.name}`,
        address: `${cam.address}#${profile.token}`,
        rtsp_url: profile.rtsp_url,
        source_type: 'onvif',
        onvif_user: cam.onvif_user || '',
        onvif_pass: cam.onvif_pass || '',
      }),
    });
    selectedId = created.id;
    await loadCameras();
    toast(`Added channel ${created.name}`, 'success');
    log(`Added channel: ${created.name}`);
  } catch (e) {
    toast('Could not add channel: ' + e.message, 'error');
  }
}

// ============================================================
//  Enrollment
// ============================================================

function faceReviewScope() {
  return $('face-review-exclusive').checked ? 'exclusive' : 'global';
}

function faceReviewRate(value) {
  const preset = ['1', '2', '3', '5'].includes(String(value));
  $('face-review-rate').value = preset ? String(value) : 'custom';
  $('face-review-custom').value = String(value);
  $('face-review-custom-group').hidden = preset;
}

function updateFaceReviewScope() {
  if (!faceReviewPolicy) return;
  const exclusive = faceReviewScope() === 'exclusive';
  $('face-review-cameras').hidden = !exclusive;
  const selected = [...$('face-review-cameras').querySelectorAll('input:checked')];
  const chosen = exclusive && selected.length === 1
    ? faceReviewPolicy.overrides?.[selected[0].value] : null;
  $('face-review-enabled').checked = exclusive ? (chosen?.enabled ?? true)
    : faceReviewPolicy.enabled;
  faceReviewRate(chosen?.per_hour ?? faceReviewPolicy.per_hour);
}

function renderFaceReviewSettings() {
  if (!faceReviewPolicy) return;
  const previous = [...$('face-review-cameras').querySelectorAll('input:checked')]
    .map((input) => input.value);
  $('face-review-cameras').innerHTML = savedCameras.length
    ? savedCameras.map((cam) => `
      <label class="check-row"><input type="checkbox" value="${escapeHtml(cam.id)}" ${previous.includes(cam.id) ? 'checked' : ''}>
        ${escapeHtml(cam.name)} ${faceReviewPolicy.overrides[cam.id] ? '(Exclusive override)' : ''}
      </label>`).join('')
    : '<p class="hint">Add cameras to use Exclusive settings.</p>';
  $('face-review-effective').innerHTML = savedCameras.map((cam) => {
    const effective = faceReviewPolicy.effective?.[cam.id];
    return effective ? `<span class="badge ${effective.enabled ? 'badge-saved' : 'badge-warn'}">${escapeHtml(cam.name)}: ${effective.enabled ? 'On' : 'Off'} · ${effective.per_hour}/hour</span>` : '';
  }).join('') || '<span class="hint">No saved cameras.</span>';
  updateFaceReviewScope();
}

async function loadFaceReviewSettings() {
  try {
    faceReviewPolicy = await api('/api/enrollment/review/settings');
    renderFaceReviewSettings();
  } catch (e) { toast('Could not load face review settings: ' + e.message, 'error'); }
}

$('face-review-global').onchange = updateFaceReviewScope;
$('face-review-exclusive').onchange = updateFaceReviewScope;
$('face-review-cameras').addEventListener('change', updateFaceReviewScope);
$('face-review-rate').onchange = () => {
  $('face-review-custom-group').hidden = $('face-review-rate').value !== 'custom';
};
$('face-review-save').onclick = async () => {
  const scope = faceReviewScope();
  const ids = [...$('face-review-cameras').querySelectorAll('input:checked')]
    .map((input) => input.value);
  if (scope === 'exclusive' && !ids.length) {
    toast('Select at least one camera', 'error'); return;
  }
  const rate = Number($('face-review-rate').value === 'custom'
    ? $('face-review-custom').value : $('face-review-rate').value);
  if (!Number.isInteger(rate) || rate < 1 || rate > 30) {
    toast('Enter a whole-number hourly maximum from 1 to 30', 'error'); return;
  }
  if (scope === 'global' && Object.keys(faceReviewPolicy?.overrides || {}).length &&
      !await confirmDialog({ title: 'Replace Exclusive settings?',
        message: 'Global settings remove all camera-specific face review overrides.',
        confirmLabel: 'Apply globally' })) return;
  const button = $('face-review-save');
  setBusy(button, true, 'Saving…');
  try {
    faceReviewPolicy = await api('/api/enrollment/review/settings', {
      method: 'PUT', body: JSON.stringify({ scope,
        enabled: $('face-review-enabled').checked, per_hour: rate,
        camera_ids: scope === 'exclusive' ? ids : [] }),
    });
    renderFaceReviewSettings();
    toast('Unrecognised face capture settings saved');
  } catch (e) { toast(e.message, 'error'); }
  finally { setBusy(button, false); }
};

function renderFaceCandidates() {
  $('face-candidate-count').textContent = String(faceCandidates.length);
  $('face-review-list').innerHTML = faceCandidates.length ? faceCandidates.map((item) => `
    <article class="review-item" data-candidate="${escapeHtml(item.id)}">
      <img src="${escapeHtml(item.image_url)}" alt="Unrecognised face from ${escapeHtml(item.camera_name)}" loading="lazy">
      <b>${escapeHtml(item.camera_name)}</b><small>${escapeHtml(item.created_at)}</small>
      <label>Existing person
        <select class="face-candidate-person">
          <option value="">Choose a person</option>
          ${persons.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`).join('')}
        </select>
      </label>
      <button type="button" class="btn-mini face-candidate-add" data-needs-face>Add to selected person</button>
      <label>Or new person name
        <input class="face-candidate-name" autocomplete="off" maxlength="120" placeholder="Full name">
      </label>
      <button type="button" class="btn-mini face-candidate-new" data-needs-face>Enroll as new person</button>
      <button type="button" class="btn-mini btn-mini-danger face-candidate-discard">Discard image</button>
    </article>`).join('') : '<div class="empty-state">No face candidates yet. Enable opt-in capture above on a camera with Person detection running.</div>';
  applyFaceAvailability();
}

async function loadFaceReviewCandidates() {
  try {
    const data = await api('/api/enrollment/review/candidates');
    faceCandidates = data.candidates || [];
    renderFaceCandidates();
  } catch (e) { toast('Could not load unrecognised faces: ' + e.message, 'error'); }
}

$('face-review-refresh').onclick = () => {
  loadFaceReviewCandidates(); loadFaceReviewSettings();
};
$('face-review-list').addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  const card = button.closest('[data-candidate]');
  if (!card) return;
  const id = card.dataset.candidate;
  const discard = button.classList.contains('face-candidate-discard');
  const existing = button.classList.contains('face-candidate-add');
  const create = button.classList.contains('face-candidate-new');
  if (!discard && !existing && !create) return;
  const personId = card.querySelector('.face-candidate-person').value;
  const name = card.querySelector('.face-candidate-name').value.trim();
  if (existing && !personId) { toast('Choose an existing person first', 'error'); return; }
  if (create && !name) { toast('Enter a name for the new person', 'error'); return; }
  if (!await confirmDialog({
    title: discard ? 'Discard face image?' : 'Confirm face identity',
    message: discard ? 'Remove this image from the review queue?'
      : `Are you sure this face belongs to ${existing ? persons.find((p) => p.id === personId)?.name : name}? Wrong assignments can cause mistaken recognition.`,
    confirmLabel: discard ? 'Discard' : 'Confirm identity',
  })) return;
  setBusy(button, true, 'Saving…');
  try {
    if (discard) {
      await api(`/api/enrollment/review/candidates/${encodeURIComponent(id)}`,
                { method: 'DELETE' });
    } else {
      await api(`/api/enrollment/review/candidates/${encodeURIComponent(id)}/assign`, {
        method: 'POST', body: JSON.stringify(existing ? { person_id: personId } : { name }),
      });
      await loadPersons(true);
    }
    await loadFaceReviewCandidates();
    toast(discard ? 'Face image discarded' : 'Face added to enrollment', 'success');
  } catch (e) { toast('Could not update face candidate: ' + e.message, 'error'); }
  finally { setBusy(button, false); }
});

async function refreshFaceEngine() {
  try {
    faceEngine = await api('/api/face-engine');
  } catch {
    // Older backend without the endpoint: let the upload speak for itself.
    faceEngine = { available: true, detail: '' };
  }

  const alertEl = $('face-engine-alert');
  if (!faceEngine.available) {
    alertEl.hidden = false;
    alertEl.textContent = `Face recognition is unavailable — ${faceEngine.detail}`;
  } else {
    alertEl.hidden = true;
  }

  applyFaceAvailability();
  renderPersons();
}

function applyFaceAvailability() {
  document.querySelectorAll('[data-needs-face]').forEach((el) => {
    el.disabled = !faceEngine.available;
    if (faceEngine.available) el.removeAttribute('title');
    else el.title = faceEngine.detail || 'Face recognition is unavailable';
  });
}

let personsLoading = false;

async function loadPersons(force = false) {
  if (personsLoading && !force) return;
  personsLoading = true;
  try {
    persons = await api('/api/persons');
    renderPersons();
    renderFaceCandidates();
  } catch (e) {
    log('Failed to load people: ' + e.message);
  } finally {
    personsLoading = false;
  }
}

function initialsOf(name) {
  const words = String(name || '?').trim().split(/\s+/).slice(0, 2);
  return words.map((w) => w[0] || '').join('').toUpperCase() || '?';
}

function personCard(p) {
  const thumb = p.has_thumbnail
    ? `<img src="/api/persons/${encodeURIComponent(p.id)}/thumbnail" alt="" loading="lazy">`
    : '';
  const notes = p.notes
    ? `<small class="person-notes" title="${escapeHtml(p.notes)}">${escapeHtml(p.notes)}</small>`
    : '';

  return `<div class="person" data-person-id="${escapeHtml(p.id)}">
    <div class="person-avatar">
      <span aria-hidden="true">${escapeHtml(initialsOf(p.name))}</span>${thumb}
    </div>
    <div class="person-info">
      <b title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</b>
      <small>${p.samples} sample${p.samples === 1 ? '' : 's'}</small>
      ${notes}
    </div>
    <div class="person-actions">
      ${isAdminUser() ? `<button class="btn-mini" data-action="add-samples" data-id="${escapeHtml(p.id)}" data-needs-face>Add samples</button>
      <button class="btn-mini" data-action="edit-person" data-id="${escapeHtml(p.id)}">Edit</button>
      <button class="btn-mini btn-mini-danger" data-action="delete-person" data-id="${escapeHtml(p.id)}">Delete</button>` : ''}
    </div>
  </div>`;
}

function renderPersons() {
  const box = $('persons');
  const q = personFilter.trim().toLowerCase();
  const list = persons.filter(
    (p) => !q || `${p.name} ${p.notes || ''}`.toLowerCase().includes(q));

  $('person-count').textContent = String(persons.length);

  if (!persons.length) {
    box.innerHTML = `<div class="empty-state empty-state-lg">
      <div class="empty-icon" aria-hidden="true">👤</div>
      <h3>No one enrolled yet</h3>
      <p>Add a few clear photos or a short video of a person and the system will learn to recognise them.</p>
      ${isAdminUser() ? '<button class="btn btn-primary" data-action="enroll" data-needs-face>Enroll person</button>' : ''}
    </div>`;
  } else if (!list.length) {
    box.innerHTML = `<div class="empty-state empty-state-sm">
      <p>No people match “${escapeHtml(personFilter)}”.</p>
      <button class="btn-mini" data-action="clear-person-filter">Clear filter</button>
    </div>`;
  } else {
    box.innerHTML = list.map(personCard).join('');
    box.querySelectorAll('.person-avatar img').forEach((img) => {
      img.onerror = () => img.remove();   // fall back to the initials
    });
  }

  applyFaceAvailability();
}

// ---------- Person modal ----------

function setPersonStatus(text, kind) {
  const el = $('person-status');
  el.className = `form-status${kind ? ' ' + kind : ''}`;
  el.textContent = text || '';
}

function renderFileList() {
  const list = $('person-file-list');
  list.innerHTML = selectedFiles.map((f, i) => `
    <li class="file-item">
      <span class="file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
      <span class="file-size">${formatBytes(f.size)}</span>
      <button type="button" class="btn-mini" data-remove-file="${i}"
              aria-label="Remove ${escapeHtml(f.name)}">Remove</button>
    </li>`).join('');
}

function addFiles(fileList) {
  let added = 0;
  for (const f of fileList) {
    if (selectedFiles.some((x) => x.name === f.name && x.size === f.size)) continue;
    selectedFiles.push(f);
    added += 1;
  }
  if (!added) return;
  renderFileList();
  setPersonStatus(
    `${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'} selected.`);
}

function openPersonModal(person = null, { focusFiles = false } = {}) {
  if (!person && !faceEngine.available) {
    toast(`Enrollment unavailable — ${faceEngine.detail}`, 'error');
    return;
  }
  if (person && focusFiles && !faceEngine.available) {
    toast(`Enrollment unavailable — ${faceEngine.detail}`, 'error');
    return;
  }

  personMode = person ? 'edit' : 'new';
  editingPersonId = person ? person.id : null;
  selectedFiles = [];
  personBusy = false;
  $('person-modal').dataset.busy = 'false';

  $('person-modal-title').textContent = person ? 'Edit person' : 'Enroll person';
  $('person-modal-hint').textContent = person
    ? 'Update the name or notes, or add more photos/videos to improve recognition.'
    : 'The system learns a face signature from the files you add. One clear face per photo works best.';
  $('person-name').value = person ? person.name : '';
  $('person-notes').value = person ? (person.notes || '') : '';
  $('person-file-label').textContent = person
    ? 'Add photos / videos (optional)'
    : 'Photos / videos';
  $('person-files-group').hidden = !!person && !faceEngine.available;
  $('person-submit').textContent = person ? 'Save changes' : 'Enroll';
  $('person-status').className = 'form-status';

  if (person) {
    setPersonStatus(`${person.samples} sample${person.samples === 1 ? '' : 's'} on record.`);
  } else if (!faceEngine.available) {
    setPersonStatus(`Unavailable — ${faceEngine.detail}`, 'error');
  } else {
    setPersonStatus('');
  }

  $('person-files').value = '';
  renderFileList();
  openModal($('person-modal'), focusFiles ? $('person-drop') : $('person-name'));
}

async function submitPerson() {
  if (personBusy) return;

  const name = $('person-name').value.trim();
  const notes = $('person-notes').value.trim();

  if (!name) {
    toast('Enter a name first.', 'error');
    $('person-name').focus();
    return;
  }
  if (personMode === 'new' && !selectedFiles.length) {
    toast('Add at least one photo or video.', 'error');
    return;
  }

  const hadFiles = selectedFiles.length > 0;
  personBusy = true;
  $('person-modal').dataset.busy = 'true';

  const btn = $('person-submit');
  setBusy(btn, true, personMode === 'new' ? 'Enrolling…' : 'Saving…');
  setPersonStatus(hadFiles
    ? 'Analysing files — this can take a few seconds per file…'
    : 'Saving…');

  try {
    if (personMode === 'new') {
      const form = new FormData();
      form.append('name', name);
      form.append('notes', notes);
      for (const f of selectedFiles) form.append('files', f);

      const res = await uploadJson('/api/persons', form);
      const skipped = res.errors?.length
        ? ` ${res.errors.length} file(s) skipped.` : '';
      toast(`Enrolled ${res.name} with ${res.samples} sample${res.samples === 1 ? '' : 's'}.${skipped}`, 'success');
      if (res.errors?.length) log(`Enrollment warnings: ${res.errors.join('; ')}`);
      log(`Enrolled ${res.name} (${res.samples} samples)`);
    } else {
      const id = editingPersonId;
      await api(`/api/persons/${encodeURIComponent(id)}`, {
        method: 'PUT',
        body: JSON.stringify({ name, notes }),
      });

      if (hadFiles) {
        const sampleForm = new FormData();
        for (const f of selectedFiles) sampleForm.append('files', f);
        const res = await uploadJson(
          `/api/persons/${encodeURIComponent(id)}/enroll`, sampleForm);

        if (!res.added) {
          throw new Error(res.errors?.join('; ') || 'No usable faces found in those files.');
        }
        const skipped = res.errors?.length
          ? ` ${res.errors.length} file(s) skipped.` : '';
        toast(`Added ${res.added} sample${res.added === 1 ? '' : 's'}.${skipped}`, 'success');
        if (res.errors?.length) log(`Enrollment warnings: ${res.errors.join('; ')}`);
      } else {
        toast('Changes saved.', 'success');
      }
    }

    $('person-modal').dataset.busy = 'false';
    closeModal($('person-modal'));
    await loadPersons(true);
  } catch (e) {
    setPersonStatus('Failed: ' + e.message, 'error');
    toast('Enrollment failed: ' + e.message, 'error');
    log('Enrollment failed: ' + e.message);
  } finally {
    personBusy = false;
    $('person-modal').dataset.busy = 'false';
    setBusy(btn, false);
  }
}

async function deletePerson(id) {
  const person = persons.find((p) => p.id === id);
  if (!person) return;

  const ok = await confirmDialog({
    title: 'Delete person?',
    message: `${person.name} and all ${person.samples} face sample${person.samples === 1 ? '' : 's'} will be removed. This cannot be undone.`,
    confirmLabel: 'Delete person',
  });
  if (!ok) return;

  try {
    await api(`/api/persons/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast(`${person.name} removed.`, 'success');
    log(`Deleted person: ${person.name}`);
    await loadPersons(true);
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

// ============================================================
//  Telegram: bot settings + confirmed events
// ============================================================

const KIND_LABELS = {
  calling: 'On a call (phone at ear)',
  looking: 'Looking at phone',
  using: 'Phone use (model prediction)',
  entered: 'Entered',
  left: 'Left',
};

function ensureEventsPolling() {
  if (eventsTimer) return;
  eventsTimer = setInterval(() => {
    loadTelegram({ silent: true, renderForm: false });
    loadEvents({ silent: true });
  }, 15000);
}

function stopEventsPolling() {
  if (!eventsTimer) return;
  clearInterval(eventsTimer);
  eventsTimer = null;
}

async function loadTelegram({ silent = false, renderForm = true } = {}) {
  try {
    telegram = await api('/api/telegram');
    renderTelegram(renderForm);
  } catch (e) {
    if (!silent) {
      toast('Could not load Telegram settings: ' + e.message, 'error');
      log('Failed to load Telegram settings: ' + e.message);
    }
  }
}

function renderTelegram(renderForm = true) {
  const chip = $('telegram-status');
  let label = 'Not configured';
  let cls = 'chip-idle';
  if (telegram.configured) {
    if (telegram.last_error) {
      label = 'Connection error';
      cls = 'chip-error';
    } else if (telegram.bot_username) {
      label = `@${telegram.bot_username}`;
      cls = 'chip-live';
    } else {
      label = 'Configured';
      cls = 'chip-live';
    }
  }
  chip.textContent = label;
  chip.className = `chip ${cls}`;

  if (renderForm) {
    $('telegram-chat').value = telegram.chat_id || '';
    const tokenInput = $('telegram-token');
    tokenInput.value = '';
    if (telegram.token_set) {
      tokenInput.placeholder = `Saved (${telegram.token_masked}) — leave blank to keep`;
      $('telegram-token-hint').textContent =
        'A bot token is saved. Type a new one only to replace it.';
    } else {
      tokenInput.placeholder = '123456789:AA…';
      $('telegram-token-hint').textContent =
        'Create a bot with @BotFather, then paste its token here.';
    }
  }

  const alertEl = $('telegram-alert');
  if (telegram.last_error) {
    alertEl.hidden = false;
    alertEl.textContent = `Telegram error: ${telegram.last_error}`;
  } else {
    alertEl.hidden = true;
  }
  $('telegram-clear').disabled = !telegram.token_set && !telegram.chat_id;
  renderEventsCount();
}

const EVENT_STATUS = {
  pending: { label: 'Not confirmed', cls: 'badge-warn' },
  confirmed: { label: 'Confirmed', cls: 'badge-saved' },
  rejected: { label: 'Rejected', cls: 'badge-new' },
  failed: { label: 'Failed', cls: 'badge-new' },
};

function eventStatusBadge(status) {
  const meta = EVENT_STATUS[status] || EVENT_STATUS.pending;
  return `<span class="badge ${meta.cls}">${escapeHtml(meta.label)}</span>`;
}

function eventIsCrossing(event) {
  return event.kind === 'entered' || event.kind === 'left';
}

function eventIcon(event) {
  return eventIsCrossing(event) ? '🚪' : '📱';
}

function renderEventsCount() {
  const counts = telegram.events || {};
  const total = Object.values(counts).reduce(
    (sum, value) => sum + (Number(value) || 0), 0);
  $('event-count').textContent = String(total || events.length);
}

async function loadEvents({ silent = false } = {}) {
  try {
    const data = await api('/api/events?status=all&limit=100');
    events = data.events || [];
    telegram.events = data.counts || telegram.events;
    renderEvents();
  } catch (e) {
    if (!silent) {
      toast('Could not load events: ' + e.message, 'error');
      log('Failed to load events: ' + e.message);
    }
  }
}

function formatEventTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(window.dashboardI18n.locale);
}

function eventCard(event) {
  const snapshot = event.snapshot_url
    ? `<a class="event-thumb" href="${escapeHtml(event.snapshot_url)}"
          target="_blank" rel="noopener" title="Open snapshot">
         <img src="${escapeHtml(event.snapshot_url)}" alt="Event snapshot" loading="lazy">
       </a>`
    : `<div class="event-thumb event-thumb-empty" aria-hidden="true">${eventIcon(event)}</div>`;
  const confidence = Number.isFinite(Number(event.confidence))
    ? `${Math.round(Number(event.confidence) * 100)}%`
    : '—';
  const handled = event.status === 'confirmed' && event.handled_by
    ? `<small>Confirmed by ${escapeHtml(event.handled_by)}</small>`
    : event.status === 'rejected' && event.handled_by
      ? `<small>Rejected by ${escapeHtml(event.handled_by)}</small>`
      : event.status === 'pending'
        ? '<small>Waiting for Telegram confirmation</small>'
        : '';
  const customer = event.customer
    ? '<span class="badge badge-customer">Customer</span>' : '';

  return `<div class="event">
    ${snapshot}
    <div class="event-info">
      <div class="event-title">
        <b>${escapeHtml(KIND_LABELS[event.kind] || event.kind || 'Event')}</b>
        ${eventStatusBadge(event.status)}${customer}
      </div>
      <small>${escapeHtml(event.camera_name || 'Camera')} · ${escapeHtml(event.person_name || 'Unknown')} · ${confidence}</small>
      <small>${escapeHtml(formatEventTime(event.created_at))}</small>
      ${handled}
    </div>
    ${isAdminUser() ? deleteEventButton(event.id) : ''}
  </div>`;
}

function deleteEventButton(id) {
  return `<button type="button" class="btn-mini btn-mini-danger" data-action="delete-event"
    data-id="${escapeHtml(id)}" aria-label="Delete event">Delete</button>`;
}

async function deleteEvent(id) {
  if (!isAdminUser()) return;
  if (!await confirmDialog({
    title: 'Delete event?',
    message: 'Permanently remove this event, its snapshot and any attendance record created from it? This cannot be undone.',
    confirmLabel: 'Delete event',
  })) return;
  try {
    await api(`/api/events/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('Event deleted.', 'success');
    await refreshEventViews();
  } catch (e) {
    if (e.status !== 401) toast('Delete failed: ' + e.message, 'error');
  }
}

function refreshEventViews() {
  return Promise.all([loadOverview(), loadAttendance(), loadCustomers(), loadEvents()]);
}

const CLEAR_EVENT_LABELS = {
  unassigned: 'unassigned crossings',
  attendance: 'attendance records',
  customers: 'customer events',
  telegram: 'all Telegram events',
};

async function clearEvents(scope, btn) {
  if (!isAdminUser() || !Object.hasOwn(CLEAR_EVENT_LABELS, scope)) return;
  const label = CLEAR_EVENT_LABELS[scope];
  if (!await confirmDialog({
    title: `Delete ${label}?`,
    message: `Permanently delete ${label}, their snapshots and any linked attendance? Events shown in other dashboard sections will disappear there too. This cannot be undone.`,
    confirmLabel: 'Delete all',
  })) return;
  setBusy(btn, true, 'Deleting…');
  try {
    const result = await api(`/api/events?scope=${encodeURIComponent(scope)}`, { method: 'DELETE' });
    toast(`Deleted ${result.deleted} ${label}.`, 'success');
    await refreshEventViews();
  } catch (e) {
    if (e.status !== 401) toast('Delete failed: ' + e.message, 'error');
  } finally {
    setBusy(btn, false);
  }
}

function renderEventList(box, rows, emptyHtml) {
  if (!rows.length) {
    box.innerHTML = emptyHtml;
    return;
  }
  box.innerHTML = rows.map(eventCard).join('');
  box.querySelectorAll('img').forEach((img) => {
    img.onerror = () => {
      const fallback = document.createElement('div');
      fallback.className = 'event-thumb event-thumb-empty';
      fallback.textContent = '📷';
      img.closest('.event-thumb').replaceWith(fallback);
    };
  });
}

function renderEvents() {
  renderEventsCount();
  renderEventList($('events'), events, `<div class="empty-state empty-state-sm">
      <div class="empty-icon" aria-hidden="true">📱</div>
      <p>No events yet. Phone-use and entrance/exit alerts appear here, including events that are <b>Not confirmed</b>.</p>
    </div>`);
}

// ============================================================
//  Attendance
// ============================================================

function ensureAttendancePolling() {
  if (attendanceTimer) return;
  attendanceTimer = setInterval(() => loadAttendance({ silent: true }), 15000);
}

function stopAttendancePolling() {
  if (!attendanceTimer) return;
  clearInterval(attendanceTimer);
  attendanceTimer = null;
}

async function loadAttendance({ silent = false } = {}) {
  try {
    attendance = await api('/api/attendance');
    renderAttendance();
  } catch (e) {
    if (!silent) {
      toast('Could not load attendance: ' + e.message, 'error');
      log('Failed to load attendance: ' + e.message);
    }
  }
}

function attendanceStatus(status) {
  if (status === 'inside') return '<span class="badge badge-saved">Inside</span>';
  if (status === 'outside') return '<span class="badge badge-kind">Outside</span>';
  return '<span class="badge badge-warn">No record</span>';
}

function attendanceStamp(record) {
  if (!record) return '<span class="attendance-muted">—</span>';
  return `<span class="attendance-stamp"><b>${escapeHtml(formatEventTime(record.occurred_at))}</b><small>${escapeHtml(record.camera_name || 'Camera')}</small></span>`;
}

function renderAttendance() {
  if (!attendance) return;
  const people = attendance.people || [];
  const summary = attendance.summary || {};
  $('attendance-count').textContent = String(people.length);
  $('attendance-summary').innerHTML = `
    <div class="attendance-stat"><b>${summary.inside ?? 0}</b><span>Inside now</span></div>
    <div class="attendance-stat"><b>${summary.today_entrances ?? 0}</b><span>Entrances today</span></div>
    <div class="attendance-stat"><b>${summary.today_exits ?? 0}</b><span>Exits today</span></div>
    <div class="attendance-stat"><b>${summary.people ?? people.length}</b><span>Enrolled people</span></div>`;

  $('attendance-rows').innerHTML = people.length
    ? people.map((person) => {
      const avatar = person.thumbnail_url
        ? `<img src="${escapeHtml(person.thumbnail_url)}" alt="" loading="lazy">`
        : `<span>${escapeHtml(initialsOf(person.name))}</span>`;
      return `<tr data-attendance-person="${escapeHtml(person.id)}">
        <td>
          <div class="attendance-person">
            <div class="attendance-avatar">${avatar}</div>
            <b>${escapeHtml(person.name)}</b>
          </div>
        </td>
        <td>${attendanceStatus(person.status)}</td>
        <td>${attendanceStamp(person.last_entrance)}</td>
        <td>${attendanceStamp(person.last_exit)}</td>
        <td><span class="attendance-today">${person.today_entrances || 0} in · ${person.today_exits || 0} out</span></td>
      </tr>`;
    }).join('')
    : `<tr><td colspan="5"><div class="empty-state empty-state-sm">
        No enrolled people yet. Enroll a person, then assign an entrance or exit
        role to a camera and confirm their crossing in Telegram.
      </div></td></tr>`;

  $('attendance-rows').querySelectorAll('.attendance-avatar img').forEach((img) => {
    img.onerror = () => {
      const person = img.closest('[data-attendance-person]');
      const name = person ? person.querySelector('.attendance-person b')?.textContent : '';
      const fallback = document.createElement('span');
      fallback.textContent = initialsOf(name);
      img.closest('.attendance-avatar').replaceChildren(fallback);
    };
  });

  const unknown = attendance.unknown_events || [];
  $('attendance-unknown-count').textContent = String(unknown.length);
  $('attendance-unknown').innerHTML = unknown.length
    ? unknown.map(attendanceCrossingCard).join('')
    : `<div class="empty-state empty-state-sm">
        No unassigned crossings. Unknown crossings confirmed in Telegram are
        saved here and are not counted as attendance.
      </div>`;
  $('attendance-unknown').querySelectorAll('img').forEach((img) => {
    img.onerror = () => {
      const fallback = document.createElement('div');
      fallback.className = 'event-thumb event-thumb-empty';
      fallback.textContent = '🚪';
      img.closest('.event-thumb').replaceWith(fallback);
    };
  });

  const recent = attendance.recent || [];
  $('attendance-log').innerHTML = recent.length
    ? recent.map(attendanceLogRow).join('')
    : `<div class="empty-state empty-state-sm">
        No attendance records yet. Confirm an entrance or exit alert in Telegram
        to record one.
      </div>`;
}

function attendanceCrossingCard(event) {
  const snapshot = event.snapshot_url
    ? `<a class="event-thumb" href="${escapeHtml(event.snapshot_url)}"
          target="_blank" rel="noopener" title="Open snapshot">
         <img src="${escapeHtml(event.snapshot_url)}" alt="Crossing snapshot" loading="lazy">
       </a>`
    : `<div class="event-thumb event-thumb-empty" aria-hidden="true">🚪</div>`;
  return `<div class="event">
    ${snapshot}
    <div class="event-info">
      <div class="event-title">
        <b>${escapeHtml(KIND_LABELS[event.kind] || event.kind || 'Crossing')}</b>
        <span class="badge badge-warn">Not counted</span>
      </div>
      <small>${escapeHtml(event.camera_name || 'Camera')} · ${escapeHtml(event.person_name || 'Unknown')}</small>
      <small>${escapeHtml(formatEventTime(event.created_at))}</small>
      ${event.handled_by ? `<small>Confirmed by ${escapeHtml(event.handled_by)}</small>` : ''}
    </div>
    ${isAdminUser() ? deleteEventButton(event.id) : ''}
  </div>`;
}

// ============================================================
//  Overview (attendance, customers, recent and pending events)
// ============================================================

function ensureOverviewPolling() {
  if (overviewTimer) return;
  overviewTimer = setInterval(() => loadOverview({ silent: true }), 15000);
}

function stopOverviewPolling() {
  if (!overviewTimer) return;
  clearInterval(overviewTimer);
  overviewTimer = null;
}

async function loadOverview({ silent = false } = {}) {
  try {
    overviewData = await api('/api/overview');
    renderOverview();
  } catch (e) {
    if (!silent) {
      toast('Could not load overview: ' + e.message, 'error');
      log('Failed to load overview: ' + e.message);
    }
  }
}

function overviewStat(value, label, cls = '') {
  const shown = typeof value === 'string'
    ? value
    : String(Number(value) || 0);
  return `<div class="attendance-stat ${cls}"><b>${escapeHtml(shown)}</b><span>${escapeHtml(label)}</span></div>`;
}

function attendanceLogRow(record) {
  return `<div class="attendance-log-row">
    <span class="attendance-log-kind ${record.kind === 'entered' ? 'is-in' : 'is-out'}"
          aria-hidden="true">${record.kind === 'entered' ? '→' : '←'}</span>
    <div class="attendance-log-info">
      <b>${escapeHtml(record.person_name)}</b>
      <small>${record.kind === 'entered' ? 'Entered' : 'Left'} · ${escapeHtml(record.camera_name || 'Camera')} · ${escapeHtml(formatEventTime(record.occurred_at))}</small>
    </div>
    <small class="attendance-log-by">${record.handled_by ? `Confirmed by ${escapeHtml(record.handled_by)}` : 'Confirmed'}</small>
    ${isAdminUser() ? deleteEventButton(record.event_id) : ''}
  </div>`;
}

function renderOverview() {
  if (!overviewData) return;
  const attendanceSummary = overviewData.attendance || {};
  const customerSummary = overviewData.customers || {};
  const eventCounts = overviewData.events || {};
  const cameras = overviewData.cameras || {};

  $('overview-summary').innerHTML = [
    overviewStat(attendanceSummary.inside, 'Inside now'),
    overviewStat(attendanceSummary.today_entrances, 'Entrances today'),
    overviewStat(attendanceSummary.today_exits, 'Exits today'),
    overviewStat(attendanceSummary.people, 'Enrolled people'),
    overviewStat(customerSummary.today_entrances, 'Customers today', 'is-customer'),
    overviewStat(eventCounts.pending, 'Not confirmed events', 'is-pending'),
    overviewStat(`${Number(cameras.running) || 0}/${Number(cameras.total) || 0}`,
                 'Cameras running'),
  ].join('');

  const recent = overviewData.recent_attendance || [];
  $('overview-attendance').innerHTML = recent.length
    ? recent.map(attendanceLogRow).join('')
    : `<div class="empty-state empty-state-sm">No attendance records yet. Confirm an entrance or exit event in Telegram.</div>`;

  $('overview-customer-count').textContent = String(customerSummary.total ?? 0);
  renderEventList($('overview-customers'), overviewData.recent_customers || [],
    `<div class="empty-state empty-state-sm">No customer events yet. Enable <b>Count as Customer</b> on an entrance or exit camera.</div>`);

  $('overview-pending-count').textContent = String(eventCounts.pending ?? 0);
  renderEventList($('overview-pending'), overviewData.pending_events || [],
    `<div class="empty-state empty-state-sm">No events are waiting for confirmation.</div>`);

  renderEventList($('overview-events'), overviewData.recent_events || [],
    `<div class="empty-state empty-state-sm">No events yet. Phone-use and entrance/exit alerts will appear here.</div>`);
}

// ============================================================
//  Customers
// ============================================================

function ensureCustomersPolling() {
  if (customersTimer) return;
  customersTimer = setInterval(() => loadCustomers({ silent: true }), 15000);
}

function stopCustomersPolling() {
  if (!customersTimer) return;
  clearInterval(customersTimer);
  customersTimer = null;
}

async function loadCustomers({ silent = false } = {}) {
  try {
    const status = $('customer-status-filter').value || 'all';
    customerData = await api(
      `/api/customers?status=${encodeURIComponent(status)}&limit=200`);
    renderCustomers();
  } catch (e) {
    if (!silent) {
      toast('Could not load customers: ' + e.message, 'error');
      log('Failed to load customers: ' + e.message);
    }
  }
}

function renderCustomers() {
  if (!customerData) return;
  const summary = customerData.summary || {};
  const events = customerData.events || [];
  $('customers-count').textContent = String(summary.total ?? events.length);
  $('customers-summary').innerHTML = [
    overviewStat(summary.today_entrances, 'Customers today', 'is-customer'),
    overviewStat(summary.today_exits, 'Customer exits today'),
    overviewStat(summary.today_pending, 'Not confirmed today', 'is-pending'),
    overviewStat(summary.pending, 'Not confirmed total', 'is-pending'),
    overviewStat(summary.confirmed, 'Confirmed customer events'),
    overviewStat(summary.total, 'All customer events'),
  ].join('');

  const status = $('customer-status-filter').value || 'all';
  const empty = status === 'all'
    ? `<div class="empty-state empty-state-sm">
        <div class="empty-icon" aria-hidden="true">🛍️</div>
        <p>No customer events yet. Edit an entrance or exit camera and tick <b>Count as Customer</b>.</p>
      </div>`
    : `<div class="empty-state empty-state-sm">No customer events match this status.</div>`;
  renderEventList($('customers-events'), events, empty);
}

// ============================================================
//  Server capacity (observation only; never starts cameras)
// ============================================================

function serverNumber(id) { return Number($(id).value); }

function renderServerResult(data) {
  const box = $('server-results');
  const verdict = {
    sufficient: 'Sufficient for the observed workload',
    insufficient: 'Insufficient / bottleneck observed',
    unverified: 'Not yet verified',
  }[data.verdict] || 'Not yet verified';
  const hw = data.hardware;
  const rec = data.recommendation;
  const list = (items) => items.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  box.hidden = false;
  box.innerHTML = `
    <h2>${escapeHtml(verdict)}</h2>
    <p class="hint">Observed ${escapeHtml(data.sample_seconds)} seconds, ${data.observed.length} running camera(s). A short check is not a guarantee for sustained operation or different scenes.</p>
    ${data.issues.length ? `<h3>Bottlenecks</h3><ul>${list(data.issues)}</ul>` : ''}
    ${data.limitations.length ? `<h3>What remains unverified</h3><ul>${list(data.limitations)}</ul>` : ''}
    <p class="hint">${escapeHtml((data.caveats || []).join(' '))}</p>
    <h3>This server</h3>
    <p>${escapeHtml(hw.logical_cores)} logical CPU cores (${escapeHtml(hw.physical_cores ?? 'unknown')} physical),
    ${escapeHtml(hw.ram_gb)} GB RAM (${escapeHtml(hw.available_ram_gb)} GB available),
    ${escapeHtml(hw.disk_free_gb)} GB free disk. Sample CPU use: ${escapeHtml(data.cpu_percent)}%.
    Application memory: ${escapeHtml(data.process_ram_gb)} GB.</p>
    <p class="hint">Inference backends: ${escapeHtml(hw.providers.map((p) => `${p.name} (${p.status})`).join(', ') || 'unknown')}.
    ${hw.gpus.length ? `NVIDIA GPUs: ${escapeHtml(hw.gpus.map((g) => `${g.name}: ${g.used_mb}/${g.vram_mb} MB used, ${g.utilization_percent}% busy`).join('; '))}.` : 'GPU memory/utilization telemetry unavailable.'}</p>
    ${data.observed.length ? `<h3>Observed cameras</h3><ul>${list(data.observed.map((p) => `${p.camera}: ${p.inference_fps} inference/s, source ${p.source_fps ?? 'unknown'} FPS, inference p95 ${p.inference_p95_ms ?? 'unknown'} ms`))}</ul>` : ''}
    <h3>Planning requirements (estimates, not a certified server specification)</h3>
    <p>CPU: ${rec.cpu_logical_cores_estimate == null ? 'requires a representative benchmark' : `at least ~${escapeHtml(rec.cpu_logical_cores_estimate)} logical cores based on observed CPU load`}.
    RAM: ${rec.ram_gb_floor == null ? 'requires a representative benchmark' : `~${escapeHtml(rec.ram_gb_floor)} GB based on observed use`}.
    Free snapshot storage: at least ${escapeHtml(rec.free_disk_gb_for_snapshots)} GB.
    Camera inbound: ~${escapeHtml(rec.inbound_mbps)} Mbps; viewer outbound planning ceiling: ~${escapeHtml(rec.viewer_outbound_mbps_upper_bound)} Mbps.</p>
    <p class="hint">${escapeHtml(rec.gpu)} ${rec.gpu_vram_gb_floor == null ? '' : `Current GPU memory occupancy with 30% headroom: at least ~${escapeHtml(rec.gpu_vram_gb_floor)} GB VRAM (includes other processes; not a GPU performance guarantee).`}
    CPU estimates assume approximately linear scaling; verify with the full camera count before buying hardware. This check does not measure storage throughput or maximum network capacity.</p>`;
}

$('server-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!isAdminUser()) return;
  const body = {
    cameras: serverNumber('server-cameras'), fps: serverNumber('server-fps'),
    width: serverNumber('server-width'), height: serverNumber('server-height'),
    people_per_frame: serverNumber('server-people'),
    viewers: serverNumber('server-viewers'),
    camera_mbps: serverNumber('server-bitrate'),
    snapshot_mb_per_day: serverNumber('server-snapshots'),
    retention_days: serverNumber('server-retention'),
    phone_detection: $('server-phone').checked,
    face_recognition: $('server-face').checked,
  };
  const btn = $('server-check');
  setBusy(btn, true, 'Measuring (15 seconds)…');
  try {
    const data = await api('/api/server/check', { method: 'POST', body: JSON.stringify(body) });
    renderServerResult(data);
  } catch (error) {
    if (error.status !== 401) toast('Server check failed: ' + error.message, 'error');
  } finally {
    setBusy(btn, false);
  }
});

// ============================================================
//  Accounts
// ============================================================

let accountsCurrentId = null;

async function loadAccounts() {
  if (!isAdminUser()) return;
  try {
    const data = await api('/api/users');
    accounts = data.users || [];
    accountsCurrentId = data.current_user_id || null;
    renderAccounts();
  } catch (e) {
    if (e.status !== 401) toast('Could not load accounts: ' + e.message, 'error');
  }
}

function accountCard(user) {
  const isSelf = user.id === accountsCurrentId;
  const badges = [
    `<span class="badge ${user.role === 'admin' ? 'badge-saved' : 'badge-kind'}">${user.role === 'admin' ? 'Administrator' : 'Reader'}</span>`,
    user.active ? '<span class="badge badge-saved">Active</span>'
      : '<span class="badge badge-new">Disabled</span>',
    user.locked ? '<span class="badge badge-warn">Locked</span>' : '',
    isSelf ? '<span class="badge">You</span>' : '',
  ].filter(Boolean).join('');
  const last = user.last_login_at
    ? `Last sign-in ${escapeHtml(formatEventTime(user.last_login_at))}`
    : 'Never signed in';
  const failed = user.locked
    ? ` · ${user.failed_attempts} failed attempt${user.failed_attempts === 1 ? '' : 's'}`
    : '';

  return `<div class="account-row">
    <div class="account-avatar" aria-hidden="true">${escapeHtml((user.username || '?').slice(0, 1))}</div>
    <div class="account-main">
      <b>${escapeHtml(user.username)}</b>
      <small>${last}${failed}</small>
      <div class="account-meta">${badges}</div>
    </div>
    <div class="account-actions">
      <button class="btn-mini" data-account-action="edit" data-id="${escapeHtml(user.id)}">Edit</button>
      ${user.locked ? `<button class="btn-mini" data-account-action="unlock" data-id="${escapeHtml(user.id)}">Unlock</button>` : ''}
      ${isSelf ? '' : `<button class="btn-mini ${user.active ? 'btn-mini-danger' : ''}"
        data-account-action="${user.active ? 'disable' : 'enable'}"
        data-id="${escapeHtml(user.id)}">${user.active ? 'Disable' : 'Enable'}</button>`}
    </div>
  </div>`;
}

function renderAccounts() {
  const box = $('accounts-list');
  if (!box) return;
  $('account-count').textContent = String(accounts.length);
  box.innerHTML = accounts.length
    ? accounts.map(accountCard).join('')
    : '<div class="empty-state">No accounts found.</div>';
}

function openAccountModal(id = null) {
  if (!isAdminUser()) return;
  const user = id ? accounts.find((item) => item.id === id) : null;
  editingAccountId = user ? user.id : null;
  $('account-modal-title').textContent = user ? 'Edit account' : 'Add account';
  $('account-id').value = user ? user.id : '';
  $('account-username').value = user ? user.username : '';
  $('account-role').value = user ? user.role : 'reader';
  $('account-password').value = '';
  $('account-password-hint').textContent = user
    ? 'Leave blank to keep the current password. A new password ends that account’s other sessions.'
    : 'Set the initial password for this account (at least 4 characters).';
  $('account-active').checked = user ? user.active : true;
  const isSelf = Boolean(user && user.id === accountsCurrentId);
  $('account-role').disabled = isSelf;
  $('account-active').disabled = isSelf;
  $('account-modal-note').textContent = user
    ? `Created ${formatEventTime(user.created_at)}${user.created_by ? ` by ${user.created_by}` : ''}.${isSelf ? ' You cannot change your own role or disable your own account.' : ''}`
    : 'Readers can view dashboards but cannot change settings, enroll people, or manage accounts.';
  openModal($('account-modal'), $('account-username'));
}

async function saveAccount() {
  if (!isAdminUser()) return;
  const id = editingAccountId;
  const username = $('account-username').value.trim();
  const password = $('account-password').value;
  const body = {
    username,
    role: $('account-role').value,
    active: $('account-active').checked,
  };
  if (password) body.password = password;
  if (!username) {
    toast('Enter a username.', 'error');
    $('account-username').focus();
    return;
  }
  if (!id && !password) {
    toast('Set a password for the new account.', 'error');
    $('account-password').focus();
    return;
  }
  if (password && password.length < 4) {
    toast('Password must be at least 4 characters.', 'error');
    $('account-password').focus();
    return;
  }

  const btn = $('account-save');
  setBusy(btn, true, 'Saving…');
  try {
    if (id) await api(`/api/users/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify(body),
    });
    else await api('/api/users', {
      method: 'POST', body: JSON.stringify(body),
    });
    closeModal($('account-modal'));
    toast(id ? 'Account updated.' : 'Account created.', 'success');
    await loadAccounts();
  } catch (e) {
    if (e.status !== 401) toast(e.message, 'error');
  } finally {
    setBusy(btn, false);
  }
}

async function unlockAccount(id) {
  try {
    await api(`/api/users/${encodeURIComponent(id)}/unlock`, { method: 'POST' });
    toast('Account unlocked.', 'success');
    await loadAccounts();
  } catch (e) {
    if (e.status !== 401) toast(e.message, 'error');
  }
}

async function setAccountActive(id, active) {
  const user = accounts.find((item) => item.id === id);
  if (!user) return;
  if (!active) {
    const ok = await confirmDialog({
      title: 'Disable account?',
      message: `${user.username} will be signed out and unable to sign in until enabled again.`,
      confirmLabel: 'Disable',
    });
    if (!ok) return;
  }
  try {
    await api(`/api/users/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ active }),
    });
    toast(active ? 'Account enabled.' : 'Account disabled.', 'success');
    await loadAccounts();
  } catch (e) {
    if (e.status !== 401) toast(e.message, 'error');
  }
}

$('accounts-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-account-action]');
  if (!button) return;
  const { accountAction, id } = button.dataset;
  if (accountAction === 'edit') openAccountModal(id);
  else if (accountAction === 'unlock') unlockAccount(id);
  else if (accountAction === 'disable') setAccountActive(id, false);
  else if (accountAction === 'enable') setAccountActive(id, true);
});

$('account-add').addEventListener('click', () => openAccountModal());
$('account-save').addEventListener('click', saveAccount);

// ----- Own password and session -----

function openPasswordModal() {
  $('current-password').value = '';
  $('new-password').value = '';
  $('confirm-password').value = '';
  $('password-error').hidden = true;
  openModal($('password-modal'), $('current-password'));
}

async function saveOwnPassword() {
  const current = $('current-password').value;
  const next = $('new-password').value;
  const confirm = $('confirm-password').value;
  const error = $('password-error');
  error.hidden = true;
  if (!current || !next) {
    error.textContent = 'Enter your current and new password.';
    error.hidden = false;
    return;
  }
  if (next !== confirm) {
    error.textContent = 'The new passwords do not match.';
    error.hidden = false;
    return;
  }
  if (next.length < 4) {
    error.textContent = 'Use at least 4 characters.';
    error.hidden = false;
    return;
  }
  const btn = $('password-save');
  setBusy(btn, true, 'Updating…');
  try {
    await api('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    closeModal($('password-modal'));
    toast('Password updated.', 'success');
  } catch (e) {
    if (e.status !== 401) {
      error.textContent = e.message;
      error.hidden = false;
    }
  } finally {
    setBusy(btn, false);
  }
}

$('change-password').addEventListener('click', openPasswordModal);
$('password-save').addEventListener('click', saveOwnPassword);
$('logout').addEventListener('click', async () => {
  try {
    await api('/api/auth/logout', { method: 'POST' });
  } catch { /* session may already be gone */ }
  location.replace('/login.html');
});

async function saveTelegram(btn) {
  const token = $('telegram-token').value.trim();
  const chatId = $('telegram-chat').value.trim();

  if (!telegram.token_set && !token) {
    toast('Enter a bot token first.', 'error');
    $('telegram-token').focus();
    return;
  }
  if (!chatId) {
    toast('Enter the Telegram group ID.', 'error');
    $('telegram-chat').focus();
    return;
  }

  const body = { chat_id: chatId };
  if (token) body.bot_token = token;

  setBusy(btn, true, 'Saving…');
  try {
    telegram = await api('/api/telegram', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    $('telegram-token').value = '';
    renderTelegram(true);
    showTelegramResult(null);
    toast('Telegram settings saved. Use Verify connection to test them.',
      'success');
    log('Telegram settings updated');
  } catch (e) {
    toast('Could not save Telegram settings: ' + e.message, 'error');
  } finally {
    setBusy(btn, false);
  }
}

function showTelegramPending() {
  const box = $('telegram-result');
  box.className = 'telegram-result is-pending';
  box.hidden = false;
  box.textContent = '';

  const title = document.createElement('strong');
  title.textContent = 'Checking connection…';
  const detail = document.createElement('div');
  detail.textContent =
    'Contacting Telegram. This usually takes a few seconds.';
  box.append(title, detail);
}

function showTelegramResult(result) {
  const box = $('telegram-result');
  if (!result) {
    box.hidden = true;
    box.textContent = '';
    return;
  }

  const state = result.ok
    ? (result.warning ? 'is-warning' : 'is-success')
    : 'is-error';
  box.className = `telegram-result ${state}`;
  box.hidden = false;
  box.textContent = '';

  const title = document.createElement('strong');
  title.textContent = result.ok
    ? (result.warning ? 'Connected with a warning' : 'Connection verified')
    : 'Connection failed';
  box.appendChild(title);

  const detail = document.createElement('div');
  detail.textContent = result.detail || 'Unknown error.';
  box.appendChild(detail);

  if (result.warning) {
    const warning = document.createElement('div');
    warning.className = 'result-line';
    warning.textContent = result.warning;
    box.appendChild(warning);
  }

  if (result.hint) {
    const hint = document.createElement('div');
    hint.className = 'result-hint';
    hint.textContent = result.hint;
    box.appendChild(hint);
  }

  if (!result.ok && result.error) {
    const details = document.createElement('details');
    details.className = 'result-technical';
    const summary = document.createElement('summary');
    summary.textContent = 'Technical details';
    const code = document.createElement('code');
    code.textContent = result.error;
    details.append(summary, code);
    box.appendChild(details);
  }

  if (!result.ok) {
    box.focus({ preventScroll: true });
    box.scrollIntoView({ block: 'nearest' });
  }
}

async function testTelegram(btn) {
  const token = $('telegram-token').value.trim();
  const chatId = $('telegram-chat').value.trim();

  if (!telegram.token_set && !token) {
    showTelegramResult({
      ok: false,
      detail: 'Enter the bot token first.',
      hint: 'Create a bot with @BotFather and paste its token here.',
    });
    $('telegram-token').focus();
    return;
  }
  if (!chatId) {
    showTelegramResult({
      ok: false,
      detail: 'Enter the group ID first.',
      hint: 'Group IDs are negative, e.g. -1001234567890 or -123456789.',
    });
    $('telegram-chat').focus();
    return;
  }

  const dirty = Boolean(token) || chatId !== (telegram.chat_id || '');
  const payload = {};
  if (dirty) {
    payload.chat_id = chatId;
    if (token) payload.bot_token = token;
  }

  setBusy(btn, true, 'Verifying…');
  showTelegramPending();
  $('telegram-save').disabled = true;
  $('telegram-clear').disabled = true;
  try {
    // Check the values from the form first; a failed check must not
    // overwrite a working configuration.
    const result = await api('/api/telegram/test', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!result.ok && dirty) {
      result.hint = [result.hint, 'Your previously saved settings were not changed.']
        .filter(Boolean)
        .join(' ');
    }
    showTelegramResult(result);

    if (!result.ok) {
      toast(result.detail || 'Telegram check failed.', 'error');
      log('Telegram check failed: ' + (result.detail || 'unknown error'));
      // Keep what the user typed visible; refresh only the status chip.
      await loadTelegram({ silent: true, renderForm: false });
      return;
    }

    // Save only settings Telegram has just accepted.
    if (dirty) {
      const saveBody = { chat_id: result.chat_id || chatId };
      if (token) saveBody.bot_token = token;
      try {
        telegram = await api('/api/telegram', {
          method: 'PUT',
          body: JSON.stringify(saveBody),
        });
      } catch (e) {
        showTelegramResult({
          ok: false,
          detail: 'Telegram is reachable, but the settings could not be saved.',
          hint: e.message,
        });
        toast('Verified, but saving failed.', 'error');
        return;
      }
      $('telegram-token').value = '';
      renderTelegram(true);
    }

    toast(result.warning ? 'Connected, but see the warning below.'
      : 'Telegram connected.', result.warning ? 'info' : 'success');
    log('Telegram: ' + result.detail);
    await loadTelegram({ silent: true });
  } catch (e) {
    showTelegramResult({
      ok: false,
      detail: 'The connection check could not be completed.',
      hint: e.message,
    });
    toast('Telegram check failed: ' + e.message, 'error');
    log('Telegram check failed: ' + e.message);
  } finally {
    setBusy(btn, false);
    $('telegram-save').disabled = false;
    renderTelegram(false);
  }
}

async function disconnectTelegram() {
  const ok = await confirmDialog({
    title: 'Disconnect Telegram?',
    message: 'Phone-use alerts will stop until you save a bot token and group ID again. '
      + 'Confirmed events stay in the Telegram section.',
    confirmLabel: 'Disconnect',
  });
  if (!ok) return;
  try {
    telegram = await api('/api/telegram', { method: 'DELETE' });
    renderTelegram(true);
    showTelegramResult(null);
    toast('Telegram disconnected.', 'success');
    log('Telegram disconnected');
  } catch (e) {
    toast('Could not disconnect: ' + e.message, 'error');
  }
}

// ============================================================
//  Event wiring
// ============================================================

$('edit-save').onclick = saveCamera;
$('person-submit').onclick = submitPerson;
$('telegram-save').onclick = () => saveTelegram($('telegram-save'));
$('telegram-test').onclick = () => testTelegram($('telegram-test'));
$('telegram-clear').onclick = disconnectTelegram;

$('manual-url').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    addManualUrl();
  }
});

$('camera-filter').addEventListener('input', (e) => {
  cameraFilter = e.target.value;
  renderCameras();
});

$('person-filter').addEventListener('input', (e) => {
  personFilter = e.target.value;
  renderPersons();
});

$('customer-status-filter').addEventListener('change', () => loadCustomers());

// File picker
$('person-files').addEventListener('change', (e) => {
  addFiles(e.target.files);
  e.target.value = '';      // allow re-picking the same file after removal
});

// Drop zone
const dropzone = $('person-drop');
dropzone.addEventListener('click', (e) => {
  if (e.target.closest('input')) return;   // avoid re-entrant click
  $('person-files').click();
});
dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    $('person-files').click();
  }
});
['dragenter', 'dragover'].forEach((name) => {
  dropzone.addEventListener(name, (e) => {
    e.preventDefault();
    dropzone.classList.add('dragging');
  });
});
['dragleave', 'dragend', 'drop'].forEach((name) => {
  dropzone.addEventListener(name, () => dropzone.classList.remove('dragging'));
});
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
});

$('person-file-list').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-remove-file]');
  if (!btn) return;
  selectedFiles.splice(Number(btn.dataset.removeFile), 1);
  renderFileList();
  setPersonStatus(
    selectedFiles.length
      ? `${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'} selected.`
      : '');
});

// Single delegated action handler for everything rendered dynamically.
document.addEventListener('click', (e) => {
  const closer = e.target.closest('[data-close]');
  if (closer) {
    closeModal($(closer.dataset.close));
    return;
  }

  const btn = e.target.closest('[data-action]');
  if (!btn || btn.disabled) return;

  const { action, id } = btn.dataset;

  switch (action) {
    case 'discover': runDiscovery(); break;
    case 'add-url': addManualUrl(); break;
    case 'toggle-run': toggleRun(btn); break;
    case 'check-feeds': findFeeds(btn); break;
    case 'edit-camera': openCameraModal(id); break;
    case 'delete-camera': deleteCamera(id); break;
    case 'add-camera': addDiscovered(btn); break;
    case 'add-channel': addChannel(btn); break;
    case 'clear-camera-filter':
      $('camera-filter').value = '';
      cameraFilter = '';
      renderCameras();
      break;
    case 'clear-person-filter':
      $('person-filter').value = '';
      personFilter = '';
      renderPersons();
      break;
    case 'clear-log':
      $('log').textContent = '';
      break;
    case 'refresh-events':
      loadEvents();
      loadTelegram({ silent: true, renderForm: false });
      break;
    case 'refresh-attendance':
      loadAttendance();
      break;
    case 'refresh-overview':
      loadOverview();
      break;
    case 'refresh-customers':
      loadCustomers();
      break;
    case 'enroll': openPersonModal(null); break;
    case 'edit-person': openPersonModal(persons.find((p) => p.id === id)); break;
    case 'add-samples':
      openPersonModal(persons.find((p) => p.id === id), { focusFiles: true });
      break;
    case 'delete-person': deletePerson(id); break;
    case 'delete-event': deleteEvent(id); break;
    case 'clear-events': clearEvents(btn.dataset.scope, btn); break;
    default: break;
  }
});

// ============================================================
//  Boot
// ============================================================

async function restoreFeeds() {
  try {
    const { active } = await api('/api/status');
    for (const s of active) {
      addFeedCard(s.camera_id, s.name || s.camera_id, s.device, s.fps);
      connectWebRTC(s.camera_id);
    }
    if (active.length) log(`Restored ${active.length} running pipeline(s)`);
  } catch (e) {
    log('Could not restore running pipelines: ' + e.message);
  }
}

async function boot() {
  try {
    const data = await api('/api/auth/me');
    authUser = data.user;
  } catch (e) {
    if (e.status === 401) {
      redirectToLogin();
    } else {
      toast('Could not verify your session: ' + e.message, 'error');
    }
    return;
  }

  applyRoleUi();
  applyView({ scroll: false });
  loadCapabilities();
  checkServer();
  setInterval(checkServer, 15000);

  await restoreFeeds();
  await loadCameras();
  if (viewFromHash() === 'training' && isAdminUser()) {
    await loadReview();
    await loadReviewCapture();
  }

  const firstLive = [...feeds.keys()].find((id) => savedCameras.some((c) => c.id === id));
  if (firstLive) {
    selectedId = firstLive;
    renderCameras();
    renderSetup();
  }

  if (viewFromHash() === 'enrollment') {
    await refreshFaceEngine();
    await loadPersons(true);
    if (isAdminUser()) {
      await loadFaceReviewSettings();
      await loadFaceReviewCandidates();
    }
  }
}

boot();
