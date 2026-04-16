(() => {
  const form = document.getElementById('secret-form');
  const compose = document.getElementById('compose');
  const tabs = document.querySelectorAll('.tab');
  const panels = { text: document.getElementById('panel-text'), image: document.getElementById('panel-image') };
  const result = document.getElementById('result');
  const errBox = document.getElementById('sender-error');
  const fileInput = document.getElementById('file');
  const dropzone = document.getElementById('dropzone');
  const preview = document.getElementById('preview');
  const fileName = document.getElementById('file-name');
  const clearFile = document.getElementById('clear-file');

  let activeTab = 'text';

  function setTab(name) {
    activeTab = name;
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    Object.entries(panels).forEach(([k, el]) => (el.hidden = k !== name));
  }

  tabs.forEach(t => t.addEventListener('click', () => setTab(t.dataset.tab)));

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }});
  dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('drag'); });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('drag');
    if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; showPreview(); }
  });
  fileInput.addEventListener('change', showPreview);
  clearFile.addEventListener('click', (e) => { e.stopPropagation(); fileInput.value = ''; preview.hidden = true; });

  function showPreview() {
    if (fileInput.files.length) {
      const f = fileInput.files[0];
      fileName.textContent = `${f.name} (${Math.round(f.size / 1024)} KB)`;
      preview.hidden = false;
    } else {
      preview.hidden = true;
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errBox.hidden = true;

    let res;
    try {
      if (activeTab === 'text') {
        const content = document.getElementById('content').value;
        if (!content.trim()) throw new Error('Please enter a message.');
        const body = {
          content,
          content_type: 'text',
          expires_in: Number(document.getElementById('expires_in').value),
          passphrase: document.getElementById('passphrase').value || null,
          track: document.getElementById('track').checked,
        };
        res = await fetch('/api/secrets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } else {
        if (!fileInput.files.length) throw new Error('Please select an image.');
        const fd = new FormData();
        fd.append('file', fileInput.files[0]);
        fd.append('expires_in', document.getElementById('expires_in').value);
        const pw = document.getElementById('passphrase').value;
        if (pw) fd.append('passphrase', pw);
        fd.append('track', document.getElementById('track').checked ? 'true' : 'false');
        res = await fetch('/api/secrets', { method: 'POST', body: fd });
      }

      if (res.status === 401) { window.location.reload(); return; }
      if (!res.ok) {
        let msg = 'Request failed (' + res.status + ').';
        try { const j = await res.json(); if (j.detail) msg = j.detail; } catch {}
        throw new Error(msg);
      }
      const data = await res.json();
      showResult(data);
    } catch (err) {
      errBox.textContent = err.message || 'Something went wrong.';
      errBox.hidden = false;
    }
  });

  let statusPoll = null;

  function showResult({ url, id, expires_at }) {
    compose.hidden = true;
    document.getElementById('result-url').textContent = url;
    const expiry = new Date(expires_at);
    document.getElementById('result-expiry').textContent =
      'Expires: ' + expiry.toLocaleString();

    const track = document.getElementById('track').checked;
    const widget = document.getElementById('status-widget');
    if (track) {
      const type = activeTab === 'image' ? 'image' : 'text';
      const label = (document.getElementById('label').value || '').trim();
      window.trackedStore.save({
        id,
        type,
        created_at: new Date().toISOString(),
        expires_at,
        label,
      });
      widget.hidden = false;
      startPolling(id);
      renderTrackedList();
    } else {
      widget.hidden = true;
    }
    result.hidden = false;
  }

  function stopPolling() {
    if (statusPoll) { clearInterval(statusPoll); statusPoll = null; }
  }

  async function fetchStatus(id) {
    try {
      const res = await fetch(`/api/secrets/${encodeURIComponent(id)}/status`);
      if (res.status === 404) return { status: 'gone' };
      if (!res.ok) return null;
      return await res.json();
    } catch {
      return null;
    }
  }

  function paintStatus(valueEl, detailEl, data) {
    const statuses = ['pending', 'viewed', 'burned', 'expired', 'gone'];
    statuses.forEach((s) => valueEl.classList.remove(s));
    const s = (data && data.status) || 'pending';
    valueEl.classList.add(s);
    valueEl.textContent = s === 'gone' ? 'no longer tracked' : s;
    if (data && data.viewed_at) {
      detailEl.textContent = 'at ' + new Date(data.viewed_at).toLocaleString();
    } else {
      detailEl.textContent = '';
    }
  }

  async function startPolling(id) {
    stopPolling();
    const valueEl = document.getElementById('status-value');
    const detailEl = document.getElementById('status-detail');
    const tick = async () => {
      const data = await fetchStatus(id);
      paintStatus(valueEl, detailEl, data);
      if (data && (data.status === 'viewed' || data.status === 'burned' || data.status === 'gone')) {
        stopPolling();
        renderTrackedList();
      }
    };
    await tick();
    statusPoll = setInterval(tick, 5000);
  }

  function fmtRelative(iso) {
    const then = new Date(iso).getTime();
    const diff = (Date.now() - then) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + ' min ago';
    if (diff < 86400) return Math.floor(diff / 3600) + ' h ago';
    return Math.floor(diff / 86400) + ' d ago';
  }

  async function renderTrackedList() {
    const section = document.getElementById('tracked-section');
    const list = document.getElementById('tracked-list');
    const toggle = document.getElementById('tracked-toggle');

    const items = window.trackedStore.read();
    if (items.length === 0) {
      section.hidden = true;
      return;
    }
    section.hidden = false;

    list.innerHTML = '';
    for (const item of items) {
      const li = document.createElement('li');
      li.className = 'tracked-item';
      const fallback = item.type === 'image' ? 'Image secret' : 'Text secret';
      const labelText = (item.label && item.label.trim()) ? item.label : fallback;
      const subtext = (item.label && item.label.trim()) ? (fallback + ' · ') : '';
      const labelEl = document.createElement('span');
      labelEl.className = 'label';
      labelEl.textContent = labelText;
      const timeEl = document.createElement('span');
      timeEl.className = 'time';
      timeEl.textContent = subtext + 'created ' + fmtRelative(item.created_at);
      const meta = document.createElement('div');
      meta.className = 'tracked-meta';
      meta.appendChild(labelEl);
      meta.appendChild(timeEl);
      const pill = document.createElement('span');
      pill.className = 'status-pill pending';
      pill.dataset.status = '';
      pill.textContent = 'pending';
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'tracked-remove';
      rm.setAttribute('aria-label', 'remove from list');
      rm.title = 'remove';
      rm.textContent = '×';
      const right = document.createElement('div');
      right.className = 'tracked-right';
      right.appendChild(pill);
      right.appendChild(rm);
      li.appendChild(meta);
      li.appendChild(right);
      rm.addEventListener('click', () => {
        window.trackedStore.remove(item.id);
        renderTrackedList();
      });
      list.appendChild(li);
      fetchStatus(item.id).then((data) => {
        if (!data) { pill.className = 'status-pill gone'; pill.textContent = 'unknown'; return; }
        pill.classList.remove('pending');
        pill.classList.add(data.status);
        pill.textContent = data.status === 'gone' ? 'no longer tracked' : data.status;
        if (data.status === 'gone') {
          window.trackedStore.remove(item.id);
        }
      });
    }

    toggle.textContent = list.hidden ? `show (${items.length})` : 'hide';
  }

  document.getElementById('tracked-toggle').addEventListener('click', () => {
    const list = document.getElementById('tracked-list');
    list.hidden = !list.hidden;
    const items = window.trackedStore.read();
    document.getElementById('tracked-toggle').textContent =
      list.hidden ? `show (${items.length})` : 'hide';
  });

  document.getElementById('copy-url').addEventListener('click', (e) => {
    const url = document.getElementById('result-url').textContent;
    window.copyWithFeedback(e.currentTarget, url);
  });

  document.getElementById('create-another').addEventListener('click', () => {
    stopPolling();
    form.reset();
    fileInput.value = '';
    preview.hidden = true;
    result.hidden = true;
    compose.hidden = false;
    document.getElementById('status-widget').hidden = true;
    setTab('text');
  });

  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      try {
        await fetch('/send/logout', { method: 'POST' });
      } catch {}
      window.location.href = '/send';
    });
  }

  const trackCheckbox = document.getElementById('track');
  const labelWrap = document.getElementById('label-wrap');
  function syncLabelVisibility() {
    labelWrap.hidden = !trackCheckbox.checked;
    if (!trackCheckbox.checked) document.getElementById('label').value = '';
  }
  trackCheckbox.addEventListener('change', syncLabelVisibility);

  setTab('text');
  syncLabelVisibility();
  renderTrackedList();
})();
