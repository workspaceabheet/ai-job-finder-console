// Run button + live progress (V2). POST /api/run returns a text/event-stream,
// but EventSource cannot send the mandatory X-App-Token header, so the stream
// is read with fetch() + a manual ReadableStream/TextDecoder parser that
// splits on the '\n\n' frame terminator emitted by app/routers/run.py's
// format_sse ('event: <name>\ndata: <json>\n\n'). Also hydrates the topbar
// and grid on page load from GET /api/run/last.
window.App = window.App || {};

(function () {
  let runInProgress = false; // client-side AC17 guard
  let statusPoll = null;

  const RUN_LABEL = 'Run search';

  function $(id) {
    return document.getElementById(id);
  }

  // ---------- notices (rendered into #run-progress) ----------

  // kind: 'info' | 'warn' | 'error' | 'progress'. `content` is text unless
  // opts.html is set (only ever used with static strings from this file).
  // opts.key replaces an existing notice with the same key.
  function showNotice(kind, content, opts = {}) {
    const box = $('run-progress');
    if (opts.key) box.querySelector(`[data-key="${opts.key}"]`)?.remove();
    const n = document.createElement('div');
    n.className = `notice notice-${kind}`;
    if (opts.key) n.dataset.key = opts.key;
    if (kind === 'error') n.setAttribute('role', 'alert');
    const icon = document.createElement('span');
    icon.className = 'ni';
    icon.textContent = { info: 'ℹ', warn: '⚠', error: '⚠', progress: '◐' }[kind] || 'ℹ';
    const body = document.createElement('div');
    body.className = 'nt';
    if (opts.html) body.innerHTML = content;
    else body.textContent = content;
    n.append(icon, body);
    if (opts.dismissible) {
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'notice-dismiss';
      x.setAttribute('aria-label', 'Dismiss');
      x.textContent = '×';
      x.addEventListener('click', () => n.remove());
      n.append(x);
    }
    box.append(n);
    if (opts.timeoutMs) setTimeout(() => n.remove(), opts.timeoutMs);
    return n;
  }

  function clearNotices() {
    $('run-progress').replaceChildren();
  }

  function setProgress(text, fraction) {
    let n = $('run-progress').querySelector('[data-key="progress"]');
    if (!n) {
      n = showNotice('progress', '', { key: 'progress' });
      const body = n.querySelector('.nt');
      const label = document.createElement('div');
      label.className = 'progress-label';
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.append(document.createElement('span'));
      body.append(label, bar);
    }
    n.querySelector('.progress-label').textContent = text;
    const fill = n.querySelector('.bar > span');
    fill.style.width = fraction == null ? '0%' : `${Math.round(fraction * 100)}%`;
  }

  function clearProgress() {
    $('run-progress').querySelector('[data-key="progress"]')?.remove();
  }

  // ---------- button state ----------

  function setButtonBusy(busy, label = 'Running…') {
    const btn = $('run-btn');
    btn.disabled = busy;
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
    btn.querySelector('.run-label').textContent = busy ? label : RUN_LABEL;
    const svg = btn.querySelector('svg');
    let spin = btn.querySelector('.spinner');
    if (busy && !spin) {
      spin = document.createElement('span');
      spin.className = 'spinner';
      spin.setAttribute('aria-hidden', 'true');
      btn.prepend(spin);
    } else if (!busy && spin) {
      spin.remove();
    }
    if (svg) svg.style.display = busy ? 'none' : '';
  }

  function scrollToResumeCard() {
    const card = document.querySelector('.resume-card');
    card?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    const btn = document.querySelector('#resume-upload-btn:not([hidden])') ||
      document.getElementById('resume-upload-btn');
    btn?.focus({ preventScroll: true });
  }

  // ---------- SSE ----------

  function handleSSEChunk(chunk, state) {
    let name = null;
    let dataText = '';
    chunk.split('\n').forEach((raw) => {
      const line = raw.replace(/\r$/, '');
      if (line.startsWith('event:')) name = line.slice(6).trim();
      else if (line.startsWith('data:')) dataText += line.slice(5).trimStart();
    });
    if (!name) return;
    let data = {};
    try {
      data = dataText ? JSON.parse(dataText) : {};
    } catch (e) {
      console.warn('Unparseable SSE data for', name, dataText);
      return;
    }
    state.events.push(name);
    console.debug('[run] SSE', name, data);

    switch (name) {
      case 'run_rejected_no_resume': {
        state.terminal = true;
        clearProgress();
        const n = showNotice(
          'error',
          '<b>Upload a resume first.</b> Runs score every listing against your resume, so a run can’t start without one. ' +
            '<button type="button" class="link-btn" data-action="goto-resume">Go to resume upload →</button>',
          { html: true, key: 'no-resume', dismissible: true },
        );
        n.querySelector('[data-action="goto-resume"]').addEventListener('click', scrollToResumeCard);
        window.App.refreshChatStatus?.(); // V3
        break;
      }
      case 'source_started':
        setProgress('Searching job sources…', null);
        break;
      case 'source_failed':
        showNotice('warn', `Source unavailable this run: ${data.source}. Results from the other sources are still shown.`, {
          key: `source-failed-${data.source}`,
          dismissible: true,
        });
        break;
      case 'scoring_progress':
        setProgress(`${data.done}/${data.total} scored`, data.total ? data.done / data.total : 0);
        break;
      case 'run_error':
        // AC16's pervasive-failure message arrives BEFORE run_complete (the
        // run still completes); a total failure ends the stream here.
        showNotice('error', `Run problem: ${data.reason}`, { key: 'run-error', dismissible: true });
        state.sawError = true;
        window.App.refreshChatStatus?.(); // V3
        break;
      case 'run_complete':
        state.terminal = true;
        clearProgress();
        window.App.renderRunComplete(data);
        window.App.refreshChatStatus?.(); // V3: pending -> applied (AC9)
        break;
      default:
        console.debug('[run] ignoring unknown event', name);
    }
  }

  async function consumeSSE(stream) {
    const state = { events: [], terminal: false, sawError: false };
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handleSSEChunk(chunk, state);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) handleSSEChunk(buffer, state);
    return state;
  }

  async function startRun() {
    if (runInProgress) return; // AC17: double-click does nothing
    runInProgress = true;
    setButtonBusy(true);
    clearNotices();
    try {
      let res;
      try {
        res = await window.App.apiFetch('/api/run', { method: 'POST' });
      } catch (err) {
        if (err.status === 409) {
          showNotice('info', 'A run is already in progress.', { key: 'busy', timeoutMs: 4000 });
          return;
        }
        throw err;
      }
      // Nothing may touch res before this: the body is the live stream.
      const state = await consumeSSE(res.body);
      if (!state.terminal) {
        clearProgress();
        if (!state.sawError) {
          showNotice('error', 'The run ended unexpectedly before finishing. Try again.', {
            key: 'run-error',
            dismissible: true,
          });
        }
        hydrateLastRun({ resultsToo: false });
      }
    } catch (err) {
      clearProgress();
      showNotice('error', `Run failed: ${err.message}`, { key: 'run-error', dismissible: true });
    } finally {
      runInProgress = false;
      setButtonBusy(false);
    }
  }

  // ---------- hydration on load ----------

  // A run started elsewhere (another tab, or before a reload) has no SSE
  // stream here; poll /api/run/status until it finishes, then re-hydrate.
  function watchForeignRun() {
    if (statusPoll) return;
    setButtonBusy(true, 'Run in progress…');
    statusPoll = setInterval(async () => {
      try {
        const res = await window.App.apiFetch('/api/run/status');
        const { in_progress: inProgress } = await res.json();
        if (!inProgress && !runInProgress) {
          clearInterval(statusPoll);
          statusPoll = null;
          setButtonBusy(false);
          hydrateLastRun();
        }
      } catch (_) {
        // transient; keep polling
      }
    }, 2000);
  }

  async function hydrateLastRun({ resultsToo = true } = {}) {
    try {
      const [lastRes, statusRes] = await Promise.all([
        window.App.apiFetch('/api/run/last'),
        window.App.apiFetch('/api/run/status'),
      ]);
      const last = await lastRes.json();
      const status = await statusRes.json();
      if (status.in_progress && !runInProgress) watchForeignRun();
      if (!last.has_run) return;
      window.App.renderSummary(last);
      if (resultsToo && last.status === 'complete') {
        await window.App.loadPage(last.run_id, 1);
      }
    } catch (err) {
      showNotice('error', `Could not load the last run: ${err.message}`, { key: 'hydrate', dismissible: true });
    }
  }

  function initRun() {
    $('run-btn').addEventListener('click', startRun);
  }

  Object.assign(window.App, {
    initRun,
    hydrateLastRun,
    showNotice,
    scrollToResumeCard,
    consumeSSE,
    isRunInProgress: () => runInProgress,
  });
})();
