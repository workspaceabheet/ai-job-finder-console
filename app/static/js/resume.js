// Sidebar resume card: empty state vs. filled state, upload / replace, and
// inline rejection message (AC1-AC3). Consumes GET /api/resume/status and
// POST /api/resume, both returning ResumeStatus {uploaded, filename, uploaded_at}.
window.App = window.App || {};

window.App.initResume = function initResume() {
  const empty = document.getElementById('resume-empty');
  const filled = document.getElementById('resume-filled');
  const icon = document.getElementById('resume-icon');
  const nameEl = document.getElementById('resume-name');
  const metaEl = document.getElementById('resume-meta');
  const errorEl = document.getElementById('resume-error');
  const input = document.getElementById('resume-input');
  const buttons = [
    document.getElementById('resume-upload-btn'),
    document.getElementById('resume-replace-btn'),
  ];

  function formatDate(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function extLabel(filename) {
    const dot = filename.lastIndexOf('.');
    return dot >= 0 ? filename.slice(dot + 1).toUpperCase().slice(0, 4) : 'FILE';
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = !message;
  }

  function setBusy(busy) {
    buttons.forEach((b) => {
      b.disabled = busy;
    });
  }

  function render(status) {
    if (status.uploaded && status.filename) {
      icon.textContent = extLabel(status.filename);
      nameEl.textContent = status.filename;
      metaEl.textContent = status.uploaded_at ? `Uploaded on ${formatDate(status.uploaded_at)}` : '';
      empty.hidden = true;
      filled.hidden = false;
    } else {
      filled.hidden = true;
      empty.hidden = false;
    }
  }

  async function loadStatus() {
    try {
      const res = await window.App.apiFetch('/api/resume/status');
      render(await res.json());
    } catch (err) {
      showError(`Could not load resume status: ${err.message}`);
    }
  }

  async function upload(file) {
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    showError('');
    setBusy(true);
    try {
      const res = await window.App.apiFetch('/api/resume', { method: 'POST', body: form });
      render(await res.json());
    } catch (err) {
      // Leave the currently-rendered card untouched; only show the message.
      showError(err.message);
    } finally {
      setBusy(false);
      input.value = ''; // allow re-selecting the same file
    }
  }

  buttons.forEach((b) => b.addEventListener('click', () => input.click()));
  input.addEventListener('change', () => upload(input.files[0]));

  loadStatus();
};
