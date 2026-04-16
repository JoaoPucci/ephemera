// localStorage-backed tracked secret list.
// Stores ONLY non-sensitive metadata: id, type, created_at, expires_at.
// The URL (which contains the decryption key fragment) is never persisted.
(() => {
  const KEY = 'ephemera_tracked_v1';
  const MAX_ENTRIES = 50;

  function read() {
    try {
      const raw = localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  }

  function write(items) {
    try {
      localStorage.setItem(KEY, JSON.stringify(items));
    } catch {}
  }

  function save({ id, type, created_at, expires_at, label }) {
    const items = read().filter((x) => x.id !== id);
    items.unshift({ id, type, created_at, expires_at, label: label || '' });
    write(items.slice(0, MAX_ENTRIES));
  }

  function remove(id) {
    write(read().filter((x) => x.id !== id));
  }

  window.trackedStore = { read, save, remove };
})();
