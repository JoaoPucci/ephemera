(() => {
  // ---------- URL cache (client-side only; server never sees the fragment) ----------
  const URL_STORE_KEY = 'ephemera_urls_v1';
  function loadUrls() {
    try { return JSON.parse(localStorage.getItem(URL_STORE_KEY) || '{}'); } catch { return {}; }
  }
  function saveUrls(obj) {
    try { localStorage.setItem(URL_STORE_KEY, JSON.stringify(obj)); } catch {}
  }
  function cacheUrl(id, url) {
    const m = loadUrls(); m[id] = url; saveUrls(m);
  }
  function forgetUrl(id) {
    const m = loadUrls(); if (m[id]) { delete m[id]; saveUrls(m); }
  }
  function getUrl(id) { return loadUrls()[id] || null; }
  function gcUrls(knownIds) {
    const m = loadUrls();
    const known = new Set(knownIds);
    let changed = false;
    for (const id of Object.keys(m)) {
      if (!known.has(id)) { delete m[id]; changed = true; }
    }
    if (changed) saveUrls(m);
  }

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

  const submitBtn = document.getElementById('submit-btn');
  const submitLabel = submitBtn.textContent;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    // In-flight guard: a rapid double-tap would otherwise create two
    // independent secrets. The UI only shows the URL of the last response,
    // so the first one silently orphans.
    if (submitBtn.disabled) return;
    errBox.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating…';

    let res;
    try {
      const track = document.getElementById('track').checked;
      const label = track ? (document.getElementById('label').value || '').trim() : '';
      if (activeTab === 'text') {
        const content = document.getElementById('content').value;
        if (!content.trim()) throw new Error('Please enter a message.');
        const body = {
          content,
          content_type: 'text',
          expires_in: Number(document.getElementById('expires_in').value),
          passphrase: document.getElementById('passphrase').value || null,
          track,
        };
        if (label) body.label = label;
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
        fd.append('track', track ? 'true' : 'false');
        if (label) fd.append('label', label);
        res = await fetch('/api/secrets', { method: 'POST', body: fd });
      }

      if (res.status === 401) { window.location.reload(); return; }
      if (!res.ok) {
        let msg = 'Request failed (' + res.status + ').';
        try { const j = await res.json(); if (j.detail) msg = j.detail; } catch {}
        throw new Error(msg);
      }
      const data = await res.json();
      if (track && data.url && data.id) cacheUrl(data.id, data.url);
      showResult(data);
    } catch (err) {
      errBox.textContent = err.message || 'Something went wrong.';
      errBox.hidden = false;
    } finally {
      // Restore the button whether we succeeded or threw: on success the
      // compose form is hidden so the user won't notice, but "Create another"
      // brings the form back and it has to be usable again.
      submitBtn.disabled = false;
      submitBtn.textContent = submitLabel;
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
      // The server is already authoritative — creation wrote track=1 + label.
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

  // ---------- tracked-list status polling ----------
  let trackedPollId = null;
  const TRACKED_POLL_MS = 5000;

  function startTrackedPoll() {
    if (trackedPollId) return;
    trackedPollId = setInterval(pollTrackedOnce, TRACKED_POLL_MS);
  }
  function stopTrackedPoll() {
    if (trackedPollId) { clearInterval(trackedPollId); trackedPollId = null; }
  }

  async function pollTrackedOnce() {
    const items = await fetchTracked();
    if (items === null) return;  // transient network error; try again next tick

    const list = document.getElementById('tracked-list');
    const existing = [...list.querySelectorAll('li[data-id]')];
    const existingMap = new Map(existing.map(li => [li.dataset.id, li.dataset.status]));
    const serverMap = new Map(items.map(i => [i.id, i.status]));

    const same = existing.length === items.length
      && [...serverMap].every(([id, s]) => existingMap.get(id) === s);

    // Respect in-flight user interaction (copy flash, remove click).
    const busy = list.querySelector('[data-busy="1"]') !== null;

    if (!same && !busy) await renderTrackedList();

    if (!items.some(i => i.status === 'pending')) stopTrackedPoll();
  }

  async function fetchTracked() {
    // Returns null on failure (so callers can leave local state alone)
    // and an array (possibly empty) on success.
    try {
      const res = await fetch('/api/secrets/tracked');
      if (res.status === 401) { window.location.reload(); return null; }
      if (!res.ok) return null;
      const body = await res.json();
      return body.items || [];
    } catch {
      return null;
    }
  }

  async function untrackOnServer(id) {
    try {
      await fetch(`/api/secrets/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      });
    } catch {}
  }

  async function cancelOnServer(id) {
    try {
      const res = await fetch(`/api/secrets/${encodeURIComponent(id)}/cancel`, {
        method: 'POST',
      });
      return res.ok || res.status === 204;
    } catch { return false; }
  }

  async function renderTrackedList() {
    const section = document.getElementById('tracked-section');
    const list = document.getElementById('tracked-list');
    const header = document.getElementById('tracked-header');
    const countEl = document.getElementById('tracked-count');

    const items = await fetchTracked();
    if (items === null) return;          // fetch failed; leave UI + URL cache alone
    gcUrls(items.map(i => i.id));

    if (items.length === 0) {
      section.hidden = true;
      section.classList.remove('open');
      if (header) header.setAttribute('aria-expanded', 'false');
      stopTrackedPoll();
      return;
    }
    section.hidden = false;
    if (countEl) countEl.textContent = String(items.length);

    list.innerHTML = '';
    for (const item of items) {
      const li = document.createElement('li');
      li.className = 'tracked-item';
      li.dataset.id = item.id;
      li.dataset.status = item.status;

      const fallback = item.content_type === 'image' ? 'Image secret' : 'Text secret';
      const labelText = (item.label && item.label.trim()) ? item.label : fallback;

      const labelEl = document.createElement('span');
      labelEl.className = 'label';
      labelEl.textContent = labelText;
      // Long labels truncate with ellipsis (see CSS); carry the full text
      // in the tooltip so hovering still reveals everything. Skip the
      // tooltip for fallback labels -- they're short and non-informative.
      if (item.label && item.label.trim()) {
        labelEl.title = labelText;
      }

      const timeEl = document.createElement('span');
      timeEl.className = 'time';
      // When there's no user-supplied label, the fallback ("Image secret" /
      // "Text secret") is already shown as the label -- no need to repeat it
      // in the footnote. When there IS a label, the user knows what they
      // tagged, so the content-type prefix would just crowd the row and
      // push the footnote into a second line (which makes the status pill
      // wrap too). Keep the footnote strictly about timing.
      let timeText = 'created ' + fmtRelative(item.created_at);
      if (item.status === 'viewed' && item.viewed_at) {
        timeText += ' · viewed ' + fmtRelative(item.viewed_at);
      } else if (item.status === 'burned' && item.viewed_at) {
        timeText += ' · burned ' + fmtRelative(item.viewed_at);
      } else if (item.status === 'canceled' && item.viewed_at) {
        timeText += ' · canceled ' + fmtRelative(item.viewed_at);
      } else if (item.status === 'expired') {
        timeText += ' · expired ' + fmtRelative(item.expires_at);
      }
      timeEl.textContent = timeText;
      // Exact timestamps on hover for older entries where "3d ago" is ambiguous.
      const hoverBits = [`created ${new Date(item.created_at).toLocaleString()}`];
      if (item.viewed_at) hoverBits.push(`${item.status} ${new Date(item.viewed_at).toLocaleString()}`);
      else if (item.status === 'expired') hoverBits.push(`expired ${new Date(item.expires_at).toLocaleString()}`);
      timeEl.title = hoverBits.join(' · ');

      const meta = document.createElement('div');
      meta.className = 'tracked-meta';
      meta.appendChild(labelEl);
      meta.appendChild(timeEl);

      const pill = document.createElement('span');
      pill.className = 'status-pill ' + item.status;
      pill.textContent = item.status;

      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'tracked-remove';
      rm.setAttribute('aria-label', 'remove from list');
      rm.title = 'remove';
      rm.textContent = '×';

      const right = document.createElement('div');
      right.className = 'tracked-right';
      right.appendChild(pill);

      // Cancel action: only for pending (live) secrets. Two-click confirm
      // pattern so accidental clicks don't revoke a link.
      if (item.status === 'pending') {
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'tracked-cancel';
        cancelBtn.textContent = 'cancel';
        cancelBtn.title = 'Revoke the URL so the receiver can no longer view this';
        cancelBtn.setAttribute('aria-label', 'cancel this secret');
        let armTimer = null;
        cancelBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!cancelBtn.classList.contains('armed')) {
            cancelBtn.classList.add('armed');
            cancelBtn.textContent = 'confirm?';
            armTimer = setTimeout(() => {
              cancelBtn.classList.remove('armed');
              cancelBtn.textContent = 'cancel';
            }, 3000);
            return;
          }
          if (armTimer) clearTimeout(armTimer);
          cancelBtn.disabled = true;
          cancelBtn.textContent = 'canceling…';
          await cancelOnServer(item.id);
          forgetUrl(item.id);
          renderTrackedList();
        });
        right.appendChild(cancelBtn);
      }

      right.appendChild(rm);

      li.appendChild(meta);
      li.appendChild(right);

      const cachedUrl = getUrl(item.id);
      const copyable = Boolean(cachedUrl) && item.status === 'pending';

      if (copyable) {
        li.classList.add('copyable');
        li.setAttribute('role', 'button');
        li.setAttribute('tabindex', '0');
        li.title = 'Click to copy link';
        const activate = async (e) => {
          // Ignore clicks that originated on row-level action buttons.
          if (e.target && e.target.closest('.tracked-remove, .tracked-cancel')) return;
          await copyRowUrl(li, timeEl, timeText, cachedUrl);
        };
        li.addEventListener('click', activate);
        li.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(e); }
        });
      } else if (!cachedUrl && item.status === 'pending') {
        // Tracked on this server but we don't have the URL -- it was created in a
        // different browser (or this browser's storage got cleared). We can never
        // reconstruct it server-side because the key fragment never leaves the
        // creating browser. Make the row clearly "informational, not actionable".
        li.classList.add('orphan');
        li.title =
          'The URL includes an encryption key stored only in the browser where this ' +
          'secret was created. Open ephemera in that browser to copy the link.';
        const hint = document.createElement('span');
        hint.className = 'orphan-hint';
        hint.textContent = 'created elsewhere';
        meta.appendChild(hint);
      }

      rm.addEventListener('click', async (e) => {
        e.stopPropagation();
        await untrackOnServer(item.id);
        forgetUrl(item.id);
        renderTrackedList();
      });

      list.appendChild(li);
    }

    // Reveal the "clear past entries" action only when there's something to clear.
    const clearBtn = document.getElementById('tracked-clear');
    const clearLbl = document.getElementById('tracked-clear-label');
    const nonPending = items.filter(i => i.status !== 'pending').length;
    if (clearBtn) {
      clearBtn.hidden = nonPending === 0;
      if (clearLbl && nonPending > 0) {
        const word = nonPending === 1 ? 'entry' : 'entries';
        clearLbl.textContent = `Clear ${nonPending} past ${word}`;
      }
    }

    if (items.some(i => i.status === 'pending')) startTrackedPoll();
    else stopTrackedPoll();
  }

  // Clear-history action: same 2-click arm pattern as per-row cancel. First
  // click arms (danger tint + "confirm?"), second click within 3s executes.
  // We mutate only the #tracked-clear-label span so the icon (a sibling SVG)
  // stays put across state transitions.
  (function wireClearHistory() {
    const clearBtn = document.getElementById('tracked-clear');
    const clearLbl = document.getElementById('tracked-clear-label');
    if (!clearBtn || !clearLbl) return;
    let armTimer = null;
    // Remember the last "idle" label so we can restore it (it carries the
    // current count, set by renderTrackedList, and may differ between calls).
    function idleLabel() { return clearBtn.dataset.idleLabel || 'Clear past entries'; }
    clearBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!clearBtn.classList.contains('armed')) {
        clearBtn.dataset.idleLabel = clearLbl.textContent;
        clearBtn.classList.add('armed');
        clearLbl.textContent = 'confirm?';
        armTimer = setTimeout(() => {
          clearBtn.classList.remove('armed');
          clearLbl.textContent = idleLabel();
        }, 3000);
        return;
      }
      if (armTimer) clearTimeout(armTimer);
      clearBtn.disabled = true;
      clearLbl.textContent = 'clearing…';
      try {
        await fetch('/api/secrets/tracked/clear', { method: 'POST' });
      } catch {}
      clearBtn.classList.remove('armed');
      clearLbl.textContent = idleLabel();
      clearBtn.disabled = false;
      renderTrackedList();
    });
  })();

  async function copyRowUrl(li, timeEl, originalTimeText, url) {
    if (li.dataset.busy === '1') return;
    li.dataset.busy = '1';
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(url);
        ok = true;
      } else {
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand('copy');
        document.body.removeChild(ta);
      }
    } catch {
      ok = false;
    }
    li.classList.add(ok ? 'flash-copy' : 'flash-error');
    timeEl.textContent = ok ? 'copied to clipboard' : 'copy failed';
    li.setAttribute('aria-live', 'polite');
    setTimeout(() => {
      li.classList.remove('flash-copy', 'flash-error');
      timeEl.textContent = originalTimeText;
      delete li.dataset.busy;
    }, 1500);
  }

  // Panel toggle: whole header bar acts as the expand/collapse control.
  (function wireTrackedToggle() {
    const section = document.getElementById('tracked-section');
    const header = document.getElementById('tracked-header');
    if (!section || !header) return;
    header.addEventListener('click', () => {
      const open = section.classList.toggle('open');
      header.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  })();

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

  const userBtn = document.getElementById('user-btn');
  const userNameEl = document.getElementById('user-name');
  if (userBtn) {
    userBtn.addEventListener('click', async () => {
      try {
        await fetch('/send/logout', { method: 'POST' });
      } catch {}
      // reload() re-fetches the same URL; setting href to the same URL is a no-op
      // in most browsers, which is why the old version appeared to "do nothing".
      window.location.reload();
    });
  }

  (async function loadMe() {
    try {
      const res = await fetch('/api/me');
      if (res.status === 401) { window.location.reload(); return; }
      if (!res.ok) return;
      const me = await res.json();
      if (userNameEl && me.username) userNameEl.textContent = me.username;
      if (userBtn) userBtn.setAttribute('aria-label', `Signed in as ${me.username}. Click to sign out.`);
    } catch {}
  })();

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
