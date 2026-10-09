// Results grid (V2): one .card per scored posting, score gradient, gap note,
// Apply link, Prev/Next pagination, and the topbar last-run summary line.
// Consumes run_complete's embedded page 1 and GET /api/run/{run_id}/results
// (1-based pages, server-default page_size 20). Every value from the API is
// written via textContent / attributes, never innerHTML.
window.App = window.App || {};

(function () {
  let currentRunId = null;
  let loadSeq = 0; // ignore responses from superseded page loads

  // Ported from the wireframe's colorForScore(): 0 -> red, 50 -> yellow,
  // 100 -> green, continuous interpolation.
  function colorForScore(s) {
    const r1 = 213, g1 = 70, b1 = 63; // red
    const r2 = 224, g2 = 178, b2 = 60; // yellow
    const r3 = 47, g3 = 140, b3 = 90; // green
    let r, g, b;
    if (s <= 50) {
      const t = s / 50;
      r = r1 + (r2 - r1) * t; g = g1 + (g2 - g1) * t; b = b1 + (b2 - b1) * t;
    } else {
      const t2 = (s - 50) / 50;
      r = r2 + (r3 - r2) * t2; g = g2 + (g3 - g2) * t2; b = b2 + (b3 - b2) * t2;
    }
    return `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function safeUrl(url) {
    try {
      const u = new URL(url);
      return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
    } catch (_) {
      return null;
    }
  }

  const APPLY_ICON =
    '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<path d="M5 5h6V3H3v8h2V5zm14-2h-6v2h4.59L9 13.59 10.41 15 19 6.41V11h2V3z"/></svg>';

  function renderCard(job) {
    const score = Math.max(0, Math.min(100, Number(job.score) || 0));
    const col = colorForScore(score);
    const card = el('article', 'card');
    card.dataset.source = job.source;
    card.dataset.sourceId = job.source_id;

    const top = el('div', 'card-top');
    const titleBlock = el('div', 'card-title-block');
    titleBlock.append(
      el('div', 'card-title', job.title),
      el('div', 'card-company', job.company),
      el('div', 'card-location', job.location),
    );
    const pill = el('div', 'score-pill');
    pill.title = `Match score ${score}/100`;
    const num = el('div', 'score-num', String(score));
    num.style.color = col;
    const track = el('div', 'score-track');
    const fill = el('div', 'score-fill');
    fill.style.width = `${score}%`;
    fill.style.background = col;
    track.append(fill);
    pill.append(num, track);
    top.append(titleBlock, pill);
    card.append(top);

    if (job.description) card.append(el('div', 'card-desc', job.description));

    if (job.gap_note) {
      const gap = el('div', 'gap-note');
      gap.append(el('span', 'gi', '⚠'), el('span', null, job.gap_note));
      card.append(gap);
    }

    const foot = el('div', 'card-foot');
    const href = safeUrl(job.url);
    const apply = el('a', 'apply-btn');
    apply.innerHTML = `Apply ${APPLY_ICON}`; // static markup only
    if (href) {
      apply.href = href;
      apply.target = '_blank';
      apply.rel = 'noopener noreferrer';
    } else {
      apply.setAttribute('aria-disabled', 'true');
      apply.title = 'No valid posting URL';
    }
    foot.append(apply);
    card.append(foot);
    return card;
  }

  function renderPagination(pageData) {
    const nav = document.getElementById('pagination');
    nav.replaceChildren();
    const page = pageData.page;
    const totalPages = pageData.total_pages;
    if (!totalPages || totalPages <= 1) return;

    const prev = el('button', 'page-btn', '← Prev');
    prev.type = 'button';
    prev.disabled = page <= 1;
    prev.addEventListener('click', () => loadPage(pageData.run_id, page - 1, { scroll: true }));

    const label = el('span', 'page-label', `Page ${page} of ${totalPages}`);

    const next = el('button', 'page-btn', 'Next →');
    next.type = 'button';
    next.disabled = page >= totalPages;
    next.addEventListener('click', () => loadPage(pageData.run_id, page + 1, { scroll: true }));

    nav.append(prev, label, next);
  }

  function renderPage(pageData) {
    currentRunId = pageData.run_id;
    const grid = document.getElementById('results-grid');
    const total = pageData.total_results;
    document.getElementById('results-count').textContent =
      `${total} listing${total === 1 ? '' : 's'}, ranked by match`;
    grid.replaceChildren();
    if (!pageData.results || pageData.results.length === 0) {
      grid.append(
        el('div', 'grid-empty', total === 0 ? 'No new listings in this run.' : 'No listings on this page.'),
      );
    } else {
      pageData.results.forEach((job) => grid.append(renderCard(job)));
    }
    renderPagination(pageData);
  }

  async function loadPage(runId, page, { scroll = false } = {}) {
    const seq = ++loadSeq;
    const buttons = document.querySelectorAll('#pagination .page-btn');
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const res = await window.App.apiFetch(`/api/run/${runId}/results?page=${page}`);
      const data = await res.json();
      if (seq !== loadSeq) return;
      renderPage(data);
      if (scroll) {
        document.querySelector('.results-head').scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    } catch (err) {
      if (seq !== loadSeq) return;
      window.App.showNotice?.('error', `Could not load results: ${err.message}`, { dismissible: true });
      renderPagination({ run_id: runId, page, total_pages: 0 });
    }
  }

  function clearResults() {
    loadSeq++;
    document.getElementById('results-grid').replaceChildren();
    document.getElementById('pagination').replaceChildren();
    document.getElementById('results-count').textContent = '';
  }

  function formatWhen(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    return `${date} · ${time}`;
  }

  // summary: {status, started_at, finished_at, new_count, seen_hidden_count,
  // failed_sources} -- the GET /api/run/last shape (run_complete is adapted
  // to it by renderRunComplete).
  function renderSummary(summary) {
    const line = document.getElementById('last-run-summary');
    const when = formatWhen(summary.finished_at || summary.started_at);
    let text;
    if (summary.status === 'running') {
      text = `Run in progress — started ${when}`;
    } else if (summary.status === 'error') {
      text = `Last run: ${when} — failed or was interrupted`;
    } else {
      text = `Last run: ${when} — ${summary.new_count ?? 0} new, ` +
        `${summary.seen_hidden_count ?? 0} already seen (hidden)`;
    }
    const failed = summary.failed_sources || [];
    if (failed.length) text += ` · unavailable: ${failed.join(', ')}`;
    line.textContent = text;
  }

  function renderRunComplete(data) {
    renderSummary({
      status: 'complete',
      finished_at: new Date().toISOString(),
      new_count: data.new_count,
      seen_hidden_count: data.seen_hidden_count,
      failed_sources: data.failed_sources,
    });
    if (data.using_default_settings) {
      window.App.showNotice?.(
        'warn',
        '<b>Ran on unreviewed default settings.</b> Edit and save your job preferences in the sidebar for better matches.',
        { html: true, dismissible: true },
      );
    }
    renderPage(data); // page 1 is embedded in run_complete: no extra request
  }

  Object.assign(window.App, {
    colorForScore,
    renderPage,
    loadPage,
    clearResults,
    renderSummary,
    renderRunComplete,
    currentRunId: () => currentRunId,
  });
})();
