// Shared fetch wrapper. Creates the window.App namespace every slice's JS
// attaches to. Adds X-App-Token (read from the <meta> tag rendered by GET /)
// and throws an Error carrying .status and the server's `detail` on non-2xx.
// Never sets Content-Type itself, so FormData bodies get a browser-generated
// multipart boundary.
window.App = window.App || {};

window.App.apiFetch = async function apiFetch(path, opts = {}) {
  const token = document.querySelector('meta[name="app-token"]').content;
  const headers = { ...(opts.headers || {}), 'X-App-Token': token };
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch (_) {
      // non-JSON error body: keep statusText
    }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return res;
};
