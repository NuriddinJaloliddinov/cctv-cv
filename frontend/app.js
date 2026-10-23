const $ = (id) => document.getElementById(id);
const feeds = new Map();          // camera_id -> { cardEl, videoEl, pc }
let savedCameras = [];            // from /api/cameras (DB-backed)
let discoveredCameras = [];       // from /api/discover (ephemeral)
let selectedId = null;
let editingId = null;

function log(msg) {
  $('log').textContent += msg + '\n';
  $('log').scrollTop = $('log').scrollHeight;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    const msg = (data && data.detail) || (data && data.error) || r.statusText;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

// ---------- Capabilities ----------

async function loadCapabilities() {
  try {
    const caps = await api('/api/capabilities');
    const sel = $('device-select');
    sel.innerHTML = '<option value="auto">Auto (recommended)</option>';
    for (const c of caps) {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = `${c.name} (${c.detail})`;
      if (c.status !== 'ready') {
        opt.disabled = true;
        opt.textContent += ' — unavailable';
      }
      sel.appendChild(opt);
    }
    // Mirror the list into the edit modal
    $('edit-device').innerHTML = sel.innerHTML;
  } catch (e) {
    log('Failed to load capabilities: ' + e.message);
  }
}

// ---------- Cameras (persisted) ----------

async function loadCameras() {
  try {
    savedCameras = await api('/api/cameras');
    if (selectedId && !savedCameras.find(c => c.id === selectedId)) selectedId = null;
    if (!selectedId && savedCameras.length) selectedId = savedCameras[0].id;
    render();
  } catch (e) {
    log('Failed to load cameras: ' + e.message);
  }
}

function render() {
  const box = $('results');
  const items = [
    ...savedCameras.map(c => ({ ...c, _saved: true })),
    ...discoveredCameras.map(c => ({ ...c, _saved: false })),
  ];

  if (!items.length) {
    box.innerHTML = '<div class="empty-state">No cameras yet. Click Discover or add a URL.</div>';
  } else {
    box.innerHTML = items.map(c => {
      const isSel = c._saved && c.id === selectedId;
      const badge = c._saved
        ? '<span class="badge badge-saved">saved</span>'
        : '<span class="badge badge-new">new</span>';
      const actions = c._saved
        ? `<button class="btn-mini" data-act="edit" data-id="${c.id}">Edit</button>
           <button class="btn-mini btn-mini-danger" data-act="delete" data-id="${c.id}">Delete</button>`
        : `<button class="btn-mini btn-mini-primary" data-act="add" data-addr="${escapeHtml(c.address)}">Add</button>`;
      return `<div class="cam${isSel ? ' sel' : ''}" data-id="${c.id}" data-saved="${c._saved}">
        <div class="cam-main">
          <b>${escapeHtml(c.name)}</b>
          <small>${escapeHtml(c.address)}</small>
        </div>
        <div class="cam-actions">
          ${badge}
          ${actions}
        </div>
      </div>`;
    }).join('');

    box.querySelectorAll('.cam').forEach(el => {
      el.onclick = (ev) => {
        if (ev.target.closest('button')) return;
        if (el.dataset.saved === 'true') { selectedId = el.dataset.id; render(); }
      };
    });
    box.querySelectorAll('[data-act="edit"]').forEach(b => {
      b.onclick = (ev) => { ev.stopPropagation(); openEdit(b.dataset.id); };
    });
    box.querySelectorAll('[data-act="delete"]').forEach(b => {
      b.onclick = (ev) => { ev.stopPropagation(); deleteCamera(b.dataset.id); };
    });
    box.querySelectorAll('[data-act="add"]').forEach(b => {
      b.onclick = (ev) => { ev.stopPropagation(); addDiscovered(b.dataset.addr); };
    });
  }

  const sel = $('camera-select');
  sel.innerHTML = savedCameras.map(c =>
    `<option value="${c.id}"${c.id === selectedId ? ' selected' : ''}>${escapeHtml(c.name)}</option>`
  ).join('');

  const cam = savedCameras.find(c => c.id === selectedId);
  $('setup').hidden = !cam;

  if (cam) {
    const devSel = $('device-select');
    const opt = [...devSel.options].find(o => o.value === cam.device);
    devSel.value = opt ? cam.device : 'auto';
    $('fps').value = cam.fps || 10;
    $('onvif-user').value = cam.onvif_user || '';
    $('onvif-pass').value = cam.onvif_pass || '';
    const needsCreds = cam.source_type === 'onvif' && !cam.rtsp_url;
    $('creds').hidden = !needsCreds;
    $('run').textContent = feeds.has(cam.id) ? 'Stop' : 'Run CV Model';

    $('find-feeds').hidden = cam.source_type !== 'onvif';
    const chans = $('channels');
    if (chans.dataset.camId !== cam.id) {
      chans.hidden = true;
      chans.innerHTML = '';
      chans.dataset.camId = '';
    }
  } else {
    $('creds').hidden = true;
    $('find-feeds').hidden = true;
    $('channels').hidden = true;
    $('run').textContent = 'Run CV Model';
  }
}

$('camera-select').onchange = () => {
  selectedId = $('camera-select').value;
  render();
};

// ---------- Discovery ----------

$('discover').onclick = async () => {
  try {
    discoveredCameras = await api('/api/discover');
    log(`Discovery found ${discoveredCameras.length} new device(s)`);
    render();
  } catch (e) {
    log('Discovery failed: ' + e.message);
  }
};

// ---------- Manual URL ----------

$('add-manual').onclick = async () => {
  const url = $('manual-url').value.trim();
  if (!url) return;
  let host = 'Mobile Cam';
  try { host = new URL(url).host; } catch {}
  try {
    const cam = await api('/api/cameras', {
      method: 'POST',
      body: JSON.stringify({
        name: 'Mobile: ' + host,
        address: url,
        rtsp_url: url,
        source_type: 'mobile',
      }),
    });
    $('manual-url').value = '';
    selectedId = cam.id;
    await loadCameras();
  } catch (e) {
    alert('Add failed: ' + e.message);
  }
};

async function addDiscovered(address) {
  try {
    const d = discoveredCameras.find(c => c.address === address);
    if (!d) return;
    const cam = await api('/api/cameras', {
      method: 'POST',
      body: JSON.stringify({
        name: d.name,
        address: d.address,
        rtsp_url: d.rtsp_url || null,
        source_type: d.source_type || 'onvif',
      }),
    });
    discoveredCameras = discoveredCameras.filter(c => c.address !== address);
    selectedId = cam.id;
    await loadCameras();
  } catch (e) {
    alert('Add failed: ' + e.message);
  }
}

// ---------- Delete camera ----------

async function deleteCamera(id) {
  const cam = savedCameras.find(c => c.id === id);
  if (!cam) return;
  if (!confirm(`Delete camera "${cam.name}"?`)) return;
  try {
    if (feeds.has(id)) await stopFeed(id, { silent: true });
    await api(`/api/cameras/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (selectedId === id) selectedId = null;
    await loadCameras();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

// ---------- Edit camera modal ----------

function openEdit(id) {
  const cam = savedCameras.find(c => c.id === id);
  if (!cam) return;
  editingId = id;
  $('edit-name').value = cam.name;
  $('edit-address').value = cam.address;
  $('edit-rtsp').value = cam.rtsp_url || '';
  $('edit-source').value = cam.source_type;
  $('edit-user').value = cam.onvif_user || '';
  $('edit-pass').value = cam.onvif_pass || '';
  $('edit-device').value = cam.device || 'auto';
  $('edit-fps').value = cam.fps || 10;
  $('edit-modal').hidden = false;
}

function closeEdit() {
  editingId = null;
  $('edit-modal').hidden = true;
}

$('edit-cancel').onclick = closeEdit;

$('edit-save').onclick = async () => {
  if (!editingId) return;
  const body = {
    name: $('edit-name').value.trim(),
    address: $('edit-address').value.trim(),
    rtsp_url: $('edit-rtsp').value.trim() || null,
    source_type: $('edit-source').value,
    onvif_user: $('edit-user').value,
    onvif_pass: $('edit-pass').value,
    device: $('edit-device').value,
    fps: parseInt($('edit-fps').value, 10) || 10,
  };
  try {
    await api(`/api/cameras/${encodeURIComponent(editingId)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    closeEdit();
    await loadCameras();
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
};

// ---------- Live feeds ----------

function addFeedCard(cameraId, name, device, fps) {
  const container = $('feeds');
  if (container.querySelector('.empty-state')) container.innerHTML = '';
  if (feeds.has(cameraId)) return feeds.get(cameraId);

  const card = document.createElement('div');
  card.className = 'feed';
  card.innerHTML = `
    <div class="feed-head">
      <span>
        <b>${escapeHtml(name)}</b><br>
        <small>${escapeHtml(device)} @ ${fps} fps</small>
      </span>
      <button class="btn btn-outline" style="padding:6px 12px;font-size:12px;" data-act="stop">Stop</button>
    </div>
    <video autoplay playsinline muted></video>`;
  container.appendChild(card);

  const entry = { cardEl: card, videoEl: card.querySelector('video'), pc: null };
  card.querySelector('[data-act="stop"]').onclick = () => stopFeed(cameraId);
  feeds.set(cameraId, entry);
  return entry;
}

async function connectWebRTC(cameraId) {
  const entry = feeds.get(cameraId);
  if (!entry) return;

  const pc = new RTCPeerConnection();
  entry.pc = pc;
  pc.ontrack = (ev) => { entry.videoEl.srcObject = ev.streams[0]; };
  pc.addTransceiver('video', { direction: 'recvonly' });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  const res = await api(`/api/webrtc/offer/${encodeURIComponent(cameraId)}`, {
    method: 'POST',
    body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
  });
  if (res.error) { log(`webrtc ${cameraId}: ${res.error}`); return; }
  await pc.setRemoteDescription({ sdp: res.sdp, type: res.type });
}

async function stopFeed(cameraId, opts = {}) {
  const entry = feeds.get(cameraId);
  if (!entry) return;
  if (entry.pc) { try { await entry.pc.close(); } catch {} }
  entry.cardEl.remove();
  feeds.delete(cameraId);
  if (!opts.silent) {
    try { await api(`/api/stop/${encodeURIComponent(cameraId)}`, { method: 'POST' }); }
    catch {}
  }
  if (feeds.size === 0) {
    $('feeds').innerHTML = '<div class="empty-state">No active streams. Select a camera and click Run.</div>';
  }
  render();
}

$('run').onclick = async () => {
  const cam = savedCameras.find(c => c.id === selectedId);
  if (!cam) return;

  if (feeds.has(cam.id)) {
    await stopFeed(cam.id);
    return;
  }

  try {
    if (cam.source_type === 'onvif' && !cam.rtsp_url) {
      const u = $('onvif-user').value;
      const p = $('onvif-pass').value;
      if (u !== cam.onvif_user || p !== cam.onvif_pass) {
        await api(`/api/cameras/${encodeURIComponent(cam.id)}`, {
          method: 'PUT',
          body: JSON.stringify({ onvif_user: u, onvif_pass: p }),
        });
      }
      const r = await api(`/api/cameras/${encodeURIComponent(cam.id)}/rtsp`, {
        method: 'POST',
      });
      cam.rtsp_url = r.rtsp_url;
    }

    const device = $('device-select').value;
    const fps = parseInt($('fps').value, 10) || 10;

    await api('/api/start', {
      method: 'POST',
      body: JSON.stringify({ camera_id: cam.id, device, fps }),
    });

    addFeedCard(cam.id, cam.name, device, fps);
    await connectWebRTC(cam.id);
    log(`Started pipeline: ${cam.name}`);
    await loadCameras();
  } catch (e) {
    log('Start failed: ' + e.message);
    alert('Failed to start: ' + e.message);
  }
};

// ---------- Credential-based feed check (ONVIF) ----------

async function findFeeds() {
  const cam = savedCameras.find(c => c.id === selectedId);
  if (!cam) return;
  const user = $('onvif-user').value;
  const pass = $('onvif-pass').value;
  const btn = $('find-feeds');
  btn.disabled = true;
  btn.textContent = 'Checking feeds…';
  try {
    await api(`/api/cameras/${encodeURIComponent(cam.id)}`, {
      method: 'PUT',
      body: JSON.stringify({ onvif_user: user, onvif_pass: pass }),
    });
    cam.onvif_user = user;
    cam.onvif_pass = pass;
    const feeds = await api(`/api/cameras/${encodeURIComponent(cam.id)}/profiles`, {
      method: 'POST',
      body: JSON.stringify({ onvif_user: user, onvif_pass: pass }),
    });
    renderChannels(cam, feeds);
    log(`${cam.name}: ${feeds.filter(f => f.accessible).length}/${feeds.length} feed(s) accessible`);
  } catch (e) {
    alert('Feed check failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check feeds with these credentials';
  }
}

function renderChannels(cam, feeds) {
  const box = $('channels');
  box.dataset.camId = cam.id;
  box.hidden = false;
  if (!feeds.length) {
    box.innerHTML = '<div class="empty-state">Device reports no media profiles.</div>';
    return;
  }
  box.innerHTML = feeds.map(f => {
    const address = `${cam.address}#${f.token}`;
    const added = savedCameras.some(c => c.address === address);
    const size = f.width ? `${f.width}×${f.height}` : '';
    const badge = f.accessible
      ? '<span class="badge badge-saved">video OK</span>'
      : `<span class="badge badge-new" title="${escapeHtml(f.error || 'no video')}">no video</span>`;
    const action = !f.accessible
      ? ''
      : added
        ? '<span class="badge">added</span>'
        : `<button class="btn-mini btn-mini-primary" data-token="${escapeHtml(f.token)}">Add</button>`;
    return `<div class="channel">
      <div><b>${escapeHtml(f.name)}</b><small>${escapeHtml(size)} ${escapeHtml(f.encoding || '')}</small></div>
      <div>${badge}${action}</div>
    </div>`;
  }).join('');
  box.querySelectorAll('button[data-token]').forEach(b => {
    const feed = feeds.find(f => f.token === b.dataset.token);
    b.onclick = () => addChannel(cam, feed);
  });
}

async function addChannel(cam, feed) {
  if (!feed || !feed.rtsp_url) return;
  try {
    const created = await api('/api/cameras', {
      method: 'POST',
      body: JSON.stringify({
        name: `${cam.name} – ${feed.name}`,
        address: `${cam.address}#${feed.token}`,
        rtsp_url: feed.rtsp_url,
        source_type: 'onvif',
        onvif_user: cam.onvif_user || '',
        onvif_pass: cam.onvif_pass || '',
      }),
    });
    selectedId = created.id;
    await loadCameras();
    log(`Added channel: ${created.name}`);
  } catch (e) {
    alert('Add failed: ' + e.message);
  }
}

$('find-feeds').onclick = findFeeds;

// ---------- Restore running pipelines on reload ----------

async function restore() {
  try {
    const { active } = await api('/api/status');
    for (const c of active) {
      addFeedCard(c.camera_id, c.name || c.camera_id, c.device, c.fps);
      await connectWebRTC(c.camera_id);
    }
    if (active.length) render();
  } catch (e) {
    log('Restore failed: ' + e.message);
  }
}

// ============================================================
//  People / Face enrollment  (NEW)
// ============================================================

async function loadPersons() {
  try {
    const persons = await api('/api/persons');
    const box = $('persons');
    if (!persons.length) {
      box.innerHTML = '<div class="empty-state">No enrolled people yet.</div>';
      return;
    }
    box.innerHTML = persons.map(p => `
      <div class="person" data-id="${p.id}">
        <img class="person-thumb" src="/api/persons/${encodeURIComponent(p.id)}/thumbnail" alt="">
        <div class="person-info">
          <b>${escapeHtml(p.name)}</b>
          <small>${p.samples} sample${p.samples === 1 ? '' : 's'}</small>
          ${p.notes ? `<small class="person-notes">${escapeHtml(p.notes)}</small>` : ''}
        </div>
        <button class="btn-mini btn-mini-primary" data-act="more" data-id="${p.id}">Add Samples</button>
        <button class="btn-mini btn-mini-danger" data-act="delete-person" data-id="${p.id}">Delete</button>
      </div>
    `).join('');
    box.querySelectorAll('[data-act="delete-person"]').forEach(b => {
      b.onclick = () => deletePerson(b.dataset.id);
    });
    box.querySelectorAll('[data-act="more"]').forEach(b => {
      b.onclick = () => addSamplesToPerson(b.dataset.id);
    });
  } catch (e) {
    log('Failed to load persons: ' + e.message);
  }
}

$('enroll-person').onclick = () => {
  $('enroll-name').value = '';
  $('enroll-notes').value = '';
  $('enroll-files').value = '';
  $('enroll-status').textContent = '';
  $('enroll-modal').hidden = false;
};

$('enroll-cancel').onclick = () => { $('enroll-modal').hidden = true; };

$('enroll-submit').onclick = async () => {
  const name = $('enroll-name').value.trim();
  if (!name) { alert('Name required'); return; }
  const files = $('enroll-files').files;
  if (!files.length) { alert('Select at least one image or video'); return; }

  const form = new FormData();
  form.append('name', name);
  form.append('notes', $('enroll-notes').value);
  for (const f of files) form.append('files', f);

  $('enroll-status').textContent = 'Processing…';
  $('enroll-submit').disabled = true;
  try {
    const r = await fetch('/api/persons', { method: 'POST', body: form })
      .then(async x => {
        const data = await x.json();
        if (!x.ok) throw new Error(data.detail || x.statusText);
        return data;
      });
    $('enroll-status').textContent = `Enrolled with ${r.samples} sample(s).`;
    $('enroll-modal').hidden = true;
    await loadPersons();
  } catch (e) {
    $('enroll-status').textContent = 'Failed: ' + e.message;
  } finally {
    $('enroll-submit').disabled = false;
  }
};

async function deletePerson(id) {
  if (!confirm('Delete this person and all their samples?')) return;
  try {
    await api(`/api/persons/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await loadPersons();
  } catch (e) { alert('Delete failed: ' + e.message); }
}

async function addSamplesToPerson(id) {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.accept = 'image/*,video/*';
  input.onchange = async () => {
    if (!input.files.length) return;
    const form = new FormData();
    for (const f of input.files) form.append('files', f);
    try {
      const r = await fetch(`/api/persons/${encodeURIComponent(id)}/enroll`, {
        method: 'POST', body: form,
      }).then(async x => {
        const data = await x.json();
        if (!x.ok) throw new Error(data.detail || x.statusText);
        return data;
      });
      alert(`Added ${r.added} sample(s).`);
      await loadPersons();
    } catch (e) { alert('Enroll failed: ' + e.message); }
  };
  input.click();
}

// ---------- Boot ----------

loadCapabilities();
loadPersons();
loadCameras().then(restore);