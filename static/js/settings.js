// Sidebar job-preferences form: load, dirty-tracking, explicit Save (AC4).
// Consumes GET/POST /api/settings (SettingsOut / SettingsIn). No autosave.
window.App = window.App || {};

window.App.initSettings = function initSettings() {
  const FIELDS = ['role_keywords', 'seniority', 'location', 'must_haves', 'dealbreakers', 'notes'];
  const form = document.getElementById('settings-form');
  const saveBtn = document.getElementById('settings-save');
  const saveRow = document.getElementById('save-row');
  const saveText = document.getElementById('save-text');
  const els = Object.fromEntries(FIELDS.map((f) => [f, form.elements[f]]));

  function setSaveState(state, text) {
    saveRow.dataset.state = state;
    saveText.textContent = text;
  }

  function renderSaved(data) {
    if (data.is_default) {
      setSaveState('default', 'Unreviewed defaults — edit and save to keep your own preferences');
    } else {
      setSaveState('saved', 'Saved · persists across restarts');
    }
  }

  function ensureOption(select, value) {
    // seniority is free text server-side; keep an unknown stored value visible.
    if (![...select.options].some((o) => o.value === value)) {
      select.add(new Option(value, value));
    }
  }

  function populate(data) {
    FIELDS.forEach((f) => {
      const value = data[f] ?? '';
      if (els[f].tagName === 'SELECT') ensureOption(els[f], value);
      els[f].value = value;
    });
  }

  async function load() {
    try {
      const res = await window.App.apiFetch('/api/settings');
      const data = await res.json();
      populate(data);
      renderSaved(data);
    } catch (err) {
      setSaveState('error', `Could not load settings: ${err.message}`);
    }
  }

  function markDirty() {
    setSaveState('dirty', 'Unsaved changes');
  }

  async function save() {
    const body = Object.fromEntries(FIELDS.map((f) => [f, els[f].value]));
    saveBtn.disabled = true;
    setSaveState('saving', 'Saving…');
    try {
      const res = await window.App.apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      populate(data);
      renderSaved(data);
    } catch (err) {
      setSaveState('error', `Save failed: ${err.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  }

  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    save();
  });

  load();
};
