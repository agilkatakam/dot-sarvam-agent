// Shared helpers for the dashboard pages.
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
const ms = v => v == null ? '—' : v + ' ms';
const inr = v => v == null ? '—' : '₹' + Number(v).toFixed(2);
const $ = id => document.getElementById(id);

async function api(path, opts = {}) {
  const init = {...opts};
  if (opts.json !== undefined) { init.method = init.method || 'POST'; init.headers = {'Content-Type': 'application/json'}; init.body = JSON.stringify(opts.json); }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j); } catch {}
    throw new Error(msg);
  }
  const type = r.headers.get('content-type') || '';
  return type.includes('json') ? r.json() : r;
}

function toast(text, ms = 3500) {
  let t = $('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = text; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', ms);
}

function nav(active) {
  const links = [['/dashboard', 'KPIs & calls'], ['/studio', 'Prompt studio'], ['/admin', 'Settings & meetings'], ['/', 'Make a test call']];
  const el = document.createElement('nav');
  el.innerHTML = `<b>Voice agent</b>` + links.map(([href, label]) =>
    `<a href="${href}" class="${href === active ? 'on' : ''}" ${href === '/' ? 'target="_blank"' : ''}>${label}</a>`).join('') + `<span class="right" id="navinfo"></span>`;
  document.body.prepend(el);
}
