// Cloudflare Pages Function: /api/olvas – saját, süti nélküli látogatásszámláló (D1, binding: DB).
// Miért saját: a Cloudflare Web Analytics szkriptjét a reklámblokkolók (és a Brave) letiltják, így 0-t mutat.
// POST /api/olvas  {p: útvonal, r: honnan jött}  <- minden oldal egyszer elküldi (sendBeacon). Robotot nem számol
//   (azok nem futtatnak JavaScriptet, a maradékot a böngésző-azonosító szerint szűrjük).
//   Egyedi látogató: napi, sóval hash-elt IP+böngésző – visszafejthetetlen, nem tárolunk IP-t, és másnap új.
// GET  /api/olvas?days=7  (fejléc: X-Queue-Secret = TG_WEBHOOK_SECRET) <- a robot /stat parancsa kérdezi le.
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});
const BOT = /bot|crawl|spider|slurp|preview|headless|lighthouse|facebookexternalhit|whatsapp|telegram|python|curl|wget|httpclient/i;

function day(offset = 0) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Budapest' }).format(new Date(Date.now() - offset * 864e5));
}

function source(ref, host) {
  try {
    if (!ref) return 'közvetlen';
    const h = new URL(ref).hostname.replace(/^www\./, '');
    if (h === host || h.endsWith('kollektva-m5a.hu')) return 'belső';
    if (/google\./.test(h)) return 'Google';
    if (/facebook\.|fb\.|messenger/.test(h)) return 'Facebook';
    if (/instagram\./.test(h)) return 'Instagram';
    if (/t\.co$|twitter\.|x\.com/.test(h)) return 'X';
    if (/bing\.|duckduckgo|yahoo/.test(h)) return 'más kereső';
    return h.slice(0, 60);
  } catch (e) { return 'ismeretlen'; }
}

async function ensure(env) {
  await env.DB.batch([
    env.DB.prepare('CREATE TABLE IF NOT EXISTS hit_pages (day TEXT, path TEXT, n INTEGER, PRIMARY KEY (day, path))'),
    env.DB.prepare('CREATE TABLE IF NOT EXISTS hit_visitors (day TEXT, h TEXT, PRIMARY KEY (day, h))'),
    env.DB.prepare('CREATE TABLE IF NOT EXISTS hit_sources (day TEXT, src TEXT, n INTEGER, PRIMARY KEY (day, src))'),
  ]);
}

export async function onRequestPost({ request, env }) {
  if (!env.DB) return new Response(null, { status: 204 });
  const ua = request.headers.get('user-agent') || '';
  if (!ua || BOT.test(ua)) return new Response(null, { status: 204 });
  let b = {};
  try { b = JSON.parse(await request.text()); } catch (e) { return new Response(null, { status: 204 }); }
  const p = String(b.p || '/').split('?')[0].slice(0, 200);
  if (!p.startsWith('/')) return new Response(null, { status: 204 });
  const d = day(), host = new URL(request.url).hostname;
  const raw = `${env.POLL_SALT || 'kollektiva'}|${d}|${request.headers.get('cf-connecting-ip') || ''}|${ua}`;
  const hb = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
  const h = [...new Uint8Array(hb)].slice(0, 12).map(x => x.toString(16).padStart(2, '0')).join('');
  try {
    await ensure(env);
    const fresh = await env.DB.prepare('INSERT OR IGNORE INTO hit_visitors (day, h) VALUES (?, ?)').bind(d, h).run();
    const stmts = [env.DB.prepare('INSERT INTO hit_pages (day, path, n) VALUES (?, ?, 1) ON CONFLICT(day, path) DO UPDATE SET n = n + 1').bind(d, p)];
    if (fresh.meta && fresh.meta.changes) {  // a forrást látogatónként egyszer számoljuk (az első oldalnál)
      stmts.push(env.DB.prepare('INSERT INTO hit_sources (day, src, n) VALUES (?, ?, 1) ON CONFLICT(day, src) DO UPDATE SET n = n + 1')
        .bind(d, source(b.r, host)));
    }
    await env.DB.batch(stmts);
  } catch (e) { /* a számlálás sosem ronthatja el az oldalt */ }
  return new Response(null, { status: 204 });
}

export async function onRequestGet({ request, env }) {
  if (!env.DB || !env.TG_WEBHOOK_SECRET || request.headers.get('x-queue-secret') !== env.TG_WEBHOOK_SECRET) {
    return json({ ok: false }, 403);
  }
  await ensure(env);
  const n = Math.min(31, Math.max(1, Number(new URL(request.url).searchParams.get('days')) || 7));
  const from = day(n - 1);
  const views = (await env.DB.prepare('SELECT day, SUM(n) AS v FROM hit_pages WHERE day >= ? GROUP BY day').bind(from).all()).results || [];
  const vis = (await env.DB.prepare('SELECT day, COUNT(*) AS u FROM hit_visitors WHERE day >= ? GROUP BY day').bind(from).all()).results || [];
  const top = (await env.DB.prepare('SELECT path, SUM(n) AS v FROM hit_pages WHERE day >= ? GROUP BY path ORDER BY v DESC LIMIT 10').bind(from).all()).results || [];
  const src = (await env.DB.prepare('SELECT src, SUM(n) AS v FROM hit_sources WHERE day >= ? GROUP BY src ORDER BY v DESC LIMIT 8').bind(from).all()).results || [];
  const days = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = day(i);
    days.push({ day: d, views: (views.find(x => x.day === d) || {}).v || 0, visitors: (vis.find(x => x.day === d) || {}).u || 0 });
  }
  return json({ ok: true, days, top, sources: src });
}
