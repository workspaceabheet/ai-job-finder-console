// Chat card (V3). Renders the status line from GET /api/chat/status, the
// append-only history from GET /api/chat/history, and posts new directions to
// POST /api/chat. Setting a direction never starts a run (AC8); the status line
// only flips to "applied" after run.js reports a finished run and calls
// App.refreshChatStatus() (AC9). "Only one active" is the server's guarantee
// (AC10) -- this file renders whatever `active` flags the server returns.
window.App = window.App || {};

(function () {
  // Mirrors CHAT_MAX_LENGTH in app/routers/chat.py -- change both together.
  const CHAT_MAX_LENGTH = 200;

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  }

  const STATUS_VIEWS = {
    none: () => ({ ico: '○', html: 'No active direction — add one below for the next run.' }),
    pending: (text) => ({ ico: '◐', html: `Will apply on next run: <b><q>${escapeHtml(text)}</q></b>` }),
    applied: (text) => ({ ico: '●', html: `Applied to last run: <b><q>${escapeHtml(text)}</q></b>` }),
  };

  function renderStatus(status) {
    // Defensive fallback: an unknown future state renders as "none".
    const view = (STATUS_VIEWS[status.state] || STATUS_VIEWS.none)(status.text ?? '');
    const box = $('chat-status');
    box.dataset.state = STATUS_VIEWS[status.state] ? status.state : 'none';
    box.innerHTML = `<span class="ico" aria-hidden="true">${view.ico}</span><span>${view.html}</span>`;
  }

  async function refreshStatus() {
    try {
      const res = await window.App.apiFetch('/api/chat/status');
      renderStatus(await res.json());
    } catch (err) {
      console.warn('[chat] status refresh failed', err);
    }
  }

  function renderHistory(records) {
    const list = $('chat-history-list');
    if (!records.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-history-empty';
      empty.textContent = 'No directions set this session yet.';
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(
      ...records.map((r) => {
        const item = document.createElement('div');
        item.className = 'chat-history-item' + (r.active ? ' current' : '');
        const t = document.createElement('span');
        t.className = 't mono';
        t.textContent = formatTime(r.set_at);
        t.title = r.set_at;
        const txt = document.createElement('span');
        txt.className = 'txt';
        txt.textContent = r.text;
        if (r.active) {
          const tag = document.createElement('span');
          tag.className = 'tag';
          tag.textContent = ' — active';
          txt.append(tag);
        }
        item.append(t, txt);
        return item;
      }),
    );
  }

  async function refreshHistory() {
    try {
      const res = await window.App.apiFetch('/api/chat/history');
      renderHistory(await res.json());
    } catch (err) {
      console.warn('[chat] history refresh failed', err);
    }
  }

  function showError(msg) {
    const el = $('chat-error');
    el.textContent = msg;
    el.hidden = false;
    $('chat-input').setAttribute('aria-invalid', 'true');
  }

  function clearError() {
    $('chat-error').hidden = true;
    $('chat-error').textContent = '';
    $('chat-input').removeAttribute('aria-invalid');
  }

  function tooLongMessage(len) {
    return `Direction too long — ${len} characters, max ${CHAT_MAX_LENGTH}. Shorten it and try again.`;
  }

  // POST /api/chat's 422 is FastAPI's standard validation-error body,
  // {"detail": [{"msg": "Value error, direction too long ..."}]} -- unlike
  // resume.py's plain-string detail. apiFetch JSON-stringifies non-string
  // details into err.message, so parse it back defensively here (the backend
  // shape is deliberately left as-is in this slice).
  function validationMessage(err) {
    let msg = 'direction too long';
    try {
      const detail = JSON.parse(err.message);
      msg = (Array.isArray(detail) ? detail[0]?.msg : null) ?? msg;
    } catch (_) {
      if (err.message) msg = err.message;
    }
    return msg.replace(/^Value error,\s*/, ''); // Pydantic v2's prefix
  }

  let submitting = false;

  async function submit() {
    if (submitting) return;
    const input = $('chat-input');
    const value = input.value.trim();
    if (!value) return;
    if (value.length > CHAT_MAX_LENGTH) {
      showError(tooLongMessage(value.length));
      return;
    }
    clearError();
    submitting = true;
    $('chat-send').disabled = true;
    try {
      await window.App.apiFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: value }),
      });
      input.value = '';
      await Promise.all([refreshStatus(), refreshHistory()]);
    } catch (err) {
      showError(err.status === 422 ? validationMessage(err) : `Could not set direction: ${err.message}`);
    } finally {
      submitting = false;
      $('chat-send').disabled = false;
    }
  }

  function initChat() {
    $('chat-send').addEventListener('click', submit);
    $('chat-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault();
        submit();
      }
    });
    // Live guard: flag over-length (e.g. pasted) text before submit.
    $('chat-input').addEventListener('input', (e) => {
      const len = e.target.value.trim().length;
      if (len > CHAT_MAX_LENGTH) showError(tooLongMessage(len));
      else clearError();
    });
    refreshStatus();
    refreshHistory();
  }

  Object.assign(window.App, {
    initChat,
    // Called by run.js after every terminal run event. Refreshes the history
    // too: a session reset at run start (AC11) clears the active flag, and the
    // history's "current" marker must not keep pointing at a cleared entry.
    refreshChatStatus: () => Promise.all([refreshStatus(), refreshHistory()]),
  });
})();
